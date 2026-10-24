"""Build Grafana dashboards from the saved reports (runs/<id>/<id>_report.json). Built-in Grafana only: the data
is embedded as CSV (TestData "CSV content"), and Grafana reloads dashboard files from grafana/dashboards/generated
automatically, so no plugin, API call or password is needed.

    python grafana_dashboards.py        # rebuild from all saved runs (the CLI also runs this after each investigation)

Step 1: "Incidents" table - one row per investigation (click an incident to open its page).
Step 2: one page per incident - diagnosis, every evidence item, the evidence picture, and the metrics of the
        services in its evidence around the incident window.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta, timezone
from pathlib import Path

import candidates
from models import IncidentReport
from report import _evidence_summary

PROM = {"type": "prometheus", "uid": "demo-prometheus"}
CONTEXT = timedelta(minutes=15)  # metrics shown before and after the incident window
TIMEZONE = "Asia/Kolkata"
IST = timezone(timedelta(hours=5, minutes=30), "IST")  # India has no daylight saving: a fixed offset is exact


def ist(dt, fmt: str) -> str:
    """Reports store UTC; pages show Indian Standard Time."""
    return dt.astimezone(IST).strftime(fmt)
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def page_uid(incident_id: str) -> str:
    """Grafana uids are at most 40 chars."""
    uid = f"agent-inc-{incident_id}".lower()
    return uid if len(uid) <= 40 else uid[:31] + hashlib.sha1(incident_id.encode()).hexdigest()[:8]

AGENT_DIR = Path(__file__).resolve().parent
RUNS = AGENT_DIR / "runs"
OUT = AGENT_DIR / "grafana" / "dashboards" / "generated"
TESTDATA = {"type": "grafana-testdata-datasource", "uid": "agent-testdata"}


def _cell(value) -> str:
    text = " ".join(str("" if value is None else value).split())  # one line
    return '"' + text.replace('"', '""') + '"'


def csv(rows: list[list], header: list[str]) -> str:
    return "\n".join(",".join(_cell(v) for v in row) for row in [header, *rows])


def load_reports(runs: Path = RUNS) -> list[IncidentReport]:
    reports = []
    for path in sorted(runs.glob("*/*_report.json")):
        try:
            reports.append(IncidentReport.model_validate_json(path.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            continue  # an older or partial run: skipped, never breaks the dashboard
    return sorted(reports, key=lambda r: r.incident.detection_time, reverse=True)


INCIDENT_COLUMNS = ["Time (IST)", "Incident", "Status", "Component", "Diagnosis", "Confidence", "Proposed fix",
                    "Evidence items", "Page"]


def incident_rows(reports: list[IncidentReport]) -> list[list]:
    rows = []
    for r in reports:
        d = r.diagnosis
        rows.append([ist(r.incident.detection_time, "%Y-%m-%d %H:%M:%S"), r.incident_id, r.status.value,
                     d.suspected_component if d else "-", d.suspected_cause if d else "no diagnosis accepted: escalate",
                     d.confidence_label.value if d else "-",
                     "; ".join(f"{p.runbook_id} {p.target_service}" for p in r.proposals) or "-",
                     len(r.evidence), page_uid(r.incident_id)])
    return rows


def _table(pid: int, title: str, content: str, grid: dict, overrides: list | None = None) -> dict:
    return {"id": pid, "type": "table", "title": title, "datasource": TESTDATA, "gridPos": grid,
            "targets": [{"refId": "A", "datasource": TESTDATA, "scenarioId": "csv_content", "csvContent": content}],
            "options": {"showHeader": True, "cellHeight": "sm"},
            "fieldConfig": {"defaults": {"custom": {"align": "left", "inspect": True}}, "overrides": overrides or []}}


def _graph(pid: int, title: str, expr: str, legend: str, unit: str, grid: dict) -> dict:
    return {"id": pid, "type": "timeseries", "title": title, "datasource": PROM, "gridPos": grid,
            "fieldConfig": {"defaults": {"unit": unit, "min": 0}, "overrides": []},
            "targets": [{"refId": "A", "datasource": PROM, "expr": expr, "legendFormat": legend}]}


def incident_services(report: IncidentReport) -> list[str]:
    """Services this incident's evidence is about (queried services + ranked candidates)."""
    names = {e.service for e in report.evidence if e.service}
    names |= {c.service for c in candidates.rank(report.evidence)}
    return sorted(n for n in names if _SAFE_NAME.match(n))


