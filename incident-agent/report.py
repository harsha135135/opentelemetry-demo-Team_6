import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from models import IncidentReport, IncidentStatus

# Keys in observed_value["value"] that summarise a tool result best, in order of preference.
_SUMMARY_KEYS = ("changed_since_previous_window", "calls_with_no_response_from", "error_origin_counts",
                 "flagged", "summary", "value", "series", "lines")


def _cell(text: Any) -> str:
    """Make text safe inside a Markdown table cell: no row breaks, no column splits."""
    return str(text).replace("\r", " ").replace("\n", " ").replace("|", "\\|")


def _code(text: Any) -> str:
    """Inline code that survives backticks inside the text (PPL queries quote fields with them)."""
    s = _cell(text)
    return f"`` {s} ``" if "`" in s else f"`{s}`"


def _evidence_summary(observed: Any, limit: int = 160) -> str:
    """The most informative short fact from an evidence item, for the readable report."""
    if isinstance(observed, dict):
        if observed.get("excerpt"):
            text = observed["excerpt"]
        elif isinstance(observed.get("value"), dict):
            val = observed["value"]
            key = next((k for k in _SUMMARY_KEYS if val.get(k) not in (None, [], {})), None)
            text = f"{key}: {json.dumps(val[key], default=str)}" if key else json.dumps(val, default=str)
        else:
            text = observed.get("message") or json.dumps(observed, default=str)
    else:
        text = json.dumps(observed, default=str)
    text = str(text)
    return text if len(text) <= limit else text[:limit - 3] + "..."


