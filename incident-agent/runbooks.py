"""Step 4: propose a remedy ONLY from the Action API's allowlist, and turn the model's request into an ActionProposal
plus an ActionPayload valid against the Action API schema (schemas/action_api/ActionPayload.schema.json).
Nothing here executes anything: execution (steps 5-6) goes only through the Action API after explicit human approval.

The allowlist is data (action_catalog.json, mirroring the team Action API's GET /operations), not code:
  restart_service  the only operation the team Action API offers today
Not offered by the Action API, so never proposed:
  toggle a feature flag  (choosing a flag needs flag settings, which are out of bounds: the agent escalates instead)
  throttle the load generator, scale a service (no such operation; this deployment runs on Docker Compose)
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

from models import ActionProposal, ConfidenceLabel

HERE = Path(__file__).resolve().parent
CATALOG = HERE / "action_catalog.json"
PAYLOAD_SCHEMA = HERE / "schemas" / "action_api" / "ActionPayload.schema.json"


@lru_cache(maxsize=1)
def operations() -> dict[str, dict]:
    return {op["name"]: op for op in json.loads(CATALOG.read_text(encoding="utf-8"))["operations"]}


@lru_cache(maxsize=1)
def _payload_validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(PAYLOAD_SCHEMA.read_text(encoding="utf-8-sig")))


def allowlist_text() -> str:
    """For the finish tool description: what the model may propose."""
    return "; ".join(f"{name} (use when {op['use_when']}; target = the diagnosed service)"
                     for name, op in operations().items())


def _defaults(op: dict) -> dict:
    return {k: spec["default"] for k, spec in op.get("parameters", {}).items() if "default" in spec}


def action_payload(proposal: ActionProposal, evidence_ids: list[str]) -> dict:
    """The request body the Action API expects (ActionPayload.schema.json)."""
    return {"incident_id": proposal.incident_id, "operation": proposal.runbook_id, "target": proposal.target_service,
            "arguments": dict(proposal.parameters), "evidence_ids": list(evidence_ids)[:20], "reason": proposal.reason}


def process_fault(evidence, target: str, known: set[str]) -> str | None:
    """Evidence that the target's PROCESS is the problem (what a restart can fix): crash loop / restarts, a sudden
    resource jump, or calls to it going unanswered. None = only application-level errors or latency."""
    import candidates

    for c in candidates.rank(evidence, known):
        if c.service == target:
            for d in c.direct:
                if d.startswith("resources:") or "got no response" in d:
                    return d
    return None


def to_proposal(incident_id: str, fix: dict | None, diagnosis, inventory: set[str],
                evidence=None) -> tuple[ActionProposal | None, str | None]:
    """Validate the model's requested fix. Returns (proposal, None) or (None, why it was not proposed)."""
    if not fix or str(fix.get("operation") or "none") == "none":
        return None, None
    op = operations().get(fix["operation"])
    target = str(fix.get("target_service") or "").strip()
    reason = " ".join(str(fix.get("reason") or "").split())
    if op is None:
        return None, f"'{fix['operation']}' is not an Action API operation (allowed: {', '.join(operations())})"
    if diagnosis.confidence_label == ConfidenceLabel.LOW:
        return None, "fix withheld: confidence is low, so the agent escalates instead of proposing an action"
    if target not in op["allowed_targets"]:
        return None, f"{op['name']} may not target '{target}' (Action API allowlist)"
    if target not in inventory:
        return None, f"fix target '{target}' is not a service in the live inventory"
    if target.lower() not in diagnosis.suspected_component.lower():
        return None, f"fix target '{target}' is not the diagnosed component"
    if len(reason) < 10:
        return None, "fix proposed without a reason"
    if op["name"] == "restart_service" and evidence is not None and not process_fault(evidence, target, inventory):
        return None, (f"restart withheld: the evidence shows application errors/latency in {target}, not a crashed, "
                      "hung or resource-starved process, and a restart does not fix code, configuration or "
                      "dependency faults: escalate to a human")
    proposal = ActionProposal(incident_id=incident_id, runbook_id=op["name"], action=op["name"], target_service=target,
                              parameters=_defaults(op), reason=reason[:1500], risk=op["risk"],
                              expected_effect=op["expected_effect"], verification_plan=op["verification_plan"])
    errors = [e.message for e in _payload_validator().iter_errors(action_payload(proposal, diagnosis.supporting_evidence_ids))]
    if errors:
        return None, f"fix does not match the Action API payload schema: {'; '.join(errors[:3])}"
    return proposal, None
