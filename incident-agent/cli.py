"""Incident Zero CLI: alert -> bounded Claude tool loop -> evidence-backed report, optionally followed by a
human-approved remediation through the Action API and measured verification.

    python cli.py endpoints [--write-env]
    python cli.py investigate --incident-id INC-001 --symptom "..." [--service checkout] --model <id>
    python cli.py investigate ... --respond [--action-catalog catalog.json] [--execute]
    python cli.py kill-switch on|off|status
    python cli.py action-api init-token | serve [--executor docker|simulate] | health | operations | services | actions | show <id>
    python cli.py investigate ... --respond --action-api [--execute]     # use the team Action API
    python cli.py audit [--server] [--incident-id ID]
    python cli.py report <incident-id>
    python cli.py grafana sync | export <dir>                            # root-cause and verification dashboards
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
DEFAULT_ACTION_API_URL = "http://127.0.0.1:8787"
ACTION_API_STATE_DIR = Path(os.getenv("ACTION_API_STATE_DIR", HERE / "runs" / "action_api"))


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
                   help="Action API catalog JSON written from an official contract")
    r.add_argument("--action-api", action="store_true",
                   help="use the team Action API (ACTION_API_URL, ACTION_API_TOKEN): catalog, validation, server-issued approval")
    r.add_argument("--approver", default=os.getenv("INCIDENT_AGENT_APPROVER", ""), help="name recorded with the approval")
    r.add_argument("--approval-ttl-s", type=float, default=120)
    r.add_argument("--settle-s", type=float, default=120, help="wait after dispatch before the first verification sample")
    r.add_argument("--interval-s", type=float, default=30)
    r.add_argument("--samples", type=int, default=2)
    i.add_argument("--grafana", action="store_true", help="after the report, publish the Grafana dashboards (grafana sync)")
    e = sub.add_parser("endpoints", help="show each backend, the tools that use it, reachability and browser URLs")
    e.add_argument("--write-env", action="store_true", help="discover published ports with `docker port` and update .env")
    k = sub.add_parser("kill-switch", help="operator kill switch for all state-changing actions")
    k.add_argument("state", choices=["on", "off", "status"])
    c = sub.add_parser("catalog", help="validate and show the Action API catalog")
    c.add_argument("--action-catalog", default=os.getenv("ACTION_API_CATALOG", ""))
    a = sub.add_parser("action-api", help="run or query the team Action API")
    asub = a.add_subparsers(dest="api_cmd", required=True)
    s = asub.add_parser("serve", help="start the Action API (the only process that can restart containers)")
    s.add_argument("--host", default=os.getenv("ACTION_API_HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.getenv("ACTION_API_PORT", "8787")))
    s.add_argument("--executor", choices=["simulate", "docker"], default=os.getenv("ACTION_API_EXECUTOR", "simulate"))
    s.add_argument("--state-dir", default=str(ACTION_API_STATE_DIR))
    s.add_argument("--bind-all", action="store_true", help="allow a non-loopback host (not recommended)")
    asub.add_parser("init-token", help="add ACTION_API_TOKEN/ACTION_API_URL to .env if absent (never overwrites)")
    asub.add_parser("health")
    asub.add_parser("operations")
    asub.add_parser("actions")
    asub.add_parser("services", help="read-only system state (container state/health, load generator)")
    sh = asub.add_parser("show")
    sh.add_argument("action_id")
    asub.add_parser("export-schemas", help="regenerate schemas/action_api/*.json and docs/action_api.openapi.json")
    au = sub.add_parser("audit", help="show the agent audit journal (or the Action API audit with --server)")
    au.add_argument("--server", action="store_true")
    au.add_argument("--incident-id")
    au.add_argument("--tail", type=int, default=50)
    au.add_argument("--out", default="runs")
    rp = sub.add_parser("report", help="print an incident report")
    rp.add_argument("incident_id")
    rp.add_argument("--json", action="store_true")
    rp.add_argument("--out", default="runs")
    g = sub.add_parser("grafana", help="root-cause and verification dashboards for every report (GRAFANA_URL)")
    gsub = g.add_subparsers(dest="grafana_cmd", required=True)
    gs = gsub.add_parser("sync", help="publish the index and each incident's dashboards to Grafana")
    gs.add_argument("--out", default="runs")
    ge = gsub.add_parser("export", help="write the dashboard JSON files instead of publishing")
    ge.add_argument("dir")
    ge.add_argument("--out", default="runs")
    return p


def _team_client():
    from action_api_client import TeamActionApiClient

    return TeamActionApiClient(os.getenv("ACTION_API_URL", DEFAULT_ACTION_API_URL), os.getenv("ACTION_API_TOKEN", ""))


def _state_client():
    """Team Action API client for the read-only container_state tool, if the API is configured and reachable."""
    if not (os.getenv("ACTION_API_URL") and os.getenv("ACTION_API_TOKEN")):
        return None
    try:
        client = _team_client()
        client.health()
        return client
    except Exception:  # noqa: BLE001 - the tool is optional; investigation continues without it
        return None


def cmd_action_api(args) -> int:
    import json

    import httpx

    from action_api_client import ActionApiError

    if args.api_cmd == "init-token":
        import secrets

        import endpoints
        values = {}
        if not os.getenv("ACTION_API_TOKEN"):
            values["ACTION_API_TOKEN"] = secrets.token_urlsafe(32)
        if not os.getenv("ACTION_API_URL"):
            values["ACTION_API_URL"] = DEFAULT_ACTION_API_URL
        if not values:
            print("ACTION_API_TOKEN and ACTION_API_URL already set; nothing changed")
            return 0
        endpoints.write_env(HERE / ".env", values)
        print(f"added {', '.join(values)} to {HERE / '.env'} (token not printed)")
        return 0
    if args.api_cmd == "export-schemas":
        import action_api_models
        from report_io import export_report_schema
        for path in action_api_models.export(HERE) + [export_report_schema(HERE)]:
            print(path.relative_to(HERE))
        return 0
    if args.api_cmd == "serve":
        return _serve(args)
    try:
        client = _team_client()
        if args.api_cmd == "health":
            data = client.health()
        elif args.api_cmd == "operations":
            data = client.operations().model_dump(mode="json")
        elif args.api_cmd == "actions":
            data = client.list_actions()
        elif args.api_cmd == "services":
            data = client.system_state()
        else:
            data = client._call("GET", f"/v1/actions/{args.action_id}")[1]
    except (ActionApiError, httpx.HTTPError) as exc:
        print(f"error: Action API: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(data, indent=2, default=str))
    return 0


def _serve(args) -> int:
    from action_executor import STATE_SERVICES, ContainerInspector, DockerExecutor, SimulatedExecutor, allowed_targets
    from action_server import ActionApiService, build_server
    from safety import KillSwitch

    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.bind_all:
        print("error: refusing to bind a non-loopback address without --bind-all", file=sys.stderr)
        return 2
    token = os.getenv("ACTION_API_TOKEN", "")
    if len(token) < 24:
        print("error: ACTION_API_TOKEN missing or shorter than 24 characters (run: python cli.py action-api init-token)", file=sys.stderr)
        return 2
    try:
        targets = allowed_targets(os.getenv("ACTION_API_EXTRA_TARGETS", ""))
        executor = (DockerExecutor(os.getenv("ACTION_API_COMPOSE_PROJECT", "opentelemetry-demo-team_6"))
                    if args.executor == "docker" else SimulatedExecutor())
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    project = os.getenv("ACTION_API_COMPOSE_PROJECT", "opentelemetry-demo-team_6")
    inspector = ContainerInspector(project)
    service = ActionApiService(executor=executor, targets=targets, state_dir=Path(args.state_dir),
                               kill_switch=KillSwitch(KILL_SWITCH_FILE).active,
                               inspector=inspector if inspector.docker else None, state_services=STATE_SERVICES,
                               loadgen_url=os.getenv("ACTION_API_LOADGEN_URL", "http://localhost:8080/loadgen"))
    server = build_server(service, token, args.host, args.port)
    print(f"Team Action API {args.host}:{args.port}  executor={executor.mode}  targets={', '.join(targets)}\n"
          f"state/audit: {args.state_dir}  kill switch: {KILL_SWITCH_FILE}\n"
          "Approval codes for this API are printed ONLY in this terminal. Ctrl+C to stop.", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_audit(args) -> int:
    import json

    path = ACTION_API_STATE_DIR / "audit.jsonl" if args.server else Path(args.out).resolve() / "audit.jsonl"
    if not path.exists():
        print(f"no audit log at {path}")
        return 0
    lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.incident_id:
        lines = [l for l in lines if l.get("incident_id") == args.incident_id]
    for rec in lines[-args.tail:]:
        extra = {k: v for k, v in rec.items() if k not in ("time", "event")}
        print(f"{rec.get('time', '')[:19]}  {rec.get('event', ''):28} {json.dumps(extra, default=str)[:300]}")
    return 0


def cmd_report(args) -> int:
    base = Path(args.out).resolve() / args.incident_id / f"{args.incident_id}_report.{'json' if args.json else 'md'}"
    if not base.exists():
        print(f"no report at {base}", file=sys.stderr)
        return 1
    print(base.read_text(encoding="utf-8"))
    return 0


def cmd_grafana(args, incident_id: str | None = None) -> int:
    import grafana_dash

    runs = Path(args.out).resolve()
    if getattr(args, "grafana_cmd", None) == "export":
        paths = grafana_dash.export(runs, Path(args.dir))
        print(f"wrote {len(paths)} dashboards to {Path(args.dir).resolve()}")
        return 0
    try:
        res = grafana_dash.sync(runs, grafana_dash.GrafanaPublisher())
    except grafana_dash.GrafanaError as exc:
        print(f"grafana: {exc} (set GRAFANA_URL, e.g. python cli.py endpoints --write-env)", file=sys.stderr)
        return 1
    print(f"Grafana index:   {res['index']}")
    for iid, (rc, ver) in res["incidents"].items():
        if incident_id in (None, iid):
            print(f"{iid}\n  root cause:    {rc}\n  verification:  {ver}")
    for s in res["skipped"]:
        print(f"skipped (not a valid report): {s}")
    return 0


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
    if args.cmd == "action-api":
        return cmd_action_api(args)
    if args.cmd == "audit":
        return cmd_audit(args)
    if args.cmd == "report":
        return cmd_report(args)
    if args.cmd == "grafana":
        return cmd_grafana(args)
    if not args.model:
        print("error: model identifier missing (use --model or ANTHROPIC_MODEL)", file=sys.stderr)
        return 2
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY is not set", file=sys.stderr)
        return 2
    catalog, client = None, None
    if args.respond and args.action_api:
        import httpx

        from action_api_client import ActionApiError
        try:
            client = _team_client()
            catalog = client.catalog()
        except (ActionApiError, httpx.HTTPError) as exc:
            print(f"error: team Action API unavailable ({exc}); start it with: python cli.py action-api serve", file=sys.stderr)
            return 2
    elif args.respond:
        try:
            catalog = load_catalog(args.action_catalog)
            client = HttpActionClient(catalog) if args.execute else None
        except CatalogError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    elif args.execute or args.action_api:
        print("error: --execute and --action-api require --respond", file=sys.stderr)
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
        run_tool, policy, _ = make_telemetry_runner(run_dir / "evidence", _state_client())
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
    print(f"Report schema (team-defined schemas/incident_report.schema.json): {art.team_schema_status}"
          " | organiser schema: none supplied")
    for e in art.team_schema_errors:
        print(f"SCHEMA ERROR: {e}")
    for e in art.internal_validation_errors + art.export_errors:
        print(f"VALIDATION/EXPORT ERROR: {e}")
    for lim in report.limitations:
        print(f"limitation: {lim}")
    if args.grafana and art.json_path:
        try:
            cmd_grafana(args, report.incident_id)
        except Exception as exc:  # the report is already written; a dashboard problem must not fail the incident
            print(f"grafana: dashboards not published ({type(exc).__name__}: {exc})", file=sys.stderr)
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
