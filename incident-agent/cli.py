"""Incident Zero CLI: alert -> bounded Claude tool loop -> evidence-backed report, optionally followed by a
human-approved remediation through the Action API and measured verification.

    python cli.py endpoints [--write-env]
    python cli.py investigate --incident-id INC-001 --symptom "..." [--service checkout] --model <id>
    python cli.py investigate ... --respond [--action-catalog catalog.json] [--execute]
    python cli.py kill-switch on|off|status
"""
from __future__ import annotations

import argparse
import getpass
import os
import select
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
os.environ.setdefault("TELEMETRY_TIMEOUT_S", "5")  # must be set before telemetry is imported

from actions import CatalogError, HttpActionClient, load_catalog  # noqa: E402
from contracts import ModelError  # noqa: E402
from integration import EvidenceRegistry, make_telemetry_runner  # noqa: E402
from models import Incident, TimeWindow  # noqa: E402
from orchestrator import (  # noqa: E402
    RECOVERY_NOT_PROVIDED, Dependencies, IncidentInputError, InvestigationConfig, investigate,
)
from reasoning import AnthropicReasoner  # noqa: E402
from report_io import write_report  # noqa: E402

KILL_SWITCH_FILE = Path(os.getenv("KILL_SWITCH_FILE", HERE / "KILL_SWITCH"))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="incident-agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("investigate", help="investigate one live incident; optionally plan and gate a remediation")
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
    r = i.add_argument_group("remediation (off unless --respond)")
    r.add_argument("--respond", action="store_true", help="plan a remediation, ask for approval, then dry-run or execute")
    r.add_argument("--execute", action="store_true", help="LIVE: send the approved request (default is dry-run)")
    r.add_argument("--action-catalog", default=os.getenv("ACTION_API_CATALOG", ""),
                   help="Action API catalog JSON written from the official contract")
    r.add_argument("--approver", default=os.getenv("INCIDENT_AGENT_APPROVER", ""), help="name recorded with the approval")
    r.add_argument("--approval-ttl-s", type=float, default=120)
    r.add_argument("--settle-s", type=float, default=120, help="wait after dispatch before the first verification sample")
    r.add_argument("--interval-s", type=float, default=30)
    r.add_argument("--samples", type=int, default=2)
    e = sub.add_parser("endpoints", help="show each backend, the tools that use it, reachability and browser URLs")
    e.add_argument("--write-env", action="store_true", help="discover published ports with `docker port` and update .env")
    k = sub.add_parser("kill-switch", help="operator kill switch for all state-changing actions")
    k.add_argument("state", choices=["on", "off", "status"])
    c = sub.add_parser("catalog", help="validate and show the Action API catalog")
    c.add_argument("--action-catalog", default=os.getenv("ACTION_API_CATALOG", ""))
    return p


def cmd_endpoints(args) -> int:
    import endpoints

    if args.write_env:
        found = endpoints.discover()
        if not found:
            print("no published backend ports found (is the observability stack running?)", file=sys.stderr)
            return 1
        changed = endpoints.write_env(HERE / ".env", found)
        for k, v in found.items():
            os.environ[k] = v
        print(f"updated {HERE / '.env'}: {', '.join(changed) or 'no changes'}")
    rows = endpoints.check() + [endpoints.action_api_status(os.getenv("ACTION_API_CATALOG", ""))]
    for row in rows:
        print(f"{row['backend']:11} {row.get('status', ''):28} {row.get('url', '-')}")
        if row.get("tools"):
            print(f"{'':11} tools:   {row['tools']}")
        if row.get("browser", "-") != "-":
            print(f"{'':11} browser: {row['browser']}")
    return 0


def cmd_kill_switch(args) -> int:
    from safety import KillSwitch

    ks = KillSwitch(KILL_SWITCH_FILE)
    if args.state != "status":
        ks.set(args.state == "on")
    print(f"kill switch: {'ACTIVE - all state-changing actions blocked' if ks.active() else 'off'} ({KILL_SWITCH_FILE})")
    return 0


