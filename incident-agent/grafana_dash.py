"""Grafana dashboards built from saved incident reports and published through the Grafana HTTP API.

Each incident gets two linked dashboards on the same time range:
- Root cause: the accepted diagnosis, every cited evidence item with its captured value, and live re-queries of the
  same signals. Fleet-wide panels highlight the diagnosed component.
- Verification: the action, the human approval, and each recovery check against its pre-action baseline, with its bound
  drawn as a threshold. It also shows the component's current state.
An index dashboard lists every incident and links to both pages.

The report's captured values are the record. Panels only re-run read-only queries, so trace and log panels of older
incidents can be empty after a stack restart (Jaeger and OpenSearch keep no data across it). Event markers are
Prometheus expressions over the report's timestamps, so nothing besides the dashboards is stored in Grafana.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
from pydantic import ValidationError

from action_api_models import printable
from endpoints import promql_of
from models import Evidence, IncidentReport, IncidentStatus, ToolStatus
from safety import VERIFICATION_METRICS, VerificationCheck
from telemetry import METRIC_TEMPLATES
from verify import IMPROVEMENT

PROM = {"type": "prometheus", "uid": "webstore-metrics"}
JAEGER = {"type": "jaeger", "uid": "webstore-traces"}
LOGS = {"type": "grafana-opensearch-datasource", "uid": "webstore-logs"}
FOLDER_UID, FOLDER_TITLE = "incident-zero", "Incident Zero"
INDEX_UID = "iz-index"
TAG = "incident-zero"
JAEGER_UI = "http://localhost:8080/jaeger/ui"  # frontend-proxy, as in the Prometheus datasource's exemplar links
APM_UID = "febljk0a32qyoa"
VERIFY_WINDOW_MIN = 2  # ResponseConfig.window_minutes, the window verification queried with
MAX_EVIDENCE_PANELS = 36
MAX_TRACE_PANELS = 4
SEVERITY = {"DEBUG": 5, "INFO": 9, "WARN": 13, "ERROR": 17, "FATAL": 21}

_UID_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")  # Grafana uids allow only letters, digits, '-' and '_'
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+!|~>.:@-])")  # markdown punctuation that could format text or form a link
_SERVICE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")  # a plain service name: safe in URLs, regexes and queries
_TRACE_ID = re.compile(r"^[0-9a-f]{16,32}$")
_LINK_TARGET = re.compile(r"\]\([^)]*\)")  # the "](url)" part of a markdown link, which is not rendered as text
_CELL = re.compile(r"(?<!\\)\|")  # a table cell separator: a pipe not escaped with a backslash
# verify.judge writes "<service>.<metric>: <state> (baseline <value|None>, after [<values>], target ..." per check
_CHECK = re.compile(r"([A-Za-z0-9_-]+)\.(" + "|".join(VERIFICATION_METRICS)
                    + r"): ([a-z_]+) \(baseline ([^,]+), after \[([^\]]*)\]")
_UNITS = {
    "request_rate": "reqps", "error_rate": "percentunit", "errors_by_operation": "reqps", "p95_latency_ms": "ms",
    "p95_latency_by_operation": "ms", "p95_ms": "ms", "container_cpu": "percent", "cpu": "percent",
    "cpu_prior": "percent", "mem": "percent", "mem_growth": "percentunit", "container_memory_trend": "Bps",
    "container_uptime": "s", "uptime": "s", "container_last_seen_age_s": "s",
}
_HEALTH_PANELS = ("error_rate", "request_rate", "p95_latency_ms", "container_cpu", "container_memory",
                  "container_last_seen_age_s")
_NOW_PANELS = ("error_rate", "request_rate", "container_last_seen_age_s", "container_cpu")


class GrafanaError(RuntimeError):
    pass


# ---------------------------------------------------------------- text helpers (report text is untrusted)
def md(text: Any, limit: int = 600) -> str:
    """Untrusted text as inert markdown: one line, no HTML, no formatting, no links."""
    s = printable(text, limit) + ("…" if len(str(text)) > limit else "")
    return _MD_SPECIAL.sub(r"\\\1", html.escape(s, quote=False))


def code(text: Any, limit: int = 120) -> str:
    return "`" + printable(text, limit).replace("`", "'").replace("|", "\\|") + "`"


def _num(v: Any) -> str:
    if v is None:
        return "no data"
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, (int, float)):
        return f"{v:.4g}"
    return str(v)


def _utc(dt: datetime, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return dt.astimezone(timezone.utc).strftime(fmt)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _service(name: Optional[str]) -> Optional[str]:
    return name if name and _SERVICE.match(name) else None


def dashboard_uids(incident_id: str) -> tuple[str, str]:
    slug = _UID_UNSAFE.sub("-", incident_id)
    if slug != incident_id or len(slug) > 26:
        slug = f"{slug[:19]}-{hashlib.sha256(incident_id.encode()).hexdigest()[:6]}"
    return f"iz-{slug}-rc", f"iz-{slug}-ver"


def _component(rep: IncidentReport) -> Optional[str]:
    return _service(rep.diagnosis.suspected_component if rep.diagnosis else None) or _service(rep.incident.affected_service)


# ---------------------------------------------------------------- layout and panels
class _Grid:
    def __init__(self) -> None:
        self.panels: list[dict] = []
        self.x = self.y = self.h = 0
        self.ids = 0
        self.row: Optional[dict] = None

    def _id(self) -> int:
        self.ids += 1
        return self.ids

    def _newline(self) -> None:
        if self.x:
            self.y, self.x, self.h = self.y + self.h, 0, 0

    def add(self, panel: dict, w: int = 24, h: int = 8) -> None:
        if self.x + w > 24:
            self._newline()
        panel.update(id=self._id(), gridPos={"h": h, "w": w, "x": self.x, "y": self.y})
        self.x, self.h = self.x + w, max(self.h, h)
        (self.row["panels"] if self.row and self.row["collapsed"] else self.panels).append(panel)

    def section(self, title: str, collapsed: bool = False) -> None:
        self._newline()
        self.row = {"type": "row", "title": title, "collapsed": collapsed, "panels": [], "id": self._id(),
                    "gridPos": {"h": 1, "w": 24, "x": 0, "y": self.y}}
        self.panels.append(self.row)
        self.y += 1


def _text(title: str, content: str) -> dict:
    return {"type": "text", "title": title, "options": {"mode": "markdown", "content": content}}


def _visible(text: str) -> int:
    return len(_LINK_TARGET.sub("]", text).replace("\\", "").replace("`", ""))


def _text_h(content: str, wrap: int = 140) -> int:
    """Grid units (38 px each) for a markdown text panel; `wrap` is the characters per line of its widest table column."""
    px = 60
    for line in content.splitlines():
        if line.startswith("| ---"):
            continue
        if line.startswith("|"):
            cells = _CELL.split(line.strip().strip("|"))
            px += 16 + 20 * max(1, max(math.ceil(_visible(c) / wrap) for c in cells))
        elif line.startswith("#"):
            px += 40
        elif not line.strip():
            px += 12
        else:
            px += 22 * max(1, math.ceil(_visible(line) / 170))
    return max(3, min(60, math.ceil(px / 38)))


def _legend(expr: str, fallback: str) -> str:
    for label in ("service_name", "container_name", "span_name"):
        if f"by ({label})" in expr or f"by (le, {label})" in expr:
            return "{{" + label + "}}"
    if expr.startswith("{__name__"):
        return "{{__name__}}"
    if expr.startswith("container_") or "(container_" in expr:
        return "{{container_name}}"
    return fallback


def _timeseries(title: str, exprs: list[tuple[str, str]], unit: str = "short", description: str = "",
                highlight: Optional[str] = None, threshold: Optional[tuple[str, float]] = None,
                links: Optional[list[dict]] = None) -> dict:
    custom: dict[str, Any] = {"drawStyle": "line", "lineWidth": 1, "fillOpacity": 0, "showPoints": "never", "spanNulls": False}
    defaults: dict[str, Any] = {"unit": unit, "color": {"mode": "palette-classic"}, "custom": custom}
    if unit == "percentunit":  # Grafana scales an all-zero ratio series to 0..10000%
        custom.update(axisSoftMin=0, axisSoftMax=0.05)
    if threshold:
        kind, value = threshold
        bad, good = {"color": "red", "value": value}, {"color": "green", "value": value}
        defaults["thresholds"] = {"mode": "absolute", "steps": [{"color": "green", "value": None}, bad] if kind == "max"
                                  else [{"color": "red", "value": None}, good]}
        custom["thresholdsStyle"] = {"mode": "dashed+area"}
    overrides = []
    if highlight:
        overrides.append({"matcher": {"id": "byRegexp", "options": f"^{highlight}$"},
                          "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}},
                                         {"id": "custom.lineWidth", "value": 3}]})
    panel = {"type": "timeseries", "title": title, "description": description, "datasource": PROM,
             "targets": [{"refId": chr(65 + i), "datasource": PROM, "expr": expr, "legendFormat": legend, "range": True}
                         for i, (legend, expr) in enumerate(exprs)],
             "fieldConfig": {"defaults": defaults, "overrides": overrides},
             "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                         "tooltip": {"mode": "multi", "sort": "desc"}}}
    if links:
        panel["links"] = links
    return panel


def _template(metric: str, service: str, window: int) -> str:
    return METRIC_TEMPLATES[metric][0].replace("{s}", service).replace("{w}", str(window))


def _link(title: str, url: str, icon: str = "dashboard", keep_time: bool = True, blank: bool = False) -> dict:
    return {"title": title, "type": "link", "url": url, "icon": icon, "keepTime": keep_time, "targetBlank": blank,
            "includeVars": False, "asDropdown": False, "tags": [], "tooltip": ""}


def _marker(name: str, when: datetime, color: str) -> dict:
    t = int(when.timestamp())
    return {"name": name, "datasource": PROM, "enable": True, "hide": False, "iconColor": color,
            "expr": f"vector(1) and on() ((vector(time()) >= {t}) < {t + 60})", "step": "60s",
            "titleFormat": name, "textFormat": f"{name} at {_utc(when, '%H:%M:%S')} UTC", "tagKeys": "",
            "useValueForTime": False}


# ---------------------------------------------------------------- Explore deep links
def _ms(iso: str) -> str:
    return str(int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000))


def explore_url(ds: dict, targets: list[dict], time_range: dict) -> str:
    """Grafana Explore with the exact queries and the incident's time range (opens in the demo's own datasources)."""
    pane = {"datasource": ds["uid"], "queries": [{"refId": chr(65 + i), "datasource": ds, **t} for i, t in enumerate(targets)],
            "range": {"from": _ms(time_range["from"]), "to": _ms(time_range["to"])}}
    return "/grafana/explore?" + urlencode({"schemaVersion": 1, "orgId": 1, "panes": json.dumps({"iz": pane}, separators=(",", ":"))})


def _logs_target(svc: str, sev: int) -> dict:
    ppl = (f"source=otel-logs-* | where resource.service.name = '{svc}' and `severity.number` >= {sev}"
           " | sort - `@timestamp` | fields `@timestamp`, severity.text, body, traceId")
    return {"queryType": "PPL", "format": "table", "query": ppl, "timeField": "observedTimestamp",
            "metrics": [{"id": "1", "type": "logs"}], "bucketAggs": []}


def _explore_links(ev: Evidence, time_range: Optional[dict]) -> list[str]:
    """Markdown links that open this evidence item's own query in Grafana Explore."""
    if not time_range:
        return []
    svc = _service(ev.service)
    if ev.source in ("prometheus", "derived"):
        return [f"[Explore {md(name, 40)}]({explore_url(PROM, [{'expr': expr}], time_range)})" for name, expr in promql_of(ev)[:2]]
    if ev.source == "opensearch" and svc:
        sev = SEVERITY.get(str(_value(ev).get("min_severity", "WARN")).upper(), 13)
        return [f"[Explore logs]({explore_url(LOGS, [_logs_target(svc, sev)], time_range)})"]
    if ev.source == "jaeger":
        return [f"[Explore trace {n}]({explore_url(JAEGER, [{'query': t}], time_range)})" for n, t in enumerate(_trace_ids(ev)[:2], 1)]
    if ev.source == "health" and svc:
        return [f"[Explore availability]({explore_url(PROM, [{'expr': _template('container_last_seen_age_s', svc, 5)}], time_range)})"]
    return []


