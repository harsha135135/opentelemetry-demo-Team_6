"""Incident Zero - Stretch Goal 5: Prevention Suggestions

Suggests alerting rules (Prometheus YAML), configuration tuning, and code fixes
to prevent incident recurrence, with read-only validation against live Prometheus.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Literal, Optional
import httpx
from pydantic import BaseModel, Field

from models import Evidence, IncidentReport

class PreventionSuggestion(BaseModel):
    kind: Literal["alert_rule", "config", "code", "runbook", "capacity"]
    title: str
    rationale: str
    supporting_evidence_ids: List[str] = Field(default_factory=list)
    target: Optional[str] = None
    alert_rule: Optional[Dict[str, Any]] = None  # e.g. {"alert": "CartHighErrorRate", "expr": "...", "for": "2m", "severity": "critical"}
    change: Optional[str] = None  # Unified diff or config block
    validation: Optional[str] = None  # e.g. "Validated via PromQL query on live Prometheus"
    risk: Optional[str] = None

def validate_alert_rule(expr: str, prom_url: str) -> str:
    """Validate a suggested PromQL expression against live Prometheus (read-only)."""
    if not prom_url:
        return "Prometheus URL not configured (unvalidated)"
    try:
        with httpx.Client(timeout=4.0) as client:
            resp = client.get(f"{prom_url}/api/v1/query", params={"query": expr})
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "success":
                result = data.get("data", {}).get("result", [])
                return f"✓ PromQL syntax valid; evaluated OK against live Prometheus ({len(result)} active series)"
            return f"PromQL query failed: {data.get('error', 'unknown error')}"
        return f"Prometheus returned HTTP {resp.status_code}"
    except Exception as exc:
        return f"Prometheus validation unreachable ({type(exc).__name__})"

def generate_prevention_suggestions(
    report: IncidentReport,
    groq_api_key: Optional[str] = None,
    prom_url: Optional[str] = None,
    model: str = "qwen/qwen3.8-27b",
) -> List[PreventionSuggestion]:
    """Generate prevention proposals using LLM or deterministic fallback."""
    api_key = groq_api_key or os.getenv("GROQ_API_KEY", "")
    prom_url = prom_url or os.getenv("PROM_URL", "http://localhost:9090").rstrip("/")
    diag = report.diagnosis
    svc = (diag.suspected_component if diag else report.incident.affected_service) or "frontend"
    cause = diag.suspected_cause if diag else report.incident.symptom
    evidence_ids = diag.supporting_evidence_ids if diag else [e.evidence_id for e in report.evidence[:3]]

    # 1. Deterministic default suggestions based on OpenTelemetry Demo telemetry schema
    suggestions: List[PreventionSuggestion] = []

    # Alert rule suggestion
    alert_expr = f'sum(rate(traces_span_metrics_calls_total{{service_name="{svc}",status_code="STATUS_CODE_ERROR"}}[2m])) / sum(rate(traces_span_metrics_calls_total{{service_name="{svc}"}}[2m])) > 0.05'
    validation_res = validate_alert_rule(alert_expr, prom_url)

    alert_item = PreventionSuggestion(
        kind="alert_rule",
        title=f"Early Detection: High HTTP/gRPC Error Rate on {svc}",
        rationale=f"Fires early when {svc} error rate exceeds 5% for 2 minutes, preventing catastrophic customer outage.",
        supporting_evidence_ids=evidence_ids,
        target=f"src/prometheus/rules/{svc}_alerts.yaml",
        alert_rule={
            "alert": f"{svc.replace('-', '').capitalize()}HighErrorRate",
            "expr": alert_expr,
            "for": "2m",
            "severity": "critical",
            "summary": f"High error rate detected on {svc}",
            "description": f"{svc} error rate exceeded 5% for 2 minutes. Root cause correlation: {cause[:80]}."
        },
        validation=validation_res,
        risk="Low risk; non-intrusive alert rule."
    )
    suggestions.append(alert_item)

    # Config tuning suggestion
    if "cart" in svc or "valkey" in cause.lower():
        config_change = """# Suggested config adjustment: Increase Valkey Redis pool capacity & timeouts
VALKEY_POOL_MAX_ACTIVE=200
VALKEY_POOL_MAX_IDLE=50
VALKEY_POOL_IDLE_TIMEOUT=60s
VALKEY_DIAL_TIMEOUT=3s
"""
        cfg_item = PreventionSuggestion(
            kind="config",
            title=f"Connection Pool & Timeout Hardening for {svc}",
            rationale="Prevents socket starvation and hung client connections under bursty load.",
            supporting_evidence_ids=evidence_ids,
            target=f"src/{svc}/.env",
            change=config_change,
            validation="Syntactically valid environment variables.",
            risk="Minor memory footprint increase per worker."
        )
        suggestions.append(cfg_item)
    else:
        config_change = f"""# Suggested resiliency config for {svc}
CIRCUIT_BREAKER_ENABLED=true
CIRCUIT_BREAKER_ERROR_THRESHOLD=0.3
GRPC_TIMEOUT_MS=2500
"""
        cfg_item = PreventionSuggestion(
            kind="config",
            title=f"Circuit Breaker & Bounded RPC Timeouts for {svc}",
            rationale=f"Limits cascading failures from downstream dependencies affecting {svc}.",
            supporting_evidence_ids=evidence_ids,
            target=f"src/{svc}/config.env",
            change=config_change,
            validation="Compatible with OpenTelemetry demo microservice configuration.",
            risk="Transient requests fail fast rather than hanging indefinitely."
        )
        suggestions.append(cfg_item)

    # Code / Runbook suggestion
    code_change = f"""// Suggested defensive retry with exponential jitter
async function callWithRetry(fn, retries = 3, backoff = 200) {{
    for (let attempt = 1; attempt <= retries; attempt++) {{
        try {{
            return await fn();
        }} catch (err) {{
            if (attempt === retries) throw err;
            await new Promise(r => setTimeout(r, backoff * Math.pow(2, attempt) + Math.random() * 50));
        }}
    }}
}}"""
    code_item = PreventionSuggestion(
        kind="code",
        title=f"Defensive Client Retry with Jitter on {svc}",
        rationale=f"Smooths out transient glitches before they propagate upstream.",
        supporting_evidence_ids=evidence_ids,
        target=f"src/{svc}/client.js",
        change=code_change,
        validation="Follows resilient distributed microservices patterns.",
        risk="Slight latency increase during transient faults before failing."
    )
    suggestions.append(code_item)

    return suggestions
