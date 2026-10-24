"""Remediation flow after a validated diagnosis: plan -> policy -> baseline -> human approval -> (dry-run | dispatch)
-> bounded observation -> verification. Every path returns an updated report; at most one action per incident.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional

import httpx
from pydantic import BaseModel, ValidationError

from action_api_models import ActionPayload, VerificationReport, printable
from actions import ActionClient, Catalog
from contracts import ModelError
from integration import EvidenceConflict, EvidenceRegistry
from models import ConfidenceLabel, Incident, IncidentReport, IncidentStatus, TimelineEvent, Verification, VerificationResult
from remediation import Planner, PlannerOutputError
from action_api_client import ActionApiError
from safety import (
    AuditJournal, DispatchLock, Escalation, KillSwitch, PlannedAction, VerificationCheck, evaluate, proposal_digest,
    request_approval, request_server_approval, wait_for_page_approval,
)
import verify

OUTCOME_STATUS = {
    "VERIFIED_RECOVERY": IncidentStatus.RECOVERED,
    "PARTIAL_RECOVERY": IncidentStatus.ESCALATED,
    "NOT_RECOVERED": IncidentStatus.ESCALATED,
    "UNVERIFIED": IncidentStatus.ESCALATED,
    "ACTION_FAILED": IncidentStatus.ESCALATED,
}
VERIFICATION_RESULT = {
    "VERIFIED_RECOVERY": VerificationResult.PASS, "PARTIAL_RECOVERY": VerificationResult.FAIL,
    "NOT_RECOVERED": VerificationResult.FAIL, "UNVERIFIED": VerificationResult.INCONCLUSIVE,
}


class ResponseConfig(BaseModel):
    mode: Literal["dry_run", "live"] = "dry_run"
    approval_ttl_s: float = 120
    approval_channel: Literal["terminal", "page"] = "terminal"  # page: the Action API's browser approval page
    max_planner_calls: int = 2
    settle_s: float = 120  # rate() windows must cover post-action time only
    interval_s: float = 30
    samples: int = 2
    window_minutes: int = 2
    min_request_rate: float = 0.05


@dataclass
class ResponseDeps:
    planner: Planner
    catalog: Catalog
    run_tool: Callable[[str, dict | None], Any]
    registry: EvidenceRegistry
    inventory: list[str]
    kill_switch: KillSwitch
    journal: AuditJournal
    lock_path: Path
    approver: str
    interactive: bool
    input_fn: Callable[[str], str] = input
    print_fn: Callable[[str], None] = print
    client: Optional[ActionClient] = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    notes: list[str] = field(default_factory=list)
    on_stage: Optional[Callable[[str, IncidentReport], None]] = None  # e.g. publish the interim report to Grafana


def respond(report: IncidentReport, cfg: ResponseConfig, deps: ResponseDeps) -> IncidentReport:
    timeline = list(report.timeline)
    limitations = list(report.limitations)
    upd: dict[str, Any] = {"execution_mode": cfg.mode}
    incident: Incident = report.incident

    def log(status: IncidentStatus, text: str, **meta: Any) -> None:
        timeline.append(TimelineEvent(status=status, description=text, metadata=meta))

    def register(ev) -> None:
        try:
            deps.registry.register(ev)
        except EvidenceConflict as exc:
            limitations.append(f"Evidence rejected: {exc}")

    def stage(name: str, status: IncidentStatus) -> None:
        if deps.on_stage is None:
            return
        try:
            deps.on_stage(name, report.model_copy(update={**upd, "status": status, "timeline": list(timeline),
                                                          "limitations": list(limitations), "evidence": deps.registry.all()}))
        except Exception as exc:  # noqa: BLE001 - publishing progress must never change the remediation
            deps.print_fn(f"(progress update {name} not published: {type(exc).__name__}: {exc})")

    def finish(status: IncidentStatus, outcome: str, reason: Optional[str] = None) -> IncidentReport:
        if reason:
            limitations.append(reason)
            upd["escalation_reason"] = reason
        log(status, f"Remediation finished: {outcome}" + (f" ({reason})" if reason else ""))
        deps.journal.append("finished", incident_id=incident.id, outcome=outcome, mode=cfg.mode)
        return report.model_copy(update={**upd, "status": status, "timeline": timeline, "limitations": limitations,
                                         "evidence": deps.registry.all(), "remediation_outcome": outcome})

    if report.status != IncidentStatus.DIAGNOSED or report.diagnosis is None:
        return finish(report.status, "ESCALATED", "No accepted diagnosis, so no remediation was planned; escalate to a human.")
    diagnosis = report.diagnosis
    if diagnosis.confidence_label == ConfidenceLabel.LOW:
        return finish(IncidentStatus.ESCALATED, "ESCALATED", "Diagnosis confidence is low; remediation not planned. Human review required.")
    if not deps.catalog.operations:
        return finish(IncidentStatus.ESCALATED, "ESCALATED",
                      "Action API contract unavailable: no supported operations are configured, so no action can be proposed.")

    try:
        # ---- plan (bounded: one correction round) and deterministic policy
        decision, feedback, plan = None, None, None
        for attempt in range(cfg.max_planner_calls):
            try:
                plan = deps.planner.plan(incident, diagnosis, deps.registry.all(), deps.catalog, deps.inventory, feedback)
            except PlannerOutputError as exc:
                feedback = str(exc)
                log(IncidentStatus.DIAGNOSED, f"Planner output unusable: {exc}")
                continue
            if isinstance(plan, Escalation):
                limitations.append("Planner escalated: " + plan.reason +
                                   (" Recommended steps: " + "; ".join(plan.recommended_human_steps) if plan.recommended_human_steps else ""))
                return finish(IncidentStatus.ESCALATED, "ESCALATED", "No safe supported remediation was proposed.")
            decision = evaluate(plan, catalog=deps.catalog, diagnosis=diagnosis, evidence=deps.registry.all(),
                                inventory=deps.inventory, incident_id=incident.id,
                                affected_service=incident.affected_service, revision=attempt + 1)
            if decision.allowed:
                break
            feedback = "; ".join(decision.errors)
            log(IncidentStatus.DIAGNOSED, "Proposal refused by policy", errors=decision.errors,
                proposal=plan.model_dump(mode="json"))
            deps.journal.append("policy_refused", incident_id=incident.id, errors=decision.errors, plan=plan.model_dump(mode="json"))
        if decision is None or not decision.allowed:
            return finish(IncidentStatus.ESCALATED, "ESCALATED",
                          f"No proposal passed the policy engine: {feedback}")
        proposal = decision.proposal
        op = deps.catalog.get(proposal.action)
        checks = [VerificationCheck(**c) for c in proposal.verification_checks]
        upd["proposals"] = [proposal]
        log(IncidentStatus.ACTION_PROPOSED, f"Proposal passed policy: {op.name} on {proposal.target_service}",
            request=decision.request_preview)

        # ---- baseline before approval
        baseline, base_traffic, base_ids = verify.collect(checks, deps.run_tool, register, cfg.window_minutes)
        observed_services = sorted({s for s in (proposal.target_service, incident.affected_service) if s and s in deps.inventory})
        obs_before, obs_ids = verify.observe(observed_services, deps.run_tool, register, cfg.window_minutes)
        base_ids += obs_ids
        symptoms = {c.service + "." + c.metric: verify.symptomatic(c, baseline.get((c.service, c.metric))) for c in checks}
        log(IncidentStatus.ACTION_PROPOSED, "Baseline captured", baseline={f"{k[0]}.{k[1]}": v for k, v in baseline.items()},
            evidence=base_ids, symptomatic=symptoms)
        if symptoms and all(v is False for v in symptoms.values()):
            return finish(IncidentStatus.ESCALATED, "ESCALATED",
                          "Verification metrics show no symptom at baseline; recovery could not be measured, so no action was requested.")

        # ---- kill switch, then explicit human approval bound to the digest
        if deps.kill_switch.active():
            deps.journal.append("blocked_kill_switch", incident_id=incident.id, action=op.name)
            return finish(IncidentStatus.ESCALATED, "ESCALATED", "Kill switch is active: state-changing actions are blocked.")
        digest = proposal_digest(proposal, deps.catalog)
        summary = _summary(report, proposal, decision.request_preview, deps, cfg, baseline)
        server = cfg.mode == "live" and getattr(deps.client, "supports_server_approval", False)
        payload = _payload(proposal, digest, incident.id) if getattr(deps.client, "supports_server_approval", False) else None
        ticket, server_code = None, None
        log(IncidentStatus.AWAITING_APPROVAL, f"Awaiting human approval (digest {digest[:16]})")
        if server:
            try:
                ticket = deps.client.open_approval(payload, deps.approver, cfg.approval_ttl_s)
            except (ActionApiError, httpx.HTTPError) as exc:
                deps.journal.append("approval_request_failed", incident_id=incident.id, error=str(exc)[:300])
                return finish(IncidentStatus.ESCALATED, "ESCALATED", f"Action API unavailable or refused the approval request: {exc}")
            deps.journal.append("approval_opened", incident_id=incident.id, digest=digest, approval_id=ticket.approval_id,
                                payload_digest=ticket.payload_digest)
            log(IncidentStatus.AWAITING_APPROVAL, f"Action API approval {ticket.approval_id} opened; code shown only on the Action API console",
                payload_digest=ticket.payload_digest, expires_at=ticket.expires_at.isoformat())
            if cfg.approval_channel == "page":
                page = deps.client.approval_page_url(ticket)
                log(IncidentStatus.AWAITING_APPROVAL, f"Approval page: {page}")
                stage("awaiting_approval", IncidentStatus.AWAITING_APPROVAL)
                approval = wait_for_page_approval(summary, digest, ticket.approval_id, status_fn=deps.client.approval_status,
                                                  page_url=page, ttl_s=cfg.approval_ttl_s, print_fn=deps.print_fn,
                                                  sleep=deps.sleep, clock=deps.clock)
                server_code = None
            else:
                approval, server_code = request_server_approval(summary, digest, ticket.approval_id, approver=deps.approver,
                                                                ttl_s=cfg.approval_ttl_s, input_fn=deps.input_fn,
                                                                interactive=deps.interactive, print_fn=deps.print_fn,
                                                                clock=deps.clock)
        else:
            approval = request_approval(summary, digest, approver=deps.approver, ttl_s=cfg.approval_ttl_s, input_fn=deps.input_fn,
                                        interactive=deps.interactive, print_fn=deps.print_fn, clock=deps.clock)
        upd["approval"] = approval
        deps.journal.append("approval", incident_id=incident.id, digest=digest, decision=approval.decision, approver=approval.approver,
                            approval_id=ticket.approval_id if ticket else None)
        if approval.decision != "approved":
            if ticket is not None:
                try:
                    deps.client.reject_approval(ticket.approval_id, approval.approver, f"agent prompt: {approval.decision}")
                except (ActionApiError, httpx.HTTPError):
                    limitations.append("Could not record the rejection on the Action API; the approval expires on its own.")
            return finish(IncidentStatus.REJECTED, "ESCALATED", f"Human approval {approval.decision}: no action executed.")
        log(IncidentStatus.APPROVED, f"Approved by {approval.approver}")

        if cfg.mode == "dry_run":
            if payload is not None:
                try:
                    v = deps.client.validate(payload)
                    log(IncidentStatus.APPROVED, f"Action API dry-run validation: {'accepted' if v.get('valid') else 'refused'}",
                        payload_digest=v.get("payload_digest"), errors=v.get("errors"))
                    if not v.get("valid"):
                        limitations.append(f"Action API would refuse this action: {v.get('errors')}")
                except (ActionApiError, httpx.HTTPError) as exc:
                    limitations.append(f"Action API validation unavailable during dry-run: {exc}")
            deps.journal.append("dry_run_would_dispatch", incident_id=incident.id, digest=digest, request=decision.request_preview)
            limitations.append("Dry-run: the approved request was not sent; no system state was changed and recovery was not tested.")
            return finish(IncidentStatus.ACTION_PROPOSED, "DRY_RUN")

        # ---- revalidate immediately before dispatch
        recheck = evaluate(plan, catalog=deps.catalog, diagnosis=diagnosis, evidence=deps.registry.all(),
                           inventory=deps.inventory, incident_id=incident.id,
                           affected_service=incident.affected_service, revision=proposal.revision)
        problems = []
        if not recheck.allowed or proposal_digest(recheck.proposal, deps.catalog) != approval.proposal_digest:
            problems.append("proposal changed after approval; the approval is void")
        if datetime.now(timezone.utc) > approval.expiry:
            problems.append("approval expired before dispatch")
        if deps.kill_switch.active():
            problems.append("kill switch activated before dispatch")
        dup = deps.journal.already_dispatched(digest, incident.id)
        if dup:
            problems.append(dup)
        if deps.client is None:
            problems.append("no live Action API client is configured")
        fresh, _, _ = verify.collect([c for c in checks if c.metric != "request_rate"], deps.run_tool, register, cfg.window_minutes)
        fresh_symptoms = [verify.symptomatic(c, fresh.get((c.service, c.metric))) for c in checks if c.metric != "request_rate"]
        if fresh_symptoms and all(v is False for v in fresh_symptoms):
            problems.append("symptom no longer present in fresh telemetry; action not needed")
        if problems:
            deps.journal.append("dispatch_refused", incident_id=incident.id, digest=digest, reasons=problems)
            return finish(IncidentStatus.ESCALATED, "ESCALATED", "Dispatch refused: " + "; ".join(problems))

        # ---- dispatch exactly once: journal intent first, then call the Action API
        approval = approval.model_copy(update={"single_use_consumed": True, "dispatch_time": datetime.now(timezone.utc)})
        key = f"{incident.id}-{digest[:16]}"
        try:
            with DispatchLock(deps.lock_path):
                deps.journal.append("dispatch_intent", incident_id=incident.id, digest=digest, idempotency_key=key,
                                    request=decision.request_preview)
                log(IncidentStatus.EXECUTING, f"Dispatching {op.name} on {proposal.target_service}")
                dispatched_at = deps.clock()
                if server:
                    result = deps.client.execute_approved(payload, ticket.approval_id, server_code, key)
                else:
                    result = deps.client.execute(op, None if op.target_kind == "none" else proposal.target_service,
                                                 proposal.parameters, key)
        except FileExistsError:
            return finish(IncidentStatus.ESCALATED, "ESCALATED",
                          f"Dispatch lock {deps.lock_path} exists (another dispatch or a crash); remove it manually after checking.")
        deps.journal.append("dispatch_result", incident_id=incident.id, digest=digest, outcome=result.outcome,
                            http_status=result.http_status, request_id=result.request_id, detail=result.detail)
        approval = approval.model_copy(update={"request_id": result.request_id, "response": result.response or {"detail": result.detail},
                                               "outcome": {"success": "success", "failed": "failed"}.get(result.outcome, "unknown")})
        upd["approval"] = approval
        log(IncidentStatus.EXECUTING, f"Action API result: {result.outcome} {result.http_status or ''} {result.detail}".strip())
        if result.refused:
            return finish(IncidentStatus.ESCALATED, "ESCALATED", f"Action not executed: {result.detail}. Not retried.")
        if result.outcome == "failed":
            return finish(IncidentStatus.ESCALATED, "ACTION_FAILED", f"Action API call failed: {result.detail}. Not retried.")
        if result.simulated:
            limitations.append("SIMULATED execution: the Action API changed nothing; verification can only show coincidental change.")
        if result.outcome == "unknown":
            limitations.append(f"Action outcome unknown ({result.detail}); not retried. Verifying from telemetry only.")

        # ---- bounded observation and independent verification
        log(IncidentStatus.VERIFYING, f"Observing for {cfg.settle_s + cfg.interval_s * (cfg.samples - 1):.0f}s")
        stage("verifying", IncidentStatus.VERIFYING)
        deps.sleep(cfg.settle_s)
        samples, traffic, post_ids = [], [], []
        for i in range(cfg.samples):
            if i:
                deps.sleep(cfg.interval_s)
            vals, tr, ids = verify.collect(checks, deps.run_tool, register, cfg.window_minutes)
            samples.append(vals); traffic.append(tr); post_ids += ids
        result_v = verify.judge(checks, baseline, samples, traffic, cfg.min_request_rate, op.reduces_load)
        obs_after, obs_ids = verify.observe(observed_services, deps.run_tool, register, cfg.window_minutes)
        post_ids += obs_ids
        outcome, details = result_v.outcome, result_v.details
        if result.simulated and outcome in ("VERIFIED_RECOVERY", "PARTIAL_RECOVERY"):
            details += f"; judged {outcome} but the action was simulated, so recovery cannot be attributed to it"
            outcome = "UNVERIFIED"
        details = "; ".join(x for x in (details, verify.describe_observations(obs_before, obs_after)) if x)
        upd["verification"] = Verification(incident_id=incident.id, baseline_evidence_ids=base_ids,
                                           post_action_evidence_ids=post_ids, result=VERIFICATION_RESULT[outcome],
                                           details=details)
        if server and result.request_id:
            try:
                deps.client.record_verification(result.request_id, VerificationReport(
                    outcome=outcome, details=details[:6000], baseline_evidence_ids=base_ids[:100],
                    post_action_evidence_ids=post_ids[:100], recorded_by="incident-agent"))
            except (ActionApiError, httpx.HTTPError, ValidationError) as exc:
                limitations.append(f"Verification not recorded on the Action API: {exc}")
        if result.outcome == "unknown" and outcome == "VERIFIED_RECOVERY":
            limitations.append("Recovery observed although the Action API outcome is unknown; the cause of recovery is uncertain.")
        reason = None if outcome == "VERIFIED_RECOVERY" else f"Recovery not verified ({outcome}); escalating with evidence. No further action taken."
        return finish(OUTCOME_STATUS[outcome], outcome, reason)
    except ModelError as exc:
        return finish(IncidentStatus.ESCALATED, "ESCALATED", f"Remediation planner failed: {exc}")
    except KeyboardInterrupt:
        dispatched = any(e.get("event") == "dispatch_intent" and e.get("incident_id") == incident.id for e in deps.journal.entries())
        return finish(IncidentStatus.ESCALATED, "UNVERIFIED" if dispatched else "ESCALATED",
                      "Cancelled by operator" + (" after dispatch; recovery not verified." if dispatched else "; no action was sent."))


def _payload(proposal, digest: str, incident_id: str) -> ActionPayload:
    return ActionPayload(incident_id=incident_id, operation=proposal.action, target=proposal.target_service,
                         arguments=proposal.parameters, evidence_ids=proposal.supporting_evidence_ids[:20],
                         reason=proposal.reason[:1500], proposal_digest=digest)


def _summary(report: IncidentReport, proposal, request: dict, deps: ResponseDeps, cfg: ResponseConfig, baseline: dict) -> str:
    """Approval screen. Model- and telemetry-derived text is escaped (no terminal control sequences) and printed
    first; the authoritative action block comes last so nothing printed earlier can redraw it."""
    P = printable
    d = report.diagnosis
    reg = {e.evidence_id: e for e in deps.registry.all()}
    ev_lines = [f"  - {eid} [{e.tool}/{e.service or '-'}] {P(e.observed_value, 160)}"
                for eid in proposal.supporting_evidence_ids if (e := reg.get(eid))]
    base = "\n".join(f"  - {s}.{m}: {v}" for (s, m), v in baseline.items())
    mode = "DRY RUN (nothing will be sent)" if cfg.mode == "dry_run" else "LIVE (the request below WILL be sent)"
    api = "team-defined, not an organiser contract" if proposal.catalog_provenance == "team" else "see catalog"
    return "\n".join([
        "=" * 78, f"APPROVAL REQUEST - incident {report.incident_id} - mode: {mode}", "=" * 78,
        f"Incident: {P(report.incident.symptom)}",
        f"Diagnosis (model, untrusted text): {P(d.suspected_component, 200)}: {P(d.suspected_cause, 600)} "
        f"(confidence label {d.confidence_label.value}, not calibrated)",
        f"Uncertainty: {P(d.uncertainty, 600)}",
        "Supporting evidence:", *ev_lines,
        f"Reason: {P(proposal.reason)}", f"Expected effect: {P(proposal.expected_effect)}",
        f"Risks: {P(proposal.risk)}", f"Rollback: {P(proposal.rollback or 'none documented')}",
        f"Verification: {P(proposal.verification_plan)}", "Baseline:", base or "  (none)",
        "Limitations: API success is not recovery; the outcome is verified from fresh telemetry. One action per incident.",
        "---- exact action (authoritative) ----",
        f"OPERATION: {proposal.action}", f"TARGET:    {proposal.target_service}", f"ARGUMENTS: {proposal.parameters!r}",
        f"REQUEST:   {request['method']} {request['url']}  body: {request['json']!r}",
        f"ACTION API: {proposal.catalog_provenance} ({api})",
    ])