# ---------------------------------------------------------------- report facts
def _captured(ev: Evidence) -> str:
    ov = ev.observed_value if isinstance(ev.observed_value, dict) else {"value": ev.observed_value}
    v, msg = ov.get("value"), ov.get("message")
    if ev.status != ToolStatus.OK:
        return f"{ev.status.value}: {msg or 'no message'}"
    if not isinstance(v, dict):
        return json.dumps(v, default=str)
    if ev.tool == "overview":
        rows = v.get("services") or []
        return "; ".join(f"{r.get('service')} err {_num(r.get('error_rate'))} p95 {_num(r.get('p95_ms'))} ms "
                         f"{_num(r.get('request_rate'))} req/s" for r in rows[:4])
    if ev.tool == "resource_overview":
        flagged = v.get("flagged") or []
        return ("flagged: " + "; ".join(f"{f.get('container')}: {', '.join(map(str, f.get('reasons') or []))}" for f in flagged)
                if flagged else "no container flagged") + (f" ({v['telemetry_silence']})" if v.get("telemetry_silence") else "")
    if ev.tool == "service_health":
        s = v.get("summary") or {}
        return ", ".join(f"{k} {_num(x[0].get('value') if isinstance(x, list) and x and isinstance(x[0], dict) else x)}"
                         for k, x in s.items())
    if ev.tool == "search_traces":
        traces = [t for t in v.get("traces") or [] if isinstance(t, dict)]
        slow = max(traces, key=lambda t: t.get("duration_ms") or 0, default=None)
        first_err = next((sp.get("error_message") for t in traces for sp in t.get("error_spans") or []
                          if isinstance(sp, dict) and sp.get("error_message")), None)
        return (f"{v.get('traces_scanned', '?')} traces; error origin {v.get('error_origin_counts') or {}}; "
                f"edges {dict(list((v.get('edges') or {}).items())[:4])}"
                + (f"; slowest {_num(slow.get('duration_ms'))} ms ({slow.get('root')})" if slow and slow.get("duration_ms") else "")
                + (f"; first error: {first_err}" if first_err else ""))
    if ev.tool == "query_logs":
        lines = v.get("lines") or []
        return f"{v.get('count', len(lines))} lines at {v.get('min_severity', '?')}+" + (
            f"; latest: {lines[0].get('body')}" if lines else "")
    if ev.tool == "container_state":
        return "; ".join(f"{r.get('service')} {r.get('state')} health {r.get('health')} restarts {r.get('restart_count')}"
                         for r in v.get("services") or [])
    if "metric" in v:
        series = v.get("series")
        if series:
            return f"{v['metric']}: " + ", ".join(
                f"{next((str(x) for k, x in s.items() if k != 'value'), '')} {_num(s.get('value'))}" for s in series[:5])
        return f"{v['metric']} = {_num(v.get('value'))} {v.get('unit', '')}".strip()
    return json.dumps(v, default=str)


