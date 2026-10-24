"""Alert watcher for `cli.py watch`: polls Prometheus and raises an alert when a service breaches a threshold on
consecutive checks. Adapted from Neha's detector on feature/neha2 (consecutive checks, own-tooling exclusions), using
the same span-metric and container queries as the investigation tools.

Signals: a container that stopped reporting, CPU far above one core, an error-rate jump against the service's own
earlier window, and a p95 latency jump. Memory is not alerted on (kafka and opensearch sit near their limits).
Breaches already present when the watcher starts, and services from a handled incident, stay quiet until they have
been healthy again, so one fault produces one incident rather than a new alert every few minutes.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

import telemetry

OWN_TOOLING = {"load-generator", "flagd", "flagd-ui", "otel-collector", "otelcol", "jaeger", "prometheus", "grafana",
               "opensearch"}
WINDOW_MIN = 2
PRIOR_OFFSET_MIN = 10
CPU_ALERT_PCT = float(os.getenv("WATCH_CPU_ALERT_PCT", "150"))   # percent of one core
ERROR_RATE_MIN = 0.05
ERROR_RPS_MIN = 0.05            # at least ~6 failed calls in the 2-minute window
P95_MIN_MS = 1000.0
JUMP = 3.0                      # p95 must be this many times its earlier window
REQ_RPS_MIN = 0.05
SILENT_AFTER_S = telemetry.SILENT_AFTER_S
SILENT_FLEET_FRAC = 0.8
SEVERITY = {"stopped": 4, "cpu": 3, "error_rate": 2, "p95_latency": 1}

_CALLS, _DUR = "traces_span_metrics_calls_total", "traces_span_metrics_duration_milliseconds_bucket"
_CPU = "container_cpu_usage_nanoseconds_total"
_UP = '{__name__=~"container_uptime.*"}'


def _q(w: int, offset: int = 0) -> dict[str, str]:
    off = f" offset {offset}m" if offset else ""
    return {
        "rps": f"sum by (service_name) (rate({_CALLS}[{w}m]{off}))",
        "err_rps": f'sum by (service_name) (rate({_CALLS}{{status_code="STATUS_CODE_ERROR"}}[{w}m]{off}))',
        "p95": f"histogram_quantile(0.95, sum by (le, service_name) (rate({_DUR}[{w}m]{off})))",
    }


QUERIES = {
    **{k: v for k, v in _q(WINDOW_MIN).items()},
    **{f"{k}_prior": v for k, v in _q(WINDOW_MIN, PRIOR_OFFSET_MIN).items()},
    "cpu": f"sum by (container_name) (rate({_CPU}[{WINDOW_MIN}m])) / 1e7",
    "last_seen_age": f"time() - max_over_time(timestamp({_UP})[60m:30s])",
}
ALERT_PROMQL = {
    "error_rate": ('(sum(rate({c}{{service_name="{s}",status_code="STATUS_CODE_ERROR"}}[{w}m])) or vector(0))'
                   ' / sum(rate({c}{{service_name="{s}"}}[{w}m]))'),
    "p95_latency": 'histogram_quantile(0.95, sum by (le) (rate({d}{{service_name="{s}"}}[{w}m])))',
    "cpu": 'sum(rate({u}{{container_name="{s}"}}[{w}m])) / 1e7',
    "stopped": 'time() - max_over_time(timestamp({{__name__=~"container_uptime.*",container_name="{s}"}})[60m:30s])',
}


@dataclass(frozen=True)
class Alert:
    service: str
    signal: str
    value: float
    threshold: float
    detail: str
    promql: str

    def as_dict(self) -> dict:
        return asdict(self)


def promql_for(signal: str, service: str) -> str:
    return ALERT_PROMQL[signal].format(c=_CALLS, d=_DUR, u=_CPU, s=service, w=WINDOW_MIN)


def _by(rows: list[dict], label: str) -> dict[str, float]:
    return {r["labels"][label]: r["value"] for r in rows if r["labels"].get(label)}


def evaluate(data: dict[str, dict[str, float]]) -> list[Alert]:
    """Pure function: current breaches, most severe first. `data` maps each QUERIES key to {name: value}."""
    out: list[Alert] = []
    ages = {n: a for n, a in data.get("last_seen_age", {}).items() if n not in OWN_TOOLING}
    silent = {n: a for n, a in ages.items() if a > SILENT_AFTER_S}
    if silent and len(silent) < SILENT_FLEET_FRAC * max(1, len(ages)):  # whole-fleet silence is a pipeline problem
        for n, a in silent.items():
            out.append(Alert(n, "stopped", round(a), SILENT_AFTER_S,
                             f"container {n} stopped reporting metrics {a:.0f}s ago while the rest of the fleet reports",
                             promql_for("stopped", n)))
    for n, c in data.get("cpu", {}).items():
        if n not in OWN_TOOLING and n not in silent and c >= CPU_ALERT_PCT:
            out.append(Alert(n, "cpu", round(c, 1), CPU_ALERT_PCT,
                             f"CPU {c:.0f}% of one core (alert at {CPU_ALERT_PCT:.0f}%)", promql_for("cpu", n)))
    rps, err = data.get("rps", {}), data.get("err_rps", {})
    rps_p, err_p = data.get("rps_prior", {}), data.get("err_rps_prior", {})
    for n, total in rps.items():
        if n in OWN_TOOLING or n in telemetry.EXCLUDED_SERVICES or total < REQ_RPS_MIN:
            continue
        e = err.get(n, 0.0)
        ratio = e / total
        prior = (err_p.get(n, 0.0) / rps_p[n]) if rps_p.get(n) else 0.0
        limit = max(ERROR_RATE_MIN, 2 * prior + 0.02)
        if ratio >= limit and e >= ERROR_RPS_MIN:
            out.append(Alert(n, "error_rate", round(ratio, 4), round(limit, 4),
                             f"error rate {ratio:.1%} over the last {WINDOW_MIN} min (was {prior:.1%} "
                             f"{PRIOR_OFFSET_MIN} min earlier; alert at {limit:.1%})", promql_for("error_rate", n)))
        p95, p95_p = data.get("p95", {}).get(n), data.get("p95_prior", {}).get(n)
        if p95 is not None and p95 >= P95_MIN_MS and (not p95_p or p95 >= JUMP * p95_p):
            out.append(Alert(n, "p95_latency", round(p95), P95_MIN_MS,
                             f"p95 latency {p95:.0f} ms (was {p95_p or 0:.0f} ms {PRIOR_OFFSET_MIN} min earlier)",
                             promql_for("p95_latency", n)))
    out.sort(key=lambda a: (-SEVERITY[a.signal], a.service))
    return out


def fetch(prom: Callable[[str], tuple[list[dict], object]] = telemetry._prom) -> dict[str, dict[str, float]]:
    data = {}
    for key, q in QUERIES.items():
        rows, _ = prom(q)
        data[key] = _by(rows, "container_name" if key in ("cpu", "last_seen_age") else "service_name")
    return data


@dataclass
class Detector:
    """Debounces breaches: an alert fires after `consecutive` checks in a row. Breaches present at the first check and
    services from a handled incident are muted until they have been clear for `clear_checks` checks."""
    consecutive: int = 2
    clear_checks: int = 3
    streak: dict[tuple[str, str], int] = field(default_factory=dict)
    muted: dict[str, int] = field(default_factory=dict)   # service -> clear checks seen so far
    started: bool = False

    def check(self, breaches: list[Alert]) -> list[Alert]:
        now = {(a.service, a.signal) for a in breaches}
        if not self.started:
            self.started = True
            for a in breaches:
                self.muted[a.service] = 0
        breaching = {a.service for a in breaches}
        for svc in list(self.muted):
            self.muted[svc] = 0 if svc in breaching else self.muted[svc] + 1
            if self.muted[svc] >= self.clear_checks:
                del self.muted[svc]
        self.streak = {k: self.streak.get(k, 0) + 1 for k in now}
        return [a for a in breaches if a.service not in self.muted and self.streak[(a.service, a.signal)] >= self.consecutive]

    def handled(self, services: list[str]) -> None:
        for s in services:
            self.muted[s] = 0
        self.streak = {}


def symptom(alerts: list[Alert]) -> str:
    head = alerts[0]
    text = f"ALERT from watcher ({head.signal}) on {head.service}: {head.detail}."
    if len(alerts) > 1:
        text += " Also firing: " + "; ".join(f"{a.service} {a.signal} ({a.detail})" for a in alerts[1:5]) + "."
    return text


def muted_summary(det: Detector) -> Optional[str]:
    return ", ".join(sorted(det.muted)) or None
