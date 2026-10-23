"""Adapter between Neha's telemetry ToolResult and the shared Evidence model, plus the per-incident evidence registry."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from contracts import ToolPolicy
from models import Evidence, TimeWindow, ToolStatus


class EvidenceConflict(ValueError):
    pass


def to_evidence(result: Any) -> Evidence:
    """Map telemetry.ToolResult -> Evidence without dropping or inventing fields."""
    ids = list(result.trace_ids) + list(result.log_ids)
    return Evidence(
        evidence_id=result.evidence_id,
        status=ToolStatus(result.status.value),
        source=result.source,
        service=result.service,
        event_time_window=TimeWindow(start_time=result.window_start, end_time=result.window_end),
        collection_time=result.collected_at,
        tool=result.tool,
        query=result.query,
        observed_value={"value": result.value, "excerpt": result.excerpt, "message": result.message},
        trace_or_log_id=ids[0] if len(ids) == 1 else None,
        trace_ids=list(result.trace_ids),
        log_ids=list(result.log_ids),
        local_capture_reference=result.capture_ref,
    )


class EvidenceRegistry:
    def __init__(self) -> None:
        self._items: dict[str, Evidence] = {}

    def register(self, ev: Evidence) -> list[str]:
        """Add evidence; reject duplicate IDs. Returns warnings (e.g. missing capture)."""
        if ev.evidence_id in self._items:
            raise EvidenceConflict(f"duplicate evidence ID {ev.evidence_id}")
        self._items[ev.evidence_id] = ev
        ref = ev.local_capture_reference
        if not ref or not Path(ref).is_file():
            return [f"capture file missing for {ev.evidence_id}"]
        return []

    def all(self) -> list[Evidence]:
        return list(self._items.values())

    def get(self, evidence_id: str) -> Evidence | None:
        return self._items.get(evidence_id)


def all_tool_specs() -> list[dict]:
    """Neha's tool specs plus the integration-layer resource_overview tool (OpenAI-style, translated by the reasoner)."""
    import resources
    import telemetry

    extra = {"type": "function", "function": {"name": "resource_overview", "description": resources.DESCRIPTION,
                                              "parameters": resources.ResourceArgs.model_json_schema()}}
    return telemetry.tool_specs() + [extra]


def make_telemetry_runner(evidence_dir: Path) -> tuple[Callable[[str, dict | None], Any], ToolPolicy, Any]:
    """Point Neha's module at a per-run evidence directory; return (run_tool, policy, telemetry module)."""
    import telemetry

    import resources

    telemetry.EVIDENCE_DIR = Path(evidence_dir).resolve()
    inventory = [s for s in telemetry.service_inventory(refresh=True) if s not in telemetry.EXCLUDED_SERVICES]
    arg_models = {name: spec[1] for name, spec in telemetry.TOOLS.items()}
    arg_models["resource_overview"] = resources.ResourceArgs
    policy = ToolPolicy(approved_services=inventory, arg_models=arg_models)

    def run_tool(name: str, arguments: dict | None):
        if name == "resource_overview":
            return resources.resource_overview(**resources.ResourceArgs.model_validate(arguments or {}).model_dump())
        return telemetry.run_tool(name, arguments)

    return run_tool, policy, telemetry