def _value(ev: Evidence) -> dict:
    v = ev.observed_value.get("value") if isinstance(ev.observed_value, dict) else None
    return v if isinstance(v, dict) else {}


def _trace_ids(ev: Evidence) -> list[str]:
    ids = list(ev.trace_ids)
    errs = [t.get("trace_id") for t in _value(ev).get("traces") or [] if isinstance(t, dict) and t.get("error_spans")]
    ids = [t for t in errs if t in ids] + [t for t in ids if t not in errs]  # traces with error spans first
    return [t for t in ids if isinstance(t, str) and _TRACE_ID.match(t)]


def _moments(rep: IncidentReport) -> list[tuple[str, datetime, str]]:
    out = [("Detected", rep.incident.detection_time, "orange")]
    for t in rep.timeline:
        if t.description.startswith("Diagnosis validated"):
            out.append(("Diagnosis accepted", t.timestamp, "purple"))
        elif t.description == "Baseline captured":
            out.append(("Baseline captured", t.timestamp, "blue"))
        elif t.status == IncidentStatus.APPROVED and t.description.startswith("Approved"):
            out.append(("Approved", t.timestamp, "green"))
        elif t.status == IncidentStatus.REJECTED:
            out.append(("Approval rejected", t.timestamp, "red"))
    if rep.approval and rep.approval.dispatch_time:
        out.append(("Action dispatched", rep.approval.dispatch_time, "red"))
    if rep.verification:
        out.append(("Verification judged", rep.verification.verification_time, "green"))
    return out


