"""
Read-only telemetry tools for the Incident Zero agent.
Owner: Neha (Member 2).

What this module does
- Queries Prometheus (metrics), Jaeger v3 (traces) and OpenSearch PPL (logs).
- Only fixed query templates. Service names are validated against the observed
  inventory, time windows are bounded, every request has a timeout.
- Every call returns a ToolResult with status ok / empty / unavailable / error,
  so "no data" is never confused with "healthy".
- Every result is saved immediately as an evidence file (evidence/<id>.json),
  so each diagnosis citation can be traced back to a real capture.
- Feature-flag data is filtered out (flagd services, feature_flag span
  events/attributes, calls to flag services) and flag wording in error messages and
  log lines is redacted. Diagnosis must come from telemetry.

What this module never does
- No writes, no shell commands, no Docker socket, no arbitrary URLs or
  model-written queries.

Quick check (Windows PowerShell, inside incident-agent/):
    python telemetry.py check
    python telemetry.py overview
    python telemetry.py traces payment
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

try:  # optional: load incident-agent/.env when present
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

# --------------------------------------------------------------------------
# Configuration (fixed by the operator, never chosen by the model)
# --------------------------------------------------------------------------
PROM_URL = os.getenv("PROM_URL", "http://localhost:9090").rstrip("/")
JAEGER_URL = os.getenv("JAEGER_URL", "http://localhost:8080").rstrip("/")
# OpenSearch host port is assigned dynamically by Docker: `docker port opensearch 9200`
OPENSEARCH_URL = os.getenv("OPENSEARCH_URL", "").rstrip("/")
EVIDENCE_DIR = Path(os.getenv("EVIDENCE_DIR", "evidence"))
REQUEST_TIMEOUT_S = float(os.getenv("TELEMETRY_TIMEOUT_S", "10"))
# OpenSearch runs near its 1 GiB limit in the demo and has slow spells; logs get a longer timeout.
OPENSEARCH_TIMEOUT_S = float(os.getenv("OPENSEARCH_TIMEOUT_S", "20"))

MIN_WINDOW_MIN, MAX_WINDOW_MIN = 1, 60
# Default "now" window. Incidents can be injected minutes apart, so a long window would mix the previous
# incident into the current one. 3 min still gives rate() enough samples at the demo's 60 s metric interval.
NOW_WINDOW_MIN = 3
MAX_ITEMS = 20            # max traces / log lines returned to the model
EXCERPT_CHARS = 300       # max chars per log body / error message
INVENTORY_TTL_S = 60

EXCLUDED_SERVICES = {"flagd", "flagd-ui"}   # flag state is out of bounds
# "feature flag" in any spelling (space, _, ., -, plural): demo services put it in error messages,
# e.g. "Product Catalog Fail Feature Flag Enabled", which would hand the model the answer.
_FLAG_PATTERN = re.compile(r"feature[\s_.-]*flags?|flagd|openfeature", re.IGNORECASE)
# Neutral on purpose: a marker that says why it was withheld would itself hint at the injected fault.
FLAG_REDACTED = "[message text withheld by policy]"
# A call whose target is the flag system itself (flagd / OpenFeature / OFREP), judged from where the call goes.
_FLAG_SERVICE = re.compile(r"flagd|openfeature|ofrep", re.IGNORECASE)
_TARGET_ATTRS = ("rpc.service", "rpc.method", "server.address", "peer.service", "net.peer.name", "url.full", "http.url")


def _redact(text: Any) -> Any:
    """Keep the fact that something happened, drop flag wording that would reveal the injected fault."""
    return FLAG_REDACTED if text and _FLAG_PATTERN.search(str(text)) else text
_SERVICE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")

_client = httpx.Client(timeout=REQUEST_TIMEOUT_S, follow_redirects=False)


# --------------------------------------------------------------------------
# Shared record (keep in sync with Harsha's models.py when it lands)
# --------------------------------------------------------------------------
class ToolStatus(str, Enum):
    OK = "ok"                    # data found
    EMPTY = "empty"              # backend answered, but no data in the window
    UNAVAILABLE = "unavailable"  # backend not reachable / not configured
    ERROR = "error"              # backend answered with an error


class ToolResult(BaseModel):
    status: ToolStatus
    evidence_id: str
    tool: str
    source: Literal["prometheus", "jaeger", "opensearch", "derived"]
    service: str | None = None
    query: str
    window_start: datetime
    window_end: datetime
    collected_at: datetime
    value: Any = None
    excerpt: str | None = None
    trace_ids: list[str] = Field(default_factory=list)
    log_ids: list[str] = Field(default_factory=list)
    capture_ref: str | None = None
    message: str | None = None

    def for_llm(self) -> str:
        """Compact JSON for the diagnosis model (bounded size)."""
        data = {
            "evidence_id": self.evidence_id,
            "tool": self.tool,
            "status": self.status.value,
            "service": self.service,
            "window": f"{self.window_start:%H:%M:%S}-{self.window_end:%H:%M:%S}Z",
            "message": self.message,
            "value": self.value,
        }
        # The full result, never cut: tool outputs are already bounded at the source (MAX_ITEMS lines/traces,
        # EXCERPT_CHARS per message). Compact separators keep it small; the run's token budget is the limit.
        return json.dumps(data, default=str, separators=(",", ":"))


class TelemetryInputError(ValueError):
    """Raised for invalid tool arguments (unknown service, bad window, unknown tool)."""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _window(minutes: int, minimum: int = MIN_WINDOW_MIN) -> tuple[datetime, datetime]:
    if not isinstance(minutes, int) or not (minimum <= minutes <= MAX_WINDOW_MIN):
        raise TelemetryInputError(f"window_minutes must be an integer {minimum}-{MAX_WINDOW_MIN}")
    end = _now()
    return end - timedelta(minutes=minutes), end


def _rfc3339(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _request(method: str, url: str, **kwargs) -> httpx.Response:
    """One request with a single retry for transient failures. Raises httpx errors."""
    for attempt in (1, 2):
        try:
            resp = _client.request(method, url, **kwargs)
            if resp.status_code >= 500 and attempt == 1:
                time.sleep(0.5)
                continue
            return resp
        except (httpx.ConnectError, httpx.TimeoutException):
            if attempt == 2:
                raise
            time.sleep(0.5)
    raise RuntimeError("unreachable")  # pragma: no cover


def _capture(result: ToolResult, raw: Any) -> ToolResult:
    """Save the result (+ bounded raw response) as an evidence file."""
    try:
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        path = EVIDENCE_DIR / f"{result.evidence_id}.json"
        raw_text = json.dumps(raw, default=str)
        if len(raw_text) > 50_000:
            raw_text = raw_text[:50_000] + "...(truncated)"
        result.capture_ref = str(path)
        path.write_text(
            json.dumps({"result": result.model_dump(mode="json"), "raw": raw_text}, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:  # capture failure must never hide the result
        result.capture_ref = None  # never point local_capture_reference at a file that wasn't written
        result.message = f"{result.message or ''} [capture failed: {exc}]".strip()
    return result


def _result(tool: str, source: str, service: str | None, query: str,
            start: datetime, end: datetime, status: ToolStatus, *, raw: Any = None,
            **fields) -> ToolResult:
    res = ToolResult(
        status=status,
        evidence_id=f"ev-{tool}-{uuid.uuid4().hex[:8]}",
        tool=tool, source=source, service=service, query=query,
        window_start=start, window_end=end, collected_at=_now(), **fields,
    )
    return _capture(res, raw)


def _failure(exc: Exception, tool: str, source: str, service: str | None, query: str,
             start: datetime, end: datetime) -> ToolResult:
    if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException)):
        status, msg = ToolStatus.UNAVAILABLE, f"{source} not reachable: {type(exc).__name__}"
    else:
        status, msg = ToolStatus.ERROR, f"{source} error: {type(exc).__name__}: {str(exc)[:200]}"
    return _result(tool, source, service, query, start, end, status, message=msg)


def _short(text: Any) -> str:
    s = str(text) if text is not None else ""
    return s if len(s) <= EXCERPT_CHARS else s[:EXCERPT_CHARS] + "..."


# --------------------------------------------------------------------------
# Service inventory and validation
# --------------------------------------------------------------------------
_inventory: tuple[float, list[str]] = (0.0, [])


def service_inventory(refresh: bool = False) -> list[str]:
    """Services observed in Prometheus and Jaeger, plus containers seen by docker_stats
    (so the cache and database, which emit no spans, can be health-checked). Flag services excluded."""
    global _inventory
    if not refresh and _inventory[1] and time.time() - _inventory[0] < INVENTORY_TTL_S:
        return _inventory[1]
    names: set[str] = set()
    for label in ("service_name", "container_name"):
        try:
            r = _request("GET", f"{PROM_URL}/api/v1/label/{label}/values")
            if r.status_code == 200:
                names.update(r.json().get("data") or [])
        except httpx.HTTPError:
            pass
    try:
        r = _request("GET", f"{JAEGER_URL}/jaeger/ui/api/v3/services")
        if r.status_code == 200:
            names.update(r.json().get("services") or [])
    except httpx.HTTPError:
        pass
    found = sorted(n for n in names if n and n not in EXCLUDED_SERVICES)
    if found:
        _inventory = (time.time(), found)
    return found


def validate_service(service: str) -> str:
    if not isinstance(service, str):
        raise TelemetryInputError("service must be a string")
    name = service.strip()
    if name in EXCLUDED_SERVICES:
        raise TelemetryInputError(f"'{name}' is out of bounds (feature-flag service)")
    if not _SERVICE_PATTERN.match(name):
        raise TelemetryInputError(f"invalid service name: {service!r}")
    inventory = service_inventory()
    if inventory and name not in inventory:
        raise TelemetryInputError(f"unknown service '{name}'. Known: {', '.join(inventory)}")
    return name


# --------------------------------------------------------------------------
# METRICS (Prometheus) - fixed templates only
# --------------------------------------------------------------------------
_CALLS = "traces_span_metrics_calls_total"
_DUR = "traces_span_metrics_duration_milliseconds_bucket"

METRIC_TEMPLATES: dict[str, tuple[str, str, str]] = {
    # name: (PromQL template, unit, label to keep in output)
    "request_rate": (f'sum(rate({_CALLS}{{service_name="{{s}}"}}[{{w}}m]))', "requests/s", ""),
    # "or vector(0)": traffic with zero errors is 0, not "no data"; no traffic stays empty.
    "error_rate": (
        f'(sum(rate({_CALLS}{{service_name="{{s}}",status_code="STATUS_CODE_ERROR"}}[{{w}}m])) or vector(0))'
        f' / sum(rate({_CALLS}{{service_name="{{s}}"}}[{{w}}m]))', "ratio 0-1", ""),
    "errors_by_operation": (
        f'topk(10, sum by (span_name) (rate({_CALLS}{{service_name="{{s}}",status_code="STATUS_CODE_ERROR"}}[{{w}}m])))',
        "errors/s", "span_name"),
    "p95_latency_ms": (
        f'histogram_quantile(0.95, sum by (le) (rate({_DUR}{{service_name="{{s}}"}}[{{w}}m])))', "ms", ""),
    "p95_latency_by_operation": (
        f'topk(10, histogram_quantile(0.95, sum by (le, span_name) (rate({_DUR}{{service_name="{{s}}"}}[{{w}}m]))))',
        "ms", "span_name"),
    # Container resource telemetry from the collector's docker_stats receiver.
    # Exact metric names are matched by pattern; confirm with `python telemetry.py names container`.
    # Current CPU from the CPU-time counter. container_cpu_utilization_ratio here is a lifetime average since the
    # container started (flat for hours), so it cannot show a CPU incident.
    "container_cpu": ('rate(container_cpu_usage_nanoseconds_total{container_name="{s}"}[{w}m]) / 1e7',
                      "percent of one core, current (can exceed 100)", ""),
    "container_memory": ('{__name__=~"container_memory_(usage_total|percent|rss).*",container_name="{s}"}',
                         "bytes/percent", "__name__"),
    "container_memory_trend": ('deriv({__name__=~"container_memory_usage_total.*",container_name="{s}"}[{w}m])',
                               "bytes/s (positive = growing)", ""),
    "container_uptime": ('{__name__=~"container_uptime.*",container_name="{s}"}', "s", ""),
}
MetricName = Literal[
    "request_rate", "error_rate", "errors_by_operation", "p95_latency_ms", "p95_latency_by_operation",
    "container_cpu", "container_memory", "container_memory_trend", "container_uptime",
]


def _prom(promql: str) -> tuple[list[dict], Any]:
    r = _request("GET", f"{PROM_URL}/api/v1/query", params={"query": promql})
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(body.get("error", "prometheus query failed"))
    rows = []
    for item in body["data"]["result"]:
        try:
            val = float(item["value"][1])
        except (KeyError, ValueError, TypeError):
            continue
        if val != val or val in (float("inf"), float("-inf")):  # NaN / inf -> no data
            continue
        rows.append({"labels": item.get("metric", {}), "value": round(val, 6)})
    return rows, body


def query_metrics(service: str, metric: str = "error_rate", window_minutes: int = 3) -> ToolResult:
    """Run one fixed metric template for one service over a bounded window."""
    service = validate_service(service)
    if metric not in METRIC_TEMPLATES:
        raise TelemetryInputError(f"unknown metric '{metric}'. Allowed: {', '.join(METRIC_TEMPLATES)}")
    start, end = _window(window_minutes, minimum=2)  # rate() needs >= 2 samples
    template, unit, keep = METRIC_TEMPLATES[metric]
    promql = template.replace("{s}", service).replace("{w}", str(window_minutes))
    try:
        rows, raw = _prom(promql)
    except Exception as exc:
        return _failure(exc, "query_metrics", "prometheus", service, promql, start, end)
    if not rows:
        msg = ("no data in window (no traffic, no errors recorded, or metric not exported)"
               if metric != "error_rate" else "no requests in window - cannot judge error rate")
        return _result("query_metrics", "prometheus", service, promql, start, end,
                       ToolStatus.EMPTY, raw=raw, value={"metric": metric, "unit": unit}, message=msg)
    if keep:
        # Per-operation series include calls to the flag service (e.g. a long-lived evaluation stream):
        # flag state is out of bounds, so those operations never reach the model.
        series = [{keep: r["labels"].get(keep, ""), "value": r["value"]} for r in rows
                  if not _FLAG_PATTERN.search(str(r["labels"].get(keep, "")))
                  and not _FLAG_SERVICE.search(str(r["labels"].get(keep, "")))][:MAX_ITEMS]
        value = {"metric": metric, "unit": unit, "series": series}
    else:
        value = {"metric": metric, "unit": unit, "value": rows[0]["value"]}
    return _result("query_metrics", "prometheus", service, promql, start, end,
                   ToolStatus.OK, raw=raw, value=value)


BASELINE_MIN = 30  # longer reference: catches an incident that has lasted longer than two "now" windows


def _overview_queries(w: int, offset: str) -> dict[str, str]:
    rng = f"[{w}m]{offset}"
    return {
        "request_rate": f"sum by (service_name) (rate({_CALLS}{rng}))",
        "error_rate": (f'(sum by (service_name) (rate({_CALLS}{{status_code="STATUS_CODE_ERROR"}}{rng}))'
                       f" or sum by (service_name) (rate({_CALLS}{rng})) * 0)"
                       f" / sum by (service_name) (rate({_CALLS}{rng}))"),
        "p95_ms": f"histogram_quantile(0.95, sum by (le, service_name) (rate({_DUR}{rng})))",
    }


def _operation_queries(w: int, offset: str) -> dict[str, str]:
    """Per (service, server operation): a failure in one rarely called operation (e.g. 0.2% of a service's
    calls) disappears in the service-wide rate, so incoming operations are also compared one by one."""
    rng = f'{{span_kind="SPAN_KIND_SERVER"}}[{w}m]{offset}'
    err_rng = f'{{span_kind="SPAN_KIND_SERVER",status_code="STATUS_CODE_ERROR"}}[{w}m]{offset}'
    by = "service_name, span_name"
    return {
        "request_rate": f"sum by ({by}) (rate({_CALLS}{rng}))",
        "error_rate": (f"(sum by ({by}) (rate({_CALLS}{err_rng})) or sum by ({by}) (rate({_CALLS}{rng})) * 0)"
                       f" / sum by ({by}) (rate({_CALLS}{rng}))"),
        "p95_ms": f"histogram_quantile(0.95, sum by (le, {by}) (rate({_DUR}{rng})))",
    }


RECOVERING = "recovering:"  # a problem from the previous window is ending (e.g. an earlier incident switched off)
MAX_OPERATIONS = 12


def _changes(r: dict, operation: bool = False) -> list[str]:
    """What moved between the previous window and now. The demo's baseline can already look
    unhealthy (slow or erroring services), so a change is a better signal than a high level.
    Operations are sparser and noisier than whole services, so they need larger moves and at least
    one actual failed call; a rare operation with no calls in the previous window is compared with
    the longer baseline instead."""
    out = []
    min_err, min_ms = (0.05, 200) if operation else (0.02, 100)
    err, p95 = r.get("error_rate"), r.get("p95_ms")
    err0, p950 = r.get("error_rate_before"), r.get("p95_ms_before")
    # Service-wide: a rise needs at least ~2 failed calls in the window (one stray failure, e.g. a leftover
    # call from an earlier incident, is not an incident).
    rise_ok = "window_s" not in r or (err or 0) * (r.get("request_rate") or 0) * r["window_s"] >= 2
    if operation:
        err0 = err0 if err0 is not None else r.get("error_rate_baseline")
        p950 = p950 if p950 is not None else r.get("p95_ms_baseline")
        rise_ok = (err or 0) * (r.get("request_rate") or 0) * r["window_s"] >= 0.9  # at least ~1 failed call
        if err is not None and err0 is None and err >= min_err and rise_ok:
            out.append(f"error_rate {err:.3f} (no calls in the earlier windows)")
    if err is not None and err0 is not None and err - err0 >= min_err and err >= 2 * err0 and rise_ok:
        out.append(f"error_rate up {err0:.3f} -> {err:.3f}")
    elif err is not None and err0 is not None and err0 - err >= min_err and err <= 0.5 * err0:
        out.append(f"{RECOVERING} error_rate down {err0:.3f} -> {err:.3f}")
    if p95 is not None and p950 and p95 >= 2 * p950 and p95 - p950 >= min_ms:  # demo p95 drifts ~1.5x on its own
        out.append(f"p95 up {p950:.0f} -> {p95:.0f} ms")
    elif p95 is not None and p950 and p95 <= 0.5 * p950 and p950 - p95 >= min_ms:
        out.append(f"{RECOVERING} p95 down {p950:.0f} -> {p95:.0f} ms")
    if operation:
        return out
    # Still high against the longer baseline: an incident that started before the previous window.
    if not out:
        errb, p95b = r.get("error_rate_baseline"), r.get("p95_ms_baseline")
        if err is not None and errb is not None and err - errb >= 0.02 and err >= 2 * errb and rise_ok:
            out.append(f"error_rate elevated {err:.3f} vs {BASELINE_MIN} min baseline {errb:.3f} (ongoing)")
        if p95 is not None and p95b and p95 >= 2 * p95b and p95 - p95b >= 100:
            out.append(f"p95 elevated {p95:.0f} vs {BASELINE_MIN} min baseline {p95b:.0f} ms (ongoing)")
    rps, rps0 = r.get("request_rate"), r.get("request_rate_before")
    if rps0 and rps0 >= 0.05:
        if rps is None:
            out.append(f"no traffic now (was {rps0:.2f} req/s)")
        elif rps <= 0.5 * rps0:
            out.append(f"traffic down {rps0:.2f} -> {rps:.2f} req/s")
        elif rps >= 2 * rps0 and rps - rps0 >= 1:
            out.append(f"traffic up {rps0:.2f} -> {rps:.2f} req/s")
    return out


def overview(window_minutes: int = 3) -> ToolResult:
    """All services at once: request rate, error rate and p95 latency now and in the previous
    window of the same length, plus what changed (fixed templates). Good first step."""
    start, end = _window(window_minutes, minimum=2)
    w = window_minutes
    queries = _overview_queries(w, "")
    queries.update({f"{k}_before": q for k, q in _overview_queries(w, f" offset {w}m").items()})
    queries.update({f"{k}_baseline": q for k, q in _overview_queries(BASELINE_MIN, f" offset {w}m").items()})
    op_queries = _operation_queries(w, "")
    op_queries.update({f"{k}_before": q for k, q in _operation_queries(w, f" offset {w}m").items()})
    op_queries.update({f"{k}_baseline": q for k, q in _operation_queries(BASELINE_MIN, f" offset {w}m").items()})
    table: dict[str, dict[str, float]] = {}
    ops: dict[tuple[str, str], dict[str, float]] = {}
    raws = {}
    try:
        for key, q in queries.items():
            rows, raws[key] = _prom(q)
            for r in rows:
                svc = r["labels"].get("service_name")
                if svc and svc not in EXCLUDED_SERVICES:
                    table.setdefault(svc, {})[key] = round(r["value"], 0 if "p95" in key else 3)
        for key, q in op_queries.items():
            rows, raws[f"operation_{key}"] = _prom(q)
            for r in rows:
                svc, op = r["labels"].get("service_name"), r["labels"].get("span_name")
                if svc and op and svc not in EXCLUDED_SERVICES and not _FLAG_PATTERN.search(op):
                    ops.setdefault((svc, op), {})[key] = round(r["value"], 0 if "p95" in key else 4)
    except Exception as exc:
        return _failure(exc, "overview", "prometheus", None, json.dumps(queries), start, end)
    queries.update({f"operation_{k}": q for k, q in op_queries.items()})
    if not any(k in v for v in table.values() for k in ("request_rate", "error_rate", "p95_ms")):
        return _result("overview", "prometheus", None, json.dumps(queries), start, end,
                       ToolStatus.EMPTY, raw=raws, message="no span metrics in window (is load running?)")
    rows = [{"service": s, **v} for s, v in table.items()]
    baseline = any(k.endswith("_before") for v in table.values() for k in v)
    for r in rows:
        r["changes"] = _changes({**r, "window_s": w * 60})
        if baseline and not any(k.endswith("_before") for k in r):
            r["baseline"] = "missing: no data for this service in the previous window"

    def active(r: dict) -> list[str]:
        return [c for c in r["changes"] if not c.startswith(RECOVERING)]

    def rank(r: dict) -> tuple:
        err_up = (r.get("error_rate") or 0) - (r.get("error_rate_before") or 0)
        p95_up = (r.get("p95_ms") or 0) / (r.get("p95_ms_before") or r.get("p95_ms") or 1)
        return (-len(active(r)), -err_up, -p95_up, -(r.get("error_rate") or 0))

    rows.sort(key=rank)
    changed = [f'{r["service"]}: {"; ".join(active(r))}' for r in rows if active(r)]
    recovering = [f'{r["service"]}: {"; ".join(c[len(RECOVERING):].strip() for c in r["changes"] if c.startswith(RECOVERING))}'
                  for r in rows if len(active(r)) < len(r["changes"])]
    op_changed, op_recovering = [], []
    for (svc, op), v in ops.items():
        if v.get("request_rate") is None:
            continue  # no calls now
        ch = _changes({**v, "window_s": w * 60}, operation=True)
        now = f'(now {v.get("request_rate", 0):.3f} req/s, error_rate {v.get("error_rate", 0):.3f}, p95 {v.get("p95_ms", 0):.0f} ms)'
        act = [c for c in ch if not c.startswith(RECOVERING)]
        if act:
            op_changed.append((v.get("error_rate") or 0, f'{svc} "{op}": {"; ".join(act)} {now}'))
        if len(act) < len(ch):
            op_recovering.append(f'{svc} "{op}": ' + "; ".join(c[len(RECOVERING):].strip() for c in ch if c.startswith(RECOVERING)))
    op_changed = [text for _, text in sorted(op_changed, key=lambda t: -t[0])][:MAX_OPERATIONS]

    if not baseline:
        changed = ["NO BASELINE: Prometheus has no data for the previous window (e.g. a history gap right after "
                   "a restart), so changes cannot be judged. Do not read this as 'no change'."]
    elif not changed and op_changed:
        changed = ["no service-wide change; see operations_changed (a single operation changed)"]
    return _result("overview", "prometheus", None, json.dumps(queries), start, end,
                   ToolStatus.OK, raw=raws,
                   message=None if baseline else "previous-window baseline missing: changes cannot be judged",
                   value={
                       "baseline_available": baseline,
                       "changed_since_previous_window": changed or ["no clear change in any service"],
                       **({"recovering_since_previous_window": recovering} if recovering and baseline else {}),
                       **({"operations_changed": op_changed} if op_changed and baseline else {}),
                       **({"operations_recovering": op_recovering[:MAX_OPERATIONS]} if op_recovering and baseline else {}),
                       "services": rows[:30],
                       "note": (f"*_before = the {w} min window before this one; *_baseline = the {BASELINE_MIN} min "
                                "before this one ('ongoing' = still high against it). The demo's normal baseline can "
                                "already show slow or erroring services; judge changes, not absolute levels. "
                                "'recovering' = a problem from the previous window is ending, not the current incident. "
                                "operations_changed compares each incoming (server) operation on its own: a failing "
                                "operation that is a small share of its service's calls shows there and not in the "
                                "service-wide numbers."),
                       "units": {"request_rate": "req/s", "error_rate": "ratio 0-1", "p95_ms": "ms"}})


# --------------------------------------------------------------------------
# TRACES (Jaeger v3 API, through the frontend proxy)
# --------------------------------------------------------------------------
_SAFE_ATTR = re.compile(r"^(http\.|rpc\.|db\.|server\.|peer\.|error\.|net\.|network\.|url\.|exception\.)")


def _attr_value(v: dict) -> Any:
    for k in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if k in v:
            return v[k]
    return None


def _attrs(items: list[dict] | None) -> dict[str, Any]:
    return {a.get("key"): _attr_value(a.get("value", {})) for a in (items or []) if a.get("key")}


def _hex_id(value: str) -> str:
    """Jaeger may return ids as hex or base64; normalise to lowercase hex."""
    if not value:
        return ""
    if re.fullmatch(r"[0-9a-fA-F]{16}|[0-9a-fA-F]{32}", value):
        return value.lower()
    try:
        return base64.b64decode(value, validate=True).hex()
    except (binascii.Error, ValueError):
        return value


def _is_error(status: dict) -> bool:
    code = status.get("code")
    return code in (2, "2", "STATUS_CODE_ERROR", "Error", "ERROR")


_KINDS = {2: "server", 3: "client", 4: "producer", 5: "consumer"}


def _kind(value: Any) -> str:
    """OTLP span kind as a word; Jaeger may send the number (3) or the enum name (SPAN_KIND_CLIENT)."""
    try:
        return _KINDS.get(int(value), "internal")
    except (TypeError, ValueError):
        return str(value or "").replace("SPAN_KIND_", "").lower() or "internal"


def _call_target(span: dict) -> dict:
    """Who an outgoing (client) span was calling: raw target plus the matching known service, if any."""
    a = span["attributes"]
    target = (a.get("rpc.service") or (str(a.get("rpc.method") or "").rsplit("/", 1)[0] or None)
              or a.get("server.address") or a.get("peer.address") or a.get("url.full") or a.get("http.url"))
    if not target:
        return {"target": None, "service": None}
    target = str(target)
    host = re.sub(r"^\w+://", "", target).split("/")[0].split(":")[0]
    stem = re.sub(r"service$", "", target.rsplit(".", 1)[-1].lower())  # oteldemo.PaymentService -> payment
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())  # noqa: E731
    match = next((s for s in service_inventory() if s == host or norm(s) == norm(stem)), None)
    return {"target": target, "service": match}


def _parse_span(sp: dict, service: str) -> dict:
    attrs = _attrs(sp.get("attributes"))
    status = sp.get("status") or {}
    exception = None
    for ev in sp.get("events") or []:
        name = ev.get("name", "")
        if _FLAG_PATTERN.search(name):
            continue  # never pass feature-flag evaluations to the model
        if name == "exception":
            ea = _attrs(ev.get("attributes"))
            exception = _short(_redact(f"{ea.get('exception.type', '')}: {ea.get('exception.message', '')}"))
    try:
        dur_ms = (int(sp.get("endTimeUnixNano", 0)) - int(sp.get("startTimeUnixNano", 0))) / 1e6
    except (TypeError, ValueError):
        dur_ms = None
    return {
        "span_id": _hex_id(sp.get("spanId", "")),
        "parent_id": _hex_id(sp.get("parentSpanId", "")),
        "service": service,
        "name": sp.get("name"),
        "kind": _kind(sp.get("kind")),
        "duration_ms": round(dur_ms, 1) if dur_ms is not None else None,
        "error": _is_error(status),
        "error_message": _short(_redact(status.get("message"))) or exception,
        "start_ns": int(sp.get("startTimeUnixNano", 0) or 0),
        "attributes": {k: _short(v) for k, v in attrs.items()
                       if _SAFE_ATTR.match(k) and not _FLAG_PATTERN.search(k) and not _FLAG_PATTERN.search(str(v))},
        # Only the call's TARGET decides this. A span whose error text merely mentions a flag (propagated
        # from the failing service) is a normal span: its text is redacted, but it stays in the analysis.
        "calls_flag_service": bool(_FLAG_SERVICE.search(" ".join(
            str(attrs.get(k) or "") for k in _TARGET_ATTRS) + " " + str(sp.get("name") or ""))),
    }


def _error_origin(spans: list[dict]) -> dict | None:
    """Deepest error span whose children have no errors = where the failure starts.
    Calls to a feature-flag service are never candidates and never count as failing children,
    but they stay in the parent/child chain so depths are not distorted."""
    by_parent: dict[str, list[dict]] = {}
    for s in spans:
        by_parent.setdefault(s["parent_id"], []).append(s)
    candidates = [s for s in spans if s["error"] and not s["calls_flag_service"] and
                  not any(c["error"] and not c["calls_flag_service"] for c in by_parent.get(s["span_id"], []))]
    if not candidates:
        return None
    ids = {s["span_id"]: s for s in spans}

    def depth(s: dict) -> int:
        d, seen = 0, set()
        while s["parent_id"] in ids and s["parent_id"] not in seen:
            seen.add(s["parent_id"])
            s, d = ids[s["parent_id"]], d + 1
        return d

    o = max(candidates, key=depth)
    origin = {"service": o["service"], "operation": o["name"], "error_message": o["error_message"],
              "span_id": o["span_id"]}
    if o["kind"] == "client" and not by_parent.get(o["span_id"]):
        # The failing step is an outgoing call and the called service recorded nothing. The caller only
        # observed the failure, so it must not be named as the origin: name the callee, marked as suspected.
        callee = _call_target(o)
        origin.update(service=callee["service"] or "unknown", certainty="suspected", observed_in=o["service"],
                      callee_responded=False, called=callee["target"], called_service=callee["service"],
                      note="the caller's outgoing call failed and the called service produced no span: it may be "
                           "unreachable, stopped or not resolving, or its span is missing/late; not proof of the cause")
    return origin


SLOW_TRACE_MS = 1000  # only slow traces get a latency origin


def _latency_origin(spans: list[dict], root: dict) -> dict | None:
    """Where a slow trace spent its time: from the root, keep following the longest child while that
    child covers at least half of its parent. A caller that timed out (HTTP 504, gRPC DEADLINE_EXCEEDED)
    is not where the problem is; the deepest span on this path is."""
    if (root.get("duration_ms") or 0) < SLOW_TRACE_MS:
        return None
    by_parent: dict[str, list[dict]] = {}
    for s in spans:
        if not s["calls_flag_service"]:
            by_parent.setdefault(s["parent_id"], []).append(s)
    node, path = root, [root]
    while True:
        kids = [c for c in by_parent.get(node["span_id"], []) if c["duration_ms"] is not None]
        nxt = max(kids, key=lambda c: c["duration_ms"], default=None)
        if not nxt or nxt["duration_ms"] < 0.5 * (node["duration_ms"] or 0):
            break
        node = nxt
        path.append(node)
    kids = by_parent.get(node["span_id"], [])
    covered = sum(c["duration_ms"] or 0 for c in kids)
    unexplained = bool(kids) and covered < 0.5 * (node["duration_ms"] or 0)
    if node is root and not unexplained:
        return None  # no children, or time spread over many calls: no single slow step
    out = {"service": node["service"], "operation": node["name"], "duration_ms": node["duration_ms"],
           "trace_duration_ms": root["duration_ms"],
           "path": [f'{s["service"]}: {s["name"]}' for s in path][-5:]}
    if node["kind"] == "client":  # waiting on a call the callee did not record
        callee = _call_target(node)
        out.update(service=callee["service"] or "unknown", certainty="suspected", observed_in=node["service"],
                   called=callee["target"])
    elif unexplained:
        # It made calls, but the finished ones explain less than half of the time. Spans are exported only when they
        # end, so a downstream call that is still hanging is invisible here.
        out.update(certainty="unexplained",
                   note="no finished child span explains this time: either work inside this service, or a "
                        "downstream call still running (its span is not exported until it ends)")
    return out


_TIMEOUT_TEXT = re.compile(r"deadline|time[d ]?\s?out|timeout", re.IGNORECASE)


def _is_timeout(span: dict) -> bool:
    a = span["attributes"]
    return (str(a.get("http.status_code") or a.get("http.response.status_code") or "") == "504"
            or str(a.get("rpc.grpc.status_code") or "") == "4"
            or bool(_TIMEOUT_TEXT.search(str(span.get("error_message") or ""))))


def search_traces(service: str, window_minutes: int = 3, errors_only: bool = True,
                  limit: int = 10) -> ToolResult:
    """Recent traces for a service: error spans, suspected error origin per trace,
    and observed caller->callee edges (dependency map)."""
    service = validate_service(service)
    start, end = _window(window_minutes)
    limit = max(1, min(int(limit), MAX_ITEMS))
    params = {
        "query.service_name": service,
        "query.num_traces": min(limit * 4 if errors_only else limit, 100),
        "query.start_time_min": _rfc3339(start),
        "query.start_time_max": _rfc3339(end),
    }
    url = f"{JAEGER_URL}/jaeger/ui/api/v3/traces"
    # In a busy service a plain sample of recent traces can miss a failing operation entirely (seen live:
    # 40 frontend traces, none from the 28%-failing route), so error traces are also asked for explicitly.
    err_params = {**params, "query.attributes": json.dumps({"error": "true"}), "query.num_traces": 2 * limit}
    queries = [err_params, params] if errors_only else [params]
    query = " + ".join(f"GET /jaeger/ui/api/v3/traces {json.dumps(q)}" for q in queries)
    raw: dict = {"result": {"resourceSpans": []}}
    try:
        for q in queries:
            r = _request("GET", url, params=q)
            if r.status_code == 404:  # Jaeger v3 answers 404 when no traces match
                continue
            r.raise_for_status()
            raw["result"]["resourceSpans"] += (r.json().get("result") or {}).get("resourceSpans") or []
    except Exception as exc:
        return _failure(exc, "search_traces", "jaeger", service, query, start, end)
    if not raw["result"]["resourceSpans"]:
        return _result("search_traces", "jaeger", service, query, start, end,
                       ToolStatus.EMPTY, raw=raw, message="no traces in window")

    traces: dict[str, list[dict]] = {}
    seen_spans: set[tuple[str, str]] = set()  # a trace in both answers must not be counted twice
    for rs in (raw.get("result") or {}).get("resourceSpans") or []:
        svc = _attrs((rs.get("resource") or {}).get("attributes")).get("service.name", "unknown")
        if svc in EXCLUDED_SERVICES:
            continue  # flag-service spans (and their edges) are out of bounds
        for ss in rs.get("scopeSpans") or []:
            for sp in ss.get("spans") or []:
                key = (_hex_id(sp.get("traceId", "")), _hex_id(sp.get("spanId", "")))
                if key in seen_spans:
                    continue
                seen_spans.add(key)
                traces.setdefault(key[0], []).append(_parse_span(sp, svc))

    summaries, origin_counts, edges, unanswered, slow_counts = [], {}, {}, {}, {}
    for tid, spans in traces.items():
        ids = {s["span_id"]: s for s in spans}
        for s in spans:  # dependency edges from parent/child spans
            parent = ids.get(s["parent_id"])
            if parent and parent["service"] != s["service"]:
                key = f'{parent["service"]} -> {s["service"]}'
                edges[key] = edges.get(key, 0) + 1
        errs = [s for s in spans if s["error"] and not s["calls_flag_service"]]
        # Root = the longest span without a parent in this trace (a stray old span must not become the root).
        roots = [s for s in spans if s["parent_id"] not in ids] or spans
        root = max(roots, key=lambda s: (s["duration_ms"] or 0, -s["start_ns"]))
        slow = _latency_origin(spans, root)
        if errors_only and not errs and not slow:
            continue
        origin = _error_origin(spans)
        if origin:
            origin_counts[origin["service"]] = origin_counts.get(origin["service"], 0) + 1
            if origin.get("callee_responded") is False:
                who = origin["called_service"] or origin["called"] or "unknown"
                unanswered[who] = unanswered.get(who, 0) + 1
            o_span = ids.get(origin["span_id"])
            if o_span and _is_timeout(o_span) and slow and slow["service"] != origin["service"]:
                origin["timed_out_waiting"] = True
                origin["note"] = (f"this span failed because it waited too long; the time was spent in "
                                  f"{slow['service']} ({slow['operation']}), see latency_origin")
        if slow:
            who = slow["service"] + (" (unexplained)" if slow.get("certainty") == "unexplained" else "")
            slow_counts[who] = slow_counts.get(who, 0) + 1
        newest = max(s["start_ns"] for s in spans)
        summaries.append({
            "trace_id": tid,
            "age_s": max(0, round(end.timestamp() - newest / 1e9)) if newest else None,
            "root": f'{root["service"]}: {root["name"]}',
            "duration_ms": root["duration_ms"],
            "services": sorted({s["service"] for s in spans}),
            "error_spans": [{k: s[k] for k in ("service", "name", "error_message", "duration_ms", "attributes")}
                            for s in errs[:4]],
            "suspected_origin": origin,
            **({"latency_origin": slow} if slow else {}),
            "_start": newest,
        })
    summaries.sort(key=lambda t: (bool(t["error_spans"]), t["_start"]), reverse=True)  # error traces first
    for t in summaries:
        t.pop("_start")
    summaries = summaries[:limit]

    if not summaries:
        msg = ("traces found but none with errors or slow steps in window" if errors_only and traces
               else "no traces in window")
        return _result("search_traces", "jaeger", service, query, start, end, ToolStatus.EMPTY,
                       raw=raw, value={"traces_scanned": len(traces), "edges": edges}, message=msg)
    value = {
        "traces_scanned": len(traces),
        "error_origin_counts": dict(sorted(origin_counts.items(), key=lambda kv: -kv[1])),
        **({"latency_origin_counts": dict(sorted(slow_counts.items(), key=lambda kv: -kv[1]))}
           if slow_counts else {}),
        **({"calls_with_no_response_from": dict(sorted(unanswered.items(), key=lambda kv: -kv[1]))}
           if unanswered else {}),
        "edges": dict(sorted(edges.items(), key=lambda kv: -kv[1])[:20]),
        "traces": summaries,
    }
    return _result("search_traces", "jaeger", service, query, start, end, ToolStatus.OK,
                   raw=raw, value=value, trace_ids=[t["trace_id"] for t in summaries])


# --------------------------------------------------------------------------
# LOGS (OpenSearch PPL, read-only)
# --------------------------------------------------------------------------
_SEVERITY = {"DEBUG": 5, "INFO": 9, "WARN": 13, "ERROR": 17, "FATAL": 21}


def _ppl(query: str) -> dict:
    r = _request("POST", f"{OPENSEARCH_URL}/_plugins/_ppl", json={"query": query},
                 headers={"Content-Type": "application/json"}, timeout=OPENSEARCH_TIMEOUT_S)
    r.raise_for_status()
    return r.json()


def _rows(body: dict) -> list[dict]:
    cols = [c.get("name") for c in body.get("schema", [])]
    return [dict(zip(cols, row)) for row in body.get("datarows", [])]


def _pick(row: dict, *keys: str) -> Any:
    for k in keys:
        if row.get(k) not in (None, ""):
            return row[k]
    return None


def query_logs(service: str, window_minutes: int = 3, min_severity: str = "WARN",
               limit: int = 20) -> ToolResult:
    """Recent log lines for a service at or above a severity (WARN by default)."""
    service = validate_service(service)
    start, end = _window(window_minutes)
    min_severity = str(min_severity).upper()
    if min_severity not in _SEVERITY:
        raise TelemetryInputError(f"min_severity must be one of {', '.join(_SEVERITY)}")
    limit = max(1, min(int(limit), MAX_ITEMS))
    if not OPENSEARCH_URL:
        return _result("query_logs", "opensearch", service, "-", start, end, ToolStatus.UNAVAILABLE,
                       message="OPENSEARCH_URL not set. Find the port with: docker port opensearch 9200")
    since = start.strftime("%Y-%m-%d %H:%M:%S")
    sev = _SEVERITY[min_severity]
    primary = (f"source=otel-logs-* | where resource.service.name = '{service}'"
               f" and `@timestamp` >= '{since}' and `severity.number` >= {sev}"
               f" | sort - `@timestamp` | head {limit * 2}"
               f" | fields `@timestamp`, severity, body, traceId, _id")  # _id -> log_ids
    fallback = (f"source=otel-logs-* | where resource.service.name = '{service}'"
                f" and `@timestamp` >= '{since}' | sort - `@timestamp` | head {limit * 5}")
    used = primary
    try:
        try:
            body = _ppl(primary)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 400:
                raise
            used = fallback  # field names differ in this index: filter severity client-side
            body = _ppl(fallback)
    except Exception as exc:
        return _failure(exc, "query_logs", "opensearch", service, used, start, end)

    lines, dropped = [], 0
    for row in _rows(body):
        text = _pick(row, "body", "body.text", "message")
        sev_obj = row.get("severity") if isinstance(row.get("severity"), dict) else {}  # otel-logs-*: {"number", "text"}
        sev_text = str(_pick(sev_obj, "text") or _pick(row, "severity.text", "severityText", "severity_text") or "")
        sev_num = _pick(sev_obj, "number") or _pick(row, "severity.number", "severityNumber")
        if used == fallback:
            try:
                ok = int(sev_num) >= sev if sev_num is not None else _SEVERITY.get(sev_text.upper()[:5], 0) >= sev
            except (TypeError, ValueError):
                ok = True
            if not ok:
                continue
        if text and _FLAG_PATTERN.search(str(text)):
            dropped += 1  # keep that the line happened (time, severity, trace), withhold the flag wording
            text = FLAG_REDACTED
        lines.append({
            "time": _pick(row, "@timestamp", "observedTimestamp", "time"),
            "severity": sev_text or sev_num,
            "body": _short(text if not isinstance(text, (dict, list)) else json.dumps(text)),
            "trace_id": _hex_id(str(_pick(row, "traceId", "trace_id") or "")),
            "log_id": str(row.get("_id") or ""),
        })
        if len(lines) >= limit:
            break

    msg = f"text of {dropped} line(s) withheld by policy" if dropped else None
    if not lines:
        return _result("query_logs", "opensearch", service, used, start, end, ToolStatus.EMPTY,
                       raw=body, message=f"no {min_severity}+ logs in window" + (f"; {msg}" if msg else ""))
    return _result("query_logs", "opensearch", service, used, start, end, ToolStatus.OK, raw=body,
                   value={"min_severity": min_severity, "count": len(lines), "lines": lines},
                   excerpt=lines[0]["body"], message=msg,
                   trace_ids=sorted({l["trace_id"] for l in lines if l["trace_id"]}),
                   log_ids=[l["log_id"] for l in lines if l["log_id"]])


LOG_SOURCES_NOT_ASSESSED = {"otelcol-contrib", "otel-collector", "opensearch", "jaeger", "prometheus", "grafana"}
MAX_LOG_MESSAGES = 20
RECENT_LOG_MIN = 15  # incidents can follow each other minutes apart


def _log_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T")[:26]).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def log_overview(window_minutes: int = 3) -> ToolResult:
    """All services at once: WARN+ log messages grouped by service and message text, each with when it
    first and last appeared in the last BASELINE_MIN + window minutes. That timeline separates the
    current incident (a message that started recently) from baseline noise (present all along) and
    from an earlier incident (a message that stopped)."""
    start, end = _window(window_minutes, minimum=2)
    if not OPENSEARCH_URL:
        return _result("log_overview", "opensearch", None, "-", start, end, ToolStatus.UNAVAILABLE,
                       message="OPENSEARCH_URL not set. Find the port with: docker port opensearch 9200")
    fmt = "%Y-%m-%d %H:%M:%S"
    hist_start = start - timedelta(minutes=BASELINE_MIN)
    by = "by `resource.service.name`, `severity.text`, body | sort - n | head 200"
    where = f"source=otel-logs-* | where `@timestamp` >= '{{}}' and `severity.number` >= {_SEVERITY['WARN']}"
    now_q = f"{where.format(start.strftime(fmt))} | stats count() as n {by}"
    hist_q = (f"{where.format(hist_start.strftime(fmt))} | stats count() as n, min(`@timestamp`) as first, "
              f"max(`@timestamp`) as last {by}")
    query = json.dumps({"now": now_q, "history": hist_q})
    try:
        now_body, hist_body = _ppl(now_q), _ppl(hist_q)
    except Exception as exc:
        return _failure(exc, "log_overview", "opensearch", None, query, start, end)

    def rows(body: dict) -> dict[tuple, dict]:
        out: dict[tuple, dict] = {}
        for row in _rows(body):
            svc = row.get("resource.service.name")
            if not svc or svc in EXCLUDED_SERVICES or svc in LOG_SOURCES_NOT_ASSESSED:
                continue
            text = row.get("body")
            text = FLAG_REDACTED if text and _FLAG_PATTERN.search(str(text)) else _short(text)
            key = (svc, str(row.get("severity.text") or "").upper(), text)
            cur = out.setdefault(key, {"n": 0, "first": None, "last": None})
            cur["n"] += int(row.get("n") or 0)
            for k, pick in (("first", min), ("last", max)):
                t = _log_time(row.get(k))
                if t:
                    cur[k] = pick(cur[k], t) if cur[k] else t
        return out

    now, hist = rows(now_body), rows(hist_body)
    ago = lambda t: round((end - t).total_seconds() / 60, 1) if t else None  # noqa: E731
    span_min = BASELINE_MIN + window_minutes
    current, stopped = [], []
    for key, h in hist.items():
        svc, sev, text = key
        n_now = now.get(key, {}).get("n", 0)
        first, last = ago(h["first"]), ago(h["last"])
        item = {"service": svc, "severity": sev, "message": text, "count_now": n_now,
                f"count_last_{span_min}m": h["n"], "first_seen_min_ago": first, "last_seen_min_ago": last}
        if n_now:
            started = first is not None and first <= RECENT_LOG_MIN  # older messages belong to an earlier phase
            item["started_recently"] = started
            current.append(((started, -(first or span_min)), item))
        elif last is not None:
            stopped.append((last, item))
    current = [i for _, i in sorted(current, key=lambda t: t[0], reverse=True)][:MAX_LOG_MESSAGES]
    stopped = [i for _, i in sorted(stopped, key=lambda t: t[0])][:8]
    for i in stopped:
        i.pop("count_now")
    value = {
        "logging_now": current or "no WARN+ messages from application services in this window",
        **({"stopped_earlier": stopped} if stopped else {}),
        "note": (f"WARN and above in the last {span_min} min, grouped by service and exact message. "
                 f"started_recently=true: the message first appeared within the last {RECENT_LOG_MIN} min "
                 "(first_seen_min_ago), a strong lead for the current incident; the smaller first_seen_min_ago, the "
                 "newer. Older messages are the demo's baseline or an earlier incident unless their count jumped. "
                 "stopped_earlier = an earlier problem "
                 "that ended, not the current incident. A slow failure can log nothing until its first call gives up, "
                 f"so a quiet log is not proof of health. Not assessed: {', '.join(sorted(LOG_SOURCES_NOT_ASSESSED))}."),
    }
    status = ToolStatus.OK if hist else ToolStatus.EMPTY
    return _result("log_overview", "opensearch", None, query, start, end, status,
                   raw={"now": now_body, "history": hist_body}, value=value,
                   message=None if hist else f"no WARN+ log messages from application services in {span_min} min")


# --------------------------------------------------------------------------
# HEALTH (derived from telemetry only)
# --------------------------------------------------------------------------
def service_health(service: str, window_minutes: int = 3) -> ToolResult:
    """One-call health summary for a service, derived from metrics only:
    traffic, errors, latency, container CPU/memory/uptime (restart detection)."""
    service = validate_service(service)
    start, end = _window(window_minutes, minimum=2)
    parts = ["request_rate", "error_rate", "p95_latency_ms", "container_cpu",
             "container_memory", "container_memory_trend", "container_uptime"]
    summary, refs, statuses = {}, [], set()
    for m in parts:
        r = query_metrics(service, m, window_minutes)
        refs.append(r.evidence_id)
        statuses.add(r.status)
        summary[m] = r.value.get("value", r.value.get("series")) if r.status == ToolStatus.OK and r.value else r.status.value
    uptime = summary.get("container_uptime")
    if isinstance(uptime, (int, float)):
        summary["recently_restarted"] = uptime < window_minutes * 60
    if statuses == {ToolStatus.UNAVAILABLE}:
        status = ToolStatus.UNAVAILABLE
    elif ToolStatus.OK in statuses:
        status = ToolStatus.OK
    elif ToolStatus.ERROR in statuses:
        status = ToolStatus.ERROR
    else:
        status = ToolStatus.EMPTY
    # Sub-query IDs stay in the capture file only: they are not registered as Evidence,
    # so showing them to the model would invite citations the report gate rejects.
    return _result("service_health", "derived", service, ", ".join(parts), start, end, status,
                   raw={"sub_evidence": refs}, value={"summary": summary})


# --------------------------------------------------------------------------
# Registry for the diagnosis module (Member 3): validated, read-only tools only
# --------------------------------------------------------------------------
class _OverviewArgs(BaseModel):
    window_minutes: int = Field(NOW_WINDOW_MIN, ge=2, le=MAX_WINDOW_MIN)


class _MetricsArgs(BaseModel):
    service: str
    metric: MetricName = "error_rate"
    window_minutes: int = Field(NOW_WINDOW_MIN, ge=2, le=MAX_WINDOW_MIN)


class _TracesArgs(BaseModel):
    service: str
    window_minutes: int = Field(NOW_WINDOW_MIN, ge=1, le=MAX_WINDOW_MIN)
    errors_only: bool = True
    limit: int = Field(10, ge=1, le=MAX_ITEMS)


class _LogsArgs(BaseModel):
    service: str
    window_minutes: int = Field(NOW_WINDOW_MIN, ge=1, le=MAX_WINDOW_MIN)
    min_severity: Literal["DEBUG", "INFO", "WARN", "ERROR", "FATAL"] = "WARN"
    limit: int = Field(20, ge=1, le=MAX_ITEMS)


class _HealthArgs(BaseModel):
    service: str
    window_minutes: int = Field(NOW_WINDOW_MIN, ge=2, le=MAX_WINDOW_MIN)


TOOLS = {
    "overview": (overview, _OverviewArgs,
                 "All services: request rate, error rate, p95 latency. Start here."),
    "query_metrics": (query_metrics, _MetricsArgs,
                      "One fixed metric for one service (errors, latency, per-operation, container CPU/memory/uptime)."),
    "search_traces": (search_traces, _TracesArgs,
                      "Recent (error) traces for a service with trace IDs, error messages, suspected error origin and call edges."),
    "log_overview": (log_overview, _OverviewArgs,
                     "All services: WARN+ log messages grouped by message, with when each first and last appeared "
                     "in the last ~33 min (started recently / baseline / stopped). Call it with overview at the start."),
    "query_logs": (query_logs, _LogsArgs,
                   "Recent log lines for a service at or above a severity. Keep the default WARN: services in this "
                   "demo often log failures at WARN, so ERROR alone can miss them."),
    "service_health": (service_health, _HealthArgs,
                       "Health summary for one service from metrics: traffic, errors, latency, CPU, memory, restarts."),
}


def tool_specs() -> list[dict]:
    """OpenAI-style function specs for the LLM."""
    return [{"type": "function", "function": {
        "name": name, "description": desc, "parameters": args.model_json_schema()}}
        for name, (_, args, desc) in TOOLS.items()]


def run_tool(name: str, arguments: dict | None) -> ToolResult:
    """Validate and run one tool. Raises TelemetryInputError for unknown tools/bad args."""
    if name not in TOOLS:
        raise TelemetryInputError(f"unknown tool '{name}'")
    fn, args_model, _ = TOOLS[name]
    try:
        args = args_model.model_validate(arguments or {})
    except ValidationError as exc:
        raise TelemetryInputError(f"bad arguments for {name}: {exc.errors()[:3]}") from exc
    return fn(**args.model_dump())


# --------------------------------------------------------------------------
# CLI for CP0/CP1 checks
# --------------------------------------------------------------------------
def check_backends() -> dict[str, str]:
    """Connectivity check for CP0: is each telemetry backend reachable and returning data?"""
    report = {}
    for name, method, url in [
        ("prometheus", "GET", f"{PROM_URL}/-/ready"),
        ("jaeger", "GET", f"{JAEGER_URL}/jaeger/ui/api/v3/services"),
        ("opensearch", "GET", f"{OPENSEARCH_URL}/_cluster/health" if OPENSEARCH_URL else ""),
    ]:
        if not url:
            report[name] = "NOT CONFIGURED (set OPENSEARCH_URL; port: docker port opensearch 9200)"
            continue
        try:
            r = _request(method, url)
            report[name] = f"HTTP {r.status_code}" + (" OK" if r.status_code == 200 else "")
        except httpx.HTTPError as exc:
            report[name] = f"UNREACHABLE ({type(exc).__name__}) at {url}"
    inv = service_inventory(refresh=True)
    report["services_seen"] = f"{len(inv)}: {', '.join(inv)}" if inv else "none yet (wait for load)"
    return report


def _print(result: ToolResult) -> None:
    print(json.dumps(result.model_dump(mode="json", exclude={"window_start", "window_end"}),
                     indent=2, default=str))


def main() -> None:
    p = argparse.ArgumentParser(description="Read-only telemetry tools")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sub.add_parser("inventory")
    o = sub.add_parser("overview"); o.add_argument("--window", type=int, default=NOW_WINDOW_MIN)
    m = sub.add_parser("metrics"); m.add_argument("service"); m.add_argument("metric", nargs="?", default="error_rate")
    m.add_argument("--window", type=int, default=NOW_WINDOW_MIN)
    t = sub.add_parser("traces"); t.add_argument("service"); t.add_argument("--window", type=int, default=NOW_WINDOW_MIN)
    t.add_argument("--all", action="store_true", help="include traces without errors")
    lg = sub.add_parser("logs"); lg.add_argument("service"); lg.add_argument("--window", type=int, default=NOW_WINDOW_MIN)
    lg.add_argument("--severity", default="WARN")
    lo = sub.add_parser("log-overview"); lo.add_argument("--window", type=int, default=NOW_WINDOW_MIN)
    h = sub.add_parser("health"); h.add_argument("service"); h.add_argument("--window", type=int, default=NOW_WINDOW_MIN)
    n = sub.add_parser("names", help="list Prometheus metric names containing text")
    n.add_argument("contains")
    a = p.parse_args()
    try:
        if a.cmd == "check":
            for k, v in check_backends().items():
                print(f"{k:14} {v}")
        elif a.cmd == "inventory":
            print("\n".join(service_inventory(refresh=True)) or "(none)")
        elif a.cmd == "overview":
            _print(overview(a.window))
        elif a.cmd == "metrics":
            _print(query_metrics(a.service, a.metric, a.window))
        elif a.cmd == "traces":
            _print(search_traces(a.service, a.window, errors_only=not a.all))
        elif a.cmd == "logs":
            _print(query_logs(a.service, a.window, a.severity))
        elif a.cmd == "log-overview":
            _print(log_overview(a.window))
        elif a.cmd == "health":
            _print(service_health(a.service, a.window))
        elif a.cmd == "names":
            r = _request("GET", f"{PROM_URL}/api/v1/label/__name__/values")
            print("\n".join(x for x in r.json().get("data", []) if a.contains in x))
    except TelemetryInputError as exc:
        print(f"Input rejected: {exc}")


if __name__ == "__main__":
    main()
