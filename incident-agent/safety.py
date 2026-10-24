"""Deterministic safety layer between the LLM's remediation plan and the Action API.

The model only proposes. This module decides whether a proposal is allowed (catalog allowlist, strict arguments,
approved targets, evidence), binds human approval to a digest of the exact proposal, enforces the kill switch and
single dispatch per incident, and writes an append-only audit journal. Nothing here is ever offered to the model.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from action_api_models import SafeText, printable
from actions import Catalog, Operation, build_request, validate_arguments
from models import ActionProposal, Approval, ConfidenceLabel, Diagnosis, Evidence, ToolStatus

BLOCKED_TARGETS = {"flagd", "flagd-ui"}
MAX_CHECKS = 6
# metric -> (default bound kind, default bound, hard lower, hard upper)
VERIFICATION_METRICS: dict[str, tuple[str, Optional[float], float, float]] = {
    "error_rate": ("max", 0.05, 0.0, 0.5),
    "p95_latency_ms": ("max", None, 1.0, 60000.0),
    "request_rate": ("min", None, 0.0, 10000.0),
    # docker_stats samples arrive every ~10-35 s for a healthy container (measured live), so tighter bounds would fail recoveries
    "container_last_seen_age_s": ("max", 150.0, 90.0, 600.0),
    "container_cpu": ("max", None, 0.0, 10000.0),
    "container_memory": ("max", None, 0.0, 1e12),
}


class VerificationCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: str
    metric: Literal["error_rate", "p95_latency_ms", "request_rate", "container_last_seen_age_s", "container_cpu",
                    "container_memory"]
    max_value: Optional[float] = None
    min_value: Optional[float] = None


class PlannedAction(BaseModel):
    """What the model may submit. Extra fields (e.g. 'approved') are rejected."""
    model_config = ConfigDict(extra="forbid")
    operation: str
    target: Optional[str] = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    supporting_evidence_ids: list[str]
    reason: SafeText = Field(min_length=10, max_length=1500)
    expected_effect: SafeText = Field(min_length=5, max_length=800)
    risks: SafeText = Field(min_length=5, max_length=800)
    rollback: Optional[SafeText] = Field(None, max_length=500)
    verification_checks: list[VerificationCheck] = Field(default_factory=list, max_length=MAX_CHECKS)


class Escalation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: SafeText = Field(min_length=5, max_length=1500)
    recommended_human_steps: list[SafeText] = Field(default_factory=list, max_length=8)
    supporting_evidence_ids: list[str] = Field(default_factory=list)


class PolicyDecision(BaseModel):
    allowed: bool
    errors: list[str] = Field(default_factory=list)
    proposal: Optional[ActionProposal] = None
    request_preview: Optional[dict[str, Any]] = None


def _mentions(name: str, text: str) -> bool:
    return re.search(rf"(?<![a-z0-9-]){re.escape(name.lower())}(?![a-z0-9-])", text.lower()) is not None


def _evidence_text(e: Evidence) -> str:
    return f"{e.service or ''} {e.query} {json.dumps(e.observed_value, default=str)}"


# Signals that the target's *process* is broken (so a restart can plausibly help). Deliberately excludes generic
# UNAVAILABLE/timeout/unreachable wording, which application, network and flag-driven faults also produce.
_PROCESS_TEXT = re.compile(r"connection refused|no such host|no route to host|connection reset|out of memory|\boom\b|"
                           r"crash|killed", re.IGNORECASE)


def process_level_evidence(target: str, evidence: list[Evidence]) -> bool:
    """True if any cited evidence shows a process-level problem for the target: container not running, unhealthy or
    restarting; flagged by resource_overview (silent, CPU, memory, restarts); stopped reporting; or callers getting
    refused connections / failed name resolution for it. A restart does not fix code, configuration or flag faults."""
    for e in evidence:
        v = (e.observed_value or {}).get("value") if isinstance(e.observed_value, dict) else None
        v = v if isinstance(v, dict) else {}
        if e.tool == "container_state":
            for r in v.get("services") or []:
                if r.get("service") == target and (r.get("state") not in (None, "running") or r.get("health") == "unhealthy"
                                                   or (r.get("restart_count") or 0) > 0):
                    return True
        if e.tool == "resource_overview" and any(f.get("container") == target for f in v.get("flagged") or []):
            return True
        if e.tool == "service_health" and e.service == target:
            summary = v.get("summary") or {}
            if summary.get("stopped_reporting") or summary.get("recently_restarted"):
                return True
        if (e.tool == "query_metrics" and e.service == target and v.get("metric") == "container_last_seen_age_s"
                and isinstance(v.get("value"), (int, float)) and v["value"] > 150):
            return True
        if e.tool in ("search_traces", "query_logs"):
            text = json.dumps(e.observed_value, default=str)
            if _mentions(target, text) and _PROCESS_TEXT.search(text):
                return True
    return False


def default_checks(op: Operation, target: Optional[str], affected: Optional[str]) -> list[VerificationCheck]:
    checks: list[VerificationCheck] = []
    for svc in [s for s in (affected, target if op.target_kind == "service" else None) if s]:
        if not any(c.service == svc and c.metric == "error_rate" for c in checks):
            checks.append(VerificationCheck(service=svc, metric="error_rate"))
    if op.target_kind == "service" and target:
        checks.append(VerificationCheck(service=target, metric="container_last_seen_age_s"))
    return checks


def evaluate(plan: PlannedAction, *, catalog: Catalog, diagnosis: Diagnosis, evidence: list[Evidence],
             inventory: list[str], incident_id: str, affected_service: Optional[str], revision: int = 1) -> PolicyDecision:
    errors: list[str] = []
    op = catalog.get(plan.operation)
    if op is None:
        names = ", ".join(o.name for o in catalog.operations) or "none"
        return PolicyDecision(allowed=False, errors=[f"operation '{plan.operation}' is not in the Action API catalog (supported: {names})"])
    target = plan.target
    if op.target_kind == "none":
        if target:
            errors.append(f"{op.name} takes no target")
        target = None
    else:
        if not target:
            errors.append(f"{op.name} requires a target")
        elif target in BLOCKED_TARGETS and op.target_kind == "service":
            errors.append(f"target '{target}' is out of bounds")
        elif target not in op.allowed_targets:
            errors.append(f"target '{target}' is not approved for {op.name} (allowed: {', '.join(op.allowed_targets)})")
        elif op.target_kind == "service" and target not in inventory:
            errors.append(f"target '{target}' is not in the observed service inventory")
    args, arg_errors = validate_arguments(op, plan.arguments)
    errors += arg_errors

    by_id = {e.evidence_id: e for e in evidence}
    if not plan.supporting_evidence_ids:
        errors.append("proposal cites no supporting evidence")
    unknown = [i for i in plan.supporting_evidence_ids if i not in by_id]
    if unknown:
        errors.append(f"cited evidence not captured in this investigation: {unknown}")
    cited = [by_id[i] for i in plan.supporting_evidence_ids if i in by_id]
    ok = [e for e in cited if e.status == ToolStatus.OK]
    silent_ok = [e for e in cited if e.status == ToolStatus.EMPTY and e.service == target]
    if cited and not ok:
        errors.append("no cited evidence has status 'ok'; missing data cannot justify an action")
    if target and op.target_kind == "service" and cited and not any(_mentions(target, _evidence_text(e)) for e in ok + silent_ok):
        errors.append(f"no cited evidence mentions the target '{target}'")
    if diagnosis.confidence_label == ConfidenceLabel.LOW:
        errors.append("diagnosis confidence is low: escalate instead of acting")
    if op.name == "restart_service" and target and cited and not process_level_evidence(target, cited):
        errors.append(f"restart_service needs process-level evidence for '{target}' (container stopped/unhealthy/"
                      "restarting, stopped reporting, CPU or memory anomaly, or refused connections / failed name "
                      "resolution). A restart does not fix application errors, configuration, latency or flag-driven "
                      "faults: escalate with steps for a human instead")
    if target and op.target_kind == "service" and not _mentions(target, diagnosis.suspected_component):
        errors.append(f"target '{target}' is not implicated by the accepted diagnosis ({diagnosis.suspected_component})")

    checks: list[VerificationCheck] = []
    for c in plan.verification_checks + default_checks(op, target, affected_service):
        if c.service in BLOCKED_TARGETS or c.service not in inventory:
            errors.append(f"verification check service '{c.service}' is not in the inventory")
            continue
        _, _, lo, hi = VERIFICATION_METRICS[c.metric]
        for bound in (c.max_value, c.min_value):
            if bound is not None and not lo <= bound <= hi:
                errors.append(f"verification bound {bound} for {c.metric} outside [{lo}, {hi}]")
        if not any(x.service == c.service and x.metric == c.metric for x in checks) and len(checks) < MAX_CHECKS:
            checks.append(c)
    if errors:
        return PolicyDecision(allowed=False, errors=errors)
    proposal = ActionProposal(
        incident_id=incident_id, revision=revision, runbook_id=op.name, action=op.name,
        target_service=target or "-", parameters=args, reason=plan.reason, risk=f"{plan.risks} | catalog: {op.risk}",
        expected_effect=plan.expected_effect,
        verification_plan="; ".join(_describe(c) for c in checks),
        supporting_evidence_ids=list(plan.supporting_evidence_ids), rollback=plan.rollback or op.rollback,
        catalog_provenance=catalog.provenance, verification_checks=[c.model_dump() for c in checks],
    )
    return PolicyDecision(allowed=True, proposal=proposal, request_preview=build_request(catalog, op, target, args))


def _describe(c: VerificationCheck) -> str:
    kind, default, *_ = VERIFICATION_METRICS[c.metric]
    if c.max_value is not None:
        return f"{c.service}.{c.metric} <= {c.max_value}"
    if c.min_value is not None:
        return f"{c.service}.{c.metric} >= {c.min_value}"
    if default is not None:
        return f"{c.service}.{c.metric} {'<=' if kind == 'max' else '>='} {default}"
    return f"{c.service}.{c.metric} improves >= 50% vs baseline"


def proposal_digest(proposal: ActionProposal, catalog: Catalog) -> str:
    op = catalog.get(proposal.action)
    payload = {"proposal": proposal.model_dump(mode="json"), "operation": op.digest() if op else None,
               "provenance": catalog.provenance, "base_url": catalog.base_url}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


class KillSwitch:
    """Operator-owned stop: env INCIDENT_AGENT_KILL_SWITCH=1 or the presence of the kill-switch file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def active(self) -> bool:
        return os.getenv("INCIDENT_AGENT_KILL_SWITCH", "").strip().lower() in {"1", "true", "on", "yes"} or self.path.exists()

    def set(self, on: bool) -> None:
        if on:
            self.path.write_text(datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8")
        elif self.path.exists():
            self.path.unlink()


class AuditJournal:
    """Append-only JSON lines. Dispatch intent is written (and flushed) before the request is sent."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: str, **fields: Any) -> None:
        rec = {"time": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def already_dispatched(self, digest: str, incident_id: str) -> Optional[str]:
        for e in self.entries():
            if e.get("event") == "dispatch_intent":
                if e.get("digest") == digest:
                    return "this exact action was already dispatched"
                if e.get("incident_id") == incident_id:
                    return "an action was already dispatched for this incident (one action per incident)"
        return None


class DispatchLock:
    """Single-process dispatch lock. A stale lock after a crash blocks dispatch until the operator removes it."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.fd: Optional[int] = None

    def __enter__(self) -> "DispatchLock":
        self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(self.fd, str(os.getpid()).encode())
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.path.unlink(missing_ok=True)


MAX_APPROVAL_ATTEMPTS = 3
_REJECT_WORDS = {"", "reject", "no", "n", "deny"}
_SERVER_CODE = re.compile(r"^[A-Z2-9]{4}-?[A-Z2-9]{4}$")


def _read_decision(instructions: str, check_code: Callable[[str], Optional[str]], *, ttl_s: float,
                   input_fn: Callable[[str], str], print_fn: Callable[[str], None],
                   clock: Callable[[], float]) -> tuple[str, Optional[str]]:
    """Ask until the human approves with a valid code, rejects, runs out of attempts or the TTL passes.

    Returns ('approved', CODE) | ('rejected', None) | ('expired', None). Angle brackets, quotes and case are forgiven;
    the word 'approve' plus a code is still required. The typed answer is never logged.
    """
    print_fn(instructions)
    started = clock()
    for attempt in range(1, MAX_APPROVAL_ATTEMPTS + 1):
        try:
            raw = input_fn("> ")
        except (EOFError, KeyboardInterrupt):
            raw = ""
        if clock() - started > ttl_s:
            return "expired", None
        parts = re.sub(r"[<>\"'`]", " ", raw or "").split()
        if " ".join(parts).lower() in _REJECT_WORDS:
            return "rejected", None
        if len(parts) == 2 and parts[0].lower() == "approve":
            code = parts[1].upper()
            problem = check_code(code)
            if problem is None:
                return "approved", code
        elif len(parts) == 1 and parts[0].lower() == "approve":
            problem = "the code is missing; type the word approve AND the code, e.g.  approve XXXX-XXXX"
        elif len(parts) == 1:
            problem = "type the word approve before the code, e.g.  approve XXXX-XXXX"
        else:
            problem = "not understood; expected  approve XXXX-XXXX"
        if attempt < MAX_APPROVAL_ATTEMPTS:
            print_fn(f"Not approved yet: {problem}. Try again ({MAX_APPROVAL_ATTEMPTS - attempt} left), "
                     "or press Enter to reject.")
    return "rejected", None


def request_approval(summary: str, digest: str, *, approver: str, ttl_s: float, input_fn: Callable[[str], str],
                     interactive: bool, print_fn: Callable[[str], None] = print,
                     clock: Callable[[], float] = time.monotonic) -> Approval:
    """Explicit human approval bound to the proposal digest. Default is rejection; the model has no path here."""
    code = digest[:8]
    expiry = datetime.now(timezone.utc) + timedelta(seconds=ttl_s)
    if not interactive:
        return Approval(proposal_digest=digest, decision="rejected", approver="none (non-interactive session)", expiry=expiry)
    print_fn(summary)
    decision, _ = _read_decision(
        f"\n>>> To APPROVE this exact action, type:   approve {code}\n"
        f">>> To REJECT: press Enter or type reject.   Expires in {ttl_s:.0f}s.",
        lambda c: None if c.lower() == code else "that code does not match this proposal (it is shown above)",
        ttl_s=ttl_s, input_fn=input_fn, print_fn=print_fn, clock=clock)
    return Approval(proposal_digest=digest, decision=decision, approver=approver, expiry=expiry)


def request_server_approval(summary: str, digest: str, approval_id: str, *, approver: str, ttl_s: float,
                            input_fn: Callable[[str], str], interactive: bool, print_fn: Callable[[str], None] = print,
                            clock: Callable[[], float] = time.monotonic) -> tuple[Approval, Optional[str]]:
    """Approval for the team Action API. The one-time code exists only on the Action API console; the agent cannot know
    it, so it cannot approve on its own. The server verifies the code against the exact payload at execution time."""
    expiry = datetime.now(timezone.utc) + timedelta(seconds=ttl_s)
    if not interactive:
        return Approval(proposal_digest=digest, decision="rejected", approver="none (non-interactive session)", expiry=expiry), None
    print_fn(summary)
    decision, code = _read_decision(
        f"\n>>> To APPROVE: find  ONE-TIME APPROVAL CODE: XXXX-XXXX  for {approval_id} in the Action API terminal\n"
        ">>>             (terminal 1), check the action there matches, then type here:   approve XXXX-XXXX\n"
        f">>> To REJECT: press Enter or type reject.   Expires in {ttl_s:.0f}s.",
        lambda c: None if _SERVER_CODE.match(c) else "codes look like XXXX-XXXX (letters and digits 2-9)",
        ttl_s=ttl_s, input_fn=input_fn, print_fn=print_fn, clock=clock)
    return Approval(proposal_digest=digest, decision=decision, approver=approver, expiry=expiry), code
