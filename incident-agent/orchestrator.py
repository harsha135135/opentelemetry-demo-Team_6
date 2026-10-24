"""Bounded investigation loop. Owns state, budgets, dispatch and evidence registration (Harsha, Member 1)."""
from __future__ import annotations

import re
import time
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import BaseModel

from contracts import (
    CallToolDecision, FinishDecision, InconclusiveDecision, MalformedDecision, ModelError, ReasoningContext,
    ReasoningModel, ToolOutcome, ToolPolicy, ToolRequest,
)
from guardrails import validate_diagnosis as default_validate_diagnosis, validate_tool_request as default_validate_tool_request
from telemetry_executor import run_with_deadline
from integration import EvidenceConflict, EvidenceRegistry, to_evidence
from models import ConfidenceLabel, Incident, IncidentReport, IncidentStatus, TimelineEvent

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RECOVERY_NOT_PROVIDED = "not provided - diagnosis only (CP0-CP2, no remediation)"


class IncidentInputError(ValueError):
    pass


class InvestigationConfig(BaseModel):
    deadline_s: float = 180
    max_tool_calls: int = 12
    max_model_calls: int = 6
    request_timeout_s: float = 60
    max_total_tokens: int = 60000
    tool_worst_case_s: float = 25  # composite tools make several backend requests
    max_repairs: int = 1
    max_follow_up_nudges: int = 1
    max_diagnosis_corrections: int = 1


@dataclass
class Dependencies:
    reasoner: ReasoningModel
    run_tool: Callable[[str, dict | None], Any]
    policy: ToolPolicy
    registry: EvidenceRegistry
    validate_tool_request: Callable = default_validate_tool_request
    validate_diagnosis: Callable = default_validate_diagnosis
    clock: Callable[[], float] = time.monotonic


def validate_incident(incident: Incident) -> None:
    if not _ID.match(incident.id):
        raise IncidentInputError("incident id must match [A-Za-z0-9._-], 1-64 chars")
    if not incident.symptom.strip():
        raise IncidentInputError("symptom must not be empty")
    for name, dt in (("detection_time", incident.detection_time),
                     ("window start", incident.observation_window.start_time),
                     ("window end", incident.observation_window.end_time)):
        if dt.tzinfo is None or dt.utcoffset() is None:
            raise IncidentInputError(f"{name} must be timezone-aware")
    if incident.observation_window.start_time >= incident.observation_window.end_time:
        raise IncidentInputError("observation window start must be before end")


def _outcome_text(result: Any) -> str:
    return result.for_llm()


