"""Deterministic recovery verification: compare fresh post-action telemetry with the pre-approval baseline.

An Action API success is never treated as recovery. Each check is judged on its own (multiple faults), error/latency
checks need minimum traffic, and missing data makes the result unverified, not recovered.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from models import Evidence, ToolStatus
from safety import VERIFICATION_METRICS, VerificationCheck

TRAFFIC_METRICS = {"error_rate", "p95_latency_ms"}
IMPROVEMENT = 0.5  # a check without an absolute bound recovers when it is at most half (or, for min checks, double) the baseline


def metric_value(ev: Optional[Evidence]) -> Optional[float]:
    if ev is None or ev.status != ToolStatus.OK:
        return None
    v = (ev.observed_value or {}).get("value")
    if isinstance(v, dict):
        v = v.get("value")
        if v is None:
            series = (ev.observed_value.get("value") or {}).get("series") or []
            vals = [s.get("value") for s in series if isinstance(s.get("value"), (int, float))]
            v = max(vals) if vals else None
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


class CheckResult(BaseModel):
    service: str
    metric: str
    baseline: Optional[float]
    samples: list[Optional[float]]
    bound: str
    state: str  # recovered | improved | persisting | no_data | unaffected | regressed | unmeasurable
    note: str = ""


class VerificationOutcome(BaseModel):
    outcome: str  # VERIFIED_RECOVERY | PARTIAL_RECOVERY | NOT_RECOVERED | UNVERIFIED
    checks: list[CheckResult] = Field(default_factory=list)
    details: str = ""


def _healthy(check: VerificationCheck, baseline: Optional[float], v: float) -> Optional[bool]:
    kind, default, *_ = VERIFICATION_METRICS[check.metric]
    if check.max_value is not None:
        return v <= check.max_value
    if check.min_value is not None:
        return v >= check.min_value
    if default is not None:
        return v <= default if kind == "max" else v >= default
    if baseline is None:
        return None
    return v <= baseline * IMPROVEMENT if kind == "max" else v >= baseline / IMPROVEMENT


def symptomatic(check: VerificationCheck, baseline: Optional[float]) -> Optional[bool]:
    """Was this check failing before the action? None when the baseline has no data (e.g. a stopped container)."""
    if baseline is None:
        return None
    kind, default, *_ = VERIFICATION_METRICS[check.metric]
    if check.max_value is None and check.min_value is None and default is None:
        return True  # relative checks are judged against the baseline itself
    return not _healthy(check, baseline, baseline)


def judge(checks: list[VerificationCheck], baseline: dict[tuple[str, str], Optional[float]],
          samples: list[dict[tuple[str, str], Optional[float]]], traffic: list[dict[str, Optional[float]]],
          min_request_rate: float, reduces_load: bool = False) -> VerificationOutcome:
    results: list[CheckResult] = []
    for c in checks:
        key = (c.service, c.metric)
        b = baseline.get(key)
        vals = [s.get(key) for s in samples]
        note = ""
        if c.metric in TRAFFIC_METRICS and any((t.get(c.service) or 0.0) < min_request_rate for t in traffic):
            vals, note = [None] * len(vals), f"request rate below {min_request_rate}/s: cannot judge"
        kind = VERIFICATION_METRICS[c.metric][0]
        bound = next((s for s in (f"<= {c.max_value}" if c.max_value is not None else None,
                                  f">= {c.min_value}" if c.min_value is not None else None) if s), "")
        if not bound:
            d = VERIFICATION_METRICS[c.metric][1]
            bound = f"{'<=' if kind == 'max' else '>='} {d}" if d is not None else f"{IMPROVEMENT:.0%} of baseline"
        was_failing = symptomatic(c, b)
        if b is None and vals and all(v is None for v in vals):
            state, note = "unmeasurable", note or "no data before or after the action"
        elif not vals or any(v is None for v in vals):
            state = "no_data"
        else:
            verdicts = [_healthy(c, b, v) for v in vals]
            if any(x is None for x in verdicts):
                state, note = "no_data", note or "no baseline for a relative check"
            elif was_failing is False:
                state = "unaffected" if all(verdicts) else "regressed"
            elif all(verdicts):
                state = "recovered"
            elif b is not None and (vals[-1] < b if kind == "max" else vals[-1] > b):
                state = "improved"
            else:
                state = "persisting"
        results.append(CheckResult(service=c.service, metric=c.metric, baseline=b, samples=vals, bound=bound, state=state, note=note))
    regressed = any(r.state == "regressed" for r in results)
    # Checks that were already healthy at baseline say nothing about recovery; they only guard against regressions.
    states = [r.state for r in results if r.state not in ("unaffected", "regressed", "unmeasurable")]
    if not states or all(s == "no_data" for s in states):
        outcome = "UNVERIFIED"
    elif regressed:
        outcome = "PARTIAL_RECOVERY" if {"recovered", "improved"} & set(states) else "NOT_RECOVERED"
    elif all(s == "recovered" for s in states):
        outcome = "PARTIAL_RECOVERY" if reduces_load else "VERIFIED_RECOVERY"
    elif "no_data" in states and not ({"improved", "persisting"} & set(states)):
        outcome = "UNVERIFIED"
    elif {"recovered", "improved"} & set(states):
        outcome = "PARTIAL_RECOVERY"
    else:
        outcome = "NOT_RECOVERED"
    parts = [f"{r.service}.{r.metric}: {r.state} (baseline {r.baseline}, after {r.samples}, target {r.bound})"
             + (f" [{r.note}]" if r.note else "") for r in results]
    if reduces_load and outcome == "PARTIAL_RECOVERY" and all(s == "recovered" for s in states):
        parts.append("action reduces load: symptoms may return when load is restored, so recovery counts as partial")
    return VerificationOutcome(outcome=outcome, checks=results, details="; ".join(parts))


def collect(checks: list[VerificationCheck], run_tool: Any, register: Any, window_minutes: int = 2
            ) -> tuple[dict[tuple[str, str], Optional[float]], dict[str, Optional[float]], list[str]]:
    """Query each check metric plus request_rate per traffic-checked service; register every result as evidence."""
    from integration import to_evidence

    values: dict[tuple[str, str], Optional[float]] = {}
    traffic: dict[str, Optional[float]] = {}
    ids: list[str] = []

    def one(service: str, metric: str) -> Optional[float]:
        try:
            ev = to_evidence(run_tool("query_metrics", {"service": service, "metric": metric, "window_minutes": window_minutes}))
        except Exception:  # a failed query is missing data, never a pass
            return None
        register(ev)
        ids.append(ev.evidence_id)
        return metric_value(ev)

    for c in checks:
        values[(c.service, c.metric)] = one(c.service, c.metric)
    for svc in sorted({c.service for c in checks if c.metric in TRAFFIC_METRICS}):
        traffic[svc] = one(svc, "request_rate")
    return values, traffic, ids


def observe(services: list[str], run_tool: Any, register: Any, window_minutes: int = 2
            ) -> tuple[dict[str, dict[str, Optional[float]]], list[str]]:
    """Unjudged context for the report: p95 latency, error-trace and error-log counts per service."""
    from integration import to_evidence

    out: dict[str, dict[str, Optional[float]]] = {}
    ids: list[str] = []

    def run(name: str, args: dict) -> Optional[Evidence]:
        try:
            ev = to_evidence(run_tool(name, args))
        except Exception:
            return None
        register(ev)
        ids.append(ev.evidence_id)
        return ev

    def count(ev: Optional[Evidence], key: str) -> Optional[float]:
        if ev is None or ev.status in (ToolStatus.UNAVAILABLE, ToolStatus.ERROR):
            return None
        if ev.status == ToolStatus.EMPTY:
            return 0.0
        v = (ev.observed_value or {}).get("value") or {}
        items = v.get(key) if isinstance(v, dict) else None
        return float(len(items)) if isinstance(items, list) else None

    def state(svc: str) -> Optional[str]:
        ev = run("container_state", {"service": svc})
        if ev is None or ev.status != ToolStatus.OK:
            return None
        rows = ((ev.observed_value or {}).get("value") or {}).get("services") or []
        return rows[0].get("state") if rows else None

    for svc in services:
        out[svc] = {
            "container_state": state(svc),
            "p95_latency_ms": metric_value(run("query_metrics", {"service": svc, "metric": "p95_latency_ms", "window_minutes": window_minutes})),
            "error_traces": count(run("search_traces", {"service": svc, "window_minutes": window_minutes, "errors_only": True, "limit": 5}), "traces"),
            "error_logs": count(run("query_logs", {"service": svc, "window_minutes": window_minutes, "min_severity": "ERROR", "limit": 5}), "lines"),
        }
    return out, ids


def describe_observations(before: dict, after: dict) -> str:
    parts = []
    for svc in sorted(set(before) | set(after)):
        b, a = before.get(svc, {}), after.get(svc, {})
        parts.append(f"{svc}: " + ", ".join(f"{k} {b.get(k)} -> {a.get(k)}" for k in ("container_state", "p95_latency_ms", "error_traces", "error_logs")))
    return "Observations (context, not judged): " + "; ".join(parts) if parts else ""
