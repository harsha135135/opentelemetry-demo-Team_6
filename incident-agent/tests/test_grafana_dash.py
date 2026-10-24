"""Grafana dashboards built from reports: navigation, evidence coverage, verification checks, escaping and publishing.
Reports come from the mocked scenarios; publishing uses an in-process transport, so nothing touches the network."""
import json
import re
from datetime import timedelta

import httpx
import pytest

import cli
import grafana_dash as gd
from models import Evidence, IncidentStatus, TimeWindow, ToolStatus
from report_io import write_report
from response import respond
from tests.fakes import DRY, FAST, NOW, Client, Planner, Telemetry, approve_shown_code, calls, inconclusive, response_deps, run_investigation
from tests.test_scenarios import payment_diagnosis, plan

UID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def panels(d):
    def walk(ps):
        for p in ps:
            yield p
            yield from walk(p.get("panels", []))
    return list(walk(d["panels"]))


def text(d):
    return "\n".join(p["options"]["content"] for p in panels(d) if p["type"] == "text")


def rendered(d):
    """Every string Grafana renders: text panels, panel titles, descriptions (markdown tooltips) and link titles."""
    parts = [text(d), d["title"], d.get("description", "")] + [link["title"] for link in d.get("links", [])]
    for p in panels(d):
        parts += [p.get("title", ""), p.get("description", "")] + [link["title"] for link in p.get("links", [])]
    return "\n".join(parts)


def markers(d):
    return [a["name"] for a in d["annotations"]["list"] if "expr" in a]


def verified(tmp_path):
    tel, rep, reg = payment_diagnosis(tmp_path)
    return respond(rep, FAST, response_deps(tmp_path, tel, reg, Planner(plan()), client=Client(tel), answer=approve_shown_code))


def test_root_cause_dashboard_lists_every_cited_item_and_links_both_pages(tmp_path):
    rep = verified(tmp_path)
    rc_uid, ver_uid = gd.dashboard_uids(rep.incident_id)
    rc = gd.root_cause_dashboard(rep)
    body = text(rc)
    assert rc["uid"] == rc_uid and UID.match(rc_uid) and UID.match(ver_uid)
    assert rep.diagnosis.supporting_evidence_ids
    for eid in rep.diagnosis.supporting_evidence_ids:
        assert gd.code(eid) in body
    assert f"[Verification](/grafana/d/{ver_uid})" in body and f"[All incidents](/grafana/d/{gd.INDEX_UID})" in body
    urls = [link["url"] for link in rc["links"]]
    assert f"/grafana/d/{gd.INDEX_UID}" in urls and f"/grafana/d/{ver_uid}" in urls
    assert f"/grafana/d/{rc_uid}?from=now-15m&to=now" in urls
    logs = [p for p in panels(rc) if p["type"] == "table"]
    assert logs and "resource.service.name = 'payment'" in logs[0]["targets"][0]["query"]
    assert {"Detected", "Diagnosis accepted"} <= set(markers(rc)) and rc["timezone"] == "utc"


def test_verification_dashboard_shows_each_check_against_its_baseline(tmp_path):
    rep = verified(tmp_path)
    assert rep.remediation_outcome == "VERIFIED_RECOVERY"
    checks = gd.verification_checks(rep)
    assert checks and all(len(c["samples"]) == 2 for c in checks)
    assert all(c["state"] in ("recovered", "unaffected") for c in checks) and any(c["state"] == "recovered" for c in checks)
    ver = gd.verification_dashboard(rep)
    body = text(ver)
    assert "VERIFIED_RECOVERY" in body and "Recovery checks" in json.dumps(ver)
    for c in checks:
        name = f"{c['check'].service}.{c['check'].metric}"
        panel = next(p for p in panels(ver) if p.get("title", "").startswith(name + " · "))
        assert c["state"] in panel["title"] and gd.code(name) in body
        if c["bound_value"] is not None:
            steps = panel["fieldConfig"]["defaults"]["thresholds"]["steps"]
            assert c["bound_value"] in [s["value"] for s in steps]
    assert {"Baseline captured", "Approved", "Action dispatched", "Verification judged"} <= set(markers(ver))


