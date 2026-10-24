"""Unit tests for the Action API adapter, policy engine, approval gate, kill switch, silent-container detection,
endpoint helpers and the bounded diagnosis correction. Mocked; no network."""
import json
from datetime import timedelta

import httpx
import pytest

import resources
from actions import CatalogError, HttpActionClient, Operation, build_request, load_catalog, validate_arguments
from contracts import FinishDecision
from models import ConfidenceLabel, Diagnosis, IncidentStatus
from orchestrator import InvestigationConfig, anomaly_follow_up
from safety import KillSwitch, PlannedAction, evaluate, proposal_digest, request_approval
from tests.fakes import MOCK_CATALOG, SERVICES, Telemetry, calls, finish_citing, run_investigation

LIVE = MOCK_CATALOG.model_copy(update={"provenance": "organiser"})
RESTART = MOCK_CATALOG.get("restart_service")
LOAD = MOCK_CATALOG.get("set_load_generator_users")


# ---- catalog and argument validation
def test_catalog_rejects_unsupported_schema_and_open_targets(tmp_path):
    bad = json.loads(json.dumps(MOCK_CATALOG.model_dump()))
    bad["operations"][0]["parameters"]["x"] = {"type": "object"}
    (tmp_path / "c.json").write_text(json.dumps(bad))
    with pytest.raises(CatalogError):
        load_catalog(tmp_path / "c.json")
    with pytest.raises(ValueError):
        Operation(name="restart", description="d", path="/r", target_kind="service", allowed_targets=[], risk="r")


def test_missing_catalog_means_no_operations():
    assert load_catalog(None).operations == [] and load_catalog("").operations == []


@pytest.mark.parametrize("args, err", [
    ({"users": "5"}, "must be integer"), ({"users": True}, "must be integer"), ({"users": 500}, "out of range"),
    ({}, "missing required"), ({"users": 5, "shell": "rm -rf /"}, "unknown arguments"),
])
def test_argument_validation_is_strict(args, err):
    _, errors = validate_arguments(LOAD, args)
    assert any(err in e for e in errors)


def test_defaults_applied():
    assert validate_arguments(RESTART, {}) == ({"grace_period_seconds": 10}, [])


# ---- HTTP adapter
def _client(handler, catalog=LIVE, monkeypatch=None):
    return HttpActionClient(catalog, transport=httpx.MockTransport(handler))


@pytest.fixture
def api_url(monkeypatch):
    monkeypatch.setenv("MOCK_ACTION_API_URL", "http://action-api.test")


def test_mock_catalog_or_missing_url_cannot_go_live(monkeypatch):
    with pytest.raises(CatalogError, match="organiser"):
        HttpActionClient(MOCK_CATALOG)
    monkeypatch.delenv("MOCK_ACTION_API_URL", raising=False)
    with pytest.raises(CatalogError, match="not set"):
        HttpActionClient(LIVE)


def test_success_records_request_id_and_sends_exact_body(api_url):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(202, json={"request_id": "r-9", "status": "accepted"})
    res = _client(handler).execute(RESTART, "cart", {"grace_period_seconds": 5}, "INC-1-abc")
    assert res.outcome == "success" and res.request_id == "r-9"
    assert json.loads(seen[0].content) == {"grace_period_seconds": 5, "service": "cart"}
    assert seen[0].headers["Idempotency-Key"] == "INC-1-abc" and str(seen[0].url) == "http://action-api.test/actions/restart"


@pytest.mark.parametrize("code, outcome", [(400, "failed"), (422, "failed"), (500, "unknown"), (302, "unknown")])
def test_error_codes_map_to_outcomes(api_url, code, outcome):
    assert _client(lambda r: httpx.Response(code, json={})).execute(RESTART, "cart", {}, "k").outcome == outcome


def test_timeout_after_send_is_unknown_and_not_retried(api_url):
    n = []

    def handler(req):
        n.append(1)
        raise httpx.ReadTimeout("slow", request=req)
    res = _client(handler).execute(LOAD, None, {"users": 1}, "k")  # idempotent op still not retried after sending
    assert res.outcome == "unknown" and len(n) == 1


