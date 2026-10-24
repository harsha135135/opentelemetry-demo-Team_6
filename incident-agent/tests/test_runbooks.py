"""Step 4: only Action API operations become proposals, as payloads valid against the Action API schema.
Nothing is executed."""
from models import ConfidenceLabel, Diagnosis

import runbooks

TARGET = runbooks.operations()["restart_service"]["allowed_targets"][0]  # any allowlisted target
INV = {TARGET, "svc-x", "load-generator"}


def diag(conf=ConfidenceLabel.HIGH, component=TARGET):
    return Diagnosis(suspected_component=component, suspected_cause="c", supporting_evidence_ids=["ev-overview-1"],
                     uncertainty="u", confidence_label=conf, next_step="n")


def fix(**kw):
    return {"operation": "restart_service", "target_service": TARGET, "reason": "crash loop: 4 restarts in 30 min", **kw}


def test_allowlisted_fix_becomes_a_schema_valid_payload():
    p, why = runbooks.to_proposal("INC-1", fix(), diag(), INV)
    assert why is None and p.runbook_id == "restart_service" and p.target_service == TARGET
    assert p.parameters == {"grace_period_seconds": 10} and p.risk and "baseline" in p.verification_plan
    payload = runbooks.action_payload(p, ["ev-overview-1"])
    assert list(runbooks._payload_validator().iter_errors(payload)) == []
    assert set(payload) == {"incident_id", "operation", "target", "arguments", "evidence_ids", "reason"}


def test_anything_else_is_refused_with_a_reason():
    cases = [
        (fix(operation="toggle_feature_flag"), "not an Action API operation"),
        (fix(operation="delete_database"), "not an Action API operation"),
        (fix(target_service="load-generator"), "may not target"),         # outside the Action API allowlist
        (fix(target_service="svc-x"), "may not target"),
        (fix(reason="restart"), "without a reason"),
    ]
    for f, expected in cases:
        p, why = runbooks.to_proposal("INC-1", f, diag(), INV)
        assert p is None and expected in why, (f, why)


def test_target_must_be_the_diagnosed_component_and_in_the_inventory():
    other = runbooks.operations()["restart_service"]["allowed_targets"][1]
    assert "not the diagnosed component" in runbooks.to_proposal("INC-1", fix(target_service=other), diag(), INV | {other})[1]
    assert "live inventory" in runbooks.to_proposal("INC-1", fix(target_service=other), diag(component=other), INV)[1]


def test_low_confidence_escalates_instead_of_proposing():
    p, why = runbooks.to_proposal("INC-1", fix(), diag(ConfidenceLabel.LOW), INV)
    assert p is None and "escalates" in why


def test_no_fix_requested_is_fine():
    assert runbooks.to_proposal("INC-1", None, diag(), INV) == (None, None)
    assert runbooks.to_proposal("INC-1", {"operation": "none"}, diag(), INV) == (None, None)


def _ev(tool, value):
    from types import SimpleNamespace
    return SimpleNamespace(evidence_id=f"ev-{tool}-1", tool=tool, service=None, source="prometheus",
                           status=SimpleNamespace(value="ok"), observed_value={"value": value})


def test_restart_needs_evidence_of_a_process_problem():
    app_errors = [_ev("overview", {"changed_since_previous_window": [f"{TARGET}: error_rate up 0.000 -> 0.300"]})]
    p, why = runbooks.to_proposal("INC-1", fix(), diag(), INV, app_errors)
    assert p is None and "restart withheld" in why   # a restart does not fix code / config / dependency faults
    crash = app_errors + [_ev("resource_overview", {"flagged": [
        {"container": TARGET, "reasons": ["CRASH LOOP: 3 restarts in the last 30 min"], "severity": 4}]})]
    p, why = runbooks.to_proposal("INC-1", fix(), diag(), INV, crash)
    assert why is None and p.target_service == TARGET
