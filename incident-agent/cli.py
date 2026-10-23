"""Incident Zero CLI: alert -> bounded Claude tool loop -> evidence-backed report (read-only).

    python cli.py investigate --incident-id INC-001 --symptom "..." [--service checkout] --model <id>
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
os.environ.setdefault("TELEMETRY_TIMEOUT_S", "5")  # must be set before telemetry is imported

from contracts import ModelError  # noqa: E402
from integration import EvidenceRegistry, make_telemetry_runner  # noqa: E402
from models import Incident, TimeWindow  # noqa: E402
from orchestrator import (  # noqa: E402
    RECOVERY_NOT_PROVIDED, Dependencies, IncidentInputError, InvestigationConfig, investigate,
)
from reasoning import AnthropicReasoner  # noqa: E402
from report_io import write_report  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="incident-agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("investigate", help="investigate one live incident (read-only)")
    i.add_argument("--incident-id", required=True)
    i.add_argument("--symptom", required=True)
    i.add_argument("--service", help="affected service, if known")
    i.add_argument("--source", default="manual-cli")
    i.add_argument("--window-minutes", type=int, default=15, help="incident observation window ending now")
    i.add_argument("--recovery-condition", default=RECOVERY_NOT_PROVIDED)
    i.add_argument("--model", default=os.getenv("ANTHROPIC_MODEL", ""))
    i.add_argument("--out", default="runs")
    i.add_argument("--deadline-s", type=float, default=180)
    i.add_argument("--max-tool-calls", type=int, default=12)
    i.add_argument("--max-model-calls", type=int, default=6)
    i.add_argument("--request-timeout-s", type=float, default=60)
    i.add_argument("--max-total-tokens", type=int, default=60000)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.model:
        print("error: model identifier missing (use --model or ANTHROPIC_MODEL)", file=sys.stderr)
        return 2
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY is not set", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc)
    run_dir = Path(args.out).resolve() / args.incident_id
    try:
        incident = Incident(
            id=args.incident_id, source=args.source, symptom=args.symptom, affected_service=args.service,
            detection_time=now, observation_window=TimeWindow(start_time=now - timedelta(minutes=args.window_minutes), end_time=now),
            recovery_condition=args.recovery_condition,
        )
        run_tool, policy, _ = make_telemetry_runner(run_dir / "evidence")
        if not policy.approved_services:
            print("error: no services visible in telemetry backends; run CP0 checks first", file=sys.stderr)
            return 2
        deps = Dependencies(reasoner=AnthropicReasoner(args.model), run_tool=run_tool, policy=policy, registry=EvidenceRegistry())
        config = InvestigationConfig(deadline_s=args.deadline_s, max_tool_calls=args.max_tool_calls,
                                     max_model_calls=args.max_model_calls, request_timeout_s=args.request_timeout_s,
                                     max_total_tokens=args.max_total_tokens)
        report = investigate(incident, config, deps)
    except (IncidentInputError, ModelError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    report, art = write_report(report, run_dir)
    meta = report.timeline[-1].metadata
    print(f"Incident: {report.incident_id}   Status: {report.status.value}")
    if report.diagnosis:
        d = report.diagnosis
        print(f"Diagnosis: {d.suspected_component}: {d.suspected_cause}\nConfidence label: {d.confidence_label.value}\n"
              f"Supporting evidence: {', '.join(d.supporting_evidence_ids)}\nUncertainty: {d.uncertainty}")
    else:
        print("No diagnosis accepted (escalate to a human).")
    print(f"Model calls: {meta.get('model_calls')}  Tool calls: {meta.get('tool_calls')}  Elapsed: {meta.get('elapsed_s')}s")
    print(f"Report JSON: {art.json_path}\nReport MD:   {art.markdown_path}")
    print("Official schema: " + art.official_schema_status + " (exports are provisional/internal)")
    for e in art.internal_validation_errors + art.export_errors:
        print(f"VALIDATION/EXPORT ERROR: {e}")
    for lim in report.limitations:
        print(f"limitation: {lim}")
    return 0 if not art.export_errors else 1


if __name__ == "__main__":
    sys.exit(main())
