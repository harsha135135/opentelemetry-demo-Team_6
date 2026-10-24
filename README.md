# Bosch Incident Zero Hackathon — Team 6

## Project

AI On-Call Engineer for Cloud-Native Incident Response

## Team Members

| No. | Name | GitHub Username | Responsibility |
| --- | --- | --- | --- |
| 1 | Harshavardhan M | harsha135135 | CLI, orchestrator and integration through CP2 |
| 2 | Padarthi Neha Sai | pnehasai | Read-only telemetry: metrics, logs, traces and permitted health information |
| 3 | Harshita C | Harshita100 | Member 3: bounded LLM tool calling and evidence-backed diagnosis |
| 4 | Deepika Raman | deepika-raman | Member 4: architecture sketch and safety explanation |
| 5 | Aditya R | adityatr64 | Member 5: authorised incident testing and checkpoint reports |

Member 3–5 assignments follow the team-table order and should be confirmed by the team. Who built which part is in
[docs/TEAM_FORM.md](docs/TEAM_FORM.md).

## What it does

**Alert → bounded Claude tool loop (read-only tools) → evidence-validated diagnosis → proposal-only planner →
deterministic policy → human approval → team-defined Action API → verification → report.**

The LLM can only read telemetry and propose. A human approves the exact action with a one-time code. A separate
Action API process is the only component that can change the system, and verification is measured from fresh
telemetry. One-page overview: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Problem Statement

Build an AI-powered incident response agent that investigates failures in the OpenTelemetry Demo microservices
application. The agent must identify root causes using real telemetry, recommend safe remediation actions, obtain
human approval, execute permitted actions, and verify system recovery.

## Technology Stack

- Python 3.11 (`uv` for the virtual environment)
- Anthropic Claude API: `claude-sonnet-5-5` in the live runs, set with `--model` or `ANTHROPIC_MODEL`
- Docker and Docker Compose
- OpenTelemetry Demo, OpenTelemetry Collector, Prometheus, Jaeger, OpenSearch, Grafana
- Git and GitHub

## Local Setup

Clone the repository:

    git clone https://github.com/harsha135135/opentelemetry-demo-Team_6.git

Enter the repository:

    cd opentelemetry-demo-Team_6

Download Docker images:

    docker compose pull

Start the application **with the observability layer**. A plain `docker compose up` sends telemetry only to the
collector's debug output, so the agent sees nothing.

    make start            # full demo + observability + extras

Check container status / stop:

    docker compose ps
    make stop

### Set up the agent

    cd incident-agent
    uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -r requirements.txt
    export ANTHROPIC_API_KEY=...                    # or put it in incident-agent/.env (gitignored)
    .venv/bin/python cli.py endpoints --write-env   # Jaeger/OpenSearch/Grafana ports change on every restart
    .venv/bin/python cli.py action-api init-token   # adds ACTION_API_TOKEN/URL to .env if absent
    .venv/bin/python cli.py endpoints               # every backend should report HTTP 200

Re-run `endpoints --write-env` after every `make start`.

## How to Run the Agent

**Hands-free demo: the alert watcher** (two terminals, started once; no commands per incident):

Terminal 1: the Action API (the only process that can restart containers; approval codes appear here).

    cd incident-agent && .venv/bin/python cli.py action-api serve --executor docker

Terminal 2: the watcher.

    cd incident-agent && .venv/bin/python cli.py watch --model claude-sonnet-5-5 --execute