def test_connect_error_retried_only_for_idempotent(api_url):
    for op, expected in ((RESTART, 1), (LOAD, 2)):
        n = []

        def handler(req):
            n.append(1)
            raise httpx.ConnectError("down", request=req)
        assert _client(handler).execute(op, None if op is LOAD else "cart", {"users": 1} if op is LOAD else {}, "k").outcome == "failed"
        assert len(n) == expected


def test_token_comes_from_environment(api_url, monkeypatch):
    cat = LIVE.model_copy(update={"auth_header": "Authorization", "token_env": "ACTION_TOKEN_T"})
    with pytest.raises(CatalogError):
        HttpActionClient(cat, transport=httpx.MockTransport(lambda r: httpx.Response(200))).execute(RESTART, "cart", {}, "k")
    monkeypatch.setenv("ACTION_TOKEN_T", "s3cret")
    got = []
    HttpActionClient(cat, transport=httpx.MockTransport(lambda r: got.append(r) or httpx.Response(200))).execute(RESTART, "cart", {}, "k")
    assert got[0].headers["Authorization"] == "Bearer s3cret"
    assert "s3cret" not in json.dumps(build_request(cat, RESTART, "cart", {}))


# ---- policy, digest and approval
def _diag(component="cart", conf=ConfidenceLabel.MEDIUM):
    return Diagnosis(suspected_component=component, suspected_cause="c", supporting_evidence_ids=["e"], uncertainty="u",
                     confidence_label=conf, next_step="n")


def _evidence(tmp_path):
    tel = Telemetry(tmp_path, {("search_traces", "cart"): ("ok", {"origin": "cart", "error_message": "cart: connection refused"}, {})})
    from integration import to_evidence
    return [to_evidence(tel("search_traces", {"service": "cart"}))]


def _plan(**kw):
    base = dict(operation="restart_service", target="cart", supporting_evidence_ids=[], reason="restart the hung cart process",
                expected_effect="errors clear", risks="brief downtime")
    base.update(kw)
    return PlannedAction(**base)


def test_policy_accepts_supported_evidence_backed_plan(tmp_path):
    ev = _evidence(tmp_path)
    d = evaluate(_plan(supporting_evidence_ids=[ev[0].evidence_id]), catalog=MOCK_CATALOG, diagnosis=_diag(), evidence=ev,
                 inventory=SERVICES, incident_id="I", affected_service="cart")
    assert d.allowed, d.errors
    assert d.request_preview["json"] == {"grace_period_seconds": 10, "service": "cart"}
    assert {"service": "cart", "metric": "container_last_seen_age_s", "max_value": None, "min_value": None} in d.proposal.verification_checks


@pytest.mark.parametrize("change, err", [
    ({"target": "flagd"}, "out of bounds"), ({"target": "opensearch"}, "not approved"),
    ({"target": "load-generator"}, "not approved"), ({"supporting_evidence_ids": []}, "cites no supporting"),
])
def test_policy_refusals(tmp_path, change, err):
    ev = _evidence(tmp_path)
    plan = _plan(**{"supporting_evidence_ids": [ev[0].evidence_id], **change})
    d = evaluate(plan, catalog=MOCK_CATALOG, diagnosis=_diag(), evidence=ev, inventory=SERVICES, incident_id="I", affected_service=None)
    assert not d.allowed and any(err in e for e in d.errors)


def test_policy_refuses_low_confidence_and_evidence_not_naming_target(tmp_path):
    ev = _evidence(tmp_path)
    d = evaluate(_plan(supporting_evidence_ids=[ev[0].evidence_id], target="payment"), catalog=MOCK_CATALOG,
                 diagnosis=_diag("payment", ConfidenceLabel.LOW), evidence=ev, inventory=SERVICES, incident_id="I", affected_service=None)
    assert any("low" in e for e in d.errors) and any("mentions the target" in e for e in d.errors)


def test_digest_changes_with_any_argument_or_catalog_change(tmp_path):
    ev = _evidence(tmp_path)
    kw = dict(catalog=MOCK_CATALOG, diagnosis=_diag(), evidence=ev, inventory=SERVICES, incident_id="I", affected_service=None)
    a = evaluate(_plan(supporting_evidence_ids=[ev[0].evidence_id]), **kw).proposal
    b = evaluate(_plan(supporting_evidence_ids=[ev[0].evidence_id], arguments={"grace_period_seconds": 11}), **kw).proposal
    assert proposal_digest(a, MOCK_CATALOG) != proposal_digest(b, MOCK_CATALOG)
    assert proposal_digest(a, MOCK_CATALOG) != proposal_digest(a, LIVE)