def diagnosis_rows(r: IncidentReport) -> list[list]:
    d, meta = r.diagnosis, (r.timeline[-1].metadata if r.timeline else {})
    w = r.incident.observation_window
    rows = [["Incident", r.incident_id], ["Status", r.status.value], ["Alert", r.incident.symptom],
            ["Window (IST)", f"{ist(w.start_time, '%Y-%m-%d %H:%M:%S')} - {ist(w.end_time, '%H:%M:%S')}"]]
    if d:
        rows += [["Component", d.suspected_component], ["Confidence", d.confidence_label.value],
                 ["Observed / likely cause", d.suspected_cause], ["Still unknown", d.uncertainty],
                 ["Alternatives", "; ".join(d.alternatives) or "-"], ["Next step", d.next_step],
                 ["Supporting evidence", ", ".join(d.supporting_evidence_ids)],
                 ["Conflicting evidence", ", ".join(d.conflicting_evidence_ids) or "-"]]
    else:
        rows.append(["Diagnosis", "none accepted: escalate to a human"])
    for p in r.proposals:  # step 4: proposed only, awaiting human approval, never executed by the agent
        rows += [["Proposed fix", f"{p.runbook_id}: {p.action} {p.target_service} (awaiting human approval; not executed)"],
                 ["Fix reason", p.reason], ["Fix risk", p.risk], ["Fix verification", p.verification_plan]]
    if d and not r.proposals:
        rows.append(["Proposed fix", "none (no allowlisted remedy fits, or confidence too low: escalate)"])
    rows += [["Run", f"{meta.get('model_calls')} model calls, {meta.get('tool_calls')} tool calls, "
                     f"{meta.get('elapsed_s')} s, ended: {meta.get('terminal_reason')}"]]
    rows += [["Limitation", lim] for lim in r.limitations]
    return rows


EVIDENCE_COLUMNS = ["Evidence ID", "Cited", "Tool", "Service", "Status", "Source", "Window (IST)", "Key fact", "Message"]


def evidence_rows(r: IncidentReport) -> list[list]:
    d = r.diagnosis
    supporting = set(d.supporting_evidence_ids) if d else set()
    conflicting = set(d.conflicting_evidence_ids) if d else set()
    rows = []
    for e in r.evidence:
        cited = "supporting" if e.evidence_id in supporting else "conflicting" if e.evidence_id in conflicting else ""
        w = e.event_time_window
        rows.append([e.evidence_id, cited, e.tool, e.service or "fleet", e.status.value, e.source,
                     f"{ist(w.start_time, '%H:%M:%S')}-{ist(w.end_time, '%H:%M:%S')}", _evidence_summary(e.observed_value, 300),
                     (e.observed_value or {}).get("message") or ""])
    return rows


def incident_dashboard(r: IncidentReport) -> dict:
    w = r.incident.observation_window
    services = incident_services(r)
    svc = "|".join(services) or ".*"
    picture = candidates.summary(r.evidence) or "(no candidates: the evidence shows no clear change)"
    status_colors = {"type": "value", "options": {"ok": {"color": "green"}, "empty": {"color": "yellow"},
                                                   "error": {"color": "red"}, "unavailable": {"color": "red"},
                                                   "supporting": {"color": "green"}, "conflicting": {"color": "orange"}}}
    color_text = [{"id": "custom.cellOptions", "value": {"type": "color-text"}}, {"id": "mappings", "value": [status_colors]}]
    panels = [
        _table(1, "Diagnosis", csv(diagnosis_rows(r), ["Field", "Value"]), {"x": 0, "y": 0, "w": 12, "h": 12},
               [{"matcher": {"id": "byName", "options": "Field"}, "properties": [{"id": "custom.width", "value": 190}]}]),
        {"id": 2, "type": "text", "title": "Evidence picture (computed by code from this run's evidence)",
         "gridPos": {"x": 12, "y": 0, "w": 12, "h": 12},
         "options": {"mode": "markdown", "content": "```\n" + picture.replace("```", "'''") + "\n```"}},
        _table(3, f"Evidence ({len(r.evidence)} items, in the order collected)",
               csv(evidence_rows(r), EVIDENCE_COLUMNS), {"x": 0, "y": 12, "w": 24, "h": 12},
               [{"matcher": {"id": "byName", "options": "Key fact"}, "properties": [{"id": "custom.width", "value": 600}]},
                {"matcher": {"id": "byName", "options": "Status"}, "properties": color_text},
                {"matcher": {"id": "byName", "options": "Cited"}, "properties": color_text}]),
        {"id": 4, "type": "row", "title": f"Metrics around the incident for: {', '.join(services) or 'all services'}",
         "gridPos": {"x": 0, "y": 24, "w": 24, "h": 1}, "collapsed": False, "panels": []},
        _graph(5, "Error rate (3 min)",
               f'sum by (service_name) (rate(traces_span_metrics_calls_total{{status_code="STATUS_CODE_ERROR",service_name=~"{svc}"}}[3m]))'
               f' / sum by (service_name) (rate(traces_span_metrics_calls_total{{service_name=~"{svc}"}}[3m]))',
               "{{service_name}}", "percentunit", {"x": 0, "y": 25, "w": 12, "h": 8}),
        _graph(6, "p95 latency (3 min)",
               f'histogram_quantile(0.95, sum by (le, service_name) (rate(traces_span_metrics_duration_milliseconds_bucket{{service_name=~"{svc}"}}[3m])))',
               "{{service_name}}", "ms", {"x": 12, "y": 25, "w": 12, "h": 8}),
        _graph(7, "Container CPU (% of one core)",
               f'rate(container_cpu_usage_nanoseconds_total{{container_name=~"{svc}"}}[3m]) / 1e7',
               "{{container_name}}", "percent", {"x": 0, "y": 33, "w": 8, "h": 8}),
        _graph(8, "Container memory (% of limit)", f'container_memory_percent_ratio{{container_name=~"{svc}"}}',
               "{{container_name}}", "percent", {"x": 8, "y": 33, "w": 8, "h": 8}),
        _graph(9, "Container restarts (last 30 min)",
               f'resets(container_uptime_seconds{{container_name=~"{svc}"}}[30m])',
               "{{container_name}}", "short", {"x": 16, "y": 33, "w": 8, "h": 8}),
    ]
    return {
        "uid": page_uid(r.incident_id), "title": f"Incident {r.incident_id}", "tags": ["incident-agent", "incident"],
        "schemaVersion": 39, "editable": False, "graphTooltip": 1, "timezone": TIMEZONE,
        "time": {"from": (w.start_time - CONTEXT).isoformat(), "to": (w.end_time + CONTEXT).isoformat()},
        "links": [{"title": "All incidents", "type": "link", "url": "/d/agent-incidents"}],
        "annotations": {"list": [{"name": "Incident window", "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                                  "enable": True, "iconColor": "red",
                                  "target": {"type": "tags", "tags": ["incident-agent"], "limit": 100}}]},
        "panels": panels,
    }


