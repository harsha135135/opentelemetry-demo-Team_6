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
    max_diagnosis_corrections: int = 1  # Harshita's citation gate: one chance to fix a rejected diagnosis


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
    stale_checks = 0
    ignored_checks = 0
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
                    # a follow-up needs one request for the tools and one more to finish: never defer the last chance
                    can_follow_up = tools_left > 0 and models_left > 2 and remaining_s > 2 * config.tool_worst_case_s
                    stale = stale_component(deps.registry, decision.diagnosis)
                    if stale and stale_checks < 1 and models_left > 1:
                        stale_checks += 1
                        rejection = ("Not accepted: " + stale + " Diagnose what changed NOW (or call inconclusive); "
                                     "if you still name this component, explain with current evidence why it is the new problem.")
                        log(IncidentStatus.INVESTIGATING, f"Finish returned: stale component ({stale})")
                        continue
                    if stale:
                        limitations.append(f"Diagnosed component may be an earlier or older incident: {stale}")
                    strong_uncited = strong_flags_ignored(deps.registry, decision.diagnosis, uncited)
                    if strong_uncited and ignored_checks < 1 and models_left > 1:
                        ignored_checks += 1
                        rejection = ("Not accepted yet: resource_overview strongly flagged " + "; ".join(strong_uncited) +
                                     ". You checked it, but the diagnosis neither names it nor cites its evidence. Decide "
                                     "explicitly: is it the cause (a container that suddenly uses far more CPU or memory "
                                     "slows its own requests and can slow other services on the same machine), an effect, "
                                     "or unrelated? Cite its evidence as supporting or conflicting, then finish again.")
                        log(IncidentStatus.INVESTIGATING, f"Finish returned: strong resource flag ignored ({strong_uncited})")
                        continue
                    if unresolved and nudges < config.max_follow_up_nudges and can_follow_up:
                        nudges += 1
                        rejection = ("Not accepted yet: resource_overview flagged " + ", ".join(unresolved) +
                                     " with no follow-up evidence. Investigate each (service_health / query_metrics / traces / logs), "
                                     "decide whether it is cause, effect or unrelated, cite that evidence, then finish again.")
                        log(IncidentStatus.INVESTIGATING, f"Finish deferred: unresolved resource anomalies {unresolved}")
                        continue
                    diagnosis = _cap_latency_only(_cap_confidence(decision.diagnosis, deps.registry), deps.registry)
                    if unresolved:
                        diagnosis = diagnosis.model_copy(update={
                            "confidence_label": ConfidenceLabel.LOW,
                            "uncertainty": f"{diagnosis.uncertainty} [Unresolved resource anomaly without follow-up evidence: {', '.join(unresolved)}.]"})
                        limitations.append(f"Resource anomalies flagged but not followed up with evidence: {', '.join(unresolved)}")
                    if uncited:
                        limitations.append(f"Follow-up evidence exists for flagged services but the diagnosis cites none of it: {', '.join(uncited)}")
                    status, terminal_reason = IncidentStatus.DIAGNOSED, "diagnosed"
                    log(IncidentStatus.DIAGNOSED, f"Diagnosis validated: {decision.diagnosis.suspected_component}")
                elif corrections < config.max_diagnosis_corrections and models_left > 1:
                    corrections += 1
                    ok_ids = [e.evidence_id for e in deps.registry.all() if e.status.value == "ok"]
                    rejection = ("Diagnosis not accepted: " + "; ".join(result.errors) +
                                 f". Cite only evidence_id values returned by tools; usable (status ok): {ok_ids or 'none yet'}. "
                                 "Fix the diagnosis and finish again, or call inconclusive.")
                    log(IncidentStatus.INVESTIGATING, "Diagnosis returned for correction: " + "; ".join(result.errors))
                    continue
                else:
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


_SAME_BACKEND = {"derived": "prometheus"}  # service_health is computed from Prometheus metrics


def _cap_confidence(diagnosis, registry):
    """High confidence needs supporting evidence from at least two independent sources (rule from Harshita's brain).
    Only 'ok' evidence counts (an empty trace search supports nothing), and derived health is Prometheus data."""
    sources = {_SAME_BACKEND.get(e.source, e.source) for e in map(registry.get, diagnosis.supporting_evidence_ids)
               if e is not None and e.status.value == "ok"}
    if diagnosis.confidence_label == ConfidenceLabel.HIGH and len(sources) < 2:
        return diagnosis.model_copy(update={
            "confidence_label": ConfidenceLabel.MEDIUM,
            "uncertainty": f"{diagnosis.uncertainty} [Capped to medium: high confidence needs 2+ independent sources.]".strip()})
    return diagnosis


