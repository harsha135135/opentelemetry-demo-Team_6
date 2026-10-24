from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ToolStatus(str, Enum):
    OK = "ok"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class IncidentStatus(str, Enum):
    RECEIVED = "RECEIVED"
    INVESTIGATING = "INVESTIGATING"
    DIAGNOSED = "DIAGNOSED"
    ACTION_PROPOSED = "ACTION_PROPOSED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RECOVERED = "RECOVERED"
    INCONCLUSIVE = "INCONCLUSIVE"
    REJECTED = "REJECTED"
    ESCALATED = "ESCALATED"


class ConfidenceLabel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class VerificationResult(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


class TimeWindow(BaseModel):
    start_time: datetime
    end_time: datetime


class Incident(BaseModel):
    id: str
    source: str
    symptom: str
    affected_service: Optional[str] = None
    detection_time: datetime = Field(default_factory=utc_now)
    observation_window: TimeWindow
    recovery_condition: str


class Evidence(BaseModel):
    evidence_id: str
    status: ToolStatus
    source: Literal["prometheus", "jaeger", "opensearch", "health", "derived"]
    service: Optional[str] = None
    event_time_window: TimeWindow
    collection_time: datetime = Field(default_factory=utc_now)
    tool: str
    query: str
    observed_value: Any
    trace_or_log_id: Optional[str] = None
    trace_ids: List[str] = Field(default_factory=list)
    log_ids: List[str] = Field(default_factory=list)
    local_capture_reference: Optional[str] = None


class Diagnosis(BaseModel):
    suspected_component: str
    suspected_cause: str
    supporting_evidence_ids: List[str] = Field(default_factory=list)
    conflicting_evidence_ids: List[str] = Field(default_factory=list)
    alternatives: List[str] = Field(default_factory=list)
    uncertainty: str
    confidence_label: ConfidenceLabel
    next_step: str


class ActionProposal(BaseModel):
    incident_id: str
    revision: int = 1
    runbook_id: str
    action: str
    target_service: str
    parameters: Dict[str, Any] = Field(default_factory=dict)
    reason: str
    risk: str
    expected_effect: str
    verification_plan: str
    supporting_evidence_ids: List[str] = Field(default_factory=list)
    rollback: Optional[str] = None
    catalog_provenance: Optional[str] = None  # "organiser" or "mock"; mock catalogs never reach a live dispatch
    verification_checks: List[Dict[str, Any]] = Field(default_factory=list)


class Approval(BaseModel):
    proposal_digest: str
    decision: Literal["approved", "rejected", "expired"]
    approver: str
    expiry: datetime
    single_use_consumed: bool = False
    request_id: Optional[str] = None
    dispatch_time: Optional[datetime] = None
    response: Optional[Dict[str, Any]] = None
    outcome: Literal["unknown", "success", "failed", "escalated"] = "unknown"


class Verification(BaseModel):
    incident_id: str
    baseline_evidence_ids: List[str] = Field(default_factory=list)
    post_action_evidence_ids: List[str] = Field(default_factory=list)
    result: VerificationResult
    details: str
    verification_time: datetime = Field(default_factory=utc_now)


class TimelineEvent(BaseModel):
    timestamp: datetime = Field(default_factory=utc_now)
    status: IncidentStatus
    description: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


REPORT_SCHEMA_VERSION = "team6.incident-report/1.0"


class IncidentReport(BaseModel):
    schema_version: str = REPORT_SCHEMA_VERSION  # team-defined schema: schemas/incident_report.schema.json
    incident_id: str
    status: IncidentStatus
    incident: Incident
    timeline: List[TimelineEvent] = Field(default_factory=list)
    evidence: List[Evidence] = Field(default_factory=list)
    diagnosis: Optional[Diagnosis] = None
    proposals: List[ActionProposal] = Field(default_factory=list)
    approval: Optional[Approval] = None
    verification: Optional[Verification] = None
    limitations: List[str] = Field(default_factory=list)
    remediation_outcome: Optional[str] = None  # VERIFIED_RECOVERY, PARTIAL_RECOVERY, NOT_RECOVERED, ACTION_FAILED, UNVERIFIED, ESCALATED, DRY_RUN
    execution_mode: Optional[Literal["dry_run", "live"]] = None
    escalation_reason: Optional[str] = None
