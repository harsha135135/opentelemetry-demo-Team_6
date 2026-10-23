from datetime import datetime, timedelta, timezone
import pytest
from models import (
    ConfidenceLabel,
    Diagnosis,
    Evidence,
    Incident,
    IncidentReport,
    IncidentStatus,
    TimeWindow,
    ToolStatus,
)
from report import ReportGenerator


@pytest.fixture
def sample_incident():
    now = datetime.now(timezone.utc)
    return Incident(
        id="inc-test-001",
        source="alert-manager",
        symptom="High HTTP 500 error rate on frontend",
        affected_service="frontend",
        detection_time=now,
        observation_window=TimeWindow(start_time=now - timedelta(minutes=15), end_time=now),
        recovery_condition="frontend HTTP 500 rate < 0.01 for 2 consecutive minutes",
    )


@pytest.fixture
def sample_evidence(sample_incident):
    now = datetime.now(timezone.utc)
    return [
        Evidence(
            evidence_id="ev-001",
            status=ToolStatus.OK,
            source="prometheus",
            service="frontend",
            event_time_window=sample_incident.observation_window,
            collection_time=now,
            tool="query_metrics",
            query="http_requests_total{status=~'5..'}",
            observed_value={"error_count": 42},
            local_capture_reference="captures/ev-001.json",
        ),
        Evidence(
            evidence_id="ev-002",
            status=ToolStatus.OK,
            source="jaeger",
            service="frontend",
            event_time_window=sample_incident.observation_window,
            collection_time=now,
            tool="search_traces",
            query="service=frontend error=true",
            observed_value={"trace_ids": ["abc12345"]},
            trace_or_log_id="abc12345",
            local_capture_reference="captures/ev-002.json",
        ),
    ]


def test_report_generation_markdown(sample_incident, sample_evidence):
    report = IncidentReport(
        incident_id=sample_incident.id,
        status=IncidentStatus.INVESTIGATING,
        incident=sample_incident,
        evidence=sample_evidence,
    )
    generator = ReportGenerator(report)
    md = generator.to_markdown()

    assert "Incident Response Report: inc-test-001" in md
    assert "High HTTP 500 error rate on frontend" in md
    assert "ev-001" in md
    assert "ev-002" in md
    assert generator.validate_citations() == []


def test_report_catches_invalid_citations(sample_incident, sample_evidence):
    diagnosis = Diagnosis(
        suspected_component="recommendation",
        suspected_cause="Timeout communicating with recommendation service",
        supporting_evidence_ids=["ev-001", "ev-non-existent"],
        conflicting_evidence_ids=[],
        alternatives=[],
        uncertainty="None",
        confidence_label=ConfidenceLabel.HIGH,
        next_step="Check recommendation service health",
    )
    report = IncidentReport(
        incident_id=sample_incident.id,
        status=IncidentStatus.DIAGNOSED,
        incident=sample_incident,
        evidence=sample_evidence,
        diagnosis=diagnosis,
    )
    generator = ReportGenerator(report)
    errors = generator.validate_citations()
    assert len(errors) == 1
    assert "ev-non-existent" in errors[0]


def test_report_to_json(sample_incident, sample_evidence):
    report = IncidentReport(
        incident_id=sample_incident.id,
        status=IncidentStatus.INVESTIGATING,
        incident=sample_incident,
        evidence=sample_evidence,
    )
    generator = ReportGenerator(report)
    json_str = generator.to_json()
    assert "inc-test-001" in json_str
    assert "ev-001" in json_str
