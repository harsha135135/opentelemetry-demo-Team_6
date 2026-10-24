import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from models import IncidentReport, IncidentStatus


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
        for p in self.report.proposals:
            for eid in p.supporting_evidence_ids:
                if eid not in known_evidence_ids:
                    errors.append(f"Proposal cites unknown evidence ID: {eid}")
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
            f"**Remediation Outcome**: `{rep.remediation_outcome or 'not attempted'}`"
            + (f" (mode: `{rep.execution_mode}`)" if rep.execution_mode else "") + "  ",
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
                lines.append(f"| {t.timestamp.isoformat()} | `{t.status.value}` | {t.description} |")
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
                val_snippet = str(ev.observed_value)
                if len(val_snippet) > 60:
                    val_snippet = val_snippet[:57] + "..."
                capture = f"`{ev.local_capture_reference}`" if ev.local_capture_reference else "-"
                svc_name = ev.service or "-"
                lines.append(f"| `{ev.evidence_id}` | `{ev.source}` | `{svc_name}` | `{ev.status.value}` | `{ev.tool}: {ev.query}` | {val_snippet} ({capture}) |")
            nav = []
            try:
                from endpoints import evidence_links
                for ev in rep.evidence:
                    nav += [f"- `{ev.evidence_id}`: <{u}>" for u in evidence_links(ev)]
            except ImportError:  # pragma: no cover
                pass
            if nav:
                lines.extend(["", "**Navigate to the evidence** (backend URLs from the operator's .env):", *nav])
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
                lines.append(f"- **Supporting Evidence**: {', '.join(f'`{e}`' for e in p.supporting_evidence_ids) or 'None'}")
                label = {"team": "team-defined Action API (not an organiser contract)", "mock": "MOCK catalog (tests/dry-run only)",
                         "organiser": "organiser-supplied contract"}.get(p.catalog_provenance or "", "-")
                lines.append(f"- **Rollback**: {p.rollback or 'none documented'} | Action API: {label}")
        else:
            lines.append("No remediation actions proposed.")

        if rep.approval:
            app = rep.approval
            lines.append("")
            lines.append(f"**Human Approval**: Decision `{app.decision}` by `{app.approver}` (Digest: `{app.proposal_digest[:16]}...`)")
            lines.append(f"- Outcome: `{app.outcome}` | Dispatch: `{app.dispatch_time.isoformat() if app.dispatch_time else 'None'}`"
                         f" | Request ID: `{app.request_id or '-'}`")
            if app.response:
                lines.append(f"- Action API response: `{json.dumps(app.response, default=str)[:300]}`")
        if rep.escalation_reason:
            lines.append("")
            lines.append(f"**Escalation**: {rep.escalation_reason}")

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
