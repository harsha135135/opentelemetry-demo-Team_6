"""Mocked unit tests for the orchestrator, adapters, guardrail stand-ins, reasoner and report export.
Nothing here touches real telemetry or any LLM API."""
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
from reasoning import LLMReasoner, llm_tools
from report_io import write_report

NOW = datetime.now(timezone.utc)
POLICY = ToolPolicy(approved_services=["checkout", "cart", "ad", "payment"], arg_models={n: s[1] for n, s in telemetry.TOOLS.items()})


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


def test_rejected_diagnosis_gets_one_correction_with_valid_ids(tmp_path):
    rep, script, _ = run(tmp_path, CallToolDecision(calls=[call(1)]), FinishDecision(diagnosis=diag(["ev-made-up"])),
                         FinishDecision(diagnosis=diag(["ev-query_metrics-0001"])))
    assert "ev-query_metrics-0001" in script.contexts[2].rejection and "not captured" in script.contexts[2].rejection
    assert rep.status == IncidentStatus.DIAGNOSED and rep.diagnosis.supporting_evidence_ids == ["ev-query_metrics-0001"]


def test_diagnosis_mentioning_feature_flags_is_rejected(tmp_path):
    d = diag(["ev-query_metrics-0001"]).model_copy(update={"suspected_cause": "The Product Catalog Fail feature flag is enabled"})
    rep, *_ = run(tmp_path, CallToolDecision(calls=[call(1)]), FinishDecision(diagnosis=d))
    assert rep.status == IncidentStatus.INCONCLUSIVE and any("feature flags" in l for l in rep.limitations)


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


# ---- reasoner (fake LangChain chat model)
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage


class FakeChat:
    """Stands in for a LangChain chat model: records bound tools and the messages of every request."""

    def __init__(self, *responses):
        self.responses, self.bound, self.requests = list(responses), [], []

    def bind_tools(self, tools, **kw):
        self.bound.append(tools)
        return self

    def invoke(self, messages, **kw):
        self.requests.append(list(messages))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def ai(*calls, text="", finish="tool_calls"):
    return AIMessage(content=text, tool_calls=[{"name": n, "args": a, "id": i} for i, n, a in calls],
                     usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                     response_metadata={"finish_reason": finish})


def rctx(tmp_path, pending=(), allow=True):
    from contracts import ReasoningContext
    return ReasoningContext(incident=incident(), evidence=[], pending_outcomes=list(pending), tool_specs=telemetry.tool_specs(),
                            allow_tools=allow, remaining_tool_calls=5, remaining_model_calls=5, remaining_seconds=100, request_timeout_s=30)


def names(tools):
    return {t["function"]["name"] for t in tools}


def test_tool_translation_is_openai_format_with_terminals_and_no_refs():
    tools = llm_tools(telemetry.tool_specs(), True)
    assert {"overview", "log_overview", "query_metrics", "search_traces", "query_logs", "service_health", "finish", "inconclusive"} == names(tools)
    assert all(t["type"] == "function" and set(t["function"]) == {"name", "description", "parameters"} for t in tools)
    assert "$ref" not in json.dumps(tools) and "$defs" not in json.dumps(tools)  # Gemini rejects schema references
    assert names(llm_tools(telemetry.tool_specs(), False)) == {"finish", "inconclusive"}


def test_tool_call_ids_preserved_and_paired(tmp_path):
    from contracts import ToolOutcome
    chat = FakeChat(ai(("call_A", "overview", {}), ("call_B", "query_metrics", {"service": "cart"}), text="check"),
                    ai(("call_C", "inconclusive", {"reason": "r"})))
    r = LLMReasoner("m", chat_model=chat)
    d = r.next_step(rctx(tmp_path))
    assert [c.call_id for c in d.calls] == ["call_A", "call_B"] and d.input_tokens == 10 and d.rationale == "check"
    outs = [ToolOutcome(call_id=i, tool_name="x", status="ok", text="t") for i in ("call_A", "call_B")]
    r.next_step(rctx(tmp_path, outs))
    msgs = chat.requests[-1]
    assert isinstance(msgs[0], SystemMessage) and isinstance(msgs[2], AIMessage)
    results = [m for m in msgs if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in results] == ["call_A", "call_B"] and "UNTRUSTED" in results[0].content
    assert isinstance(msgs[-1], HumanMessage)  # budget status follows the tool results


