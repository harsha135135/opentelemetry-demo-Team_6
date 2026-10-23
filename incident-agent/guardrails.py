"""Guardrails. PLACEHOLDER implementation for Member 4 (Deepika) so the flow runs end to end; replace with the real module."""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from contracts import DiagnosisValidation, ToolPolicy, ToolRequest, ToolValidation
from models import Diagnosis, Evidence, ToolStatus

BLOCKED_SERVICES = {"flagd", "flagd-ui"}


def validate_tool_request(request: ToolRequest, policy: ToolPolicy) -> ToolValidation:
    model = policy.arg_models.get(request.tool_name)
    if model is None:
        return ToolValidation(allowed=False, reasons=[f"tool '{request.tool_name}' is not registered"])
    unknown = set(request.arguments) - set(model.model_fields)
    if unknown:
        return ToolValidation(allowed=False, reasons=[f"unexpected arguments: {sorted(unknown)}"])
    try:
        args = model.model_validate(request.arguments).model_dump()
    except ValidationError as exc:
        return ToolValidation(allowed=False, reasons=[f"invalid arguments: {exc.errors()[:3]}"])
    service = args.get("service")
    if "service" in model.model_fields:
        if service in BLOCKED_SERVICES:
            return ToolValidation(allowed=False, reasons=[f"service '{service}' is out of bounds"])
        if service not in policy.approved_services:
            return ToolValidation(allowed=False, reasons=[f"service '{service}' is not in the approved inventory"])
    if request.remaining_tool_calls <= 0:
        return ToolValidation(allowed=False, reasons=["tool-call budget exhausted"])
    return ToolValidation(allowed=True, tool_name=request.tool_name, arguments=args)


def validate_diagnosis(diagnosis: Diagnosis, evidence: list[Evidence]) -> DiagnosisValidation:
    errors: list[str] = []
    by_id = {e.evidence_id: e for e in evidence}
    cited = diagnosis.supporting_evidence_ids + diagnosis.conflicting_evidence_ids
    if not diagnosis.supporting_evidence_ids:
        errors.append("diagnosis cites no supporting evidence")
    for eid in cited:
        ev = by_id.get(eid)
        if ev is None:
            errors.append(f"cited evidence '{eid}' was not captured in this investigation")
        elif not ev.local_capture_reference or not Path(ev.local_capture_reference).is_file():
            errors.append(f"capture for evidence '{eid}' is missing on disk")
    if set(diagnosis.supporting_evidence_ids) & set(diagnosis.conflicting_evidence_ids):
        errors.append("an evidence ID is cited as both supporting and conflicting")
    supporting = [by_id[i] for i in diagnosis.supporting_evidence_ids if i in by_id]
    if supporting and not any(e.status == ToolStatus.OK for e in supporting):
        errors.append("no supporting evidence has status 'ok' (empty/unavailable/error results cannot support a cause)")
    if supporting:
        component = diagnosis.suspected_component.lower()
        haystack = " ".join(
            f"{e.service or ''} {e.query} {json.dumps(e.observed_value, default=str)}" for e in supporting
        ).lower()
        if component not in haystack:
            errors.append(f"suspected component '{diagnosis.suspected_component}' does not appear in any cited supporting evidence (heuristic check)")
    return DiagnosisValidation(valid=not errors, errors=errors)