def test_approval_requires_exact_code_and_expires():
    digest = "abcdef0123456789" * 4
    ok = request_approval("s", digest, approver="h", ttl_s=60, input_fn=lambda p: "approve abcdef01", interactive=True, print_fn=lambda s: None)
    assert ok.decision == "approved" and ok.proposal_digest == digest
    t = iter([0.0, 500.0])
    late = request_approval("s", digest, approver="h", ttl_s=60, input_fn=lambda p: "approve abcdef01", interactive=True,
                            print_fn=lambda s: None, clock=lambda: next(t))
    assert late.decision == "expired"

    def interrupted(_):
        raise KeyboardInterrupt
    assert request_approval("s", digest, approver="h", ttl_s=60, input_fn=interrupted, interactive=True, print_fn=lambda s: None).decision == "rejected"


def test_kill_switch_env_and_file(tmp_path, monkeypatch):
    ks = KillSwitch(tmp_path / "KS")
    assert not ks.active()
    ks.set(True)
    assert ks.active()
    ks.set(False)
    monkeypatch.setenv("INCIDENT_AGENT_KILL_SWITCH", "1")
    assert ks.active()


# ---- silent containers (unknown infrastructure faults)
def test_silent_container_flagged_while_others_report():
    v = resources.analyse({"a": 1.0, "b": 1.0}, {}, {}, {}, {"a": 900.0, "b": 900.0},
                          {"a": 10.0, "b": 12.0, "shipping": 400.0})
    silent = [f for f in v["flagged"] if f.get("kind") == "silent"]
    assert [f["container"] for f in silent] == ["shipping"] and v["telemetry_silence"] is None


def test_fleet_wide_silence_reported_as_pipeline_or_stack_outage_not_per_container():
    v = resources.analyse({}, {}, {}, {}, {}, {n: 3000.0 for n in "abcdef"})
    assert v["telemetry_silence"]["silent_containers"] == 6 and not v["flagged"]


def test_empty_follow_up_resolves_a_silent_flag(tmp_path):
    flagged = {"flagged": [{"container": "shipping", "kind": "silent", "severity": 3, "reasons": ["silent"]}]}
    tel = Telemetry(tmp_path, {("resource_overview", None): ("ok", flagged, {})})
    rep, reg = run_investigation(tmp_path, tel, calls(("resource_overview", {})), calls(("service_health", {"service": "shipping"})),
                                 finish_citing("shipping", "stopped", ["resource_overview", "service_health"]))
    assert anomaly_follow_up(reg, rep.diagnosis) == ([], [])


# ---- bounded diagnosis correction (fix for the smoke-test INCONCLUSIVE)
def test_unsupported_component_gets_one_correction_then_succeeds(tmp_path):
    tel = Telemetry(tmp_path, {("search_traces", "cart"): ("ok", {"origin": "cart"}, {})})
    wrong = finish_citing("redis", "cache problem", ["search_traces"])
    right = finish_citing("cart", "cart errors", ["search_traces"])
    rep, _ = run_investigation(tmp_path, tel, calls(("search_traces", {"service": "cart"})), wrong, right)
    assert rep.status == IncidentStatus.DIAGNOSED and rep.diagnosis.suspected_component == "cart"
    assert any("bounded correction" in t.description for t in rep.timeline)


def test_second_unsupported_component_is_inconclusive(tmp_path):
    tel = Telemetry(tmp_path, {("search_traces", "cart"): ("ok", {"origin": "cart"}, {})})
    wrong = finish_citing("redis", "cache problem", ["search_traces"])
    rep, _ = run_investigation(tmp_path, tel, calls(("search_traces", {"service": "cart"})), wrong)
    assert rep.status == IncidentStatus.INCONCLUSIVE and rep.timeline[-1].metadata["terminal_reason"] == "diagnosis_rejected"