def test_missing_tool_call_ids_are_generated_and_reused(tmp_path):
    from contracts import ToolOutcome
    chat = FakeChat(AIMessage(content="", tool_calls=[{"name": "overview", "args": {}, "id": None}]),
                    ai(("c", "inconclusive", {"reason": "r"})))
    r = LLMReasoner("m", chat_model=chat)
    cid = r.next_step(rctx(tmp_path)).calls[0].call_id
    assert cid.startswith("call_")
    r.next_step(rctx(tmp_path, [ToolOutcome(call_id=cid, tool_name="overview", status="ok", text="t")]))
    assert [m.tool_call_id for m in chat.requests[-1] if isinstance(m, ToolMessage)] == [cid]


def test_mismatched_result_ids_rejected(tmp_path):
    from contracts import ToolOutcome
    r = LLMReasoner("m", chat_model=FakeChat(ai(("call_A", "overview", {}))))
    r.next_step(rctx(tmp_path))
    with pytest.raises(ModelError):
        r.next_step(rctx(tmp_path, [ToolOutcome(call_id="call_Z", tool_name="x", status="ok", text="t")]))


def test_malformed_finish_gets_error_tool_results(tmp_path):
    chat = FakeChat(ai(("call_A", "finish", {"suspected_component": "x"})), ai(("call_B", "inconclusive", {"reason": "r"})))
    r = LLMReasoner("m", chat_model=chat)
    assert isinstance(r.next_step(rctx(tmp_path)), MalformedDecision)
    assert isinstance(r.next_step(rctx(tmp_path)), InconclusiveDecision)
    err = [m for m in chat.requests[-1] if isinstance(m, ToolMessage)][0]
    assert err.tool_call_id == "call_A" and err.status == "error"


def test_text_only_and_truncated_responses_are_malformed(tmp_path):
    chat = FakeChat(AIMessage(content="hi", response_metadata={"finish_reason": "stop"}))
    assert isinstance(LLMReasoner("m", chat_model=chat).next_step(rctx(tmp_path)), MalformedDecision)
    chat = FakeChat(ai(("t", "overview", {}), finish="MAX_TOKENS"))
    assert isinstance(LLMReasoner("m", chat_model=chat).next_step(rctx(tmp_path)), MalformedDecision)


def test_provider_error_becomes_model_error_and_last_turn_offers_terminals_only(tmp_path):
    chat = FakeChat(TimeoutError("provider timed out"), TimeoutError("again"))
    with pytest.raises(ModelError):
        LLMReasoner("m", chat_model=chat, retry_wait_s=0.01).next_step(rctx(tmp_path, allow=False))
    assert names(chat.bound[0]) == {"finish", "inconclusive"}


def test_transient_provider_error_retried_once_then_model_error(tmp_path):
    overloaded = RuntimeError("503 UNAVAILABLE: This model is currently experiencing high demand")
    chat = FakeChat(overloaded, ai(("call_A", "inconclusive", {"reason": "r"})))
    d = LLMReasoner("m", chat_model=chat, retry_wait_s=0.01).next_step(rctx(tmp_path))
    assert isinstance(d, InconclusiveDecision) and len(chat.requests) == 2
    chat = FakeChat(overloaded, overloaded)
    with pytest.raises(ModelError):
        LLMReasoner("m", chat_model=chat, retry_wait_s=0.01).next_step(rctx(tmp_path))
    chat = FakeChat(RuntimeError("400 INVALID_ARGUMENT: bad schema"))  # not transient: no retry
    with pytest.raises(ModelError):
        LLMReasoner("m", chat_model=chat, retry_wait_s=0.01).next_step(rctx(tmp_path))
    assert len(chat.requests) == 1