def test_check_samples_are_the_judged_ones_not_every_matching_query(tmp_path):
    tel, rep, reg = payment_diagnosis(tmp_path)
    extra = [{"service": "payment", "metric": "request_rate", "min_value": 0.5}]
    out = respond(rep, FAST, response_deps(tmp_path, tel, reg, Planner(plan(verification_checks=extra)), client=Client(tel),
                                           answer=approve_shown_code))
    checks = {(c["check"].service, c["check"].metric): c for c in gd.verification_checks(out)}
    assert ("payment", "request_rate") in checks and ("payment", "error_rate") in checks
    assert all(len(c["samples"]) == FAST.samples for c in checks.values())
    assert checks[("payment", "request_rate")]["samples"] == [2.0, 2.0] and checks[("payment", "error_rate")]["baseline"] == 0.6


def test_dry_run_checks_are_marked_not_run(tmp_path):
    tel, rep, reg = payment_diagnosis(tmp_path)
    out = respond(rep, DRY, response_deps(tmp_path, tel, reg, Planner(plan()), client=Client(tel), answer=approve_shown_code))
    checks = gd.verification_checks(out)
    assert checks and all(c["state"] == "not run (dry-run)" and c["samples"] == [] for c in checks)
    assert "Action dispatched" not in markers(gd.verification_dashboard(out))


def test_inconclusive_incident_explains_why_nothing_was_verified(tmp_path):
    tel = Telemetry(tmp_path, {("overview", None): ("ok", {"services": []}, {})})
    rep, _ = run_investigation(tmp_path, tel, calls(("overview", {})), inconclusive())
    assert rep.status == IncidentStatus.INCONCLUSIVE
    assert "No accepted diagnosis" in text(gd.root_cause_dashboard(rep))
    body = text(gd.verification_dashboard(rep))
    assert "No action was executed, so recovery was not verified" in body and "remediation was not requested" in body


def test_untrusted_report_text_cannot_inject_html_or_links(tmp_path):
    rep = verified(tmp_path)
    evil = "<script>alert(1)</script> [click](http://evil.example) http://evil.example/x www.evil.example a@evil.example"
    logs = {"value": {"lines": [{"body": evil}], "count": 1, "min_severity": "WARN"}, "message": evil}
    rep = rep.model_copy(update={"diagnosis": rep.diagnosis.model_copy(update={"suspected_cause": evil, "uncertainty": evil}),
                                 "incident": rep.incident.model_copy(update={"symptom": evil}),
                                 "evidence": [e.model_copy(update={"observed_value": logs}) if e.tool == "query_logs" else e
                                              for e in rep.evidence]})
    assert any(evil in json.dumps(e.observed_value) for e in rep.evidence)
    for d in gd.build_all([rep]):
        body = rendered(d)
        assert "<script>" not in body and "](http://evil" not in body
        assert "http://evil" not in body and "www.evil" not in body and "a@evil" not in body
    assert "&lt;script&gt;" in text(gd.root_cause_dashboard(rep))


def test_uids_are_valid_and_distinct_for_awkward_ids():
    ids = ["INC 1/../x", "INC-1", "inc-1", "a" * 80, "a" * 81]
    uids = [u for i in ids for u in gd.dashboard_uids(i)]
    assert all(UID.match(u) for u in uids) and len(set(uids)) == len(uids)
    assert gd.dashboard_uids("LIVE-2-PAYMENT") == ("iz-LIVE-2-PAYMENT-rc", "iz-LIVE-2-PAYMENT-ver")


def test_index_lists_newest_first_with_both_links(tmp_path):
    rep = verified(tmp_path)
    older = rep.model_copy(update={"incident_id": "OLDER", "incident": rep.incident.model_copy(
        update={"id": "OLDER", "detection_time": rep.incident.detection_time - timedelta(hours=1)})})
    body = text(gd.index_dashboard([older, rep]))
    assert body.index(rep.incident_id.replace("-", "\\-")) < body.index("OLDER")
    for r in (rep, older):
        rc, ver = gd.dashboard_uids(r.incident_id)
        assert f"[Root cause](/grafana/d/{rc}) · [Verification](/grafana/d/{ver})" in body
    assert "Faults by root-cause component" in body and gd.code("payment", 40) in body