def _cap_latency_only(diagnosis, registry):
    """No new errors, no new log messages and no resource flags anywhere: a slowdown alone (the demo's latency
    drifts 2-3x on its own) cannot carry more than low confidence. Needs the fleet-wide tools to have run."""
    seen, strong = set(), False
    for ev in registry.all():
        v = (ev.observed_value or {}).get("value") or {}
        if ev.status.value != "ok" or not isinstance(v, dict):
            continue
        if ev.tool == "overview":
            seen.add(ev.tool)
            changes = [x for k in ("changed_since_previous_window", "operations_changed") for x in v.get(k) or []]
            strong |= any("error_rate" in str(x) or "no traffic" in str(x) for x in changes)
        elif ev.tool == "log_overview":
            seen.add(ev.tool)
            now = v.get("logging_now") if isinstance(v.get("logging_now"), list) else []
            strong |= any(m.get("started_recently") for m in now)
        elif ev.tool == "resource_overview":
            seen.add(ev.tool)
            strong |= bool(v.get("flagged"))
    if seen >= {"overview", "log_overview", "resource_overview"} and not strong and \
            diagnosis.confidence_label != ConfidenceLabel.LOW:
        return diagnosis.model_copy(update={
            "confidence_label": ConfidenceLabel.LOW,
            "uncertainty": f"{diagnosis.uncertainty} [Capped to low: only latency changed; no new errors, new log "
                           "messages or resource changes anywhere, and this demo's latency varies on its own.]".strip()})
    return diagnosis


def _specs(deps: Dependencies) -> list[dict]:
    from integration import all_tool_specs  # only registered, read-only tools

    allowed = set(deps.policy.arg_models)
    return [s for s in all_tool_specs() if s["function"]["name"] in allowed]


def _lead_service(text: str) -> str:
    """'cart "POST /x": ...' or 'checkout: error_rate ...' -> the service name."""
    return re.split(r'[\s:"]', str(text).strip(), maxsplit=1)[0]


def stale_component(registry: EvidenceRegistry, diagnosis) -> str | None:
    """The diagnosed component is an earlier incident that is ending: the fleet-wide tools list it only as
    recovering (metrics) or as a log message that stopped, and nowhere as changing now. Returns why, or None."""
    active, ended = set(), set()
    old_logs: set[str] = set()                # services whose current log messages started long ago
    new_logs: dict[str, float] = {}           # service -> minutes since its newest message first appeared
    for ev in registry.all():
        v = (ev.observed_value or {}).get("value") or {}
        if ev.status.value != "ok" or not isinstance(v, dict):
            continue
        if ev.tool == "overview":
            for key, bucket in (("changed_since_previous_window", active), ("operations_changed", active),
                                ("recovering_since_previous_window", ended), ("operations_recovering", ended)):
                bucket.update(_lead_service(x) for x in v.get(key) or [] if isinstance(x, str))
        elif ev.tool == "resource_overview":  # a restart / crash loop is current even while callers look "recovering"
            active.update(f.get("container") for f in v.get("flagged") or [] if isinstance(f, dict))
        elif ev.tool == "log_overview":
            now = v.get("logging_now") if isinstance(v.get("logging_now"), list) else []
            active.update(m["service"] for m in now if m.get("started_recently"))
            ended.update(m["service"] for m in v.get("stopped_earlier") or [])
            old_logs.update(m["service"] for m in v.get("stopped_earlier") or [])  # its error messages stopped
            for m in now:
                if m.get("started_recently"):
                    first = m.get("first_seen_min_ago") or 0
                    new_logs[m["service"]] = min(new_logs.get(m["service"], first), first)
                else:
                    old_logs.add(m["service"])
    component = diagnosis.suspected_component.lower()

    def mentions(services) -> set[str]:
        return {s for s in services if s and re.search(rf"(?<![\w-]){re.escape(s.lower())}(?![\w-])", component)}

    named = mentions(ended | active)
    if named and named <= ended and not named & active:
        return (f"{', '.join(sorted(named))} is listed only as recovering / stopped (an earlier incident ending), never as "
                f"changing now. Changing now: {', '.join(sorted(active)) or 'nothing clear'}.")
    # An older, still-ongoing problem chosen over one that just started: the newest change is the incident.
    named_old = mentions(old_logs) - set(new_logs)
    newest = {s: m for s, m in new_logs.items() if m <= NEW_INCIDENT_MIN and s not in mentions(new_logs)}
    if named_old and newest:
        lead = ", ".join(f"{s} (first seen {m} min ago)" for s, m in sorted(newest.items(), key=lambda kv: kv[1]))
        return (f"{', '.join(sorted(named_old))}'s error messages are old: going on for a long time or already stopped "
                f"(an earlier problem, possibly with a few leftover slow or failed calls), while new errors started "
                f"only minutes ago in: {lead}. The alert is about the newest change: diagnose that and list the older "
                f"problem under alternatives.")
    return None


NEW_INCIDENT_MIN = 5  # a log message first seen this recently is the freshest lead


STRONG_FLAG_SEVERITY = 3  # sudden CPU/memory jump or crash loop (restart-only flags are 1-2)


def strong_flags_ignored(registry: EvidenceRegistry, diagnosis, uncited: list[str]) -> list[str]:
    """Strongly flagged containers that were followed up but that the diagnosis neither names nor cites."""
    component = diagnosis.suspected_component.lower()
    out = []
    for ev in registry.all():
        v = (ev.observed_value or {}).get("value") or {}
        if ev.tool != "resource_overview" or ev.status.value != "ok" or not isinstance(v, dict):
            continue
        for f in v.get("flagged") or []:
            svc = f.get("container")
            if (svc in uncited and (f.get("severity") or 0) >= STRONG_FLAG_SEVERITY
                    and not re.search(rf"(?<![\w-]){re.escape(svc.lower())}(?![\w-])", component)):
                out.append(f'{svc} ({"; ".join(f.get("reasons") or [])})')
    return out


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
            follow = [e for e in items[i + 1:] if e.service == svc and e.tool in FOLLOW_UP_TOOLS and e.status.value == "ok"]
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