def test_overloaded_model_switches_to_fallback(tmp_path):
    overloaded = RuntimeError("503 UNAVAILABLE: high demand")
    primary, backup = FakeChat(overloaded, overloaded), FakeChat(ai(("call_A", "inconclusive", {"reason": "r"})))
    r = LLMReasoner("primary", chat_model=primary, retry_wait_s=0.01, fallback_model="backup", fallback_chat=backup)
    assert isinstance(r.next_step(rctx(tmp_path)), InconclusiveDecision)
    assert r.model == "backup" and r.switched_from == "primary" and len(backup.requests) == 1


def test_chat_model_is_deterministic_and_never_retries():
    import reasoning
    seen = {}

    def fake_init(model, model_provider, **kw):
        seen.update(kw, model=model, provider=model_provider)
        return FakeChat()
    import langchain.chat_models as lcm
    orig = lcm.init_chat_model
    lcm.init_chat_model = fake_init
    try:
        reasoning.make_chat_model("google_genai", "some-flash-model", 1500, 60)
    finally:
        lcm.init_chat_model = orig
    assert seen["temperature"] == 0 and seen["max_retries"] == 0 and seen["provider"] == "google_genai"


def test_missing_model_fails_clearly():
    with pytest.raises(ModelError):
        LLMReasoner("")


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


def test_two_source_rule_ignores_empty_evidence_and_derived_health(tmp_path):
    reg = EvidenceRegistry()
    prom = Runner(tmp_path)("query_metrics", {"service": "checkout"})
    health = Runner(tmp_path)("service_health", {"service": "checkout"})
    health.evidence_id, health.source = "ev-health-x", "derived"
    traces = Runner(tmp_path, status="empty")("search_traces", {"service": "checkout"})
    traces.evidence_id, traces.source = "ev-traces-x", "jaeger"
    for r in (prom, health, traces):
        reg.register(to_evidence(r))
    from orchestrator import _cap_confidence
    d = diag([prom.evidence_id, health.evidence_id, traces.evidence_id]).model_copy(update={"confidence_label": ConfidenceLabel.HIGH})
    assert _cap_confidence(d, reg).confidence_label == ConfidenceLabel.MEDIUM  # really one source: Prometheus


def test_component_phrase_matches_service_word_in_evidence(tmp_path):
    d = diag(["ev-query_metrics-0001"], "checkout container (cpu saturation, restart about 7 minutes ago)")
    rep, *_ = run(tmp_path, CallToolDecision(calls=[call(1)]), FinishDecision(diagnosis=d))
    assert rep.status == IncidentStatus.DIAGNOSED


def test_component_phrase_with_no_matching_word_still_rejected(tmp_path):
    d = diag(["ev-query_metrics-0001"], "payment gateway outage")
    rep, *_ = run(tmp_path, CallToolDecision(calls=[call(1)]), FinishDecision(diagnosis=d))
    assert rep.status == IncidentStatus.INCONCLUSIVE


def test_prompt_requires_resource_checks():
    from reasoning import SYSTEM_PROMPT
    assert all(w in SYSTEM_PROMPT for w in ("resource_overview", "service_health", "several tools at once",
                                            "feature flags", "inconclusive", "allowlist", "also active"))
    # generic: no demo service, fault or flag names (hidden test cases must not depend on them)
    for name in ("checkout", "payment", "cart", "product-catalog", "recommendation", "frontend", "adHighCpu",
                 "paymentFailure", "container_cpu", "valkey", "redis"):
        assert name not in SYSTEM_PROMPT, name


# ---- resource_overview, anomaly follow-up, eval harness
import resources
from orchestrator import anomaly_follow_up

FLEET_CPU = {"ad": 260.0, "cart": 1.5, "checkout": 8.0, "payment": 17.0, "load-generator": 77.0, "email": 3.0}
UP = {n: 1400.0 for n in FLEET_CPU}


def test_cpu_outlier_flagged_against_fleet_and_baseline_not_by_fixed_limit():
    v = resources.analyse(FLEET_CPU, {"ad": 80.0}, {}, {}, UP)
    assert [f["container"] for f in v["flagged"]] == ["ad"]
    assert any("fleet median" in r for r in v["flagged"][0]["reasons"]) and any("prior-window" in r for r in v["flagged"][0]["reasons"])
    assert "not proven causes" in v["note"]  # load-generator at 77% is not flagged, and nothing claims causation