def _time_range(rep: IncidentReport) -> dict:
    detected = rep.incident.detection_time
    times = [rep.incident.observation_window.start_time, detected] + [t.timestamp for t in rep.timeline]
    for e in rep.evidence:
        times += [e.event_time_window.start_time, e.collection_time]
    if rep.verification:
        times.append(rep.verification.verification_time)
    start = max(min(times), detected - timedelta(hours=3)) - timedelta(minutes=5)
    return {"from": _iso(start), "to": _iso(max(times) + timedelta(minutes=10))}


def _baseline(rep: IncidentReport) -> tuple[dict[str, Any], list[str]]:
    for t in rep.timeline:
        if t.description == "Baseline captured":
            return dict(t.metadata.get("baseline") or {}), list(t.metadata.get("evidence") or [])
    return {}, []


def _float(text: str) -> Optional[float]:
    try:
        return float(text)
    except ValueError:  # "None": no data in that sample
        return None


def _bound(c: VerificationCheck, baseline: Optional[float]) -> tuple[str, Optional[float], str]:
    """The same bound verify.judge applies, as text, value (None when relative without a baseline) and direction."""
    kind, default, *_ = VERIFICATION_METRICS[c.metric]
    if c.max_value is not None:
        return f"<= {c.max_value:g}", c.max_value, "max"
    if c.min_value is not None:
        return f">= {c.min_value:g}", c.min_value, "min"
    if default is not None:
        return f"{'<=' if kind == 'max' else '>='} {default:g}", default, kind
    if not isinstance(baseline, (int, float)):
        return f"{IMPROVEMENT:.0%} of baseline", None, kind
    value = baseline * IMPROVEMENT if kind == "max" else baseline / IMPROVEMENT
    return f"{IMPROVEMENT:.0%} of baseline ({value:.4g})", value, kind


def verification_checks(rep: IncidentReport) -> list[dict[str, Any]]:
    """Per check: bound, baseline, post-action samples and the judged state, rebuilt from the report."""
    if not rep.proposals:
        return []
    baseline, _ = _baseline(rep)
    judged = {(s, m): (st, _float(b), [_float(x) for x in after.split(",") if x.strip()])
              for s, m, st, b, after in _CHECK.findall(rep.verification.details if rep.verification else "")}
    if rep.verification:
        pending = "not judged"
    elif rep.remediation_outcome == "DRY_RUN":
        pending = "not run (dry-run)"
    else:
        pending = "not run (no action executed)"
    out = []
    for raw in rep.proposals[0].verification_checks:
        try:
            c = VerificationCheck(**raw)
        except ValidationError:
            continue
        state, b, samples = judged.get((c.service, c.metric), (pending, baseline.get(f"{c.service}.{c.metric}"), []))
        text, value, kind = _bound(c, b)
        out.append({"check": c, "baseline": b, "samples": samples, "bound": text, "bound_value": value, "kind": kind,
                    "state": state})
    return out


# ---------------------------------------------------------------- dashboards
def _nav(rep: IncidentReport, here: str) -> str:
    rc, ver = dashboard_uids(rep.incident_id)
    return "**Navigate:** " + " · ".join([
        f"[All incidents](/grafana/d/{INDEX_UID})",
        "**Root cause** (this page)" if here == "rc" else f"[Root cause](/grafana/d/{rc})",
        "**Verification** (this page)" if here == "ver" else f"[Verification](/grafana/d/{ver})",
        f"[This page now (last 15 min)](/grafana/d/{rc if here == 'rc' else ver}?from=now-15m&to=now)",
    ])


def _links(rep: IncidentReport, time_range: dict, here: str) -> list[dict]:
    rc, ver = dashboard_uids(rep.incident_id)
    links = [_link("All incidents", f"/grafana/d/{INDEX_UID}", keep_time=False),
             _link("Root cause", f"/grafana/d/{rc}", "info"), _link("Verification", f"/grafana/d/{ver}", "info"),
             _link("Now (last 15 min)", f"/grafana/d/{rc if here == 'root-cause' else ver}?from=now-15m&to=now", "bolt", False)]
    comp = _component(rep)
    if comp:
        start, end = (int(datetime.fromisoformat(time_range[k].replace("Z", "+00:00")).timestamp() * 1e6) for k in ("from", "to"))
        links.append(_link(f"APM: {comp}", f"/grafana/d/{APM_UID}?var-service_name={comp}"))
        links.append(_link(f"Jaeger: {comp}", f"{JAEGER_UI}/search?" + urlencode(
            {"service": comp, "start": start, "end": end, "lookback": "custom", "limit": 20}), "external link", False, True))
    return links


def _dashboard(uid: str, title: str, grid: _Grid, rep: IncidentReport, kind: str, description: str) -> dict:
    time_range = _time_range(rep)
    builtin = {"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"}, "enable": True, "hide": True,
               "iconColor": "rgba(0, 211, 255, 1)", "name": "Annotations & Alerts", "type": "dashboard"}
    tags = [TAG, kind] + ([c] if (c := _component(rep)) else [])
    return {"uid": uid, "title": title, "description": description, "tags": tags, "timezone": "utc", "editable": True,
            "graphTooltip": 1, "schemaVersion": 39, "time": time_range, "refresh": "", "templating": {"list": []},
            "annotations": {"list": [builtin] + [_marker(n, t, c) for n, t, c in _moments(rep)]},
            "links": _links(rep, time_range, kind), "panels": grid.panels}


