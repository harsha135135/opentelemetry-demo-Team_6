"""Step 1: detect an incident from threshold alerts on the demo's metrics and start an investigation automatically.

    python cli.py watch                 # poll every 15 s until Ctrl+C
    python cli.py watch --once          # one check (prints what it sees)

Generic thresholds per service, each against that service's own 30-min baseline (no service or fault names):
  error rate   >= 2%, > 3x baseline + 1 point, and >= 3 failed calls in the 2-min window
  p95 latency  >= 1000 ms and >= 3x baseline
  both need >= 20 requests in the window (rates from a handful of requests are noise)
A breach must hold on 2 consecutive checks (filters one-sample blips). Every service breaching at the same time
goes into one alert; a service that starts breaching later raises a new alert (several faults can overlap).
After an alert, its services are quiet for COOLDOWN_S so one incident is not investigated twice.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

import telemetry

WINDOW_MIN, BASELINE_MIN = 2, 30  # 2 min: the shortest window that still holds 2 samples at 60 s resolution
ERR_MIN, ERR_FACTOR, ERR_POINTS, ERR_MIN_CALLS = 0.02, 3.0, 0.01, 3  # partial faults (one product, 10% of calls) stay in single digits
P95_MIN_MS, P95_FACTOR = 1000.0, 3.0
MIN_REQUESTS = 20  # per window: below this, error rates and p95 swing on a few requests
CONSECUTIVE = 2
COOLDOWN_S = 300

_C, _D = "traces_span_metrics_calls_total", "traces_span_metrics_duration_milliseconds_bucket"
QUERIES = {
    "rate": f"sum by (service_name) (rate({_C}[{WINDOW_MIN}m]))",
    "err": (f'sum by (service_name) (rate({_C}{{status_code="STATUS_CODE_ERROR"}}[{WINDOW_MIN}m]))'
            f" / sum by (service_name) (rate({_C}[{WINDOW_MIN}m]))"),
    "err_base": (f'sum by (service_name) (rate({_C}{{status_code="STATUS_CODE_ERROR"}}[{BASELINE_MIN}m] offset {WINDOW_MIN}m))'
                 f" / sum by (service_name) (rate({_C}[{BASELINE_MIN}m] offset {WINDOW_MIN}m))"),
    "p95": f"histogram_quantile(0.95, sum by (le, service_name) (rate({_D}[{WINDOW_MIN}m])))",
    "p95_base": f"histogram_quantile(0.95, sum by (le, service_name) (rate({_D}[{BASELINE_MIN}m] offset {WINDOW_MIN}m)))",
}


@dataclass
class Breach:
    service: str
    text: str


def measure() -> dict[str, dict[str, float]]:
    table: dict[str, dict[str, float]] = {}
    for key, q in QUERIES.items():
        rows, _ = telemetry._prom(q)
        for r in rows:
            svc = r["labels"].get("service_name")
            if svc and svc not in telemetry.EXCLUDED_SERVICES and svc not in telemetry.OWN_TOOLING:
                table.setdefault(svc, {})[key] = r["value"]
    return table


def breaches(table: dict[str, dict[str, float]]) -> list[Breach]:
    out = []
    for svc, m in sorted(table.items()):
        err, base, rate = m.get("err"), m.get("err_base") or 0.0, m.get("rate") or 0.0
        if (err is not None and err >= ERR_MIN and err >= ERR_FACTOR * base + ERR_POINTS
                and err * rate * WINDOW_MIN * 60 >= ERR_MIN_CALLS and rate * WINDOW_MIN * 60 >= MIN_REQUESTS):
            out.append(Breach(svc, f"error rate of {svc} is {err:.1%} (its 30-min baseline {base:.1%})"))
        p95, p95b = m.get("p95"), m.get("p95_base")
        enough = rate * WINDOW_MIN * 60 >= MIN_REQUESTS  # a rate or p95 from a handful of requests is noise
        if p95 is not None and p95b and enough and p95 >= P95_MIN_MS and p95 >= P95_FACTOR * p95b:
            out.append(Breach(svc, f"p95 latency of {svc} is {p95 / 1000:.1f} s (its 30-min baseline {p95b / 1000:.1f} s)"))
    return out


class Detector:
    """Stateful: consecutive-check filter and per-service cooldown."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.streak: dict[str, int] = {}
        self.quiet_until: dict[str, float] = {}

    def check(self, found: list[Breach]) -> list[Breach]:
        """Breaches that should raise an alert now."""
        now, seen = self.clock(), {b.service for b in found}
        self.streak = {s: self.streak.get(s, 0) + 1 for s in seen}
        ready = [b for b in found if self.streak[b.service] >= CONSECUTIVE and self.quiet_until.get(b.service, 0) <= now]
        for b in ready:
            self.quiet_until[b.service] = now + COOLDOWN_S
        return ready


def alert_text(ready: list[Breach]) -> str:
    return "Threshold alert: " + "; ".join(b.text for b in ready)


def new_incident_id() -> str:
    return datetime.now(timezone.utc).strftime("INC-%Y%m%d-%H%M%S")


def watch(investigate, interval_s: float = 15, once: bool = False, post_alert=None) -> int:
    """investigate(incident_id, symptom) runs one investigation; post_alert(incident_id, text) shows the alert."""
    det = Detector()
    print(f"Watching the demo's metrics every {interval_s:.0f} s (Ctrl+C to stop). Thresholds: error rate >= "
          f"{ERR_MIN:.0%} and > {ERR_FACTOR:.0f}x baseline; p95 >= {P95_MIN_MS / 1000:.0f} s and >= {P95_FACTOR:.0f}x baseline.")
    try:
        while True:
            try:
                found = breaches(measure())
            except Exception as exc:  # Prometheus briefly unreachable: keep watching
                print(f"[{datetime.now():%H:%M:%S}] metrics unavailable: {type(exc).__name__}")
                found = []
            ready = det.check(found)
            stamp = f"[{datetime.now():%H:%M:%S}]"
            if once:
                print(f"{stamp} over threshold now: {[b.text for b in found] or 'nothing'}")
                return 0
            if ready:
                inc_id, text = new_incident_id(), alert_text(ready)
                print(f"{stamp} ALERT {inc_id}: {text}\n{stamp} starting investigation automatically...")
                if post_alert:
                    print(post_alert(inc_id, text))
                investigate(inc_id, text)
                print(f"[{datetime.now():%H:%M:%S}] investigation {inc_id} finished; watching again.")
            elif found:
                print(f"{stamp} over threshold (waiting for a 2nd check or in cooldown): {[b.service for b in found]}")
            time.sleep(interval_s)
    except KeyboardInterrupt:
        print("stopped watching")
        return 0
