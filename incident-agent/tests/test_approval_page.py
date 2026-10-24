"""Browser approval page, page-channel respond flow, alert watcher and the Grafana approval links.

The HTTP server runs for real on 127.0.0.1. The test plays the human: it reads the code from the Action API console
and submits it on the page, as an operator would."""
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import action_api_models as m
import detector
import grafana_dash
from action_executor import DockerExecutor
from models import IncidentStatus, TimelineEvent
from report_io import write_report
from response import ResponseConfig, respond
from safety import wait_for_page_approval
from tests.fakes import Planner, response_deps
from tests.test_action_api import (  # noqa: F401 - api is a fixture
    PROJECT, Console, FakeDocker, api, approve, execute, make_service, payload, silent_payment,
)
from tests.test_scenarios import plan

PAGE = ResponseConfig(mode="live", settle_s=0, interval_s=0, samples=2, approval_channel="page", approval_ttl_s=60)
_TOKEN = re.compile(r'name="form_token" value="([^"]+)"')


def submit(url: str, approval_id: str, **form) -> httpx.Response:
    page = httpx.get(f"{url}/ui/approvals/{approval_id}")
    token = _TOKEN.search(page.text).group(1)
    return httpx.post(f"{url}/ui/approvals/{approval_id}", data={"form_token": token, **form}, headers={"Origin": url})


def test_page_approval_runs_action_without_terminal_and_publishes_interim(api, tmp_path):
    start, console, _ = api
    tel, rep, reg = silent_payment(tmp_path)
    svc, url, client = start(DockerExecutor(PROJECT, run=FakeDocker(on_restart=lambda: setattr(tel, "phase", "after")), docker="docker"))
    stages, pages = [], []

    def on_stage(name, interim):
        stages.append((name, interim.status))
        _, art = write_report(interim, tmp_path / "interim")
        assert art.team_schema_status == "passed", art.team_schema_errors
        if name == "awaiting_approval":
            dash = grafana_dash.verification_dashboard(interim)
            text = " ".join(p.get("options", {}).get("content", "") for p in dash["panels"])
            assert "Waiting for your approval" in text and "/ui/approvals/apr-" in text
            assert grafana_dash.approval_page(interim) in grafana_dash.index_dashboard([interim])["panels"][0]["options"]["content"]
            aid = re.search(r"apr-[a-f0-9]{16}", grafana_dash.approval_page(interim)).group(0)
            html = httpx.get(f"{url}/ui/approvals/{aid}").text
            pages.append(html)
            r = submit(url, aid, approver="operator-1", code=console.code(), decision="approve")
            assert r.status_code == 200 and "Approved" in r.text

    deps = response_deps(tmp_path, tel, reg, Planner(plan(target="payment")), client=client, catalog=client.catalog(),
                         interactive=False, answer="should never be read")
    deps.on_stage = on_stage
    out = respond(rep, PAGE, deps)
    assert out.remediation_outcome == "VERIFIED_RECOVERY", out.escalation_reason
    assert [s for s, _ in stages] == ["awaiting_approval", "verifying"]
    assert out.approval.approver == "operator-1" and out.approval.decision == "approved"
    rec = svc.list_actions().actions[0]
    assert rec.status == "succeeded" and rec.audit.approval_id in pages[0]
    assert console.code() not in pages[0] and console.code().replace("-", "") not in pages[0]
    assert '&quot;target&quot;: &quot;payment&quot;' in pages[0] and "Approve and run" in pages[0]
    audit = (tmp_path / "api" / "audit.jsonl").read_text()
    assert "approval_granted" in audit and console.code() not in audit


def test_page_rejection_runs_nothing(api, tmp_path):
    start, console, _ = api
    tel, rep, reg = silent_payment(tmp_path)
    svc, url, client = start()
    deps = response_deps(tmp_path, tel, reg, Planner(plan(target="payment")), client=client, catalog=client.catalog())

    def on_stage(name, interim):
        aid = re.search(r"apr-[a-f0-9]{16}", grafana_dash.approval_page(interim)).group(0)
        assert "Rejected" in submit(url, aid, approver="op", decision="reject").text
    deps.on_stage = on_stage
    out = respond(rep, PAGE, deps)
    assert out.status == IncidentStatus.REJECTED and svc.list_actions().actions == []