def investigate(incident: Incident, config: InvestigationConfig, deps: Dependencies) -> IncidentReport:
    validate_incident(incident)
    t0 = deps.clock()
    deadline = t0 + config.deadline_s
    timeline: list[TimelineEvent] = []
    limitations: list[str] = []
    stats = {"model_calls": 0, "tool_calls": 0, "tool_requests_rejected": 0, "tokens": 0}

    def log(status: IncidentStatus, text: str, **meta: Any) -> None:
        timeline.append(TimelineEvent(status=status, description=text, metadata=meta))

    if incident.affected_service and incident.affected_service not in deps.policy.approved_services:
        raise IncidentInputError(f"affected_service '{incident.affected_service}' is not in the approved inventory")
    if incident.recovery_condition == RECOVERY_NOT_PROVIDED:
        limitations.append("No recovery condition supplied; recovery is out of scope for CP0-CP2.")
    log(IncidentStatus.RECEIVED, f"Incident received from {incident.source}")
    log(IncidentStatus.INVESTIGATING, "Investigation started")
    live_note = ("Telemetry tools query live windows ending at call time, not the incident's historical window. "
                 f"Incident window: {incident.observation_window.start_time.isoformat()} to {incident.observation_window.end_time.isoformat()}.")

    pending: list[ToolOutcome] = []
    status, terminal_reason, diagnosis = IncidentStatus.INCONCLUSIVE, "unknown", None
    repairs = 0
    nudges = 0
    corrections = 0
    rejection: str | None = None
    rejected_diag_errors: list[str] = []
    try:
        while True:
            remaining_s = deadline - deps.clock()
            tools_left = config.max_tool_calls - stats["tool_calls"]
            models_left = config.max_model_calls - stats["model_calls"]
            if remaining_s <= 0:
                status, terminal_reason = IncidentStatus.ESCALATED, "deadline_exceeded"; break
            if models_left <= 0:
                status, terminal_reason = IncidentStatus.INCONCLUSIVE, "model_call_budget_exhausted"; break
            if stats["tokens"] >= config.max_total_tokens:
                status, terminal_reason = IncidentStatus.INCONCLUSIVE, "token_budget_exhausted"; break
            allow_tools = tools_left > 0 and models_left > 1 and remaining_s > config.tool_worst_case_s
            ctx = ReasoningContext(
                incident=incident, evidence=deps.registry.all(), pending_outcomes=pending,
                tool_specs=_specs(deps), allow_tools=allow_tools, remaining_tool_calls=tools_left,
                remaining_model_calls=models_left, remaining_seconds=remaining_s,
                request_timeout_s=config.request_timeout_s, live_only_note=live_note, rejection=rejection,
            )
            pending, rejection = [], None
            stats["model_calls"] += 1  # counted before the request so failures and retries consume budget
            try:
                decision = deps.reasoner.next_step(ctx)
            except ModelError as exc:
                status, terminal_reason = IncidentStatus.ESCALATED, "model_error"
                limitations.append(f"Model request failed: {exc}")
                break
            stats["tokens"] += decision.input_tokens + decision.output_tokens

            if isinstance(decision, FinishDecision):
                result = deps.validate_diagnosis(decision.diagnosis, deps.registry.all())
                if result.valid:
                    unresolved, uncited = anomaly_follow_up(deps.registry, decision.diagnosis)
                    can_follow_up = tools_left > 0 and models_left > 1 and remaining_s > config.tool_worst_case_s
                    if unresolved and nudges < config.max_follow_up_nudges and can_follow_up:
                        nudges += 1
                        rejection = ("Not accepted yet: resource_overview flagged " + ", ".join(unresolved) +
                                     " with no follow-up evidence. Investigate each (service_health / query_metrics / traces / logs), "
                                     "decide whether it is cause, effect or unrelated, cite that evidence, then finish again.")
                        log(IncidentStatus.INVESTIGATING, f"Finish deferred: unresolved resource anomalies {unresolved}")
                        continue
                    diagnosis = _cap_confidence(decision.diagnosis, deps.registry)
                    if unresolved:
                        diagnosis = diagnosis.model_copy(update={
                            "confidence_label": ConfidenceLabel.LOW,
                            "uncertainty": f"{diagnosis.uncertainty} [Unresolved resource anomaly without follow-up evidence: {', '.join(unresolved)}.]"})
                        limitations.append(f"Resource anomalies flagged but not followed up with evidence: {', '.join(unresolved)}")
                    if uncited:
                        limitations.append(f"Follow-up evidence exists for flagged services but the diagnosis cites none of it: {', '.join(uncited)}")
                    status, terminal_reason = IncidentStatus.DIAGNOSED, "diagnosed"
                    log(IncidentStatus.DIAGNOSED, f"Diagnosis validated: {decision.diagnosis.suspected_component}")
                else:
                    if corrections < config.max_diagnosis_corrections and models_left > 1:
                        corrections += 1
                        rejection = ("Diagnosis not accepted by evidence validation: " + "; ".join(result.errors) +
                                     ". Fix it using only captured evidence: cite ok evidence whose service, query or values "
                                     "name the component, collect that evidence first if needed, or call inconclusive.")
                        log(IncidentStatus.INVESTIGATING, "Diagnosis returned for one bounded correction",
                            errors=result.errors, rejected=decision.diagnosis.model_dump(mode="json"))
                        continue
                    rejected_diag_errors = result.errors
                    status, terminal_reason = IncidentStatus.INCONCLUSIVE, "diagnosis_rejected"
                    limitations.append("Model diagnosis rejected by validation: " + "; ".join(result.errors))
                    log(IncidentStatus.INCONCLUSIVE, "Diagnosis rejected", rejected=decision.diagnosis.model_dump(mode="json"))
                break
            if isinstance(decision, InconclusiveDecision):
                status, terminal_reason = IncidentStatus.INCONCLUSIVE, "model_inconclusive"
                limitations.append(f"Model stopped without a diagnosis: {decision.reason}")
                log(IncidentStatus.INCONCLUSIVE, f"Inconclusive: {decision.reason}", relevant=decision.relevant_evidence_ids)
                break
            if isinstance(decision, MalformedDecision):
                repairs += 1
                log(IncidentStatus.INVESTIGATING, f"Unusable model output: {decision.detail}")
                if repairs > config.max_repairs:
                    status, terminal_reason = IncidentStatus.INCONCLUSIVE, "malformed_model_output"
                    limitations.append(f"Model output unusable after repair attempt: {decision.detail}")
                    break
                continue

            assert isinstance(decision, CallToolDecision)
            log(IncidentStatus.INVESTIGATING, f"Model requested {len(decision.calls)} tool call(s): {decision.rationale}"[:600])
            for call in decision.calls:
                pending.append(_dispatch(call, incident, config, deps, deadline, stats, limitations, log))
    except KeyboardInterrupt:
        status, terminal_reason = IncidentStatus.ESCALATED, "cancelled"
        limitations.append("Investigation cancelled by operator; collected evidence preserved.")

    elapsed = deps.clock() - t0
    reason_text = {"deadline_exceeded": "Investigation deadline exceeded.",
                   "model_call_budget_exhausted": "Model-call budget exhausted without a diagnosis.",
                   "token_budget_exhausted": "Token budget exhausted without a diagnosis."}.get(terminal_reason)
    if reason_text:
        limitations.append(reason_text)
    if status != IncidentStatus.DIAGNOSED:
        limitations.append("No diagnosis accepted: escalate to a human operator.")
    limitations.append("Confidence labels are model-reported and not calibrated; no evaluation was run.")
    limitations.append("Telemetry tools are live-only; queried windows are recorded per evidence item.")
    evidence = deps.registry.all()
    outside = [e.evidence_id for e in evidence if e.event_time_window.end_time < incident.observation_window.start_time
               or e.event_time_window.start_time > incident.observation_window.end_time]
    if outside:
        limitations.append(f"{len(outside)} evidence item(s) were queried outside the incident observation window: {', '.join(outside)}")
    stats.update(elapsed_s=round(elapsed, 2), terminal_reason=terminal_reason, evidence_count=len(evidence),
                 rejected_diagnosis_errors=rejected_diag_errors)
    log(status, f"Finished: {terminal_reason}; {stats['model_calls']} model calls, {stats['tool_calls']} tool calls, {elapsed:.1f}s", **stats)
    return IncidentReport(
        incident_id=incident.id, status=status, incident=incident, timeline=timeline, evidence=evidence,
        diagnosis=diagnosis, proposals=[], approval=None, verification=None, limitations=limitations,
    )