def test_no_baseline_still_flags_on_fleet_comparison():
    v = resources.analyse(FLEET_CPU, {}, {}, {}, UP)
    assert v["flagged"][0]["container"] == "ad" and "prior-window" not in " ".join(v["flagged"][0]["reasons"])


def test_memory_growth_and_limit_flagged():
    v = resources.analyse({"a": 1.0, "b": 1.0}, {}, {"a": 90.0, "b": 40.0}, {"b": 0.3}, {"a": 1000.0, "b": 1000.0})
    assert {f["container"] for f in v["flagged"]} == {"a", "b"}


def test_steady_baseline_is_not_flagged_but_a_change_is():
    # demo baseline: product-catalog CPU-bound and near its memory limit in both windows -> not an anomaly
    cpu = {"product-catalog": 230.0, "cart": 2.0, "ad": 3.0, "email": 2.0}
    mem = {"product-catalog": 92.0, "cart": 40.0, "ad": 95.0, "email": 30.0}
    v = resources.analyse(cpu, {"product-catalog": 220.0, "cart": 2.0, "ad": 3.0, "email": 2.0}, mem, {},
                          {k: 1400.0 for k in cpu}, mem_base={"product-catalog": 91.0, "cart": 40.0, "ad": 70.0, "email": 30.0})
    assert [f["container"] for f in v["flagged"]] == ["ad"]  # ad's memory rose 70 -> 95; the steady ones stay quiet
    assert "up from 70%" in v["flagged"][0]["reasons"][0]


def test_observability_containers_never_flagged():
    cpu = {"opensearch": 400.0, "prometheus": 300.0, "cart": 2.0, "email": 2.0}
    v = resources.analyse(cpu, {}, {"opensearch": 98.0}, {}, {k: 1400.0 for k in cpu})
    assert v["flagged"] == [] and v["observability_containers_not_assessed"] == ["opensearch", "prometheus"]
    assert {r[0] for r in v["containers"]["rows"]} == {"cart", "email"}


def test_clustered_restarts_reported_and_flagged_not_dismissed():
    up = {"cart": 100.0, "product-catalog": 130.0, "a": 3000.0, "b": 3000.0, "c": 3000.0}
    v = resources.analyse({k: 1.0 for k in up}, {}, {}, {}, up)
    assert v["restart_clusters"][0]["containers"] == ["cart", "product-catalog"]
    assert {f["container"] for f in v["flagged"]} == {"cart", "product-catalog"}


def test_resource_overview_unavailable_is_distinct(monkeypatch, tmp_path):
    import httpx
    monkeypatch.setattr(telemetry, "EVIDENCE_DIR", tmp_path)
    monkeypatch.setattr(resources, "_instant", lambda c, q: (_ for _ in ()).throw(httpx.ConnectError("x")))
    r = resources.resource_overview(5)
    assert r.status.value == "unavailable" and (tmp_path / f"{r.evidence_id}.json").is_file()


def overview_runner(tmp_path, flagged=("ad",)):
    base = Runner(tmp_path)

    def run_tool(name, args):
        res = base(name, args)
        if name == "overview":
            res.tool = "resource_overview"
            res.service = None
            res.value = {"flagged": [{"container": s, "reasons": ["cpu high"], "severity": 3} for s in flagged]}
        return res
    return run_tool


def call_ov(i):
    return ToolCall(call_id=f"toolu_{i}", tool_name="overview", arguments={})


def test_naming_flagged_service_is_not_follow_up(tmp_path):
    rep, *_ = run(tmp_path, CallToolDecision(calls=[call_ov(1), call(2, service="checkout")]),
                  FinishDecision(diagnosis=diag(["ev-query_metrics-0002"], "checkout")), runner=overview_runner(tmp_path),
                  cfg=InvestigationConfig(max_follow_up_nudges=0))
    assert rep.diagnosis.confidence_label == ConfidenceLabel.LOW and "ad" in " ".join(rep.limitations)
    assert "Unresolved" in rep.diagnosis.uncertainty


