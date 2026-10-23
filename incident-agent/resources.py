"""resource_overview: one read-only call that compares every container's CPU, memory and uptime with the fleet and a prior window.

Lives in the integration layer because it needs a fleet-wide view (Neha's per-service tools do not have one). It uses only
telemetry's public names (PROM_URL, ToolResult, ToolStatus, EVIDENCE_DIR, EXCLUDED_SERVICES) and fixed PromQL templates.
Pending agreement with Neha (Member 2): if she adds an equivalent tool to telemetry.py, drop this module.

Flags are anomalies to investigate, not causes. CPU is percent of one core (can exceed 100); no CPU limit is exported,
so CPU is judged against the fleet median and the prior window, never an absolute "limit".
"""
from __future__ import annotations

import json
import statistics
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

import telemetry
from telemetry import ToolResult, ToolStatus

CPU, MEM, UP, MEM_BYTES = (
    "container_cpu_utilization_ratio", "container_memory_percent_ratio",
    "container_uptime_seconds", "container_memory_usage_total_bytes",
)
CPU_NS = "container_cpu_usage_nanoseconds_total"  # cumulative CPU time: its rate is the current usage
CPU_MIN_FLAG = 100.0      # percent of one core: below this a "spike" is not worth flagging
CPU_FLEET_MULT = 5.0      # with no prior window: flag when this many times the fleet median
CPU_BASE_MULT = 2.0       # flag when this many times its own prior-window average
MEM_PCT_FLAG = 85.0       # percent of the container memory limit ...
MEM_RISE_PTS = 10.0       # ... only if it rose this many points since the prior window (or no prior data)
MEM_GROWTH_FLAG = 0.15    # fractional growth over the window, counted only if memory is also
MEM_GROWTH_RISE_PTS = 5.0  # this many points above its prior average (GC sawtooth grows and falls back)
MEM_PEAK_MARGIN_PTS = 2.0  # and above its prior 30 min peak by this much: a swing back up is not growth
PRIOR_MIN = 30            # prior baseline = this many minutes before the current window (tolerates scrape gaps)
RESTART_CLUSTER_S = 90    # containers whose uptimes differ by less than this restarted "together"
RECENT_RESTART_S = 600    # a restart older than this is history (stable since), not a current incident
MAX_FLAGGED = 6
# The demo's normal baseline already runs some containers near their memory limits or at high CPU, so
# steady levels are not anomalies: only changes against the container's own prior window are flagged.
# Observability and tooling containers are reported in rows but never flagged (they are not the shop).
CONTAINER_COLUMNS = ["container", "cpu_pct", "cpu_prior_pct", "memory_pct", "memory_prior_pct",
                     "memory_growth_frac", "uptime_s", "restarts_30m"]  # prior = average over the 30 min before the window
NOT_FLAGGED = {"opensearch", "prometheus", "grafana", "jaeger", "otel-collector", "opamp-server", "telemetry-docs"}


class ResourceArgs(BaseModel):
    window_minutes: int = Field(telemetry.NOW_WINDOW_MIN, ge=2, le=telemetry.MAX_WINDOW_MIN)


DESCRIPTION = ("Fleet-wide container resources: CPU, memory %, memory growth and uptime per container, compared with the "
               "fleet median and a prior window. Returns flagged anomalies to investigate (flags are not causes).")


def _instant(client: httpx.Client, promql: str) -> dict[str, float]:
    r = client.get(f"{telemetry.PROM_URL}/api/v1/query", params={"query": promql})
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(body.get("error", "prometheus query failed"))
    out: dict[str, float] = {}
    for item in body["data"]["result"]:
        name = item.get("metric", {}).get("container_name")
        try:
            val = float(item["value"][1])
        except (KeyError, ValueError, TypeError):
            continue
        if name and val == val and val not in (float("inf"), float("-inf")):
            out[name] = val
    return out


def _r(v: float | None, nd: int = 1) -> float | None:
    return None if v is None else round(v, nd)


def _memory_runway(n: str, mem: dict, mem_bytes: dict, mem_slope: dict) -> str:
    """'rising 27 MB/min, ~8 min to its limit' when memory climbs steadily; '' otherwise."""
    pct, used, slope = mem.get(n), mem_bytes.get(n), mem_slope.get(n)
    if not pct or not used or not slope or slope <= 0:
        return ""
    limit = used * 100 / pct
    minutes = (limit - used) / slope / 60
    return f"memory {pct:.0f}% and rising {slope * 60 / 1e6:.1f} MB/min, ~{max(minutes, 0):.0f} min to its limit"