def incidents_dashboard(reports: list[IncidentReport]) -> dict:
    return {
        "uid": "agent-incidents", "title": "Incident Agent - Incidents", "tags": ["incident-agent"],
        "schemaVersion": 39, "editable": False, "timezone": TIMEZONE, "refresh": "10s", "time": {"from": "now-24h", "to": "now"},
        "panels": [{
            "id": 1, "type": "table", "title": f"Investigations ({len(reports)}), newest first - click an incident ID to open it",
            "datasource": TESTDATA, "gridPos": {"x": 0, "y": 0, "w": 24, "h": 20},
            "targets": [{"refId": "A", "datasource": TESTDATA, "scenarioId": "csv_content",
                         "csvContent": csv(incident_rows(reports), INCIDENT_COLUMNS)}],
            "options": {"showHeader": True, "cellHeight": "sm"},
            "fieldConfig": {"defaults": {"custom": {"align": "left", "cellOptions": {"type": "auto"},
                                                    "inspect": True}},
                            "overrides": [
                                {"matcher": {"id": "byName", "options": "Incident"},
                                 "properties": [{"id": "links", "value": [{
                                     "title": "Open this incident: diagnosis, evidence, metrics",
                                     "url": "/d/${__data.fields.Page}"}]}]},
                                {"matcher": {"id": "byName", "options": "Page"},
                                 "properties": [{"id": "custom.hidden", "value": True}]},
                                {"matcher": {"id": "byName", "options": "Diagnosis"},
                                 "properties": [{"id": "custom.width", "value": 700}]},
                                {"matcher": {"id": "byName", "options": "Confidence"},
                                 "properties": [{"id": "custom.cellOptions", "value": {"type": "color-text"}},
                                                {"id": "mappings", "value": [{"type": "value", "options": {
                                                    "high": {"color": "green"}, "medium": {"color": "orange"},
                                                    "low": {"color": "red"}}}]}]},
                            ]},
        }],
    }


def build(runs: Path = RUNS, out: Path = OUT) -> str:
    reports = load_reports(runs)
    out.mkdir(parents=True, exist_ok=True)
    (out / "incidents.json").write_text(json.dumps(incidents_dashboard(reports), indent=1), encoding="utf-8")
    pages = out / "incidents"
    pages.mkdir(exist_ok=True)
    wanted = {f"{page_uid(r.incident_id)}.json" for r in reports}
    for old in pages.glob("*.json"):
        if old.name not in wanted:
            old.unlink()  # report removed: its page goes too
    for r in reports:
        (pages / f"{page_uid(r.incident_id)}.json").write_text(json.dumps(incident_dashboard(r), indent=1),
                                                               encoding="utf-8")
    return f"Grafana dashboards rebuilt from {len(reports)} report(s): http://localhost:3001/d/agent-incidents"


if __name__ == "__main__":
    from grafana_publish import reload_dashboards

    print(build())
    print(reload_dashboards())