def _cap_confidence(diagnosis, registry):
    """High confidence needs supporting evidence from at least two independent sources (rule from Harshita's brain)."""
    sources = {registry.get(i).source for i in diagnosis.supporting_evidence_ids if registry.get(i)}
    if diagnosis.confidence_label == ConfidenceLabel.HIGH and len(sources) < 2:
        return diagnosis.model_copy(update={
            "confidence_label": ConfidenceLabel.MEDIUM,
            "uncertainty": f"{diagnosis.uncertainty} [Capped to medium: high confidence needs 2+ independent sources.]".strip()})
    return diagnosis


def _specs(deps: Dependencies) -> list[dict]:
    from integration import all_tool_specs  # only registered, read-only tools

    allowed = set(deps.policy.arg_models)
    return [s for s in all_tool_specs() if s["function"]["name"] in allowed]


FOLLOW_UP_TOOLS = {"service_health", "query_metrics", "search_traces", "query_logs"}


def anomaly_follow_up(registry: EvidenceRegistry, diagnosis) -> tuple[list[str], list[str]]:
    """Return (unresolved, uncited) services flagged by resource_overview.

    Unresolved: no ok follow-up evidence for that service collected after the overview. Merely naming the service in the
    diagnosis does not resolve it. Uncited: follow-up exists but the diagnosis cites none of it.
    """
    items = registry.all()
    unresolved, uncited = [], []
    cited = set(diagnosis.supporting_evidence_ids) | set(diagnosis.conflicting_evidence_ids)
    for i, ev in enumerate(items):
        if ev.tool != "resource_overview" or ev.status.value != "ok":
            continue
        for flag in (ev.observed_value.get("value") or {}).get("flagged", []):
            svc = flag["container"]
            # A silent container is expected to return empty follow-up data; that emptiness is the confirmation.
            accepted = {"ok", "empty"} if flag.get("kind") == "silent" else {"ok"}
            follow = [e for e in items[i + 1:] if e.service == svc and e.tool in FOLLOW_UP_TOOLS and e.status.value in accepted]
            if not follow:
                if svc not in unresolved:
                    unresolved.append(svc)
            elif not cited & {e.evidence_id for e in follow} and svc not in uncited:
                uncited.append(svc)
    return unresolved, uncited