Then only use the browser:
1. **Inject a fault**: toggle a flag in the flagd UI (<http://localhost:8080/feature>) or stop a container.
2. **The watcher alerts by itself.** It polls Prometheus every 20 s. A breach must hold for 2 checks in a row before
   it alerts: a container that stopped reporting, CPU above 150% of one core, an error-rate jump against the same
   service 10 minutes earlier, or a p95 jump. The alert becomes the incident's symptom. It is saved as `alert.json`
   and is the first timeline event, and the root-cause page shows it with a link to its exact query.
3. **Approve in the browser.** Once the proposal passes policy, the Grafana index
   (<http://localhost:8080/grafana/d/iz-index>) shows **Waiting for human approval**. Its link opens the approval
   page (<http://127.0.0.1:8787/ui>), which shows the exact Action API JSON and links to the evidence. Type the
   one-time code printed in terminal 1 and choose Approve, or choose Reject.
4. **Read the report in Grafana.** The agent runs the action once, verifies recovery against the baseline, writes the
   report and republishes the dashboards.

Breaches already present when the watcher starts stay muted until they clear. A service from a handled incident
stays muted until it has been healthy for 3 checks. `cli.py watch --once` prints the current breaches. Without
`--execute`, approved actions are only validated (dry-run).

**Read-only investigation** (one terminal; no Action API needed):

    cd incident-agent
    .venv/bin/python cli.py investigate --incident-id INC-001 --symptom "Checkout failing" --service checkout \
        --model claude-sonnet-5-5
    .venv/bin/python cli.py report INC-001

If the Action API is configured and reachable, the agent is also offered the read-only `container_state` tool. It
reads live Docker state, which tells a stopped container apart from missing telemetry.

**Dry-run remediation** (the default): add `--respond --action-api`. The agent plans, runs the policy and asks the
Action API to validate the payload. Nothing is executed. This still needs the Action API running (terminal 1
below). Without it, the run escalates with "Action API unavailable".

**Live, human-approved remediation** (two terminals):

Terminal 1: the Action API. It is the only process that can change the system, and approval codes appear here.

    cd incident-agent && .venv/bin/python cli.py action-api serve --executor docker   # or: simulate (default)

Terminal 2: the agent.

    cd incident-agent
    .venv/bin/python cli.py investigate --incident-id INC-002 --symptom "..." --model claude-sonnet-5-5 \
        --respond --action-api --execute --approval-ttl-s 300
    .venv/bin/python cli.py report INC-002 ; .venv/bin/python cli.py audit --server

**Read-only flagd demo.** The operator toggles a failure flag by hand in the flagd UI
(<http://localhost:8080/feature>). After 2–3 minutes, describe the symptom you observe and run a dry-run:

    .venv/bin/python cli.py investigate --incident-id FLAG-1 --symptom "<symptom you observe>" \
        --model claude-sonnet-5-5 --respond --action-api --grafana

The agent never reads flag state: it diagnoses from telemetry alone. A restart needs process-level evidence, so a
flag-driven fault is escalated with steps for a human instead of being restarted. Reset the flag in the flagd UI
afterwards.

**Grafana navigation.** `cli.py grafana sync`, or `--grafana` on a run, publishes the Incident Zero index:
<http://localhost:8080/grafana/d/iz-index>. Each incident row links to:
- the root cause;
- the verification result;
- the Action API request;
- Grafana Explore for the component's metrics and logs, and Jaeger traces in the incident window;
- the symptom metrics and logs of the calling service.

Each cited evidence row on the root-cause page opens its own exact PromQL, log query or trace in Explore.

Use a new `--incident-id` for every run, because each incident allows one action. Full walkthrough and scenarios
A–J: [incident-agent/docs/END_TO_END_DEMO.md](incident-agent/docs/END_TO_END_DEMO.md). Every CLI command:
[incident-agent/INTEGRATION.md](incident-agent/INTEGRATION.md).

## How the Approval Gate Works

- **The LLM cannot act.** It has read-only tools and a proposal-only planner. Deterministic policy then checks the
  proposal:
  - the operation is allowlisted and its arguments are valid;
  - the target is the diagnosed component;
  - the cited evidence exists;
  - a restart needs process-level evidence about the target: stopped, unhealthy or restarting, stopped reporting, a
    CPU or memory anomaly, or callers getting refused connections or failed name resolution;
  - confidence is not low;
  - a symptom is present at baseline.
- **Live approval (`--execute`).** The Action API creates an approval bound to the SHA-256 of the exact action and
  prints a one-time code **only on its own console**. The on-call human checks the action against the agent's
  summary and types, in the agent terminal, the word `approve` followed by that code: `approve XXXX-XXXX`. The
  server then verifies the code.
  - Case, angle brackets and quotes are ignored. Typing only `approve`, or only the code, prints a hint and asks
    again, up to 3 attempts within the TTL.
  - An empty line, `reject`, `no` or `deny` rejects immediately. So do a non-interactive session and a timeout.
  - Single use. The TTL defaults to 120 s (`--approval-ttl-s`; use 300 for the live demo). 5 wrong codes lock the
    approval on the server.
  - Any change to the action voids it.
- **Dry-run (default).** No server approval is opened. The agent shows its own digest-bound code, and the server
  only validates the payload (`POST /v1/actions/validate`).
- One action per incident, idempotency keys, and uncertain outcomes are never retried. Recovery is measured from
  fresh telemetry, never assumed from an HTTP 200.

Security model: [incident-agent/docs/ACTION_API_ARCHITECTURE.md](incident-agent/docs/ACTION_API_ARCHITECTURE.md).

## How the Kill Switch Works

    .venv/bin/python cli.py kill-switch on      # creates incident-agent/KILL_SWITCH
    .venv/bin/python cli.py kill-switch status
    .venv/bin/python cli.py kill-switch off

- While it is on, every state-changing action is blocked.
  - The agent checks it before approval and before dispatch.
  - The Action API checks it at execute and returns HTTP 423. It checks again in its worker, and a blocked record
    becomes `blocked`.
  - `action-api health` shows its state.
- Both processes read the same `KILL_SWITCH` file, so `kill-switch on` stops both terminals.
  `INCIDENT_AGENT_KILL_SWITCH=1` also works, but only in the process whose environment sets it.
- It prevents new actions. It cannot reverse a restart the server has already started.

## Known Limitations

- **Team-defined interfaces.** The Action API and the incident-report JSON schema are team-defined. No organiser
  contract was supplied.
- **Restart only.** The only remediation is `restart_service` on allowlisted application and worker containers.
  - flagd, the load generator, the collector and the observability containers are hard-denied.
  - Feature-flag, network, code, config and load faults are diagnosed and escalated, not fixed.
  - Flag toggles and load-generator throttling are not supported. Scaling is Kubernetes-only and does not apply
    here.
- **Same OS user.** The Action API and the agent run as the same OS user. The privilege separation is by process,
  not enforced by the OS.
- **Uncalibrated confidence.** Confidence labels are model-reported estimates.
- **Live-only telemetry windows.** Tools query live windows of at most 60 minutes, so a past incident can't be
  re-examined.
- **60-minute silence lookback.** A stopped container counts as silent only from about 150 s to 60 min after its
  last sample. After that it drops out of view, and a long outage looks like a telemetry-pipeline outage.
- **`make start` is required for telemetry.** Without the observability layer every tool returns empty, and the
  agent abstains.
- **Gaps in container metrics.** After `make start`, `docker_stats` exported no `container_uptime` series for
  `cart`, `product-catalog`, `accounting` and `fraud-detection`, although they ran normally. Metric-based uptime and
  silence checks can't judge them, but `container_state` still can.
- **Optional services.** The load generator's `user_ask_agent` task fails unless the optional `compose.agent.yaml`
  services run. The agent correctly reports this as environmental.
- **Tool budget.** Investigations are budgeted at 12 tool calls, so secondary anomalies may remain unexamined. The
  report lists them as limitations.

## Checkpoints

The checkpoint requirements are unchanged. A checkpoint counts as passed only once the required behaviour has been
demonstrated live to mentors. For CP2, report the number correctly diagnosed out of the number of real
mentor-selected incidents tested. Mocked tests don't count toward the three incidents.

| Checkpoint | Required demonstration | Responsibility | Status |
| --- | --- | --- | --- |
| CP0 — Setup | Docker services and observability backends are operational; telemetry endpoints respond; setup commands are reproducible. | Harsha integrates setup; Neha verifies telemetry access; Member 5 records results. | `TODO (team to fill)` |
| CP1 — Eyes | Investigate one authorised practice incident with real telemetry, show symptoms and evidence, and present a concise architecture sketch. | Harsha connects CLI and orchestration; Neha supplies read-only tools; Member 4 explains architecture and safety; Member 5 captures the run. | `TODO (team to fill)` |
| CP2 — Brain | Bounded LLM tool calling selects useful investigation steps and produces a root-cause diagnosis supported by real evidence. Demonstrate correct diagnoses on at least three mentor-selected authorised practice incidents. | Member 3 owns diagnosis; Harsha integrates it; Neha supports queries; Member 5 checks and reports each result; Member 4 explains boundaries. | `TODO (team to fill)`: correct / tested |

Run-by-run results: [docs/EVALUATION_NOTE.md](docs/EVALUATION_NOTE.md).

<details>
<summary>Pre-CP2 working plan (checkpoint presentation, 23 October 2026, 5:30 PM IST)</summary>

Before CP2, the working flow had to be:

**Alert → targeted telemetry queries → evidence → diagnosis or honest escalation → checkpoint report.**

- Harsha: validate inputs, connect shared contracts and modules, bound the orchestration loop, and provide one
  reproducible CLI demonstration path.
- Neha: return real, timestamped telemetry with source/query references; distinguish missing data from healthy
  results.
- Member 3: bound model/tool calls and time, validate structured output, compare hypotheses, cite captured evidence
  and report uncertainty.
- Member 4: explain components, data flow, read-only access and why the agent cannot mutate the demo or read
  feature-flag state for diagnosis.
- Member 5: obtain the mentor-selected incidents, record expected versus observed diagnoses, separate mocked tests
  from live runs, and document failures honestly.

</details>

Do not hard-code incident answers, fabricate evidence, inspect feature flags for diagnosis or modify the demo.
Implementation and code commits must stay within the organiser-confirmed coding window.

## Safety Constraints

- Do not inspect feature flags to discover incident causes.
- Do not modify the demo or scoring harness.
- Do not execute state-changing actions without approval.
- Execute only allowlisted operations through the Action API. No organiser actions were supplied, so the API is
  team-defined.
- Do not fabricate evidence.
- Escalate uncertain diagnoses.
- Record actions and verification results.

## Hackathon Deliverables

| Deliverable | Status |
| --- | --- |
| Working AI On-Call Engineer | Implemented in `incident-agent/`. Read-only diagnosis has run on live telemetry; a human-approved live remediation is still to be demonstrated. |
| Source code and setup instructions | This README and [incident-agent/INTEGRATION.md](incident-agent/INTEGRATION.md) |
| Human approval gate and kill switch | Implemented and covered by mocked tests; [see above](#how-the-approval-gate-works). Live demonstration pending. |
| One-page architecture overview | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Incident reports | [incident-reports/](incident-reports/README.md): JSON plus Markdown. LIVE-1-BASELINE, LIVE-2-PAYMENT and DEMO-PAY-4 carry the team-defined schema version `team6.incident-report/1.0`; the two earlier runs predate the schema. |
| Evaluation note | [docs/EVALUATION_NOTE.md](docs/EVALUATION_NOTE.md). Practice-incident ground truth still to fill in. |
| Team contribution and AI tools declaration | [docs/TEAM_FORM.md](docs/TEAM_FORM.md). Members must confirm their rows and declare AI tools. |
| Final live demonstration | `TODO (team to fill)` |

## Current Status

The incident agent is implemented in `incident-agent/`:

- read-only telemetry investigation and an evidence-validated Claude diagnosis;
- gated remediation through our **team-defined Action API**, with a server-issued single-use approval code, a kill
  switch and an audit trail;
- measured recovery verification and reports.

229 mocked and in-process tests pass (24 October). Checkpoints count only once they are demonstrated live.

| Guide | Contents |
| --- | --- |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | One-page architecture overview, as built |
| [docs/EVALUATION_NOTE.md](docs/EVALUATION_NOTE.md) | Tests, live runs, failures |
| [docs/TEAM_FORM.md](docs/TEAM_FORM.md) | Team, contributions, tools and libraries |
| [incident-agent/INTEGRATION.md](incident-agent/INTEGRATION.md) | Setup and all CLI commands |
| [incident-agent/docs/END_TO_END_DEMO.md](incident-agent/docs/END_TO_END_DEMO.md) | Two-terminal demo, scenarios A–J |
| [incident-agent/docs/ACTION_API_ARCHITECTURE.md](incident-agent/docs/ACTION_API_ARCHITECTURE.md) | Action API design, security model, examples |
| [incident-agent/ACTION_API.md](incident-agent/ACTION_API.md) | Team API vs organiser contract status |

## Shared Contracts

- **[Team integration contract](docs/TEAM_INTEGRATION_CONTRACT.md)**: who sends what to whom (updated for the revised `Evidence` model).
- [`incident-agent/models.py`](incident-agent/models.py): shared Pydantic models. `Evidence.service` is optional, `source` may be `"derived"`, and `trace_ids`/`log_ids` were added.
- [`incident-agent/contracts.py`](incident-agent/contracts.py): orchestrator, reasoning and guardrail interfaces.
- [`incident-agent/schemas/incident_report.schema.json`](incident-agent/schemas/incident_report.schema.json): team-defined incident-report schema.

## Local Services

| Service | URL |
| --- | --- |
| Astronomy Shop | <http://localhost:8080> |
| Grafana | <http://localhost:8080/grafana/> |
| Jaeger | <http://localhost:8080/jaeger/ui/> |
| Load Generator | <http://localhost:8080/loadgen/> |
| Feature Flags | <http://localhost:8080/feature/> |

URLs depend on the Docker Compose configuration. The agent never reads feature-flag state.

## Design and Implementation Guides

- [Architecture overview, as built](docs/ARCHITECTURE.md)
- [Simple implementation guide](docs/IMPLEMENTATION_GUIDE.md): the original plan from 23 October, kept for
  reference. "Current Status" above describes what is implemented.

## References

- <https://github.com/open-telemetry/opentelemetry-demo>
- <https://opentelemetry.io/docs/>
- <https://docs.docker.com/>

---

Bosch Incident Zero Hackathon 2026 — Team 6
