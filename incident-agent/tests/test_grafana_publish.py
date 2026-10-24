"""Grafana annotation body (no network): the posted text matches the report and never breaks the CLI."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx

import grafana_publish as g
from models import ConfidenceLabel, Diagnosis, Incident, IncidentStatus, TimeWindow

NOW = datetime.now(timezone.utc)


def report(diagnosis=None, status=IncidentStatus.DIAGNOSED):
    inc = Incident(id="INC-9", source="cli", symptom="error rate <crossed>", affected_service=None, detection_time=NOW,
                   observation_window=TimeWindow(start_time=NOW - timedelta(minutes=3), end_time=NOW),
                   recovery_condition="x")
    return SimpleNamespace(incident_id="INC-9", incident=inc, status=status, diagnosis=diagnosis, proposals=[],
                           limitations=["No diagnosis accepted: escalate"],
                           timeline=[SimpleNamespace(metadata={"model_calls": 4, "tool_calls": 9, "elapsed_s": 21.0})])


def test_annotation_carries_the_diagnosis_over_the_incident_window():
    d = Diagnosis(suspected_component="svc-a", suspected_cause="returns errors on op X", supporting_evidence_ids=["ev-1"],
                  alternatives=["svc-b slow"], uncertainty="why unknown", confidence_label=ConfidenceLabel.HIGH,
                  next_step="check svc-a")
    a = g.annotation(report(d))
    assert a["timeEnd"] - a["time"] == 180_000
    assert a["tags"] == ["incident-agent", "diagnosed", "svc-a", "confidence:high"]
    for part in ("svc-a", "returns errors on op X", "why unknown", "ev-1", "svc-b slow", "check svc-a"):
        assert part in a["text"]
    assert "&lt;crossed&gt;" in a["text"]  # telemetry/alert text is escaped, never rendered as HTML


def test_no_diagnosis_says_escalate():
    a = g.annotation(report(None, IncidentStatus.INCONCLUSIVE))
    assert "escalate" in a["text"] and a["tags"] == ["incident-agent", "inconclusive"]


def test_grafana_down_never_fails_the_investigation(monkeypatch):
    def down(*_a, **_k):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(g.httpx, "post", down)
    monkeypatch.setenv("GRAFANA_URL", "http://localhost:1")
    assert g.publish(report(None)).startswith("Grafana: not posted")
    monkeypatch.setenv("GRAFANA_URL", "")
    assert "disabled" in g.publish(report(None))
