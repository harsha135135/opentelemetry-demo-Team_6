"""Privileged execution component of the team Action API. Imported only by action_server.py, never by the agent,
the orchestrator or anything the LLM can reach.

Only fixed argv lists are run (no shell): `docker inspect` to check the container's identity, and
`docker restart --time <0-30> <container>`. Targets come from the server's allowlist and must carry the expected
compose service and project labels. The simulate executor changes nothing and says so in every result.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from typing import Any, Callable, Protocol

from action_api_models import ExecutionResult

# Never restartable, whatever the configuration says: flag/fault control, load, telemetry pipeline, observability,
# the agent stack itself.
HARD_DENY = frozenset({
    "flagd", "flagd-ui", "load-generator", "otel-collector", "jaeger", "prometheus", "grafana", "opensearch",
    "opamp-server", "telemetry-docs", "agent", "mcp", "chatbot", "otel-ebpf-profiler", "firepit",
})
APP_SERVICES = frozenset({
    "ad", "cart", "checkout", "currency", "email", "frontend", "image-provider", "payment", "product-catalog", "quote",
    "recommendation", "shipping",
    "accounting", "fraud-detection",  # Kafka consumer workers (compose.full.yaml)
})
# Off by default; enable explicitly with ACTION_API_EXTRA_TARGETS. Restarting stateful stores can lose data.
OPTIONAL_TARGETS = frozenset({"valkey-cart", "astronomy-db", "kafka", "frontend-proxy"})


def allowed_targets(extra: str = "") -> list[str]:
    requested = {t.strip() for t in extra.split(",") if t.strip()}
    unknown = requested - OPTIONAL_TARGETS
    if unknown:
        raise ValueError(f"ACTION_API_EXTRA_TARGETS may only enable {sorted(OPTIONAL_TARGETS)}; refused: {sorted(unknown)}")
    return sorted((APP_SERVICES | requested) - HARD_DENY)


class Executor(Protocol):
    mode: str

    def restart(self, target: str, grace_period_seconds: int) -> ExecutionResult: ...


class SimulatedExecutor:
    mode = "simulate"

    def restart(self, target: str, grace_period_seconds: int) -> ExecutionResult:
        return ExecutionResult(success=True, simulated=True, duration_s=0.0,
                               detail=f"SIMULATED: restart of '{target}' was not performed; no system state changed")


class DockerExecutor:
    mode = "docker"

    def __init__(self, project: str, run: Callable[..., Any] = subprocess.run, docker: str | None = None) -> None:
        self.project = project
        self.run = run
        self.docker = docker or shutil.which("docker")
        if not self.docker:
            raise RuntimeError("docker CLI not found; use ACTION_API_EXECUTOR=simulate")

    def _inspect(self, target: str) -> tuple[str | None, str]:
        fmt = '{{.State.Status}}|{{index .Config.Labels "com.docker.compose.service"}}|{{index .Config.Labels "com.docker.compose.project"}}'
        try:
            p = self.run([self.docker, "inspect", "--format", fmt, target], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, f"docker inspect failed: {type(exc).__name__}"
        if p.returncode != 0:
            return None, "container not found"
        parts = (p.stdout or "").strip().split("|")
        if len(parts) != 3:
            return None, "unexpected docker inspect output"
        status, service, project = parts
        if service != target or project != self.project:
            return None, f"identity mismatch: container '{target}' has service={service!r} project={project!r}"
        return status, "ok"

    def restart(self, target: str, grace_period_seconds: int) -> ExecutionResult:
        if target in HARD_DENY:
            return ExecutionResult(success=False, detail=f"'{target}' is never restartable")
        grace = int(grace_period_seconds)
        if not 0 <= grace <= 30:
            return ExecutionResult(success=False, detail="grace_period_seconds out of range")
        before, why = self._inspect(target)
        if before is None:
            return ExecutionResult(success=False, detail=f"precondition failed: {why}")
        started = time.monotonic()
        try:
            p = self.run([self.docker, "restart", "--time", str(grace), target], capture_output=True, text=True,
                         timeout=grace + 60)
        except subprocess.TimeoutExpired:
            return ExecutionResult(success=False, status_before=before, duration_s=round(time.monotonic() - started, 2),
                                   detail="docker restart timed out; container state unknown")
        except OSError as exc:
            return ExecutionResult(success=False, status_before=before, detail=f"docker restart failed: {type(exc).__name__}")
        after, _ = self._inspect(target)
        ok = p.returncode == 0 and after == "running"
        return ExecutionResult(success=ok, status_before=before, status_after=after, exit_code=p.returncode,
                               duration_s=round(time.monotonic() - started, 2),
                               detail="container restarted and running" if ok else
                               f"restart returned {p.returncode}; container status {after!r}")


# Read-only state view: restartable targets plus infrastructure whose health explains incidents. Flag services are
# deliberately absent (flag state is out of bounds); nothing here changes the system.
STATE_SERVICES = sorted((APP_SERVICES | OPTIONAL_TARGETS | {"load-generator", "otel-collector"}) - {"flagd", "flagd-ui"})


class ContainerInspector:
    """`docker inspect` only. One call for all containers; missing containers are reported as 'absent'."""

    FORMAT = ('{{.Name}}|{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|'
              '{{.RestartCount}}|{{.State.StartedAt}}|{{index .Config.Labels "com.docker.compose.service"}}|'
              '{{index .Config.Labels "com.docker.compose.project"}}')

    def __init__(self, project: str, run: Callable[..., Any] = subprocess.run, docker: str | None = None) -> None:
        self.project, self.run = project, run
        self.docker = docker or shutil.which("docker")

    def __call__(self, names: list[str]) -> list[dict[str, Any]]:
        from datetime import datetime, timezone

        if not self.docker:
            raise RuntimeError("docker CLI not available")
        p = self.run([self.docker, "inspect", "--format", self.FORMAT, *names], capture_output=True, text=True, timeout=15)
        seen: dict[str, dict[str, Any]] = {}
        now = datetime.now(timezone.utc)
        for line in (p.stdout or "").splitlines():
            parts = line.strip().split("|")
            if len(parts) != 7:
                continue
            name, status, health, restarts, started, service, project = parts
            name = name.lstrip("/")
            if name not in names:
                continue
            if service != name or project != self.project:
                seen[name] = {"service": name, "state": "identity_mismatch"}
                continue
            try:
                uptime = round((now - datetime.fromisoformat(started[:19] + "+00:00")).total_seconds())
            except ValueError:
                uptime = None
            seen[name] = {"service": name, "state": status, "health": health, "restart_count": int(restarts or 0),
                          "started_at": started[:19] + "Z", "uptime_s": uptime if status == "running" else None}
        return [seen.get(n, {"service": n, "state": "absent"}) for n in names]
