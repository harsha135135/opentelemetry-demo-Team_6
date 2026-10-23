"""Report export at the CLI boundary. Internal validation only; no official organiser schema is available."""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from models import IncidentReport, IncidentStatus
from report import ReportGenerator


class ReportArtifacts(BaseModel):
    json_path: Optional[str] = None
    markdown_path: Optional[str] = None
    internal_validation_errors: list[str] = Field(default_factory=list)
    official_schema_status: Literal["passed", "failed", "unavailable", "not_run"] = "unavailable"
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
