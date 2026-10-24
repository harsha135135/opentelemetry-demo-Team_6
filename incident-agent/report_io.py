"""Report export at the CLI boundary. Every JSON report is validated against our team-defined incident report schema
(schemas/incident_report.schema.json, generated from models.IncidentReport). No organiser schema was supplied."""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError

from models import REPORT_SCHEMA_VERSION, IncidentReport, IncidentStatus
from report import ReportGenerator


class ReportArtifacts(BaseModel):
    json_path: Optional[str] = None
    markdown_path: Optional[str] = None
    internal_validation_errors: list[str] = Field(default_factory=list)
    official_schema_status: Literal["passed", "failed", "unavailable", "not_run"] = "unavailable"
    team_schema_status: Literal["passed", "failed", "not_run"] = "not_run"
    team_schema_errors: list[str] = Field(default_factory=list)
    export_errors: list[str] = Field(default_factory=list)


def write_report(report: IncidentReport, out_dir: Path) -> tuple[IncidentReport, ReportArtifacts]:
    art = ReportArtifacts()
    errors = ReportGenerator(report).validate_citations()
    if errors:
        art.internal_validation_errors = errors
        report = report.model_copy(update={
            "status": IncidentStatus.INCONCLUSIVE,
            "diagnosis": None,
            "limitations": report.limitations + ["Report failed citation validation; diagnosis withheld: " + "; ".join(errors)],
        })
    try:
        paths = ReportGenerator(report).save_files(base_path=out_dir)
        art.json_path, art.markdown_path = str(paths["json"]), str(paths["markdown"])
    except OSError as exc:
        art.export_errors.append(f"could not write report: {exc}")
        return report, art
    try:  # round-trip the written file through the schema's source model
        IncidentReport.model_validate_json(Path(art.json_path).read_text(encoding="utf-8"))
        art.team_schema_status = "passed"
    except ValidationError as exc:
        art.team_schema_status = "failed"
        art.team_schema_errors = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:10]]
    return report, art


def report_schema() -> dict:
    schema = IncidentReport.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://incident-zero.team6.local/schemas/incident_report.schema.json"
    schema["title"] = "Team 6 Incident Report (team-defined, " + REPORT_SCHEMA_VERSION + ")"
    return schema


def export_report_schema(root: Path) -> Path:
    import json
    path = root / "schemas" / "incident_report.schema.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report_schema(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