def ev(tool, query, service=None, source="prometheus", value=None, minutes=5):
    return Evidence(evidence_id=f"ev-{tool}-00000001", status=ToolStatus.OK, source=source, service=service, tool=tool,
                    event_time_window=TimeWindow(start_time=NOW - timedelta(minutes=minutes), end_time=NOW), query=query,
                    observed_value={"value": value})


def test_promql_is_rebuilt_for_every_stored_query_format():
    from endpoints import promql_of

    assert promql_of(ev("overview", json.dumps({"request_rate": "sum(x)", "p95_ms": "q2"}))) == [("request_rate", "sum(x)"), ("p95_ms", "q2")]
    assert promql_of(ev("resource_overview", "cpu: a; mem: b{c=\"d\"}")) == [("cpu", "a"), ("mem", 'b{c="d"}')]
    assert promql_of(ev("query_metrics", "sum(y)", "ad", value={"metric": "request_rate"})) == [("request_rate", "sum(y)")]
    health = promql_of(ev("service_health", "request_rate, error_rate, bogus", "ad", source="derived", minutes=3))
    assert [n for n, _ in health] == ["request_rate", "error_rate"] and all('service_name="ad"' in e and "[3m]" in e for _, e in health)
    assert promql_of(ev("query_logs", "source=otel-logs-*", "ad", source="opensearch")) == []


def test_publisher_creates_the_folder_and_overwrites_by_uid():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        if request.url.path.endswith("/api/folders/incident-zero"):
            return httpx.Response(404 if len(seen) == 1 else 200, json={})
        return httpx.Response(200, json={"status": "success"})

    pub = gd.GrafanaPublisher("http://grafana.test:3000", token="", transport=httpx.MockTransport(handler))
    assert pub.put({"uid": "iz-x-rc", "title": "x"}) == "http://grafana.test:3000/grafana/d/iz-x-rc"
    pub.put({"uid": "iz-x-ver", "title": "y"})
    assert [(m, p) for m, p, _ in seen] == [("GET", "/grafana/api/folders/incident-zero"), ("POST", "/grafana/api/folders"),
                                             ("POST", "/grafana/api/dashboards/db"), ("POST", "/grafana/api/dashboards/db")]
    body = seen[2][2]
    assert body["overwrite"] is True and body["folderUid"] == gd.FOLDER_UID and body["dashboard"]["uid"] == "iz-x-rc"