def cmd_catalog(args) -> int:
    try:
        cat = load_catalog(args.action_catalog)
    except CatalogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"provenance: {cat.provenance}  base URL ({cat.base_url_env}): {cat.base_url or 'unset'}")
    if not cat.operations:
        print("no operations: remediation will always escalate (official Action API contract not configured)")
    for op in cat.operations:
        print(f"- {op.name}: {op.method} {op.path} target={op.target_kind} {op.allowed_targets} params={list(op.parameters)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "endpoints":
        return cmd_endpoints(args)
    if args.cmd == "kill-switch":
        return cmd_kill_switch(args)
    if args.cmd == "catalog":
        return cmd_catalog(args)
    if not args.model:
        print("error: model identifier missing (use --model or ANTHROPIC_MODEL)", file=sys.stderr)
        return 2
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY is not set", file=sys.stderr)
        return 2
    catalog, client = None, None
    if args.respond:
        try:
            catalog = load_catalog(args.action_catalog)
            client = HttpActionClient(catalog) if args.execute else None
        except CatalogError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    elif args.execute:
        print("error: --execute requires --respond", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc)
    out = Path(args.out).resolve()
    run_dir = out / args.incident_id
    try:
        incident = Incident(
            id=args.incident_id, source=args.source, symptom=args.symptom, affected_service=args.service,
            detection_time=now, observation_window=TimeWindow(start_time=now - timedelta(minutes=args.window_minutes), end_time=now),
            recovery_condition=args.recovery_condition,
        )
        run_tool, policy, _ = make_telemetry_runner(run_dir / "evidence")
        if not policy.approved_services:
            print("error: no services visible in telemetry backends; run `python cli.py endpoints` first", file=sys.stderr)
            return 2
        registry = EvidenceRegistry()
        deps = Dependencies(reasoner=AnthropicReasoner(args.model), run_tool=run_tool, policy=policy, registry=registry)
        config = InvestigationConfig(deadline_s=args.deadline_s, max_tool_calls=args.max_tool_calls,
                                     max_model_calls=args.max_model_calls, request_timeout_s=args.request_timeout_s,
                                     max_total_tokens=args.max_total_tokens)
        report = investigate(incident, config, deps)
        if args.respond:
            report = _respond(args, report, catalog, client, run_tool, registry, policy.approved_services, out)
    except (IncidentInputError, ModelError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    report, art = write_report(report, run_dir)
    meta = next((t.metadata for t in reversed(report.timeline) if "model_calls" in t.metadata), {})
    print(f"Incident: {report.incident_id}   Status: {report.status.value}")
    if report.diagnosis:
        d = report.diagnosis
        print(f"Diagnosis: {d.suspected_component}: {d.suspected_cause}\nConfidence label: {d.confidence_label.value}\n"
              f"Supporting evidence: {', '.join(d.supporting_evidence_ids)}\nUncertainty: {d.uncertainty}")
    else:
        print("No diagnosis accepted (escalate to a human).")
    if report.remediation_outcome:
        print(f"Remediation: {report.remediation_outcome} (mode {report.execution_mode})"
              + (f" - {report.escalation_reason}" if report.escalation_reason else ""))
        for p in report.proposals:
            print(f"Proposal: {p.action} on {p.target_service} {p.parameters} | verify: {p.verification_plan}")
        if report.verification:
            print(f"Verification: {report.verification.result.value}: {report.verification.details}")
    print(f"Model calls: {meta.get('model_calls')}  Tool calls: {meta.get('tool_calls')}  Elapsed: {meta.get('elapsed_s')}s")
    print(f"Report JSON: {art.json_path}\nReport MD:   {art.markdown_path}")
    print("Official schema: " + art.official_schema_status + " (exports are provisional/internal)")
    for e in art.internal_validation_errors + art.export_errors:
        print(f"VALIDATION/EXPORT ERROR: {e}")
    for lim in report.limitations:
        print(f"limitation: {lim}")
    return 0 if not art.export_errors else 1


def timed_input(ttl_s: float):
    """input() that gives up after the approval TTL; an unanswered prompt becomes a rejection, never a hang."""
    def read(prompt: str) -> str:
        print(prompt, end="", flush=True)
        ready, _, _ = select.select([sys.stdin], [], [], ttl_s)
        return sys.stdin.readline() if ready else ""
    return read


def _respond(args, report, catalog, client, run_tool, registry, inventory, out: Path):
    from remediation import AnthropicPlanner
    from response import ResponseConfig, ResponseDeps, respond
    from safety import AuditJournal, KillSwitch

    cfg = ResponseConfig(mode="live" if args.execute else "dry_run", approval_ttl_s=args.approval_ttl_s,
                         settle_s=args.settle_s, interval_s=args.interval_s, samples=max(1, args.samples))
    deps = ResponseDeps(planner=AnthropicPlanner(args.model), catalog=catalog, run_tool=run_tool, registry=registry,
                        inventory=inventory, kill_switch=KillSwitch(KILL_SWITCH_FILE), journal=AuditJournal(out / "audit.jsonl"),
                        lock_path=out / ".dispatch.lock", approver=args.approver or getpass.getuser(),
                        interactive=sys.stdin.isatty(), input_fn=timed_input(args.approval_ttl_s), client=client)
    return respond(report, cfg, deps)


if __name__ == "__main__":
    sys.exit(main())