def analyse(cpu: dict, cpu_base: dict, mem: dict, mem_growth: dict, uptime: dict,
            mem_base: dict | None = None, restarts: dict | None = None, mem_bytes: dict | None = None,
            mem_slope: dict | None = None, mem_max: dict | None = None) -> dict[str, Any]:
    """Pure function: build per-container rows, fleet baselines and flagged anomalies (changes, not levels)."""
    mem_base, restarts, mem_bytes, mem_slope = mem_base or {}, restarts or {}, mem_bytes or {}, mem_slope or {}
    mem_max = mem_max or {}
    names = sorted(set(cpu) | set(mem) | set(uptime))
    med_cpu = statistics.median(cpu.values()) if cpu else None
    med_up = statistics.median(uptime.values()) if uptime else None
    rows, flagged, restarts_ended = [], [], []
    for n in names:
        if n in NOT_FLAGGED:
            continue  # listed by name only (keeps the model's view within its size limit)
        rows.append([n, _r(cpu.get(n)), _r(cpu_base.get(n)), _r(mem.get(n)), _r(mem_base.get(n)),
                     _r(mem_growth.get(n), 3), _r(uptime.get(n), 0), int(restarts.get(n) or 0)])
        reasons, severity = [], 0
        k = int(restarts.get(n) or 0)
        up = uptime.get(n)
        if k and up is not None and up >= RECENT_RESTART_S:
            # Restarts that stopped: an earlier problem that ended (e.g. its fault was switched off), not current.
            restarts_ended.append(f"{n}: {k} restart(s) in the last {PRIOR_MIN} min, none in the last {up / 60:.0f} min (stable now)")
        elif k >= 2:
            # A crash loop alternates between failing and briefly recovering: its callers' errors can look
            # "recovering" right after each restart, so it must stay visible as the current problem.
            runway = _memory_runway(n, mem, mem_bytes, mem_slope)
            reasons.append(f"CRASH LOOP: {k} restarts in the last {PRIOR_MIN} min (uptime now {uptime.get(n, 0):.0f}s)"
                           + (f"; {runway}" if runway else "")); severity = 4
        c, b = cpu.get(n), cpu_base.get(n)
        if c is not None and c >= CPU_MIN_FLAG:
            rose = b is not None and c >= CPU_BASE_MULT * max(b, 1.0)
            if rose:
                reasons.append(f"cpu {c:.0f}% is {c / max(b, 1.0):.1f}x its prior-window average ({b:.1f}%)"); severity = max(severity, 3)
            if med_cpu and c >= CPU_FLEET_MULT * med_cpu and (rose or b is None):
                reasons.append(f"cpu {c:.0f}% is {c / med_cpu:.1f}x the fleet median ({med_cpu:.1f}%)"); severity = max(severity, 3)
        m, mb, mx = mem.get(n), mem_base.get(n), mem_max.get(n)
        # A container whose memory swings (GC sawtooth) already reached this level in the prior window:
        # only memory above its prior peak is new.
        above_peak = mx is None or m is None or m > mx + MEM_PEAK_MARGIN_PTS
        if m is not None and m >= MEM_PCT_FLAG and (mb is None or m - mb >= MEM_RISE_PTS) and above_peak:
            since = (f", up from {mb:.0f}% in the prior window" + (f" (prior peak {mx:.0f}%)" if mx is not None else "")
                     if mb is not None else "")
            reasons.append(f"memory {m:.0f}% of limit{since}"); severity = max(severity, 3)
        g = mem_growth.get(n)
        if g is not None and g >= MEM_GROWTH_FLAG and above_peak and (mb is None or m is None or m - mb >= MEM_GROWTH_RISE_PTS):
            reasons.append(f"memory grew {g:.0%} over the window"); severity = max(severity, 2)
        if reasons:
            flagged.append({"container": n, "reasons": reasons, "severity": severity})
    # Clustered restarts: reported, neither dismissed as environmental nor assumed to be the cause.
    # Only restarts in the last RECENT_RESTART_S count as current (a container that came back long ago is stable).
    young = sorted((u, n) for n, u in uptime.items() if med_up and u < min(0.5 * med_up, RECENT_RESTART_S))
    clusters = []
    for u, n in young:
        if clusters and u - clusters[-1][-1][0] <= RESTART_CLUSTER_S:
            clusters[-1].append((u, n))
        else:
            clusters.append([(u, n)])
    restart_clusters = [{"containers": [n for _, n in c], "uptime_s": [round(u) for u, _ in c]} for c in clusters]
    for c in restart_clusters:
        for n in c["containers"]:
            if n not in NOT_FLAGGED and not any(f["container"] == n for f in flagged):
                runway = _memory_runway(n, mem, mem_bytes, mem_slope)
                flagged.append({"container": n, "reasons": [f"restarted recently (uptime {uptime[n]:.0f}s vs fleet median {med_up:.0f}s)"
                                                            + (f"; {runway}" if runway else "")], "severity": 2 if runway else 1})
    flagged.sort(key=lambda f: -f["severity"])
    return {"fleet": {"cpu_median_pct": med_cpu, "uptime_median_s": med_up, "containers": len(names)},
            "flagged": flagged[:MAX_FLAGGED], "flagged_total": len(flagged), "restart_clusters": restart_clusters,
            **({"restarts_ended": restarts_ended} if restarts_ended else {}),
            "note": ("Flags are changes against each container's own prior window, to investigate; they are not proven "
                     "causes. Steady high levels are the demo's baseline and are not flagged. High CPU may be effect or "
                     "load; clustered restarts may be a cause, a side effect or environmental."),
            "containers": {"columns": CONTAINER_COLUMNS, "rows": rows},
            "observability_containers_not_assessed": sorted(n for n in names if n in NOT_FLAGGED)}