def test_execute_needs_page_approval_or_code(tmp_path):
    console = Console()
    svc = make_service(tmp_path, console=console)
    a, code = approve(svc, console)
    with pytest.raises(Exception) as exc:
        execute(svc, a, None)
    assert "APPROVAL_NOT_GRANTED" in str(exc.value.code)
    rec = svc.ui_record(a.approval_id)
    assert "Wrong code" in svc.decide_on_page(a.approval_id, approve=True, code="ZZZZ-ZZZZ", approver="x", form_token=rec["form_token"])
    assert "Approved" in svc.decide_on_page(a.approval_id, approve=True, code=code.lower(), approver="x", form_token=rec["form_token"])
    with pytest.raises(Exception) as exc:  # approved for one payload, a different payload is refused
        execute(svc, a, None, p=payload(target="cart"))
    assert "PAYLOAD_MISMATCH" in str(exc.value.code)
    status, rec2 = execute(svc, a, None)
    assert status == 202 and svc.get_approval(a.approval_id).status == "consumed"
    with pytest.raises(Exception):
        execute(svc, a, None, key="idem-key-0002")


def test_page_wrong_code_locks_and_form_token_required(tmp_path):
    console = Console()
    svc = make_service(tmp_path, console=console)
    a, code = approve(svc, console)
    tok = svc.ui_record(a.approval_id)["form_token"]
    with pytest.raises(Exception):
        svc.decide_on_page(a.approval_id, approve=True, code=code, approver="x", form_token="forged")
    for _ in range(5):
        svc.decide_on_page(a.approval_id, approve=True, code="AAAA-AAAA", approver="x", form_token=tok)
    assert svc.get_approval(a.approval_id).status == "locked"
    assert "already locked" in svc.decide_on_page(a.approval_id, approve=True, code=code, approver="x", form_token=tok)


def test_page_http_guards_and_escaping(api, tmp_path):
    start, console, _ = api
    svc, url, _ = start()
    a, code = approve(svc, console, p=payload(reason="restart payment <script>alert(1)</script> now"))
    page = httpx.get(f"{url}/ui/approvals/{a.approval_id}")
    assert page.status_code == 200 and "<script>" not in page.text and "&lt;script&gt;" in page.text
    assert "default-src 'none'" in page.headers["content-security-policy"] and page.headers["x-frame-options"] == "DENY"
    assert httpx.get(f"{url}/ui/approvals/{a.approval_id}", headers={"Host": "evil.example:80"}).status_code == 400
    token = _TOKEN.search(page.text).group(1)
    cross = httpx.post(f"{url}/ui/approvals/{a.approval_id}", data={"form_token": token, "code": code, "decision": "approve"},
                       headers={"Origin": "http://evil.example"})
    assert cross.status_code == 403 and svc.get_approval(a.approval_id).status == "pending"
    assert httpx.get(f"{url}/ui/approvals/../v1/actions").status_code in (400, 404)
    assert httpx.post(f"{url}/v1/approvals/{a.approval_id}/approve", json={}).status_code == 401  # no approve under /v1
    assert a.approval_id in httpx.get(f"{url}/ui").text


def test_wait_for_page_approval_expires_and_fails_closed():
    t = [0.0]

    def clock():
        t[0] += 1
        return t[0]

    def pending(_):
        return type("S", (), {"status": "pending", "approved_by": None})()
    out = wait_for_page_approval("s", "d" * 64, "apr-" + "0" * 16, status_fn=pending, page_url="u", ttl_s=5,
                                 print_fn=lambda _: None, sleep=lambda _: None, clock=clock)
    assert out.decision == "expired"

    def down(_):
        raise httpx.ConnectError("down")
    out = wait_for_page_approval("s", "d" * 64, "apr-" + "0" * 16, status_fn=down, page_url="u", ttl_s=60,
                                 print_fn=lambda _: None, sleep=lambda _: None, clock=clock)
    assert out.decision == "rejected"


# ---------------------------------------------------------------- alert watcher
def data(**over):
    base = {"rps": {"checkout": 2.0, "ad": 1.0, "load-generator": 3.0}, "err_rps": {"load-generator": 0.6},
            "p95": {"checkout": 80.0}, "rps_prior": {"checkout": 2.0, "ad": 1.0}, "err_rps_prior": {}, "p95_prior": {"checkout": 70.0},
            "cpu": {"ad": 3.0, "load-generator": 300.0}, "last_seen_age": {"ad": 5.0, "checkout": 5.0, "payment": 6.0, "cart": 4.0}}
    base.update(over)
    return base