def _evidence_table(items: list[tuple[str, Evidence]], time_range: Optional[dict] = None) -> str:
    rows = ["| # | Evidence | Role | Tool | Service | Window (UTC) | Captured value (from the report) | Open |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for i, (role, ev) in enumerate(items, 1):
        traces = _trace_ids(ev)[:3]
        if traces:
            opener = " ".join(f"[trace {n}]({JAEGER_UI}/trace/{t})" for n, t in enumerate(traces, 1)) + " · waterfall in Cited traces"
        else:
            opener = {"prometheus": "panel below", "derived": "panels below", "opensearch": "log panel below",
                      "health": "metric panel below"}.get(ev.source, "-")
        opener = " · ".join([opener] + _explore_links(ev, time_range)) if opener != "-" else (" · ".join(_explore_links(ev, time_range)) or "-")
        window = f"{_utc(ev.event_time_window.start_time, '%H:%M')}–{_utc(ev.event_time_window.end_time, '%H:%M')}"
        rows.append(f"| {i} | {code(ev.evidence_id)} | {role} | {code(ev.tool)} | {code(ev.service or 'fleet')} | "
                    f"{window} | {md(_captured(ev), 260)} | {opener} |")
    return "\n".join(rows)


def _evidence_panels(grid: _Grid, ev: Evidence, comp: Optional[str], seen: set[str], budget: list[int]) -> None:
    """Live re-query panels for one evidence item, titled with its evidence ID."""
    def room() -> bool:
        budget[0] -= 1
        return budget[0] >= 0

    desc = f"Captured {_utc(ev.collection_time)} UTC: {md(_captured(ev), 400)}"
    svc = _service(ev.service)
    if ev.source in ("prometheus", "derived"):
        exprs = promql_of(ev)
        if ev.tool == "service_health":
            summary = _value(ev).get("summary") or {}
            exprs = [(n, e) for n, e in exprs if n in _HEALTH_PANELS and summary.get(n) not in ("empty", "unavailable", "error")]
        for name, expr in exprs:
            if expr in seen or not room():
                continue
            seen.add(expr)
            fleet = ev.service is None
            grid.add(_timeseries(f"{name} ({ev.service or 'fleet'}) · {ev.evidence_id}",
                                 [(_legend(expr, ev.service or name), expr)], _UNITS.get(name, "short"), desc,
                                 highlight=comp if fleet else None), 12 if fleet else 8, 8)
    elif ev.source == "opensearch" and svc and room():
        sev = SEVERITY.get(str(_value(ev).get("min_severity", "WARN")).upper(), 13)
        grid.add({"type": "table", "title": f"logs ({svc}) · {ev.evidence_id}", "description": desc, "datasource": LOGS,
                  "targets": [{"refId": "A", "datasource": LOGS, **_logs_target(svc, sev)}],
                  "options": {"showHeader": True, "cellHeight": "sm"}}, 24, 8)
    elif ev.source == "health" and svc:
        expr = _template("container_last_seen_age_s", svc, 5)
        if expr not in seen and room():
            seen.add(expr)
            grid.add(_timeseries(f"container_last_seen_age_s ({svc}) · {ev.evidence_id}", [(svc, expr)], "s",
                                 desc + " | Panel: the Prometheus view of the same container."), 8, 8)


def root_cause_dashboard(rep: IncidentReport) -> dict:
    rc, _ = dashboard_uids(rep.incident_id)
    comp, d, inc = _component(rep), rep.diagnosis, rep.incident
    lines = [_nav(rep, "rc"), ""]
    if d:
        lines += [f"## Root cause: {code(d.suspected_component)} · confidence {code(d.confidence_label.value)} (not calibrated)",
                  f"**Status** {code(rep.status.value)} · **Detected** {_utc(inc.detection_time)} UTC · "
                  f"**Observation window** {_utc(inc.observation_window.start_time, '%H:%M')}–"
                  f"{_utc(inc.observation_window.end_time, '%H:%M')} UTC · "
                  f"**Remediation** {code(rep.remediation_outcome or 'not requested')} (see Verification)", "",
                  f"**Suspected cause.** {md(d.suspected_cause, 1500)}", "",
                  f"**Uncertainty.** {md(d.uncertainty, 1000)}", ""]
        if d.alternatives:
            lines += ["**Alternatives considered.**", *[f"- {md(a, 300)}" for a in d.alternatives], ""]
        lines += [f"**Next step.** {md(d.next_step, 400)}", ""]
        if rep.proposals:
            pr, ver_uid = rep.proposals[0], dashboard_uids(rep.incident_id)[1]
            lines += [f"**Proposed Action API request.** {code(pr.action)} on {code(pr.target_service)} "
                      f"[open the exact request, approval and verification](/grafana/d/{ver_uid})", ""]
    else:
        lines += ["## No accepted diagnosis",
                  f"**Status** {code(rep.status.value)} · **Detected** {_utc(inc.detection_time)} UTC. The agent abstained "
                  "or its diagnosis failed validation, so no root cause is claimed.", ""]
        lines += [f"- {md(x, 300)}" for x in rep.limitations[:6]] + [""]
    lines.append(f"**Reported symptom.** {md(inc.symptom, 400)}")
    header = "\n".join(lines)
    grid = _Grid()
    grid.add(_text("Diagnosis", header), 24, _text_h(header))

    by_id = {e.evidence_id: e for e in rep.evidence}
    supporting = [by_id[i] for i in (d.supporting_evidence_ids if d else []) if i in by_id]
    conflicting = [by_id[i] for i in (d.conflicting_evidence_ids if d else []) if i in by_id]
    if not d:  # no citations: show what was collected
        supporting = [e for e in rep.evidence if e.status == ToolStatus.OK][:12]
    table = _evidence_table([("supporting" if d else "collected", e) for e in supporting] + [("conflicting", e) for e in conflicting],
                            _time_range(rep))
    grid.add(_text("Cited evidence: captured values (the record) · re-query panels below", table), 24, _text_h(table, 38))

    seen: set[str] = set()
    budget = [MAX_EVIDENCE_PANELS]
    own = [e for e in supporting if e.service in (None, comp)]
    other = [e for e in supporting if e not in own]
    for title, group, collapsed in ((f"Supporting evidence: {comp or 'fleet'}", own, False),
                                    ("Supporting evidence: related services", other, False),
                                    ("Conflicting evidence", conflicting, True)):
        if group:
            grid.section(title, collapsed)
            for ev in group:
                _evidence_panels(grid, ev, comp, seen, budget)
    cited_traces = [(ev, t) for ev in supporting + conflicting if ev.source == "jaeger" for t in _trace_ids(ev)[:1]][:MAX_TRACE_PANELS]
    if cited_traces:
        # Collapsed: Jaeger keeps no traces across a stack restart, and an expired trace ID is an error, not "no data".
        grid.section("Cited traces: Jaeger waterfalls (expand; empty for traces older than the last stack restart)", True)
        for ev, t in cited_traces:
            grid.add({"type": "traces", "title": f"trace {t} · {ev.evidence_id}", "datasource": JAEGER,
                      "description": f"Captured {_utc(ev.collection_time)} UTC: {md(_captured(ev), 400)}",
                      "targets": [{"refId": "A", "datasource": JAEGER, "query": t}],
                      "links": [{"title": "Open in Jaeger", "url": f"{JAEGER_UI}/trace/{t}", "targetBlank": True}]}, 24, 12)
    if budget[0] < 0:
        grid.section("Panel limit reached")
        note = f"{-budget[0]} more re-query panels were omitted; every item is still listed in the evidence table above."
        grid.add(_text("Omitted panels", note), 24, 3)
    timeline = "\n".join(["| Time (UTC) | State | Event |", "| --- | --- | --- |"] + [
        f"| {_utc(t.timestamp, '%H:%M:%S')} | {code(t.status.value)} | {md(t.description, 220)} |" for t in rep.timeline])
    grid.section("Investigation timeline", collapsed=True)
    grid.add(_text("Timeline (every tool call and decision)", timeline), 24, _text_h(timeline))
    title = f"{rep.incident_id} · Root cause" + (f": {comp}" if comp and d else "")
    return _dashboard(rc, title, grid, rep, "root-cause",
                      "Diagnosis and the evidence it cites, generated by incident-agent from the saved report.")


_APPROVAL_ID = re.compile(r"\bapr-[a-f0-9]{16}\b")


def action_request_markdown(rep: IncidentReport) -> str:
    """The proposal as the exact team Action API ActionPayload (validated against its schema), with the server IDs."""
    from action_api_models import ActionPayload

    p, a = rep.proposals[0], rep.approval
    payload = {"incident_id": rep.incident_id, "operation": p.action, "target": p.target_service,
               "arguments": p.parameters, "evidence_ids": p.supporting_evidence_ids[:20], "reason": p.reason[:1500],
               "proposal_digest": a.proposal_digest if a else None}
    try:
        ActionPayload.model_validate(payload)
        valid = "valid against `schemas/action_api/ActionPayload.schema.json`"
    except ValidationError as exc:
        valid = f"does **not** validate as an ActionPayload ({len(exc.errors())} error(s)); shown as recorded"
    body = json.dumps(payload, indent=2, ensure_ascii=True).replace("`", "\\u0060")  # no fence breaking
    api = ("POST /v1/actions/execute on the **team-defined** Action API (not an organiser contract)"
           if p.catalog_provenance == "team" else f"catalog provenance {code(p.catalog_provenance or '-')}")
    approval_id = next((m.group(0) for t in rep.timeline for m in [_APPROVAL_ID.search(t.description)] if m), None)
    lines = [f"**Endpoint.** {api} · payload {valid}.", "", "```json", body, "```", ""]
    if approval_id:
        lines.append(f"**Approval.** {code(approval_id)}: the one-time code was shown only on the Action API console.")
    if a and a.request_id:
        lines.append(f"**Server action record.** {code(a.request_id)}: inspect with "
                     f"{code('python cli.py action-api show ' + a.request_id, 80)} and "
                     f"{code('python cli.py audit --server --incident-id ' + rep.incident_id, 90)}.")
    return "\n".join(lines)


def verification_dashboard(rep: IncidentReport) -> dict:
    _, ver = dashboard_uids(rep.incident_id)
    comp, p, a, v = _component(rep), (rep.proposals[0] if rep.proposals else None), rep.approval, rep.verification
    outcome = rep.remediation_outcome or "not requested"
    lines = [_nav(rep, "ver"), "", f"## Outcome: {code(outcome)} · status {code(rep.status.value)}"
             + (f" · mode {code(rep.execution_mode)}" if rep.execution_mode else ""), ""]
    if not p:
        reason = rep.escalation_reason or ("Investigation only: remediation was not requested (run without --respond)."
                                           if not rep.remediation_outcome else "No action was proposed.")
        lines += ["**No action was executed, so recovery was not verified.**", "", f"**Why.** {md(reason, 800)}", ""]
    else:
        rows = [("Action", f"{code(p.action)} on {code(p.target_service)} {code(json.dumps(p.parameters, sort_keys=True))}"),
                ("Reason", md(p.reason, 700)), ("Expected effect", md(p.expected_effect, 400)), ("Risk", md(p.risk, 500)),
                ("Rollback", md(p.rollback or "none documented", 300)), ("Verification plan", md(p.verification_plan, 400))]
        if a:
            rows.append(("Human approval", f"{code(a.decision)} by {md(a.approver, 80)}"
                         + (f" · dispatched {_utc(a.dispatch_time)} UTC" if a.dispatch_time else " · not dispatched")
                         + (f" · request {code(a.request_id)}" if a.request_id else "") + f" · API outcome {code(a.outcome)}"))
        if v:
            rows.append(("Verification", f"{code(v.result.value)} at {_utc(v.verification_time)} UTC: {md(v.details, 900)}"))
        if rep.escalation_reason:
            rows.append(("Escalation", md(rep.escalation_reason, 500)))
        lines += ["| | |", "| --- | --- |", *[f"| **{k}** | {x} |" for k, x in rows], ""]
    lines.append(f"**Recovery condition.** {md(rep.incident.recovery_condition, 300)}")
    header = "\n".join(lines)
    grid = _Grid()
    grid.add(_text("Remediation and verification", header), 24, _text_h(header))
    if p:
        request = action_request_markdown(rep)
        grid.add(_text("Action API request (exact payload sent for approval and execution)", request), 24, _text_h(request))

    checks = verification_checks(rep)
    if checks:
        table = "\n".join(["| Check | Target | Baseline (before approval) | After the action (samples) | Judged |",
                           "| --- | --- | --- | --- | --- |"] + [
            f"| {code(c['check'].service + '.' + c['check'].metric)} | {code(c['bound'])} | {_num(c['baseline'])} | "
            f"{', '.join(_num(s) for s in c['samples']) or '-'} | **{md(c['state'])}** |" for c in checks])
        grid.add(_text("Recovery checks: baseline vs post-action (from the report)", table), 24, _text_h(table, 60))
        grid.section("Verification checks (live re-query; dashed line = recovery bound)")
        for c in checks:
            chk = c["check"]
            expr = _template(chk.metric, chk.service, VERIFY_WINDOW_MIN)
            threshold = (c["kind"], c["bound_value"]) if c["bound_value"] is not None else None
            grid.add(_timeseries(f"{chk.service}.{chk.metric} · {c['state'].split(' (')[0]} · target {c['bound']}",
                                 [(chk.service, expr)], _UNITS.get(chk.metric, "short"),
                                 f"Same template and {VERIFY_WINDOW_MIN}-minute window as verification. Markers: baseline, "
                                 "approval, dispatch and verification time.", threshold=threshold), 12, 8)
    if comp:
        # Grafana ignores a panel's relative time override under an absolute dashboard range, hence the "now" link.
        grid.section(f"Symptom signals: {comp} (open 'Now (last 15 min)' to check the current state)")
        services = [comp] + [s for s in [_service(rep.incident.affected_service)] if s and s != comp]
        for s in services:
            for m in _NOW_PANELS if s == comp else ("error_rate",):
                grid.add(_timeseries(f"{s}.{m}", [(s, _template(m, s, 2))], _UNITS.get(m, "short"),
                                     "Live re-query of the component's health signal."), 6, 7)
    _, base_ids = _baseline(rep)
    ids = list(dict.fromkeys(base_ids + (v.baseline_evidence_ids + v.post_action_evidence_ids if v else [])))
    by_id = {e.evidence_id: e for e in rep.evidence}
    post = set(v.post_action_evidence_ids) if v else set()
    items = [("post-action" if i in post else "baseline", by_id[i]) for i in ids if i in by_id]
    if items:
        grid.section("Baseline and post-action evidence", collapsed=True)
        table = _evidence_table(items, _time_range(rep))
        grid.add(_text("Verification evidence: captured values", table), 24, _text_h(table, 38))
    return _dashboard(ver, f"{rep.incident_id} · Verification: {outcome}", grid, rep, "verification",
                      "Action, approval and recovery checks, generated by incident-agent from the saved report.")


def _index_links(r: IncidentReport, rc: str, ver: str) -> str:
    links = [f"[Root cause](/grafana/d/{rc})", f"[Verification](/grafana/d/{ver})"]
    if r.proposals:
        links.append(f"[Action API request](/grafana/d/{ver})")
    comp = _component(r)
    if comp:
        tr = _time_range(r)
        links.append(f"[Metrics]({explore_url(PROM, [{'expr': _template('error_rate', comp, 2)}, {'expr': _template('p95_latency_ms', comp, 2)}], tr)})")
        start, end = (int(datetime.fromisoformat(tr[k].replace("Z", "+00:00")).timestamp() * 1e6) for k in ("from", "to"))
        links.append(f"[Traces]({JAEGER_UI}/search?" + urlencode({"service": comp, "start": start, "end": end,
                                                                  "lookback": "custom", "limit": 20}) + ")")
        links.append(f"[Logs]({explore_url(LOGS, [_logs_target(comp, SEVERITY['WARN'])], tr)})")
        caller = _service(r.incident.affected_service)
        if caller and caller != comp:  # a stopped or silent component logs nothing; its caller shows the symptom
            links.append(f"[Symptom metrics: {caller}]({explore_url(PROM, [{'expr': _template('error_rate', caller, 2)}], tr)})")
            links.append(f"[Symptom logs: {caller}]({explore_url(LOGS, [_logs_target(caller, SEVERITY['WARN'])], tr)})")
    return " · ".join(links)


def index_dashboard(reports: list[IncidentReport]) -> dict:
    reports = sorted(reports, key=lambda r: r.incident.detection_time, reverse=True)
    rows = ["| Incident | Detected (UTC) | Status | Root cause | Remediation | Verification | Open |",
            "| --- | --- | --- | --- | --- | --- | --- |"]
    # Open column: root cause, Action API request + verification, and the component's metrics/traces/logs in the incident window
    faults: dict[str, list[str]] = {}
    for r in reports:
        rc, ver = dashboard_uids(r.incident_id)
        d = r.diagnosis
        cause = f"{code(d.suspected_component, 40)} ({d.confidence_label.value}): {md(d.suspected_cause, 140)}" if d else "none accepted"
        action = f"{code(r.remediation_outcome or 'not requested')}" + (
            f" {code(r.proposals[0].action)} → {code(r.proposals[0].target_service)}" if r.proposals else "")
        verified = code(r.verification.result.value) if r.verification else "not run"
        rows.append(f"| **{md(r.incident_id, 60)}** | {_utc(r.incident.detection_time, '%m-%d %H:%M')} | {code(r.status.value)} | "
                    f"{cause} | {action} | {verified} | {_index_links(r, rc, ver)} |")
        if d:
            faults.setdefault(d.suspected_component, []).append(
                f"[{md(r.incident_id, 60)}](/grafana/d/{rc}) ({d.confidence_label.value}, {md(r.remediation_outcome or 'not requested')}, "
                f"[verification](/grafana/d/{ver}))")
    intro = "\n".join([
        "## Incident Zero: incidents investigated by the agent",
        "Every incident has two pages. **Root cause** shows the accepted diagnosis, each evidence item it cites with the "
        "value captured at the time, and live re-queries of the same signals (the diagnosed component is drawn in red). "
        "**Verification** shows the proposed action, the human approval and each recovery check against its pre-action "
        "baseline. Both pages share the incident's time range and carry markers for detection, diagnosis, baseline, "
        "approval, dispatch and verification.",
        "",
        "Captured values come from the saved report and never expire. Jaeger and OpenSearch keep no data across a stack "
        "restart, so live trace and log panels of older incidents can be empty. Regenerate with "
        "`python cli.py grafana sync` after a restart."])
    by_fault = "\n".join(["### Faults by root-cause component"] + (
        [f"- {code(c, 40)}: " + "; ".join(xs) for c, xs in sorted(faults.items())] or ["- No accepted diagnosis yet."]))
    grid = _Grid()
    grid.add(_text("How to use", intro), 24, _text_h(intro))
    table = "\n".join(rows) if reports else "No incident reports found."
    grid.add(_text("Incidents (newest first)", table), 24, _text_h(table, 85))
    grid.add(_text("Faults", by_fault), 24, _text_h(by_fault))
    return {"uid": INDEX_UID, "title": "Incident Zero · Incidents", "tags": [TAG, "index"], "timezone": "utc",
            "editable": True, "schemaVersion": 39, "time": {"from": "now-24h", "to": "now"}, "templating": {"list": []},
            "annotations": {"list": []}, "links": [], "panels": grid.panels,
            "description": "Index of incident-agent reports with links to each root-cause and verification dashboard."}


def build_all(reports: list[IncidentReport]) -> list[dict]:
    out = [index_dashboard(reports)]
    for r in reports:
        out += [root_cause_dashboard(r), verification_dashboard(r)]
    return out


def load_reports(runs_dir: Path) -> tuple[list[IncidentReport], list[str]]:
    reports, skipped = [], []
    for path in sorted(runs_dir.glob("*/*_report.json")):
        try:
            reports.append(IncidentReport.model_validate_json(path.read_text(encoding="utf-8")))
        except (OSError, ValidationError) as exc:
            skipped.append(f"{path}: {type(exc).__name__}")
    return reports, skipped


# ---------------------------------------------------------------- publishing
def grafana_base(url: Optional[str] = None) -> str:
    base = (url or os.getenv("GRAFANA_URL") or "http://localhost:8080").rstrip("/")
    return base if base.endswith("/grafana") else base + "/grafana"


class GrafanaPublisher:
    """Writes dashboards into the 'Incident Zero' folder. Uses GRAFANA_TOKEN when set, else the demo's anonymous access."""

    def __init__(self, base_url: Optional[str] = None, token: Optional[str] = None, timeout: float = 10,
                 transport: Optional[httpx.BaseTransport] = None):
        self.base = grafana_base(base_url)
        token = token if token is not None else os.getenv("GRAFANA_TOKEN", "")
        self.http = httpx.Client(base_url=self.base, timeout=timeout, transport=transport, follow_redirects=False,
                                 headers={"Authorization": f"Bearer {token}"} if token else {})
        self._folder = False

    def _ensure_folder(self) -> None:
        if self._folder:
            return
        r = self.http.get(f"/api/folders/{FOLDER_UID}")
        if r.status_code == 404:
            r = self.http.post("/api/folders", json={"uid": FOLDER_UID, "title": FOLDER_TITLE})
            if r.status_code in (409, 412):  # created concurrently
                r = self.http.get(f"/api/folders/{FOLDER_UID}")
        if r.status_code != 200:
            raise GrafanaError(f"folder {FOLDER_UID}: HTTP {r.status_code} {r.text[:200]}")
        self._folder = True

    def put(self, dashboard: dict) -> str:
        try:
            self._ensure_folder()
            r = self.http.post("/api/dashboards/db", json={"dashboard": dashboard, "folderUid": FOLDER_UID,
                                                           "overwrite": True, "message": "incident-agent sync"})
        except httpx.HTTPError as exc:
            raise GrafanaError(f"Grafana unreachable at {self.base}: {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise GrafanaError(f"{dashboard['uid']}: HTTP {r.status_code} {r.text[:200]}")
        return f"{self.base}/d/{dashboard['uid']}"


def sync(runs_dir: Path, publisher: GrafanaPublisher) -> dict[str, Any]:
    """Publish the index and both dashboards of every report under runs_dir (idempotent: fixed uids, overwrite)."""
    reports, skipped = load_reports(runs_dir)
    urls = {d["uid"]: publisher.put(d) for d in build_all(reports)}
    incidents = {r.incident_id: tuple(urls[u] for u in dashboard_uids(r.incident_id)) for r in reports}
    return {"index": urls[INDEX_UID], "incidents": incidents, "skipped": skipped}


def export(runs_dir: Path, out_dir: Path) -> list[Path]:
    """Write the dashboards as JSON files (for review or file provisioning) instead of publishing them."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for d in build_all(load_reports(runs_dir)[0]):
        path = out_dir / f"{d['uid']}.json"
        path.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
        paths.append(path)
    return paths
