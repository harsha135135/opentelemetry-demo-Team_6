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
CPU_MIN_FLAG = 100.0      # percent of one core: below this a "spike" is not worth flagging
CPU_FLEET_MULT = 5.0      # flag when this many times the fleet median ...
CPU_BASE_MULT = 2.0       # ... or this many times its own prior-window average
MEM_PCT_FLAG = 85.0       # percent of the container memory limit
MEM_GROWTH_FLAG = 0.15    # fractional growth over the window
RESTART_CLUSTER_S = 90    # containers whose uptimes differ by less than this restarted "together"
MAX_FLAGGED = 4


class ResourceArgs(BaseModel):
    window_minutes: int = Field(5, ge=2, le=telemetry.MAX_WINDOW_MIN)


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


def analyse(cpu: dict, cpu_base: dict, mem: dict, mem_growth: dict, uptime: dict) -> dict[str, Any]:
    """Pure function: build per-container rows, fleet baselines and flagged anomalies."""
    names = sorted(set(cpu) | set(mem) | set(uptime))
    med_cpu = statistics.median(cpu.values()) if cpu else None
    med_up = statistics.median(uptime.values()) if uptime else None
    rows, flagged = [], []
    for n in names:
        row = {"container": n, "cpu_pct": cpu.get(n), "cpu_prior_window_pct": cpu_base.get(n),
               "memory_pct": mem.get(n), "memory_growth_frac": mem_growth.get(n), "uptime_s": uptime.get(n)}
        reasons, severity = [], 0
        c, b = cpu.get(n), cpu_base.get(n)
        if c is not None and c >= CPU_MIN_FLAG:
            if med_cpu and c >= CPU_FLEET_MULT * med_cpu:
                reasons.append(f"cpu {c:.0f}% is {c / med_cpu:.1f}x the fleet median ({med_cpu:.1f}%)"); severity = max(severity, 3)
            if b and c >= CPU_BASE_MULT * b:
                reasons.append(f"cpu {c:.0f}% is {c / b:.1f}x its prior-window average ({b:.1f}%)"); severity = max(severity, 3)
        if mem.get(n) is not None and mem[n] >= MEM_PCT_FLAG:
            reasons.append(f"memory {mem[n]:.0f}% of limit"); severity = max(severity, 3)
        g = mem_growth.get(n)
        if g is not None and g >= MEM_GROWTH_FLAG:
            reasons.append(f"memory grew {g:.0%} over the window"); severity = max(severity, 2)
        if reasons:
            flagged.append({"container": n, "reasons": reasons, "severity": severity})
        rows.append(row)
    # Clustered restarts: reported, neither dismissed as environmental nor assumed to be the cause.
    young = sorted((u, n) for n, u in uptime.items() if med_up and u < 0.5 * med_up)
    clusters = []
    for u, n in young:
        if clusters and u - clusters[-1][-1][0] <= RESTART_CLUSTER_S:
            clusters[-1].append((u, n))
        else:
            clusters.append([(u, n)])
    restart_clusters = [{"containers": [n for _, n in c], "uptime_s": [round(u) for u, _ in c]} for c in clusters]
    for c in restart_clusters:
        for n in c["containers"]:
            if not any(f["container"] == n for f in flagged):
                flagged.append({"container": n, "reasons": [f"restarted recently (uptime {uptime[n]:.0f}s vs fleet median {med_up:.0f}s)"], "severity": 1})
    flagged.sort(key=lambda f: -f["severity"])
    return {"fleet": {"cpu_median_pct": med_cpu, "uptime_median_s": med_up, "containers": len(names)},
            "flagged": flagged[:MAX_FLAGGED], "flagged_total": len(flagged), "restart_clusters": restart_clusters,
            "note": "Flags are anomalies to investigate, not proven causes. High CPU may be effect or load; clustered restarts may be a cause, a side effect or environmental.",
            "containers": rows}


def resource_overview(window_minutes: int = 5) -> ToolResult:
    now = datetime.now(timezone.utc)
    start, end = now - timedelta(minutes=window_minutes), now
    w = window_minutes
    queries = {
        "cpu": f"{CPU}",
        "cpu_prior": f"avg_over_time({CPU}[{w}m] offset {w + 5}m)",
        "mem": f"{MEM}",
        "mem_growth": f"deriv({MEM_BYTES}[{w}m]) * {w * 60} / {MEM_BYTES}",
        "uptime": f"{UP}",
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
    value = analyse(data["cpu"], data["cpu_prior"], data["mem"], data["mem_growth"], data["uptime"])
    if not data["cpu_prior"]:
        value["baseline_note"] = "no prior-window CPU data available; CPU judged against the fleet median only"
    return build(ToolStatus.OK, value=value,
                 excerpt="; ".join(f"{f['container']}: {', '.join(f['reasons'])}" for f in value["flagged"])[:300] or None)