def test_finish_deferred_then_follow_up_resolves(tmp_path):
    rep, script, _ = run(tmp_path, CallToolDecision(calls=[call_ov(1), call(2, service="checkout")]),
                         FinishDecision(diagnosis=diag(["ev-query_metrics-0002"], "checkout")),
                         CallToolDecision(calls=[call(3, tool="service_health", service="ad")]),
                         FinishDecision(diagnosis=diag(["ev-query_metrics-0002", "ev-service_health-0003"], "checkout")),
                         runner=overview_runner(tmp_path))
    assert script.contexts[2].rejection and "ad" in script.contexts[2].rejection
    assert rep.status == IncidentStatus.DIAGNOSED and "Unresolved" not in rep.diagnosis.uncertainty


def test_follow_up_gate_never_spends_the_last_model_call(tmp_path):
    # 3 model calls: tools, finish (would be deferred), ... -> with only 2 left the finish must be accepted
    rep, script, _ = run(tmp_path, CallToolDecision(calls=[call_ov(1), call(2, service="checkout")]),
                         FinishDecision(diagnosis=diag(["ev-query_metrics-0002"], "checkout")),
                         runner=overview_runner(tmp_path), cfg=InvestigationConfig(max_model_calls=3))
    assert rep.status == IncidentStatus.DIAGNOSED and rep.diagnosis.confidence_label == ConfidenceLabel.LOW
    assert script.contexts[1].rejection is None and "Unresolved" in rep.diagnosis.uncertainty


def test_follow_up_that_is_not_ok_does_not_resolve(tmp_path):
    reg = EvidenceRegistry()
    runner = overview_runner(tmp_path)
    reg.register(to_evidence(runner("overview", {})))
    bad = Runner(tmp_path, status="unavailable")("service_health", {"service": "ad"})
    reg.register(to_evidence(bad))
    assert anomaly_follow_up(reg, diag([]))[0] == ["ad"]


def test_uncited_follow_up_reported(tmp_path):
    reg = EvidenceRegistry()
    reg.register(to_evidence(overview_runner(tmp_path)("overview", {})))
    f = Runner(tmp_path)("service_health", {"service": "ad"})
    reg.register(to_evidence(f))
    assert anomaly_follow_up(reg, diag(["x"])) == ([], ["ad"])
    assert anomaly_follow_up(reg, diag([f.evidence_id])) == ([], [])


def test_eval_scoring_is_separate_and_strict():
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "evals"))
    from run_eval import score
    case = {"incident_id": "I", "expected_component": "ad", "expected_terms": ["cpu"]}
    d = {"status": "DIAGNOSED", "diagnosis": {"suspected_component": "ad", "suspected_cause": "CPU saturation"}}
    assert score(d, case)["correct"]
    assert not score({**d, "diagnosis": {"suspected_component": "cart", "suspected_cause": "cpu"}}, case)["correct"]
    assert not score({"status": "INCONCLUSIVE", "diagnosis": None}, case)["correct"]
    assert not score({**d, "diagnosis": {"suspected_component": "ad", "suspected_cause": "restart"}}, case)["correct"]


def test_diagnosing_a_recovering_service_is_sent_back_once(tmp_path):
    # live INC-BLIND-1 shape: an earlier incident's errors were falling while checkout was changing now
    base = Runner(tmp_path)

    def run_tool(name, args):
        res = base(name, args)
        if name == "overview":
            res.value = {"changed_since_previous_window": ["checkout: p95 up 1580 -> 3950 ms"],
                         "recovering_since_previous_window": ["ad: error_rate down 0.044 -> 0.005"]}
        return res
    rep, script, _ = run(tmp_path, CallToolDecision(calls=[call_ov(1)]),
                         FinishDecision(diagnosis=diag(["ev-overview-0001"], "ad")),
                         FinishDecision(diagnosis=diag(["ev-overview-0001"], "checkout")), runner=run_tool)
    assert "recovering" in script.contexts[2].rejection and "checkout" in script.contexts[2].rejection
    assert rep.status == IncidentStatus.DIAGNOSED and rep.diagnosis.suspected_component == "checkout"


