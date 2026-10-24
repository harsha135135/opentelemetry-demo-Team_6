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

from actions import Catalog, Operation, build_request, validate_arguments
from models import ActionProposal, Approval, ConfidenceLabel, Diagnosis, Evidence, ToolStatus

BLOCKED_TARGETS = {"flagd", "flagd-ui"}
MAX_CHECKS = 6
# metric -> (default bound kind, default bound, hard lower, hard upper)
VERIFICATION_METRICS: dict[str, tuple[str, Optional[float], float, float]] = {
    "error_rate": ("max", 0.05, 0.0, 0.5),
    "p95_latency_ms": ("max", None, 1.0, 60000.0),
    "request_rate": ("min", None, 0.0, 10000.0),
    "container_last_seen_age_s": ("max", 150.0, 30.0, 600.0),
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
    reason: str = Field(min_length=10, max_length=1500)
    expected_effect: str = Field(min_length=5, max_length=800)
    risks: str = Field(min_length=5, max_length=800)
    rollback: Optional[str] = Field(None, max_length=500)
    verification_checks: list[VerificationCheck] = Field(default_factory=list, max_length=MAX_CHECKS)


class Escalation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=5, max_length=1500)
    recommended_human_steps: list[str] = Field(default_factory=list, max_length=8)
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


def request_approval(summary: str, digest: str, *, approver: str, ttl_s: float, input_fn: Callable[[str], str],
                     interactive: bool, print_fn: Callable[[str], None] = print,
                     clock: Callable[[], float] = time.monotonic) -> Approval:
    """Explicit human approval bound to the proposal digest. Default is rejection; the model has no path here."""
    code = digest[:8]
    expiry = datetime.now(timezone.utc) + timedelta(seconds=ttl_s)
    if not interactive:
        return Approval(proposal_digest=digest, decision="rejected", approver="none (non-interactive session)", expiry=expiry)
    print_fn(summary)
    print_fn(f"\nTo APPROVE this exact action type:  approve {code}\nAnything else rejects. Expires in {ttl_s:.0f}s.")
    started = clock()
    try:
        answer = input_fn("> ").strip()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if clock() - started >= ttl_s:
        return Approval(proposal_digest=digest, decision="expired", approver=approver, expiry=expiry)
    decision = "approved" if answer == f"approve {code}" else "rejected"
    return Approval(proposal_digest=digest, decision=decision, approver=approver, expiry=expiry)
