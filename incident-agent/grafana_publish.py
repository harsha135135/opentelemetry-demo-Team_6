"""Post a finished investigation to Grafana as an annotation (a marked time region on every graph, listed in the
"Agent diagnoses" panel). Team 6's own Grafana by default (grafana/compose.yaml, port 3001); never the
organisers' Grafana unless GRAFANA_URL is pointed there with their permission.

Env: GRAFANA_URL (default http://localhost:3001; empty = disabled), GRAFANA_USER, GRAFANA_PASSWORD.
Failure to post never fails the investigation: the report files are the record.
"""
from __future__ import annotations

import html
import os
from typing import Any

import httpx

TAG = "incident-agent"


def _ms(dt: Any) -> int:
    return int(dt.timestamp() * 1000)


def annotation(report: Any) -> dict:
    """Build the annotation body from an IncidentReport (pure: no network)."""
    inc, d = report.incident, report.diagnosis
    status = report.status.value
    meta = report.timeline[-1].metadata if report.timeline else {}
    lines = [f"<b>{html.escape(report.incident_id)}</b> - {status}"]
    tags = [TAG, status.lower()]
    if d:
        lines += [f"<b>Component:</b> {html.escape(d.suspected_component)}",
                  f"<b>Confidence:</b> {d.confidence_label.value} (where/how it fails, not why)",
                  f"<b>Observed / likely cause:</b> {html.escape(d.suspected_cause)}",
                  f"<b>Still unknown:</b> {html.escape(d.uncertainty)}",
                  f"<b>Evidence:</b> {html.escape(', '.join(d.supporting_evidence_ids))}"]
        if d.alternatives:
            lines.append(f"<b>Alternatives:</b> {html.escape('; '.join(d.alternatives))}")
        lines.append(f"<b>Next step:</b> {html.escape(d.next_step)}")
        for p in report.proposals:
            lines.append(f"<b>Proposed fix:</b> {html.escape(p.runbook_id)} on {html.escape(p.target_service)}: "
                         f"{html.escape(p.reason)} (awaiting human approval; not executed)")
        tags += [d.suspected_component.split(":")[0].strip()[:40], f"confidence:{d.confidence_label.value}"]
    else:
        lines.append("No diagnosis accepted: escalate to a human.")
        lines += [f"- {html.escape(x)}" for x in report.limitations[:3]]
    lines.append(f"Alert: {html.escape(inc.symptom)}")
    lines.append(f"Model calls {meta.get('model_calls')}, tool calls {meta.get('tool_calls')}, {meta.get('elapsed_s')} s")
    return {"time": _ms(inc.observation_window.start_time), "timeEnd": _ms(inc.observation_window.end_time),
            "tags": tags, "text": "<br>".join(lines)}


def reload_dashboards(timeout_s: float = 30) -> str:
    """Ask Grafana to re-read the dashboard files (file changes are not always noticed through Docker
    bind mounts on Windows)."""
    url = os.getenv("GRAFANA_URL", "http://localhost:3001").rstrip("/")
    if not url:
        return "Grafana: disabled (GRAFANA_URL empty)"
    auth = (os.getenv("GRAFANA_USER", "admin"), os.getenv("GRAFANA_PASSWORD", "incident-agent"))
    try:
        httpx.post(f"{url}/api/admin/provisioning/dashboards/reload", auth=auth, timeout=timeout_s).raise_for_status()
        return f"Grafana: incident pages reloaded ({url}/d/agent-incidents)"
    except httpx.HTTPError as exc:
        return f"Grafana: pages not reloaded ({type(exc).__name__}); they appear after a Grafana restart"


def clear_annotations(timeout_s: float = 10) -> str:
    """Remove this agent's annotations (tag incident-agent) from Team 6's Grafana. Never touches other dashboards."""
    url = os.getenv("GRAFANA_URL", "http://localhost:3001").rstrip("/")
    if not url:
        return "Grafana: disabled (GRAFANA_URL empty)"
    auth = (os.getenv("GRAFANA_USER", "admin"), os.getenv("GRAFANA_PASSWORD", "incident-agent"))
    removed = 0
    try:
        while True:
            batch = httpx.get(f"{url}/api/annotations", params={"tags": TAG, "limit": 500}, auth=auth,
                              timeout=timeout_s).raise_for_status().json()
            if not batch:
                break
            for a in batch:
                httpx.delete(f"{url}/api/annotations/{a['id']}", auth=auth, timeout=timeout_s).raise_for_status()
                removed += 1
        return f"Grafana: removed {removed} agent annotation(s)"
    except httpx.HTTPError as exc:
        return f"Grafana: annotations not cleared ({type(exc).__name__}: {str(exc)[:100]})"


def post_alert(incident_id: str, text: str, timeout_s: float = 5) -> str:
    """Step 1 on the dashboard: the threshold alert that started an investigation."""
    url = os.getenv("GRAFANA_URL", "http://localhost:3001").rstrip("/")
    if not url:
        return "Grafana: disabled (GRAFANA_URL empty)"
    auth = (os.getenv("GRAFANA_USER", "admin"), os.getenv("GRAFANA_PASSWORD", "incident-agent"))
    body = {"tags": [TAG, "alert"], "text": f"<b>ALERT {html.escape(incident_id)}</b><br>{html.escape(text)}"
                                            f"<br>Investigation started automatically."}
    try:
        httpx.post(f"{url}/api/annotations", json=body, auth=auth, timeout=timeout_s).raise_for_status()
        return "Grafana: alert posted"
    except httpx.HTTPError as exc:
        return f"Grafana: alert not posted ({type(exc).__name__})"


def publish(report: Any, timeout_s: float = 5) -> str:
    """Post the annotation. Returns a one-line status for the CLI."""
    url = os.getenv("GRAFANA_URL", "http://localhost:3001").rstrip("/")
    if not url:
        return "Grafana: disabled (GRAFANA_URL empty)"
    auth = (os.getenv("GRAFANA_USER", "admin"), os.getenv("GRAFANA_PASSWORD", "incident-agent"))
    try:
        r = httpx.post(f"{url}/api/annotations", json=annotation(report), auth=auth, timeout=timeout_s)
        r.raise_for_status()
        return f"Grafana: posted to {url}/d/incident-agent (annotation {r.json().get('id')})"
    except httpx.HTTPError as exc:
        return (f"Grafana: not posted ({type(exc).__name__}: {str(exc)[:120]}). "
                f"Start it with: docker compose -f grafana/compose.yaml up -d")