def test_publisher_failures_raise_grafana_error():
    def refuse(request):
        return httpx.Response(200 if "folders" in request.url.path else 500, json={}, text=None)

    with pytest.raises(gd.GrafanaError, match="HTTP 500"):
        gd.GrafanaPublisher("http://g.test/grafana", transport=httpx.MockTransport(refuse)).put({"uid": "u"})

    def down(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(gd.GrafanaError, match="unreachable"):
        gd.GrafanaPublisher("http://g.test", transport=httpx.MockTransport(down)).put({"uid": "u"})


def test_cli_export_writes_index_and_both_dashboards_per_report(tmp_path):
    rep = verified(tmp_path)
    runs = tmp_path / "runs"
    write_report(rep, runs / rep.incident_id)
    (runs / "BROKEN").mkdir()
    (runs / "BROKEN" / "BROKEN_report.json").write_text("{}")
    assert cli.main(["grafana", "export", str(tmp_path / "out"), "--out", str(runs)]) == 0
    rc, ver = gd.dashboard_uids(rep.incident_id)
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == sorted([f"{gd.INDEX_UID}.json", f"{rc}.json", f"{ver}.json"])
    assert gd.load_reports(runs)[1] and "BROKEN" in gd.load_reports(runs)[1][0]


# ---- navigation hardening: Explore deep links, Action API request, trace details
from urllib.parse import parse_qs, urlparse


def _pane(url):
    q = parse_qs(urlparse(url).query)
    return json.loads(q["panes"][0])["iz"]


def test_explore_url_carries_exact_query_and_incident_range(tmp_path):
    rep = verified(tmp_path)
    tr = gd._time_range(rep)
    url = gd.explore_url(gd.PROM, [{"expr": 'sum(rate(x{service_name="payment"}[2m]))'}], tr)
    pane = _pane(url)
    assert url.startswith("/grafana/explore?") and pane["datasource"] == "webstore-metrics"
    assert pane["queries"][0]["expr"] == 'sum(rate(x{service_name="payment"}[2m]))'
    assert int(pane["range"]["from"]) < int(pane["range"]["to"])


def test_evidence_rows_open_their_own_metric_log_and_trace_queries(tmp_path):
    rep = verified(tmp_path)
    body = text(gd.root_cause_dashboard(rep))
    links = re.findall(r"\[Explore [^\]]*\]\((/grafana/explore\?[^)]*)\)", body)
    sources = {_pane(u)["datasource"] for u in links}
    assert {"webstore-logs"} <= sources  # the cited payment logs open in Explore
    log_pane = next(_pane(u) for u in links if _pane(u)["datasource"] == "webstore-logs")
    assert "resource.service.name = 'payment'" in log_pane["queries"][0]["query"]


def test_verification_page_shows_exact_action_api_request(tmp_path):
    from action_api_models import ActionPayload
    rep = verified(tmp_path)
    body = text(gd.verification_dashboard(rep))
    payload = json.loads(re.search(r"```json\n(.*?)\n```", body, re.S).group(1))
    ActionPayload.model_validate(payload)
    assert payload["operation"] == "restart_service" and payload["target"] == "payment"
    assert "valid against `schemas/action_api/ActionPayload.schema.json`" in body


def test_action_request_cannot_break_out_of_the_code_block(tmp_path):
    rep = verified(tmp_path)
    p = rep.proposals[0].model_copy(update={"reason": "evil ``` [link](http://x) <script>alert(1)</script> reason"})
    body = gd.action_request_markdown(rep.model_copy(update={"proposals": [p]}))
    inner = re.search(r"```json\n(.*?)\n```", body, re.S).group(1)
    assert "`" not in inner and body.count("```") == 2


def test_index_row_links_to_every_view(tmp_path):
    rep = verified(tmp_path)
    body = text(gd.index_dashboard([rep]))
    for label in ("[Root cause]", "[Verification]", "[Action API request]", "[Metrics]", "[Traces]", "[Logs]"):
        assert label in body


def test_trace_summary_names_slowest_trace_and_first_error():
    ev = Evidence(evidence_id="ev-t-1", status=ToolStatus.OK, source="jaeger", service="checkout",
                  event_time_window=TimeWindow(start_time=NOW - timedelta(minutes=5), end_time=NOW), tool="search_traces",
                  query="q", observed_value={"value": {"traces_scanned": 3, "traces": [
                      {"trace_id": "a" * 32, "root": "frontend: POST /api/checkout", "duration_ms": 4100.0,
                       "error_spans": [{"error_message": "dial payment:50051: connection refused"}]},
                      {"trace_id": "b" * 32, "root": "frontend: GET /", "duration_ms": 12.0, "error_spans": []}]}})
    captured = gd._captured(ev)
    assert "slowest 4100" in captured and "frontend: POST /api/checkout" in captured and "connection refused" in captured


def test_index_links_symptom_logs_of_the_caller_when_component_differs(tmp_path):
    rep = verified(tmp_path)  # diagnosed payment, affected service checkout
    body = text(gd.index_dashboard([rep]))
    url = re.search(r"\[Symptom logs: checkout\]\((/grafana/explore\?[^)]*)\)", body).group(1)
    assert "resource.service.name = 'checkout'" in _pane(url)["queries"][0]["query"]
