# Team 6 — Internal Integration & Data Contracts

**Author**: Aditya R (`adityatr64`), Member 5 (Testing, Verification & Reporting)  
**Date**: 23 October 2026  
**Audience**: Harsha (M1), Neha (M2), Harshita (M3), Deepika (M4), Aditya (M5)  
**Source Code Reference**: [`incident-agent/models.py`](file:///mnt/d/opentelemetry-demo-Team_6/incident-agent/models.py)

---

## 1. Overview & Collaboration Architecture

This document defines the exact data structures, field types, and function interfaces exchanged between team members. By adhering to these contracts, each member can develop and test their module independently.

```
[Aditya: Test Incident / Alert]
             │
             ▼
      [Harsha: M1] (CLI & State Machine)
             │
             ├──> [Neha: M2] (Queries Telemetry) ──> returns list[Evidence]
             │                                              │
             ├──> [Harshita: M3] (LLM Diagnosis) <──────────┘
             │         │
             │         └──> returns Diagnosis (cites Evidence IDs)
             │
             ├──> [Deepika: M4] (Safety & Proposal) ──> returns ActionProposal & Approval
             │
             ▼
      [Aditya: M5]
             ├──> verify.py (Evaluates recovery signals) ──> returns Verification
             └──> report.py (Validates citations & exports JSON/MD) ──> Checkpoint Deliverables
```

---

## 2. Member-by-Member Contract Breakdown

---

### Member 1: Harsha (`harsha135135`) — CLI & Orchestrator

#### What Aditya & the Test Harness send to Harsha:
An `Incident` alert record initiating the investigation:

```python
from models import Incident, TimeWindow
from datetime import datetime, timezone, timedelta

# Input data to kick off the agent
incident = Incident(
    id="INC-20261023-001",
    source="alertmanager",  # or "manual-cli"
    symptom="Elevated 500 status codes on /api/cart",
    affected_service="frontend",  # optional if unknown
    detection_time=datetime.now(timezone.utc),
    observation_window=TimeWindow(
        start_time=datetime.now(timezone.utc) - timedelta(minutes=15),
        end_time=datetime.now(timezone.utc)
    ),
    recovery_condition="frontend HTTP 500 error rate < 0.01 for 2 consecutive minutes"
)
```

> **Updated 23 Oct 2026 (Harsha):** the orchestrator is `incident-agent/orchestrator.py` (`investigate(incident, config, deps) -> IncidentReport`),
> the CLI is `incident-agent/cli.py`, and the reasoning/guardrail interfaces are in `incident-agent/contracts.py`.
> For CP0-CP2 `proposals=[]`, `approval=None`, `verification=None`, and status is `DIAGNOSED`, `INCONCLUSIVE` or `ESCALATED`, never `RECOVERED`.
> Telemetry tools are live-only; the actual queried windows are stored in each `Evidence.event_time_window`.
> Exports are internal/provisional: no official organiser schema was available.

#### What Harsha sends to Aditya's modules:
Harsha's orchestrator compiles the state and calls Aditya's `report.py` and `verify.py`:

```python
from report import ReportGenerator
from models import IncidentReport, IncidentStatus, TimelineEvent

# 1. Compile the complete incident report
report = IncidentReport(
    incident_id=incident.id,
    status=IncidentStatus.DIAGNOSED, # CP0-CP2: DIAGNOSED, INCONCLUSIVE or ESCALATED
    incident=incident,
    timeline=timeline_events,        # list[TimelineEvent]
    evidence=collected_evidence,      # list[Evidence] from Neha
    diagnosis=diagnosis_record,       # Diagnosis from Harshita
    proposals=proposals_list,        # list[ActionProposal] from Deepika
    approval=approval_record,        # Approval from Deepika
    verification=verification_record,# Verification from Aditya
    limitations=["Safety: no flagd inspection performed"]
)

# 2. Hand over to Aditya's generator for validation and export
generator = ReportGenerator(report)
errors = generator.validate_citations()
if not errors:
    generator.save_files(base_path=Path("incident-agent/captures/INC-001"))
```

---

### Member 2: Neha (`pnehasai`) — Read-Only Telemetry Tools

#### What Harsha & Harshita send to Neha:
Target queries with service name and bounded time window:
- Metrics query string (PromQL)
- Logs query string (PPL / Lucene)
- Trace query parameters (`service_name`, `num_traces`, `time_window`)

#### What Neha MUST return (The `Evidence` Contract):
Neha's `telemetry.run_tool()` returns a `ToolResult`; Harsha's `integration.to_evidence()` maps it to `Evidence`
(`window_start/window_end` -> `event_time_window`, `collected_at` -> `collection_time`,
`value`/`excerpt`/`message` -> `observed_value`, `capture_ref` -> `local_capture_reference`). Evidence IDs are preserved
and duplicates are rejected. Every tool call must end up as an `Evidence` object. Aditya's test suite and report generator validate these exact fields:

```python
from models import Evidence, ToolStatus, TimeWindow

evidence_item = Evidence(
    evidence_id="ev-met-a1b2c3d4",   # Unique ID (e.g. ev-<type>-<hex>)
    status=ToolStatus.OK,             # "ok" | "empty" | "unavailable" | "error"
    source="prometheus",             # "prometheus" | "jaeger" | "opensearch" | "health" | "derived"
    service="frontend",             # Optional: None for cross-service tools such as overview (never invent a name)
    event_time_window=TimeWindow(start_time=..., end_time=...),
    collection_time=datetime.now(timezone.utc),
    tool="query_metrics",            # Name of the tool invoked
    query='traces_span_metrics_calls_total{service_name="frontend"}',
    observed_value=[...],            # Raw parsed result or summary dict
    trace_or_log_id=None,            # Set only when exactly one trace/log ID exists (kept for compatibility)
    trace_ids=["..."],               # All trace IDs returned by the tool
    log_ids=[],                      # All log IDs returned by the tool
    local_capture_reference="captures/INC-001/ev-met-a1b2c3d4.json" # Disk path
)
```

> **Rules for Neha**:
> 1. Distinguish `empty` (query succeeded, 0 rows) from `unavailable` (service unreachable) and `error` (bad syntax).
> 2. Dump the raw JSON to `local_capture_reference` so evidence is auditable on disk.

---

### Member 3: Harshita (`Harshita100`) — LLM Tool-Calling & Diagnosis

#### What Harshita receives:
The `Incident` record and the accumulated list of `Evidence` records returned by Neha.

#### What Harshita MUST return (The `Diagnosis` Contract):
Harshita’s module parses the LLM output into this strict schema:

```python
from models import Diagnosis, ConfidenceLabel

diagnosis = Diagnosis(
    suspected_component="cart",
    suspected_cause="Valkey Redis connection pool exhaustion causing timeouts on /GetCart",
    supporting_evidence_ids=["ev-met-a1b2c3d4", "ev-tra-3873a079"], # MUST match Neha's IDs
    conflicting_evidence_ids=[],                                      # Evidence disagreeing with hypothesis
    alternatives=["Frontend CPU throttling", "Network packet loss"],
    uncertainty="No logs captured for valkey container directly",
    confidence_label=ConfidenceLabel.HIGH,                           # "low" | "medium" | "high"
    next_step="Propose cart service restart and valkey pool resize"
)
```

> [!CAUTION]
> **Citation Integrity Gate**:
> Aditya’s `ReportGenerator.validate_citations()` checks every ID in `supporting_evidence_ids` and `conflicting_evidence_ids`.
> If Harshita’s LLM invents an evidence ID that Neha never produced, **validation fails and the report is rejected**.

---

### Member 4: Deepika (`deepika-raman`) — Safety, Approval Gate & Action API

#### What Deepika receives:
The `Incident` and the validated `Diagnosis`.

#### What Deepika sends to Aditya for the report:
1. **The Action Proposal**:
```python
from models import ActionProposal

proposal = ActionProposal(
    incident_id="INC-20261023-001",
    revision=1,
    runbook_id="restart-service",
    action="restart",
    target_service="cart",
    parameters={"replicas": 1, "grace_period_seconds": 15},
    reason="Clear hung Redis client connections",
    risk="Transient 5-second dropped in-flight cart operations",
    expected_effect="Error rate drops below 1% within 60s",
    verification_plan="Query cart HTTP 500 count for 2 minutes post-restart"
)
```

2. **The Terminal Approval Record**:
```python
from models import Approval

approval = Approval(
    proposal_digest="sha256-digest-of-proposal-fields",
    decision="approved",              # "approved" | "rejected" | "expired"
    approver="human-operator-aditya",
    expiry=datetime.now(timezone.utc) + timedelta(seconds=120),
    single_use_consumed=True,
    request_id="req-act-98765",
    dispatch_time=datetime.now(timezone.utc),
    response={"status": 200, "message": "Action dispatched"},
    outcome="success"                # "unknown" | "success" | "failed" | "escalated"
)
```

---

### Member 5: Aditya (`adityatr64`) — Verification, Testing & Reports

#### What Aditya produces & provides to the team:

1. **Recovery Verification (`verify.py`)**:
   Evaluates fresh post-action telemetry against baseline telemetry:
   ```python
   from verify import RecoveryVerifier

   verifier = RecoveryVerifier(incident_id="INC-001")
   verification = verifier.verify_telemetry(
       baseline_evidence=pre_action_evidence,
       post_action_evidence=fresh_post_action_evidence
   )
   # verification.result -> VerificationResult.PASS, FAIL, or INCONCLUSIVE
   ```

2. **Report Generation (`report.py`)**:
   Produces the official Markdown and JSON reports for mentors and scoring:
   - Cites all evidence with clickable disk paths.
   - Validates that zero fake citations exist.
   - Formats timelines, decisions, and escalation reasons.

3. **Shared Pytest Suite (`incident-agent/tests/`)**:
   Automated regression tests to verify that changes made by any teammate do not break the models, citation checks, or report exports.

---

## 3. Quick Reference: Enum Values

To prevent string mismatch bugs across team branches:

| Enum Name | Allowed Values |
| :--- | :--- |
| `ToolStatus` | `"ok"`, `"empty"`, `"unavailable"`, `"error"` |
| `ConfidenceLabel` | `"low"`, `"medium"`, `"high"` |
| `VerificationResult` | `"pass"`, `"fail"`, `"inconclusive"` |
| `IncidentStatus` | `"RECEIVED"`, `"INVESTIGATING"`, `"DIAGNOSED"`, `"ACTION_PROPOSED"`, `"AWAITING_APPROVAL"`, `"APPROVED"`, `"EXECUTING"`, `"VERIFYING"`, `"RECOVERED"`, `"INCONCLUSIVE"`, `"REJECTED"`, `"ESCALATED"` |