def test_crash_loop_is_flagged_first_with_memory_runway():
    # live recommendationCacheFailure: 4 restarts in 30 min, memory climbing ~27 MB/min toward a 500 MB limit
    v = resources.analyse(cpu={"recommendation": 60.0, "cart": 2.0}, cpu_base={"recommendation": 55.0, "cart": 2.0},
                          mem={"recommendation": 33.0, "cart": 45.0}, mem_growth={}, mem_base={"recommendation": 40.0},
                          uptime={"recommendation": 24.0, "cart": 15800.0}, restarts={"recommendation": 4, "cart": 0},
                          mem_bytes={"recommendation": 165e6, "cart": 70e6}, mem_slope={"recommendation": 454e3, "cart": 3e3})
    top = v["flagged"][0]
    assert top["container"] == "recommendation" and top["severity"] == 4
    assert top["reasons"][0].startswith("CRASH LOOP: 4 restarts") and "min to its limit" in top["reasons"][0]
    assert not any(f["container"] == "cart" for f in v["flagged"])


def test_crash_loop_that_stopped_is_history_not_a_flag():
    # live: flag switched off, recommendation stable for 15 min, but 4 restarts still inside the 30 min count
    v = resources.analyse(cpu={"recommendation": 5.0, "cart": 2.0}, cpu_base={"recommendation": 5.0, "cart": 2.0},
                          mem={"recommendation": 41.0, "cart": 45.0}, mem_growth={}, mem_base={"recommendation": 45.0},
                          uptime={"recommendation": 930.0, "cart": 15800.0}, restarts={"recommendation": 4, "cart": 0})
    assert v["flagged"] == []
    assert v["restarts_ended"] == ["recommendation: 4 restart(s) in the last 30 min, none in the last 16 min (stable now)"]


def test_older_ongoing_problem_is_sent_back_for_the_newest_one(tmp_path):
    # live INC-DEMO-10: paymentFailure just started; cart's errors had been draining for 90 min (demo lock queue)
    base = Runner(tmp_path)

    def run_tool(name, args):
        res = base(name, args)
        if name == "overview":
            res.tool = "log_overview"
            res.value = {"logging_now": [
                {"service": "payment", "message": "Payment request failed. Invalid token.", "count_now": 2,
                 "first_seen_min_ago": 1.1, "started_recently": True},
                {"service": "cart", "message": "Wasn't able to connect to redis", "count_now": 2,
                 "first_seen_min_ago": 31.8, "started_recently": False}]}
        return res
    rep, script, _ = run(tmp_path, CallToolDecision(calls=[call_ov(1)]),
                         FinishDecision(diagnosis=diag(["ev-overview-0001"], "cart")),
                         FinishDecision(diagnosis=diag(["ev-overview-0001"], "payment")), runner=run_tool)
    rejection = script.contexts[2].rejection
    assert "ranks payment" in rejection and "began before the current incident" in rejection
    assert rep.status == IncidentStatus.DIAGNOSED


def test_memory_swing_back_to_its_usual_peak_is_not_flagged():
    # live INC-DEMO-14: product-catalog swings 75-93% of its 20 MiB limit; 90% vs a 77% average is not growth
    args = dict(cpu={"product-catalog": 40.0}, cpu_base={"product-catalog": 40.0}, mem={"product-catalog": 90.0},
                mem_growth={}, uptime={"product-catalog": 15000.0}, mem_base={"product-catalog": 77.0})
    assert resources.analyse(**args, mem_max={"product-catalog": 93.0})["flagged"] == []
    leak = resources.analyse(**{**args, "mem": {"product-catalog": 99.0}}, mem_max={"product-catalog": 93.0})
    assert leak["flagged"][0]["container"] == "product-catalog"  # above its prior peak: real growth