def resource_overview(window_minutes: int = telemetry.NOW_WINDOW_MIN) -> ToolResult:
    now = datetime.now(timezone.utc)
    start, end = now - timedelta(minutes=window_minutes), now
    w = window_minutes
    # CPU from the CPU-time counter: container_cpu_utilization_ratio in this setup is a lifetime average since the
    # container started (flat for hours, seen live: ad at 400% showed as 26%), so it cannot show a change.
    cpu_now = f"rate({CPU_NS}[{w}m]) / 1e7"  # ns per s -> percent of one core
    queries = {
        "cpu": cpu_now,
        "cpu_prior": f"avg_over_time(({cpu_now})[{PRIOR_MIN}m:1m] offset {w}m)",
        # The 30 min before that: an incident older than ~15 min has already lifted the recent average.
        "cpu_prior_old": f"avg_over_time(({cpu_now})[{PRIOR_MIN}m:1m] offset {w + PRIOR_MIN}m)",
        "mem": f"{MEM}",
        "mem_prior": f"avg_over_time({MEM}[{PRIOR_MIN}m] offset {w}m)",
        "mem_prior_max": f"max_over_time({MEM}[{PRIOR_MIN}m] offset {w}m)",
        "mem_growth": f"deriv({MEM_BYTES}[{w}m]) * {w * 60} / {MEM_BYTES}",
        "uptime": f"{UP}",
        "restarts": f"resets({UP}[{PRIOR_MIN}m])",  # uptime dropping back = one restart
        "mem_bytes": f"{MEM_BYTES}",
        "mem_slope": f"deriv({MEM_BYTES}[{w}m])",
    }
    eid = f"ev-resource_overview-{uuid.uuid4().hex[:8]}"
    desc = "; ".join(f"{k}: {v}" for k, v in queries.items())

    def build(status, **kw) -> ToolResult:
        res = ToolResult(status=status, evidence_id=eid, tool="resource_overview", source="prometheus", service=None,
                         query=desc, window_start=start, window_end=end, collected_at=datetime.now(timezone.utc), **kw)
        try:
            d = Path(telemetry.EVIDENCE_DIR)
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"{eid}.json"
            path.write_text(json.dumps({"result": res.model_dump(mode="json")}, indent=2, default=str), encoding="utf-8")
            res.capture_ref = str(path)
        except OSError as exc:
            res.message = f"{res.message or ''} [capture failed: {exc}]".strip()
        return res

    try:
        with httpx.Client(timeout=telemetry.REQUEST_TIMEOUT_S, follow_redirects=False) as client:
            data = {k: _instant(client, q) for k, q in queries.items()}
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        return build(ToolStatus.UNAVAILABLE, message=f"prometheus not reachable: {type(exc).__name__}")
    except Exception as exc:
        return build(ToolStatus.ERROR, message=f"prometheus error: {type(exc).__name__}: {str(exc)[:200]}")
    for d in data.values():
        for ex in telemetry.EXCLUDED_SERVICES:
            d.pop(ex, None)
    if not data["cpu"] and not data["mem"] and not data["uptime"]:
        return build(ToolStatus.EMPTY, message="no container resource metrics in window (docker_stats not reporting?)")
    cpu_base = {n: min(v for v in (data["cpu_prior"].get(n), data["cpu_prior_old"].get(n)) if v is not None)
                for n in set(data["cpu_prior"]) | set(data["cpu_prior_old"])}
    value = analyse(data["cpu"], cpu_base, data["mem"], data["mem_growth"], data["uptime"],
                    mem_base=data["mem_prior"], restarts=data["restarts"], mem_bytes=data["mem_bytes"],
                    mem_slope=data["mem_slope"], mem_max=data["mem_prior_max"])
    value["baseline_available"] = bool(data["cpu_prior"] or data["mem_prior"])
    if not value["baseline_available"]:
        value["baseline_note"] = ("NO BASELINE: Prometheus has no data for the 30 min before this window (e.g. a "
                                  "history gap after a restart). Flags fall back to fleet comparison and fixed "
                                  "memory limits, so steady baseline levels may be flagged; treat them with caution.")
    return build(ToolStatus.OK, value=value,
                 excerpt="; ".join(f"{f['container']}: {', '.join(f['reasons'])}" for f in value["flagged"])[:300] or None)
