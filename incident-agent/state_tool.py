"""Read-only `container_state` investigation tool: live container state, health, restart count and uptime from the team
Action API (GET /v1/services). Permitted health information only; feature-flag state is never part of it."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel

import telemetry
from telemetry import ToolResult, ToolStatus

DESCRIPTION = ("Live container state from Docker via the Action API (read-only): state (running/exited/restarting/absent), "
               "health check, restart count and uptime for application services, workers, the cache, the database, the "
               "collector and the load generator (with its user count). Omit 'service' for the whole fleet.")
_KEEP = ("service", "state", "health", "restart_count", "uptime_s")


class StateArgs(BaseModel):
    service: Optional[str] = None


def make_state_tool(client: Any):
    def container_state(service: Optional[str] = None) -> ToolResult:
        now = datetime.now(timezone.utc)
        query = "GET /v1/services" + (f" (service={service})" if service else "")
        try:
            data = client.system_state()
        except Exception as exc:  # noqa: BLE001 - any failure is 'unavailable', never 'healthy'
            return telemetry._result("container_state", "health", service, query, now, now, ToolStatus.UNAVAILABLE,
                                     message=f"Action API state unavailable: {type(exc).__name__}")
        rows = [r for r in data.get("services") or [] if not service or r.get("service") == service]
        value: dict[str, Any] = {"services": [{k: r.get(k) for k in _KEEP} for r in rows], "source": data.get("source")}
        if not service or service == "load-generator":
            value["load_generator"] = data.get("load_generator")
        if not rows:
            return telemetry._result("container_state", "health", service, query, now, now, ToolStatus.EMPTY, raw=data,
                                     value=value, message="no state for this service (not in the state view or inspection unavailable)")
        down = [r["service"] for r in rows if r.get("state") != "running"]
        return telemetry._result("container_state", "health", service, query, now, now, ToolStatus.OK, raw=data, value=value,
                                 message=f"not running: {', '.join(down)}" if down else "all listed containers running")
    return container_state