def test_latency_only_change_is_capped_to_low(tmp_path):
    # live INC-DEMO-14: one slower frontend route, no errors, no new logs, no resource flags -> not high
    base = Runner(tmp_path)
    values = {"overview": {"changed_since_previous_window": ["no service-wide change"],
                           "operations_changed": ['checkout "GET /x": p95 up 520 -> 3800 ms']},
              "log_overview": {"logging_now": "no WARN+ messages from application services in this window"},
              "resource_overview": {"flagged": []}}

    def run_tool(name, args):
        res = base(name, args)
        res.value = values.get(name, res.value)
        return res
    calls = [ToolCall(call_id=f"toolu_{i}", tool_name=n, arguments={}) for i, n in enumerate(values)]
    d = diag(["ev-overview-0001"], "checkout").model_copy(update={"confidence_label": ConfidenceLabel.HIGH})
    pol = ToolPolicy(approved_services=POLICY.approved_services,
                     arg_models={**POLICY.arg_models, "resource_overview": resources.ResourceArgs})
    dd = Dependencies(reasoner=Script(CallToolDecision(calls=calls), FinishDecision(diagnosis=d)), run_tool=run_tool,
                      policy=pol, registry=EvidenceRegistry())
    rep = investigate(incident(), InvestigationConfig(max_follow_up_nudges=0), dd)
    assert rep.diagnosis.confidence_label == ConfidenceLabel.LOW and "only latency changed" in rep.diagnosis.uncertainty


def test_strong_resource_flag_checked_but_ignored_is_sent_back(tmp_path):
    # live INC-DEMO-15 (adHighCpu): ad at 345% CPU was followed up, but the diagnosis blamed checkout latency
    rep, script, _ = run(tmp_path, CallToolDecision(calls=[call_ov(1), call(2, service="checkout"),
                                                           call(3, tool="service_health", service="ad")]),
                         FinishDecision(diagnosis=diag(["ev-query_metrics-0002"], "checkout")),
                         FinishDecision(diagnosis=diag(["ev-service_health-0003"], "checkout")),
                         runner=overview_runner(tmp_path))
    assert "ranks ad (tier 3: resources: cpu high)" in script.contexts[2].rejection
    assert rep.status == IncidentStatus.DIAGNOSED


def test_confidence_is_capped_to_the_evidence_tier(tmp_path):
    # live INC-PIC-1: nothing injected, only latency drifts from one source; the model said medium
    base = Runner(tmp_path)

    def run_tool(name, args):
        res = base(name, args)
        if name == "overview":
            res.value = {"operations_changed": ['checkout "GET /x": p95 up 500 -> 2000 ms'],
                         "changed_since_previous_window": ["cart: error_rate up 0.020 -> 0.070"]}
        return res
    d = diag(["ev-overview-0001"], "checkout").model_copy(update={"confidence_label": ConfidenceLabel.MEDIUM})
    rep, *_ = run(tmp_path, CallToolDecision(calls=[call_ov(1)]), FinishDecision(diagnosis=d), runner=run_tool)
    assert rep.diagnosis.confidence_label == ConfidenceLabel.LOW and "evidence for this component is weak" in rep.diagnosis.uncertainty


def test_only_the_newest_message_carries_the_evidence_picture(tmp_path):
    # live INC-DEMO-20: re-sending every older picture pushed the run past the 60k token budget
    from contracts import ToolOutcome
    chat = FakeChat(ai(("c1", "overview", {})), ai(("c2", "overview", {})), ai(("c3", "overview", {})))
    r = LLMReasoner("m", chat_model=chat)
    for i in range(3):
        outs = [] if i == 0 else [ToolOutcome(call_id=f"c{i}", tool_name="overview", status="ok", text="{}")]
        r.next_step(rctx(tmp_path, outs).model_copy(update={"evidence_picture": f"EVIDENCE PICTURE round {i}"}))
    humans = [m.content for m in chat.requests[-1] if isinstance(m, HumanMessage)]
    assert sum("EVIDENCE PICTURE" in h for h in humans) == 1 and "round 2" in humans[-1]
    assert "older evidence picture removed" in humans[0]
