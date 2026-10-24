# Incident agent: investigation, gated remediation and verification

    Alert -> cli.py -> orchestrator.investigate -> reasoning.AnthropicReasoner (one request per next_step)
          -> guardrails.validate_tool_request -> telemetry.run_tool / resources.resource_overview -> EvidenceRegistry
          -> guardrails.validate_diagnosis (one bounded correction) -> [--respond] response.respond:
             remediation.AnthropicPlanner (propose_action | escalate) -> safety.evaluate (catalog allowlist, args,
             targets, evidence) -> verify.collect baseline -> kill switch -> safety.request_approval (human, digest-bound)
             -> dry-run | actions.HttpActionClient (journal first, once) -> observe -> verify.judge
          -> report_io.write_report (report.py) -> JSON + Markdown with evidence links

The model only ever gets read-only telemetry tools and, after an accepted diagnosis, one proposal-only call.
Approval, kill switch, dispatch and verification are deterministic code. Remediation runs through our team Action API
(a separate process: `action_server.py` + `action_executor.py`), with a server-issued, single-use approval code.
See `ACTION_API.md`, `docs/ACTION_API_ARCHITECTURE.md` and `docs/END_TO_END_DEMO.md`.

## Setup
    cd incident-agent && uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -r requirements.txt
    export ANTHROPIC_API_KEY=...          # never commit; or put it in the gitignored .env
    .venv/bin/python cli.py endpoints --write-env   # discovers Jaeger/OpenSearch/Grafana host ports into .env

## Check endpoints (tools, status, browser URLs)
    .venv/bin/python cli.py endpoints
    .venv/bin/python telemetry.py check

## Investigate (read-only)
    .venv/bin/python cli.py investigate --incident-id INC-001 --symptom "<symptom>" [--service checkout] --model <model-id>

## Team Action API
    .venv/bin/python cli.py action-api init-token            # once; adds ACTION_API_TOKEN/URL to .env if absent
    .venv/bin/python cli.py action-api serve [--executor docker]   # separate terminal; approval codes print here
    .venv/bin/python cli.py action-api health | operations | services | actions | show <action_id>
    .venv/bin/python cli.py investigate ... --respond --action-api            # dry-run + server-side validation
    .venv/bin/python cli.py investigate ... --respond --action-api --execute  # live: type 'approve <CODE>'
    .venv/bin/python cli.py audit [--server] [--incident-id ID]
    .venv/bin/python cli.py report <incident-id> [--json]

## Investigate and remediate (catalog file, e.g. an organiser contract)
    # dry-run (default): full plan/policy/approval flow, nothing sent
    .venv/bin/python cli.py investigate ... --respond --action-catalog <catalog.json>
    # live: needs an organiser catalog, ACTION_API_URL and an interactive terminal for approval
    .venv/bin/python cli.py investigate ... --respond --action-catalog <catalog.json> --execute
    .venv/bin/python cli.py kill-switch on|off|status
    .venv/bin/python cli.py catalog --action-catalog <catalog.json>

## Grafana dashboards (root cause and verification per incident)
    .venv/bin/python cli.py grafana sync                  # publish every runs/*/*_report.json to the "Incident Zero" folder
    .venv/bin/python cli.py investigate ... --grafana     # publish right after the report is written
    .venv/bin/python cli.py grafana export <dir>          # write the dashboard JSON instead (review or file provisioning)

Open `$GRAFANA_URL/grafana/d/iz-index`. It lists every incident and its root-cause component, and links to two pages
per incident. **Root cause** shows the diagnosis, every cited evidence item with its captured value, and live re-queries
of the same signals, with the diagnosed component in red. **Verification** shows the action, the approval, and each
recovery check (baseline, post-action samples, judged state) with its bound as a dashed line. Both pages use the
incident's time range and have markers for detection, diagnosis, baseline, approval, dispatch and verification. The
**Now (last 15 min)** link re-runs the same panels on live data.

`grafana_dash.py` builds the dashboards from the saved reports only; it never queries the backends itself. Publishing
uses the demo's anonymous Grafana access, or `GRAFANA_TOKEN` (a service-account token) when set. Grafana has no data
volume, so run `grafana sync` again after `make start`. Jaeger and OpenSearch keep no data across a restart either, so
trace and log panels of older incidents can be empty; the captured values in the tables remain.

Outcomes: `VERIFIED_RECOVERY` (status RECOVERED), `PARTIAL_RECOVERY`, `NOT_RECOVERED`, `UNVERIFIED`, `ACTION_FAILED`,
`ESCALATED` (status ESCALATED/REJECTED), `DRY_RUN` (status ACTION_PROPOSED). Reports are internal and provisional
until the official schema is supplied.

## Tests (mocked, no network)
    .venv/bin/python -m pytest -q tests