# ---- endpoints and report
def test_write_env_updates_only_given_keys(tmp_path):
    import endpoints
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=keep-me\nPROM_URL=http://old\n")
    changed = endpoints.write_env(env, {"PROM_URL": "http://localhost:9090", "JAEGER_URL": "http://localhost:1"})
    text = env.read_text()
    assert "ANTHROPIC_API_KEY=keep-me" in text and "PROM_URL=http://localhost:9090" in text and set(changed) == {"PROM_URL", "JAEGER_URL"}


def test_discover_parses_docker_port_output():
    import endpoints

    class R:
        def __init__(self, out):
            self.stdout = out
    outs = {"opensearch": "0.0.0.0:62208\n[::]:62208\n", "jaeger": "0.0.0.0:62205\n"}
    found = endpoints.discover(run=lambda cmd, **kw: R(outs.get(cmd[2], "")))
    assert found == {"OPENSEARCH_URL": "http://localhost:62208", "JAEGER_URL": "http://localhost:62205"}


def test_evidence_links(tmp_path, monkeypatch):
    import endpoints
    from integration import to_evidence
    monkeypatch.setenv("PROM_URL", "http://p:9090")
    monkeypatch.setenv("JAEGER_URL", "http://j:16686")
    tel = Telemetry(tmp_path)
    ev = to_evidence(tel("query_metrics", {"service": "cart", "metric": "error_rate"}))
    ev = ev.model_copy(update={"query": 'sum(rate(x{service_name="cart"}[5m]))'})
    assert endpoints.evidence_links(ev)[0].startswith("http://p:9090/query?g0.expr=sum%28rate")
    tr = ev.model_copy(update={"source": "jaeger", "trace_ids": ["abc123"]})
    assert endpoints.evidence_links(tr) == ["http://j:16686/jaeger/ui/trace/abc123"]


def test_report_rejects_unknown_proposal_evidence(tmp_path):
    from models import ActionProposal
    from report import ReportGenerator
    tel = Telemetry(tmp_path, {("search_traces", "cart"): ("ok", {"origin": "cart"}, {})})
    rep, _ = run_investigation(tmp_path, tel, calls(("search_traces", {"service": "cart"})), finish_citing("cart", "c", ["search_traces"]))
    bad = rep.model_copy(update={"proposals": [ActionProposal(incident_id="I", runbook_id="r", action="a", target_service="cart",
                                                              reason="r", risk="r", expected_effect="e", verification_plan="v",
                                                              supporting_evidence_ids=["ev-fake"])]})
    assert any("ev-fake" in e for e in ReportGenerator(bad).validate_citations())


def test_cli_refuses_live_execution_with_mock_catalog(monkeypatch, capsys):
    import cli
    from pathlib import Path
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    path = str(Path(__file__).parent / "fixtures" / "mock_action_catalog.json")
    rc = cli.main(["investigate", "--incident-id", "I", "--symptom", "s", "--model", "m", "--respond", "--execute", "--action-catalog", path])
    assert rc == 2 and "organiser" in capsys.readouterr().err


def test_unanswered_approval_prompt_times_out(monkeypatch):
    import os
    import cli
    r, w = os.pipe()
    monkeypatch.setattr(cli.sys, "stdin", os.fdopen(r))
    t = iter([0.0, 1.0])
    res = request_approval("s", "f" * 64, approver="h", ttl_s=0.2, input_fn=cli.timed_input(0.2), interactive=True,
                           print_fn=lambda s: None, clock=lambda: next(t))
    os.close(w)
    assert res.decision == "expired"


def test_links_for_container_metric_and_overview_queries(tmp_path, monkeypatch):
    import endpoints
    from integration import to_evidence
    monkeypatch.setenv("PROM_URL", "http://p:9090")
    ev = to_evidence(Telemetry(tmp_path)("query_metrics", {"service": "cart", "metric": "container_cpu"}))
    raw = ev.model_copy(update={"query": '{__name__=~"container_cpu_utilization.*",container_name="cart"}'})
    assert endpoints.evidence_links(raw)[0].startswith("http://p:9090/query?g0.expr=%7B__name__")
    ov = ev.model_copy(update={"query": json.dumps({"request_rate": "sum(rate(a[5m]))", "p95_ms": "histogram_quantile(0.95, b)"})})
    assert len(endpoints.evidence_links(ov)) == 2