class ReportGenerator:
    def __init__(self, report: IncidentReport):
        self.report = report

    def validate_citations(self) -> List[str]:
        known_evidence_ids = {e.evidence_id for e in self.report.evidence}
        errors: List[str] = []
        if self.report.diagnosis:
            for eid in self.report.diagnosis.supporting_evidence_ids:
                if eid not in known_evidence_ids:
                    errors.append(f"Diagnosis cites unknown supporting evidence ID: {eid}")
            for eid in self.report.diagnosis.conflicting_evidence_ids:
                if eid not in known_evidence_ids:
                    errors.append(f"Diagnosis cites unknown conflicting evidence ID: {eid}")
        if self.report.verification:
            for eid in self.report.verification.baseline_evidence_ids:
                if eid not in known_evidence_ids:
                    errors.append(f"Verification cites unknown baseline evidence ID: {eid}")
            for eid in self.report.verification.post_action_evidence_ids:
                if eid not in known_evidence_ids:
                    errors.append(f"Verification cites unknown post-action evidence ID: {eid}")
        return errors

    def to_markdown(self) -> str:
        rep = self.report
        inc = rep.incident
        lines = [
            f"# Incident Response Report: {inc.id}",
            "",
            f"**Status**: `{rep.status.value}`  ",
            f"**Affected Service**: `{inc.affected_service or 'Unknown'}`  ",
            f"**Detection Time (UTC)**: `{inc.detection_time.isoformat()}`  ",
            f"**Observation Window**: `{inc.observation_window.start_time.isoformat()}` to `{inc.observation_window.end_time.isoformat()}`",
            "",
            "## 1. Symptom & Recovery Condition",
            f"- **Symptom**: {inc.symptom}",
            f"- **Recovery Condition**: {inc.recovery_condition}",
            "",
            "## 2. Investigation Timeline",
        ]

        if rep.timeline:
            lines.append("| Timestamp (UTC) | State | Description |")
            lines.append("| --- | --- | --- |")
            for t in rep.timeline:
                lines.append(f"| {t.timestamp.isoformat()} | `{t.status.value}` | {_cell(t.description)} |")
        else:
            lines.append("No timeline events recorded.")

        lines.extend([
            "",
            "## 3. Evidence Collection",
        ])

        if rep.evidence:
            lines.append("| ID | Source | Service | Status | Tool / Query | Observed / Capture |")
            lines.append("| --- | --- | --- | --- | --- | --- |")
            for ev in rep.evidence:
                val_snippet = _cell(_evidence_summary(ev.observed_value))
                capture = _code(ev.local_capture_reference) if ev.local_capture_reference else "-"
                svc_name = ev.service or "-"
                lines.append(f"| `{ev.evidence_id}` | `{ev.source}` | `{svc_name}` | `{ev.status.value}` | "
                             f"{_code(f'{ev.tool}: {ev.query}')} | {val_snippet} ({capture}) |")
        else:
            lines.append("No evidence captured.")

        lines.extend([
            "",
            "## 4. Diagnosis",
        ])

        if rep.diagnosis:
            diag = rep.diagnosis
            lines.append(f"- **Suspected Component**: `{diag.suspected_component}`")
            lines.append(f"- **Suspected Root Cause**: {diag.suspected_cause}")
            lines.append(f"- **Confidence**: `{diag.confidence_label.value}`")
            lines.append(f"- **Uncertainty / Notes**: {diag.uncertainty}")
            lines.append(f"- **Supporting Evidence**: {', '.join([f'`{e}`' for e in diag.supporting_evidence_ids]) or 'None'}")
            lines.append(f"- **Conflicting Evidence**: {', '.join([f'`{e}`' for e in diag.conflicting_evidence_ids]) or 'None'}")
            lines.append(f"- **Alternative Hypotheses**: {', '.join(diag.alternatives) or 'None'}")
            lines.append(f"- **Next Step**: {diag.next_step}")
        else:
            lines.append("Diagnosis not yet reached.")

        lines.extend([
            "",
            "## 5. Action Proposals & Approvals",
        ])

        if rep.proposals:
            for i, p in enumerate(rep.proposals, 1):
                lines.append(f"### Proposal {i} (Rev {p.revision})")
                lines.append(f"- **Runbook**: `{p.runbook_id}` ({p.action}) on `{p.target_service}`")
                lines.append(f"- **Parameters**: `{json.dumps(p.parameters)}`")
                lines.append(f"- **Reason & Risk**: {p.reason} | Risk: {p.risk}")
                lines.append(f"- **Expected Effect**: {p.expected_effect}")
                lines.append(f"- **Verification Plan**: {p.verification_plan}")
        else:
            lines.append("No remediation actions proposed.")

        if rep.approval:
            app = rep.approval
            lines.append("")
            lines.append(f"**Human Approval**: Decision `{app.decision}` by `{app.approver}` (Digest: `{app.proposal_digest[:16]}...`)")
            lines.append(f"- Outcome: `{app.outcome}` | Dispatch: `{app.dispatch_time.isoformat() if app.dispatch_time else 'None'}`")

        lines.extend([
            "",
            "## 6. Verification",
        ])

        if rep.verification:
            v = rep.verification
            lines.append(f"- **Result**: `{v.result.value}`")
            lines.append(f"- **Details**: {v.details}")
            lines.append(f"- **Baseline Signals**: {', '.join([f'`{e}`' for e in v.baseline_evidence_ids]) or 'None'}")
            lines.append(f"- **Post-Action Signals**: {', '.join([f'`{e}`' for e in v.post_action_evidence_ids]) or 'None'}")
        else:
            lines.append("Verification not performed or pending.")

        lines.extend([
            "",
            "## 7. Known Limitations & Escalation Notes",
        ])
        if rep.limitations:
            for lim in rep.limitations:
                lines.append(f"- {lim}")
        else:
            lines.append("- None reported.")

        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return self.report.model_dump(mode="json")

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def save_files(self, base_path: Path) -> Dict[str, Path]:
        base_path.mkdir(parents=True, exist_ok=True)
        md_path = base_path / f"{self.report.incident_id}_report.md"
        json_path = base_path / f"{self.report.incident_id}_report.json"
        md_path.write_text(self.to_markdown(), encoding="utf-8")
        json_path.write_text(self.to_json(), encoding="utf-8")
        return {"markdown": md_path, "json": json_path}