def test_detector_signals_and_exclusions():
    assert detector.evaluate(data()) == []  # load-generator errors and CPU are our own tooling
    got = detector.evaluate(data(cpu={"ad": 398.0}, err_rps={"checkout": 0.4},
                                 last_seen_age={"ad": 5.0, "checkout": 5.0, "payment": 400.0, "cart": 4.0}))
    assert [(a.service, a.signal) for a in got] == [("payment", "stopped"), ("ad", "cpu"), ("checkout", "error_rate")]
    assert 'container_name="payment"' in got[0].promql and "ALERT" in detector.symptom(got)
    # an error rate that was already this high ten minutes ago is not a new alert
    assert detector.evaluate(data(err_rps={"checkout": 0.4}, err_rps_prior={"checkout": 0.4})) == []
    # the whole fleet silent is a pipeline problem, not a stopped container
    assert detector.evaluate(data(last_seen_age={"ad": 900.0, "checkout": 900.0, "payment": 900.0, "cart": 900.0})) == []
    assert [a.signal for a in detector.evaluate(data(p95={"checkout": 4000.0}))] == ["p95_latency"]


def test_detector_debounces_mutes_preexisting_and_handled():
    cpu = detector.Alert("ad", "cpu", 400, 150, "cpu", "q")
    stop = detector.Alert("payment", "stopped", 400, 150, "stopped", "q")
    det = detector.Detector()
    assert det.check([cpu]) == []                     # present at start: pre-existing, muted
    assert det.check([cpu]) == []
    for _ in range(3):
        det.check([])                                 # healthy three times: unmuted
    assert det.check([stop]) == [] and det.check([stop]) == [stop]   # needs two checks in a row
    det.handled(["payment"])
    assert det.check([stop]) == [] and det.check([stop]) == []        # muted until it has been healthy again


def test_grafana_shows_alert_with_query_link(tmp_path):
    from tests.test_grafana_dash import verified  # noqa: PLC0415
    rep = verified(tmp_path)
    alert = detector.Alert("payment", "stopped", 400, 150, "container payment stopped reporting", detector.promql_for("stopped", "payment"))
    rep = rep.model_copy(update={"timeline": [TimelineEvent(status=IncidentStatus.RECEIVED, description="Alert received",
                                                            metadata={"alerts": [alert.as_dict()]})] + list(rep.timeline)})
    text = grafana_dash.root_cause_dashboard(rep)["panels"][0]["options"]["content"]
    assert "Alert that opened this incident" in text and "alert query" in text and "/grafana/explore?" in text
    assert grafana_dash.approval_page(rep) is None


def test_watch_fires_once_runs_full_pipeline_and_mutes_spread(monkeypatch, tmp_path):
    import cli
    import telemetry

    stop = detector.Alert("payment", "stopped", 400, 150, "container payment stopped", detector.promql_for("stopped", "payment"))
    spread = detector.Alert("checkout", "error_rate", 0.4, 0.05, "error rate 40%", detector.promql_for("error_rate", "checkout"))
    checks = iter([[], [stop], [stop], [stop, spread]])
    monkeypatch.setattr(detector, "fetch", lambda: {})
    monkeypatch.setattr(detector, "evaluate", lambda _: next(checks))
    monkeypatch.setattr(telemetry, "service_inventory", lambda refresh=False: ["checkout", "frontend"])
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    runs = []
    monkeypatch.setattr(cli, "run_incident", lambda args, iid, symptom, service, source, catalog, client, alerts=None:
                        runs.append((iid, symptom, service, source, alerts)) or 0)
    args = cli.build_parser().parse_args(["watch", "--model", "m", "--execute", "--max-incidents", "1", "--out", str(tmp_path)])
    assert args.respond and args.action_api and args.grafana and args.approval_channel == "page"
    assert cli.cmd_watch(args, None, None) == 0
    [(iid, symptom, service, source, alerts)] = runs
    assert iid.startswith("AUTO-payment-") and "ALERT" in symptom and "payment" in symptom
    assert service is None and source == "alert-watcher"  # payment is not in the span inventory: no affected service
    assert alerts == [stop.as_dict()]