def test_availability_bound_cannot_be_tighter_than_reporting_interval(tmp_path):
    from safety import VerificationCheck
    ev = _evidence(tmp_path)
    plan = _plan(supporting_evidence_ids=[ev[0].evidence_id],
                 verification_checks=[VerificationCheck(service="cart", metric="container_last_seen_age_s", max_value=30.0)])
    d = evaluate(plan, catalog=MOCK_CATALOG, diagnosis=_diag(), evidence=ev, inventory=SERVICES, incident_id="I", affected_service=None)
    assert not d.allowed and any("outside [90.0, 600.0]" in e for e in d.errors)


# ---- approval prompt UX (DEMO-PAY-4: the operator typed "APPROVE" without the code)
def _answers(*seq):
    it = iter(seq)
    return lambda _prompt: next(it)


def _server(answers, clock=None):
    from safety import request_server_approval
    shown = []
    a, code = request_server_approval("summary", "d" * 64, "apr-0123456789abcdef", approver="h", ttl_s=60,
                                      input_fn=answers, interactive=True, print_fn=shown.append,
                                      clock=clock or (lambda: 0.0))
    return a.decision, code, shown


def test_missing_code_reprompts_with_hint_then_approves():
    decision, code, shown = _server(_answers("APPROVE", "approve qx7e-fp9n"))
    assert decision == "approved" and code == "QX7E-FP9N"
    assert any("the code is missing" in s for s in shown)


@pytest.mark.parametrize("typed, expected", [("approve <QX7E-FP9N>", "QX7E-FP9N"), ("  Approve   'QX7EFP9N' ", "QX7EFP9N")])
def test_brackets_quotes_and_case_are_forgiven(typed, expected):
    assert _server(_answers(typed))[:2] == ("approved", expected)


def test_code_alone_three_times_rejects():
    decision, code, shown = _server(_answers("QX7E-FP9N", "QX7E-FP9N", "QX7E-FP9N"))
    assert decision == "rejected" and code is None and any("type the word approve before the code" in s for s in shown)


@pytest.mark.parametrize("typed", ["", "reject", "No", "deny"])
def test_explicit_or_empty_rejects_without_reprompt(typed):
    calls = []

    def answer(_p):
        calls.append(1)
        return typed
    assert _server(answer)[0] == "rejected" and len(calls) == 1


def test_reprompts_cannot_extend_the_ttl():
    t = iter([0.0, 10.0, 70.0])
    assert _server(_answers("APPROVE", "approve QX7E-FP9N"), clock=lambda: next(t))[0] == "expired"


def test_malformed_server_code_reprompts():
    decision, _, shown = _server(_answers("approve QX7", "approve QX7E-FP9N"))
    assert decision == "approved" and any("codes look like XXXX-XXXX" in s for s in shown)


def test_local_wrong_code_reprompts_then_correct_code_approves():
    digest = "abcdef0123456789" * 4
    shown = []
    a = request_approval("s", digest, approver="h", ttl_s=60, input_fn=_answers("approve 00000000", "approve ABCDEF01"),
                         interactive=True, print_fn=shown.append, clock=lambda: 0.0)
    assert a.decision == "approved" and any("does not match" in s for s in shown)



@pytest.mark.parametrize("tool, value, expected", [
    ("container_state", {"services": [{"service": "cart", "state": "exited"}]}, True),
    ("container_state", {"services": [{"service": "cart", "state": "running", "health": "unhealthy"}]}, True),
    ("container_state", {"services": [{"service": "cart", "state": "running", "restart_count": 0}]}, False),
    ("resource_overview", {"flagged": [{"container": "cart", "kind": "silent"}]}, True),
    ("resource_overview", {"flagged": [{"container": "ad"}]}, False),
    ("search_traces", {"error_message": "cart: lookup cart on 127.0.0.11:53: no such host"}, True),
    ("search_traces", {"error_message": "cart: rpc error: code = Unavailable desc = timeout"}, False),
    ("query_logs", {"lines": [{"body": "cart request failed: invalid token"}]}, False),
])
def test_process_level_evidence_signals(tmp_path, tool, value, expected):
    from integration import to_evidence
    from safety import process_level_evidence
    ev = to_evidence(Telemetry(tmp_path, {(tool, "cart"): ("ok", value, {})})(tool, {"service": "cart"}))
    assert process_level_evidence("cart", [ev]) is expected
