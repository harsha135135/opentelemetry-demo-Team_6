"""Operator-side endpoint configuration: which backend each tool uses, reachability, browser links and .env discovery.

Discovery reads Docker port mappings once at setup (published host ports are dynamic and change on every restart).
It is never offered to the model and never runs during an investigation.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlencode

import httpx

# name, env var, container, container port, readiness path, browser path, tools that use it
BACKENDS = [
    ("prometheus", "PROM_URL", "prometheus", 9090, "/-/ready", "/query",
     ["overview", "query_metrics", "service_health", "resource_overview", "verification"]),
    ("jaeger", "JAEGER_URL", "jaeger", 16686, "/jaeger/ui/api/v3/services", "/jaeger/ui/search", ["search_traces"]),
    ("opensearch", "OPENSEARCH_URL", "opensearch", 9200, "/_cluster/health", None, ["query_logs"]),
    ("grafana", "GRAFANA_URL", "grafana", 3000, "/grafana/api/health", "/grafana/", ["(browser only)"]),
]
_PORT_LINE = re.compile(r"^0\.0\.0\.0:(\d+)$")


def _url(env: str) -> str:
    return os.getenv(env, "").rstrip("/")


def discover(run=subprocess.run) -> dict[str, str]:
    """Map env var -> http://localhost:<published port> for each backend container (Docker port mappings only)."""
    found: dict[str, str] = {}
    for _, env, container, port, *_ in BACKENDS:
        try:
            out = run(["docker", "port", container, str(port)], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in out.splitlines():
            m = _PORT_LINE.match(line.strip())
            if m:
                found[env] = f"http://localhost:{m.group(1)}"
                break
    return found


def write_env(path: Path, values: dict[str, str]) -> list[str]:
    """Insert or update only the given keys in a .env file; every other line (including secrets) is kept unchanged."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    changed, seen = [], set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in values:
            seen.add(key)
            if line != f"{key}={values[key]}":
                lines[i] = f"{key}={values[key]}"
                changed.append(key)
    for key, val in values.items():
        if key not in seen:
            lines.append(f"{key}={val}")
            changed.append(key)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


def check(timeout: float = 5.0) -> list[dict[str, Any]]:
    rows = []
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        for name, env, _, _, ready, browse, tools in BACKENDS:
            base = _url(env)
            row = {"backend": name, "env": env, "url": base or "-", "tools": ", ".join(tools),
                   "browser": f"{base}{browse}" if base and browse else "-"}
            if not base:
                row["status"] = "NOT CONFIGURED"
            else:
                try:
                    r = client.get(f"{base}{ready}")
                    row["status"] = f"HTTP {r.status_code}" + (" OK" if r.status_code == 200 else "")
                except httpx.HTTPError as exc:
                    row["status"] = f"UNREACHABLE ({type(exc).__name__})"
            rows.append(row)
    return rows


def action_api_status(catalog_path: Optional[str]) -> dict[str, str]:
    from actions import CatalogError, load_catalog

    url = _url("ACTION_API_URL")
    if url and os.getenv("ACTION_API_TOKEN") and not catalog_path:
        try:
            h = httpx.get(f"{url}/health", timeout=3).json()
            return {"backend": "action-api", "env": "ACTION_API_URL", "url": url,
                    "status": f"team API {h.get('api_version')} executor={h.get('executor')} "
                              f"kill_switch={'ON' if h.get('kill_switch_active') else 'off'}"}
        except (httpx.HTTPError, ValueError):
            return {"backend": "action-api", "env": "ACTION_API_URL", "url": url,
                    "status": "team API UNREACHABLE (python cli.py action-api serve)"}
    if not catalog_path:
        return {"backend": "action-api", "status": "NOT CONFIGURED (no ACTION_API_CATALOG; remediation escalates)"}
    try:
        cat = load_catalog(catalog_path)
    except CatalogError as exc:
        return {"backend": "action-api", "status": f"INVALID CATALOG: {exc}"}
    live = cat.provenance == "organiser" and bool(cat.base_url)
    return {"backend": "action-api", "env": cat.base_url_env, "url": cat.base_url or "-",
            "status": f"{len(cat.operations)} operation(s), provenance={cat.provenance}, "
                      + ("live dispatch possible after approval" if live else "dry-run only")}


def promql_of(ev: Any) -> list[tuple[str, str]]:
    """(name, PromQL) pairs that reproduce a Prometheus evidence item, whatever format its tool stored the query in."""
    if not ev.query:
        return []
    if ev.tool == "service_health" and ev.service:  # stores template names; rebuild them for the service and window
        from telemetry import METRIC_TEMPLATES

        w = max(2, round((ev.event_time_window.end_time - ev.event_time_window.start_time).total_seconds() / 60))
        return [(m, METRIC_TEMPLATES[m][0].replace("{s}", ev.service).replace("{w}", str(w)))
                for m in ev.query.split(", ") if m in METRIC_TEMPLATES]
    if ev.source != "prometheus":
        return []
    try:  # overview stores a JSON object of named queries
        return [(str(k), str(v)) for k, v in json.loads(ev.query).items()]
    except (ValueError, AttributeError):
        pass
    if "; " in ev.query and ": " in ev.query:  # resource_overview stores "name: promql; name: promql"
        return [(name, expr) for name, expr in (part.split(": ", 1) for part in ev.query.split("; ") if ": " in part)]
    value = ev.observed_value.get("value") if isinstance(ev.observed_value, dict) else None
    return [(str((value or {}).get("metric") or ev.tool) if isinstance(value, dict) else ev.tool, ev.query)]


def evidence_links(ev: Any, limit: int = 3) -> list[str]:
    """Browser URLs that reproduce one evidence item (Prometheus query page, Jaeger traces or search)."""
    links: list[str] = []
    if ev.source == "prometheus" and _url("PROM_URL") and ev.query:
        for _, expr in promql_of(ev)[:limit]:
            links.append(f"{_url('PROM_URL')}/query?" + urlencode({"g0.expr": expr, "g0.tab": "table"}))
    elif ev.source == "jaeger" and _url("JAEGER_URL"):
        ids = list(ev.trace_ids)[:limit]
        if ids:
            links.extend(f"{_url('JAEGER_URL')}/jaeger/ui/trace/{quote(t)}" for t in ids)
        elif ev.service:
            links.append(f"{_url('JAEGER_URL')}/jaeger/ui/search?" + urlencode({"service": ev.service}))
    return links