def _dispatch(call, incident, config, deps, deadline, stats, limitations, log) -> ToolOutcome:
    def refuse(status: str, why: str) -> ToolOutcome:
        stats["tool_requests_rejected"] = stats.get("tool_requests_rejected", 0) + 1
        return ToolOutcome(call_id=call.call_id, tool_name=call.tool_name, arguments=call.arguments,
                           status=status, text=f"Tool call not executed: {why}", is_error=True)

    tools_left = config.max_tool_calls - stats["tool_calls"]
    if tools_left <= 0:
        return refuse("skipped", "tool-call budget exhausted")
    remaining = deadline - deps.clock()
    if remaining < config.tool_worst_case_s:
        return refuse("skipped", "not enough time left for another telemetry call")
    window = call.arguments.get("window_minutes")
    verdict = deps.validate_tool_request(
        ToolRequest(incident_id=incident.id, tool_name=call.tool_name, arguments=call.arguments,
                    window_minutes=window if isinstance(window, int) else None, remaining_tool_calls=tools_left),
        deps.policy)
    if not verdict.allowed:
        log(IncidentStatus.INVESTIGATING, f"Blocked {call.tool_name}: {'; '.join(verdict.reasons)}")
        return refuse("rejected", "; ".join(verdict.reasons))
    stats["tool_calls"] += 1  # dispatched calls count even if they fail or time out
    try:
        result = run_with_deadline(deps.run_tool, verdict.tool_name, verdict.arguments, timeout=remaining)
    except FutureTimeout:
        limitations.append(f"{call.tool_name} did not return before the deadline; any late result is not recorded.")
        return ToolOutcome(call_id=call.call_id, tool_name=call.tool_name, arguments=verdict.arguments,
                           status="error", text="Tool call timed out; no evidence recorded.", is_error=True)
    except Exception as exc:  # telemetry input errors and unexpected failures stay visible
        return ToolOutcome(call_id=call.call_id, tool_name=call.tool_name, arguments=verdict.arguments,
                           status="error", text=f"Tool failed: {type(exc).__name__}: {str(exc)[:300]}", is_error=True)
    try:
        ev = to_evidence(result)
        warnings = deps.registry.register(ev)
    except EvidenceConflict as exc:
        limitations.append(f"Evidence rejected: {exc}")
        return ToolOutcome(call_id=call.call_id, tool_name=call.tool_name, arguments=verdict.arguments,
                           status="error", text=f"Evidence rejected: {exc}", is_error=True)
    limitations.extend(warnings)
    log(IncidentStatus.INVESTIGATING, f"{call.tool_name}({verdict.arguments}) -> {ev.status.value} [{ev.evidence_id}]",
        window_start=ev.event_time_window.start_time.isoformat(), window_end=ev.event_time_window.end_time.isoformat())
    return ToolOutcome(call_id=call.call_id, tool_name=call.tool_name, arguments=verdict.arguments,
                       status=ev.status.value, evidence_id=ev.evidence_id, text=_outcome_text(result),
                       is_error=ev.status.value in ("error", "unavailable"))
