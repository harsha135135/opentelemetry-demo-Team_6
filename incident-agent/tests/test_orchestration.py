"""Mocked unit tests for the orchestrator, adapters, guardrail stand-ins, reasoner and report export.
Nothing here touches real telemetry or the Anthropic API."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import telemetry
from contracts import (
    CallToolDecision, FinishDecision, InconclusiveDecision, MalformedDecision, ModelError, ToolCall, ToolPolicy,
)
from integration import EvidenceConflict, EvidenceRegistry, to_evidence
from models import ConfidenceLabel, Diagnosis, Incident, IncidentStatus, TimeWindow
from orchestrator import Dependencies, IncidentInputError, InvestigationConfig, investigate, validate_incident
from reasoning import AnthropicReasoner, anthropic_tools
from report_io import write_report

NOW = datetime.now(timezone.utc)
POLICY = ToolPolicy(approved_services=["checkout", "cart"], arg_models={n: s[1] for n, s in telemetry.TOOLS.items()})


def incident(**kw):
    base = dict(id="INC-1", source="t", symptom="errors", affected_service="checkout", detection_time=NOW,
                observation_window=TimeWindow(start_time=NOW - timedelta(minutes=15), end_time=NOW), recovery_condition="x")
    base.update(kw)
    return Incident(**base)


class Runner:
    def __init__(self, tmp_path, status="ok", fail=None):
        self.tmp, self.status, self.fail, self.calls, self.n = tmp_path, status, fail, [], 0

    def __call__(self, name, args):
        self.calls.append((name, args))
        if self.fail:
            raise self.fail
        self.n += 1
        eid = f"ev-{name}-{self.n:04d}"
        path = self.tmp / f"{eid}.json"
        path.write_text("{}")
        return telemetry.ToolResult(
            status=self.status, evidence_id=eid, tool=name, source="prometheus", service=(args or {}).get("service"),
            query="q", window_start=NOW - timedelta(minutes=5), window_end=NOW, collected_at=NOW,
            value={"checkout": 0.4}, trace_ids=["t1", "t2"], capture_ref=str(path))


class Script:
    def __init__(self, *decisions):
        self.decisions, self.contexts = list(decisions), []

    def next_step(self, ctx):
        self.contexts.append(ctx)
        d = self.decisions.pop(0) if len(self.decisions) > 1 else self.decisions[0]
        if isinstance(d, BaseException):
            raise d
        return d


def call(i, tool="query_metrics", **args):
    args = args or {"service": "checkout"}
    return ToolCall(call_id=f"toolu_{i}", tool_name=tool, arguments=args)


def diag(ids, component="checkout"):
    return Diagnosis(suspected_component=component, suspected_cause="c", supporting_evidence_ids=ids,
                     uncertainty="u", confidence_label=ConfidenceLabel.LOW, next_step="n")


def deps(tmp_path, script, runner=None, **kw):
    return Dependencies(reasoner=script, run_tool=runner or Runner(tmp_path), policy=POLICY, registry=EvidenceRegistry(), **kw)


def run(tmp_path, *decisions, cfg=None, runner=None, **kw):
    script = Script(*decisions)
    d = deps(tmp_path, script, runner, **kw)
    return investigate(incident(), cfg or InvestigationConfig(), d), script, d


# ---- incident validation
@pytest.mark.parametrize("kw", [
    {"id": "../x"}, {"symptom": "  "}, {"detection_time": datetime(2026, 1, 1)},
    {"observation_window": TimeWindow(start_time=NOW, end_time=NOW - timedelta(minutes=1))},
    {"observation_window": TimeWindow(start_time=datetime(2026, 1, 1), end_time=NOW)},
])
def test_invalid_incident_rejected(kw):
    with pytest.raises(IncidentInputError):
        validate_incident(incident(**kw))


def test_unknown_affected_service_rejected(tmp_path):
    with pytest.raises(IncidentInputError):
        investigate(incident(affected_service="nope"), InvestigationConfig(), deps(tmp_path, Script()))


# ---- evidence adapter
def test_to_evidence_preserves_fields_and_does_not_invent_service(tmp_path):
    r = Runner(tmp_path)("overview", {})
    r.source = "derived"
    ev = to_evidence(r)
    assert ev.evidence_id == r.evidence_id and ev.service is None and ev.source == "derived"
    assert ev.trace_ids == ["t1", "t2"] and ev.trace_or_log_id is None
    assert ev.local_capture_reference == r.capture_ref and ev.observed_value["value"] == {"checkout": 0.4}


def test_registry_rejects_duplicates_and_flags_missing_capture(tmp_path):
    ev = to_evidence(Runner(tmp_path)("overview", {}))
    reg = EvidenceRegistry()
    assert reg.register(ev) == []
    with pytest.raises(EvidenceConflict):
        reg.register(ev)
    gone = ev.model_copy(update={"evidence_id": "ev-x", "local_capture_reference": str(tmp_path / "nope.json")})
    assert "missing" in reg.register(gone)[0]


# ---- orchestration
def test_valid_diagnosis_is_diagnosed_with_empty_proposals(tmp_path):
    rep, script, _ = run(tmp_path, CallToolDecision(calls=[call(1)]), FinishDecision(diagnosis=diag(["ev-query_metrics-0001"])))
    assert rep.status == IncidentStatus.DIAGNOSED and rep.diagnosis
    assert rep.proposals == [] and rep.approval is None and rep.verification is None
    assert [o.call_id for o in script.contexts[1].pending_outcomes] == ["toolu_1"]
    assert rep.timeline[-1].metadata["model_calls"] == 2 and rep.timeline[-1].metadata["tool_calls"] == 1


def test_invented_evidence_id_cannot_succeed(tmp_path):
    rep, *_ = run(tmp_path, FinishDecision(diagnosis=diag(["ev-made-up"])))
    assert rep.status == IncidentStatus.INCONCLUSIVE and rep.diagnosis is None
    assert any("not captured" in l for l in rep.limitations)


def test_unsupported_component_rejected(tmp_path):
    rep, *_ = run(tmp_path, CallToolDecision(calls=[call(1)]), FinishDecision(diagnosis=diag(["ev-query_metrics-0001"], "payment")))
    assert rep.status == IncidentStatus.INCONCLUSIVE


def test_empty_evidence_cannot_support_diagnosis(tmp_path):
    rep, *_ = run(tmp_path, CallToolDecision(calls=[call(1)]), FinishDecision(diagnosis=diag(["ev-query_metrics-0001"])),
                  runner=Runner(tmp_path, status="empty"))
    assert rep.status == IncidentStatus.INCONCLUSIVE and rep.evidence[0].status.value == "empty"


def test_missing_capture_rejects_diagnosis(tmp_path):
    runner = Runner(tmp_path)
    inner = runner.__call__

    def runner2(name, args):
        res = inner(name, args)
        (tmp_path / f"{res.evidence_id}.json").unlink()
        return res
    rep, *_ = run(tmp_path, CallToolDecision(calls=[call(1)]), FinishDecision(diagnosis=diag(["ev-query_metrics-0001"])), runner=runner2)
    assert rep.status == IncidentStatus.INCONCLUSIVE and any("missing on disk" in l for l in rep.limitations)


@pytest.mark.parametrize("c", [call(1, tool="bash", cmd="ls"), call(1, service="checkout", promql="up"),
                               call(1, service="flagd"), call(1, service="unknown-svc")])
def test_blocked_before_execution(tmp_path, c):
    runner = Runner(tmp_path)
    rep, script, _ = run(tmp_path, CallToolDecision(calls=[c]), InconclusiveDecision(reason="r"), runner=runner)
    assert runner.calls == [] and script.contexts[1].pending_outcomes[0].status == "rejected"
    assert rep.timeline[-1].metadata["tool_calls"] == 0


def test_multiple_calls_respect_tool_budget(tmp_path):
    runner = Runner(tmp_path)
    rep, script, _ = run(tmp_path, CallToolDecision(calls=[call(1), call(2), call(3)]), InconclusiveDecision(reason="r"),
                         cfg=InvestigationConfig(max_tool_calls=2), runner=runner)
    outs = script.contexts[1].pending_outcomes
    assert len(runner.calls) == 2 and [o.status for o in outs] == ["ok", "ok", "skipped"]
    assert [o.call_id for o in outs] == ["toolu_1", "toolu_2", "toolu_3"]


def test_model_budget_exhaustion_is_inconclusive(tmp_path):
    rep, script, _ = run(tmp_path, CallToolDecision(calls=[call(1)]), cfg=InvestigationConfig(max_model_calls=3))
    assert rep.status == IncidentStatus.INCONCLUSIVE and len(script.contexts) == 3
    assert script.contexts[-1].allow_tools is False  # last request offers terminal tools only


def test_malformed_output_repair_counts_toward_budget(tmp_path):
    rep, script, _ = run(tmp_path, MalformedDecision(detail="bad"), cfg=InvestigationConfig(max_model_calls=6))
    assert rep.status == IncidentStatus.INCONCLUSIVE and len(script.contexts) == 2
    assert rep.timeline[-1].metadata["model_calls"] == 2


def test_failed_empty_unavailable_stay_distinguishable(tmp_path):
    for status in ("empty", "unavailable", "error"):
        rep, script, _ = run(tmp_path, CallToolDecision(calls=[call(1)]), InconclusiveDecision(reason="r"), runner=Runner(tmp_path, status=status))
        assert script.contexts[1].pending_outcomes[0].status == status and rep.evidence[0].status.value == status
    rep, script, _ = run(tmp_path, CallToolDecision(calls=[call(1)]), InconclusiveDecision(reason="r"),
                         runner=Runner(tmp_path, fail=telemetry.TelemetryInputError("bad")))
    o = script.contexts[1].pending_outcomes[0]
    assert o.status == "error" and o.is_error and rep.timeline[-1].metadata["tool_calls"] == 1 and rep.evidence == []


def test_model_error_escalates_and_keeps_evidence(tmp_path):
    rep, *_ = run(tmp_path, CallToolDecision(calls=[call(1)]), ModelError("timeout"))
    assert rep.status == IncidentStatus.ESCALATED and len(rep.evidence) == 1


def test_deadline_exceeded(tmp_path):
    t = iter(range(0, 10000, 100))
    rep, *_ = run(tmp_path, CallToolDecision(calls=[call(1)]), clock=lambda: float(next(t)), cfg=InvestigationConfig(deadline_s=150))
    assert rep.status == IncidentStatus.ESCALATED and rep.timeline[-1].metadata["terminal_reason"] == "deadline_exceeded"


def test_cancellation_preserves_evidence(tmp_path):
    rep, *_ = run(tmp_path, CallToolDecision(calls=[call(1)]), KeyboardInterrupt())
    assert rep.status == IncidentStatus.ESCALATED and len(rep.evidence) == 1
    assert rep.timeline[-1].metadata["terminal_reason"] == "cancelled"


def test_contradictory_evidence_inconclusive(tmp_path):
    rep, *_ = run(tmp_path, CallToolDecision(calls=[call(1), call(2)]), InconclusiveDecision(reason="conflicting", relevant_evidence_ids=["ev-query_metrics-0001"]))
    assert rep.status == IncidentStatus.INCONCLUSIVE and "conflicting" in " ".join(rep.limitations)


# ---- reasoner (fake Anthropic client)
def block(type_, **kw):
    return SimpleNamespace(type=type_, **kw)


class FakeClient:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.requests.append(json.loads(json.dumps({k: v for k, v in kw.items() if k != "messages"}, default=str)))
        self.last_messages = list(kw["messages"])
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def resp(*blocks, stop="tool_use"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop, usage=SimpleNamespace(input_tokens=10, output_tokens=5))


def rctx(tmp_path, pending=(), allow=True):
    from contracts import ReasoningContext
    return ReasoningContext(incident=incident(), evidence=[], pending_outcomes=list(pending), tool_specs=telemetry.tool_specs(),
                            allow_tools=allow, remaining_tool_calls=5, remaining_model_calls=5, remaining_seconds=100, request_timeout_s=30)


def test_tool_translation_has_anthropic_shape_and_terminals():
    tools = anthropic_tools(telemetry.tool_specs(), True)
    assert {"overview", "query_metrics", "search_traces", "query_logs", "service_health", "finish", "inconclusive"} == {t["name"] for t in tools}
    assert all(set(t) == {"name", "description", "input_schema"} for t in tools)
    assert {t["name"] for t in anthropic_tools(telemetry.tool_specs(), False)} == {"finish", "inconclusive"}


def test_tool_use_ids_preserved_and_paired(tmp_path):
    from contracts import ToolOutcome
    client = FakeClient(resp(block("text", text="check"), block("tool_use", id="toolu_A", name="overview", input={}),
                             block("tool_use", id="toolu_B", name="query_metrics", input={"service": "cart"})),
                        resp(block("tool_use", id="toolu_C", name="inconclusive", input={"reason": "r"})))
    r = AnthropicReasoner("m", client=client)
    d = r.next_step(rctx(tmp_path))
    assert [c.call_id for c in d.calls] == ["toolu_A", "toolu_B"] and d.input_tokens == 10
    outs = [ToolOutcome(call_id=i, tool_name="x", status="ok", text="t") for i in ("toolu_A", "toolu_B")]
    r.next_step(rctx(tmp_path, outs))
    user = client.last_messages[-1]["content"]
    assert [b["tool_use_id"] for b in user if b["type"] == "tool_result"] == ["toolu_A", "toolu_B"]
    assert user[0]["type"] == "tool_result" and "UNTRUSTED" in user[0]["content"]
    assert client.last_messages[-2]["role"] == "assistant"


def test_mismatched_result_ids_rejected(tmp_path):
    from contracts import ToolOutcome
    client = FakeClient(resp(block("tool_use", id="toolu_A", name="overview", input={})))
    r = AnthropicReasoner("m", client=client)
    r.next_step(rctx(tmp_path))
    with pytest.raises(ModelError):
        r.next_step(rctx(tmp_path, [ToolOutcome(call_id="toolu_Z", tool_name="x", status="ok", text="t")]))


def test_malformed_finish_gets_error_tool_results(tmp_path):
    client = FakeClient(resp(block("tool_use", id="toolu_A", name="finish", input={"suspected_component": "x"})),
                        resp(block("tool_use", id="toolu_B", name="inconclusive", input={"reason": "r"})))
    r = AnthropicReasoner("m", client=client)
    assert isinstance(r.next_step(rctx(tmp_path)), MalformedDecision)
    assert isinstance(r.next_step(rctx(tmp_path)), InconclusiveDecision)
    first = client.last_messages[-1]["content"][0]
    assert first["tool_use_id"] == "toolu_A" and first["is_error"] is True


def test_text_only_and_truncated_responses_are_malformed(tmp_path):
    client = FakeClient(resp(block("text", text="hi"), stop="end_turn"))
    assert isinstance(AnthropicReasoner("m", client=client).next_step(rctx(tmp_path)), MalformedDecision)
    client = FakeClient(resp(block("tool_use", id="t", name="overview", input={}), stop="max_tokens"))
    assert isinstance(AnthropicReasoner("m", client=client).next_step(rctx(tmp_path)), MalformedDecision)


def test_api_timeout_becomes_model_error_and_last_turn_forces_terminal(tmp_path):
    import anthropic
    client = FakeClient(anthropic.APITimeoutError(request=None))
    with pytest.raises(ModelError):
        AnthropicReasoner("m", client=client).next_step(rctx(tmp_path, allow=False))
    assert client.requests[0]["tool_choice"] == {"type": "any"} and len(client.requests[0]["tools"]) == 2


def test_missing_model_fails_clearly():
    with pytest.raises(ModelError):
        AnthropicReasoner("")


# ---- report export on terminal paths
def test_report_written_for_every_status_and_citation_errors_withheld(tmp_path):
    rep, *_ = run(tmp_path, CallToolDecision(calls=[call(1)]), FinishDecision(diagnosis=diag(["ev-query_metrics-0001"])))
    out, art = write_report(rep, tmp_path / "out")
    assert art.json_path and art.markdown_path and art.official_schema_status == "unavailable"
    bad = rep.model_copy(update={"diagnosis": diag(["ev-ghost"])})
    out, art = write_report(bad, tmp_path / "out2")
    assert out.status == IncidentStatus.INCONCLUSIVE and out.diagnosis is None and art.internal_validation_errors
    blocker = tmp_path / "file"
    blocker.write_text("x")
    _, art = write_report(rep, blocker / "sub")
    assert art.export_errors and art.json_path is None
    for decisions in ([ModelError("t")], [InconclusiveDecision(reason="r")], [KeyboardInterrupt()]):
        r, *_ = run(tmp_path, *decisions)
        assert write_report(r, tmp_path / "o3")[1].json_path


def test_high_confidence_capped_with_single_source(tmp_path):
    d = diag(["ev-query_metrics-0001"]).model_copy(update={"confidence_label": ConfidenceLabel.HIGH})
    rep, *_ = run(tmp_path, CallToolDecision(calls=[call(1)]), FinishDecision(diagnosis=d))
    assert rep.status == IncidentStatus.DIAGNOSED and rep.diagnosis.confidence_label == ConfidenceLabel.MEDIUM
    assert "Capped" in rep.diagnosis.uncertainty
