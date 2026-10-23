# Incident Zero — Simple Implementation Guide

**Status: final planning baseline, documentation only.** No modules, runnable
scaffolding, incident reports or automated tests are implemented by this change.
All file names, interfaces and behaviour below are plans for later work.
The [design guide](ARCHITECTURE.md) is the architecture reference.

## 1. Confirm the environment and contracts

### Repository findings

Audit date: **23 October 2026**. Target:
`harsha135135/opentelemetry-demo-Team_6`, branch `feature/harsha`.
Audited baseline: `0715307b`, including the latest team README updates.

| Verified from repository files | Consequence for implementation |
| --- | --- |
| [Compose layering](../Makefile): `compose.yaml` is the base; `compose.observability.yaml` adds telemetry backends. | Plain `docker compose up -d` does not include those backends. Use the organiser-approved launch recipe with the observability layer. |
| [Existing agent](../src/agent/README.md) is a Python 3.14 shopping assistant with cart/checkout tools. | Create a separate Python 3.11 `incident-agent/`; do not adapt or change the demo's agent. |
| [Metrics test](../test/telemetry/test_metrics.py) uses `GET /api/v1/query`; Prometheus has a published port, default 9090. | Start with the observed service labels and bounded query templates. Confirm actual metric names at runtime. |
| [Trace helpers](../test/telemetry/conftest.py) use `/jaeger/ui/api/v3/services` and `/jaeger/ui/api/v3/traces`. | Use this fork's v3 contract. Trace search takes `query.service_name`, `query.num_traces`, `query.start_time_min`, `query.start_time_max` and returns `result.resourceSpans`. |
| [Proxy configuration](../src/frontend-proxy/envoy.tmpl.yaml) forwards `/jaeger/`; the default frontend port is 8080. | Candidate host access is `http://localhost:8080/jaeger/ui/api/v3/...`; verify it in the authorised running stack. |
| [Logs test](../test/telemetry/test_logs.py) queries `otel-logs-*` via read-only `POST /_plugins/_ppl`, filtering `resource.service.name`. | Use bounded service/time queries. A POST can be read-only; permit only this validated query operation. |
| [Observability Compose](../compose.observability.yaml) dynamically publishes Jaeger and OpenSearch host ports. | Discover the actual port mapping; do not assume host ports 16686 or 9200. |
| [Collector configuration](../src/otel-collector/otelcol-config.yml) includes container resource telemetry. | Prefer these observations for health; direct container status needs a separately permitted interface. |
| [Telemetry registry](../telemetry-schema/README.md) describes instrumentation. | It is not the official incident-report JSON schema. |

These are configuration findings, not proof that services are currently running.
No demo services, fault injectors or live telemetry tests were executed for this
documentation change. Do not use the existing shopping MCP as a remediation API.

### Inputs needed before dependent work

| Input to obtain from organisers | What remains blocked without it |
| --- | --- |
| Alert format, service inventory, permitted telemetry access and deployment-history source, if available | Live intake and adapters for unavailable sources; use explicitly labelled internal fixtures for unit work only. |
| Action API address/authentication, exact request/response contract and approved runbooks | All real remediation. Also confirm execution-status lookup, idempotency, dry-run and rollback support; assume none until documented. |
| Official incident-report JSON schema and scoring invocation | Final schema-compliant export and scoring claims. An internal report may still be developed. |
| LLM provider/model, credentials, limits and permission to use the supplied endpoint | Live model integration. Never store keys in Git or audit output. |
| Practice-incident procedure and offline bundle format | Reproducible evaluation. The human operator handles fault injection/reset outside the agent. |
| Final coding window, submission deadline and team rules | Starting later implementation and scheduling the final submission. |

The supplied slides conflict: one describes a one-day event with a 09:15 coding
start and 16:30 freeze, while another describes two days and a 13:30–14:00
submission slot. They also mix teams of five with “all three members”. Plan for
five members as requested and confirm the controlling rules with organisers.
Use the relative steps below instead of assuming a deadline.

### Small file layout and ownership

Keep one CLI process and one incident in progress. Use Python 3.11, Pydantic,
httpx, a small provider adapter, pytest and the official schema validator when
the schema arrives. Pin compatible versions during implementation. All paths
below are **proposed**, not newly created modules.

| Proposed path under `incident-agent/` | Responsibility | Owner |
| --- | --- | --- |
| `models.py`, `main.py` | Shared records, CLI, bounded state transitions and integration | Harsha |
| `telemetry.py` | Metrics, logs, traces and permitted health tools | Neha |
| `diagnosis.py` | LLM adapter, tool selection and hypothesis checks | Member 3, to confirm |
| `safety.py`, `actions.py` | Policy, terminal approval, kill switch and Action API | Member 4, to confirm |
| `verify.py`, `report.py`, `tests/` | Recovery, exports, shared regression tests and evaluation | Member 5, to confirm |

Neha's tool-layer ownership is recorded on `origin/feature/neha` in
`incident-agent/team/neha.md`. Preserve it when that branch is integrated.
Other assignments are proposals, not claims of work already completed.
Each owner supplies their module's tests; Harsha coordinates shared model changes.
Agree the records in the design guide first so everyone can work independently.

Avoid a web dashboard, multiple agents, incident memory, a vector database and
Kubernetes in the initial build. Add optional features only after the complete
CLI flow passes the acceptance checks.

## 2. Build the read-only path first

Accept an alert from the supplied interface or a clearly labelled manual CLI
input. Validate its service, timestamp and symptom; add simple threshold polling
only if the starter kit does not supply alerts. Deduplicate repeated alerts
while an incident is active.

Implement three small tools: `query_metrics`, `query_logs`, `search_traces`.
Each accepts a validated service and bounded time window and returns the shared
tool-result record. Add trace-detail lookup and deployment history only after
their contracts are verified. Derive dependency relationships from parent/child
trace spans and observed service identities, not from presumed incident answers.

Keep backend URLs fixed in operator configuration. Reject arbitrary URLs,
redirects, raw shell commands and unrestricted queries from the model. Validate
service names against the approved inventory and construct queries from templates.
Filter out feature-flag state and fault-control metadata before evidence reaches
the model or report; do not search those sources to infer a cause.

Save evidence immediately with its query, time window, observed value, source
ID and a local capture reference. Distinguish empty results from unavailable
tools and stale data. Bound and redact captured data; keep credentials and raw
private telemetry out of Git. Every diagnosis citation must resolve to a capture.

**Done when:** an alert produces a readable investigation with real evidence
references, including a clear result when a telemetry backend is unavailable.

## 3. Add bounded diagnosis

Investigate the alert symptom, locate the affected service, follow a relevant
trace, inspect related metrics/logs and test at least one plausible alternative
when the data allows it. Let the model select the next useful read-only tool;
do not paste every backend's contents into one prompt or encode practice answers.

Validate tool arguments and model responses with Pydantic. Reject unknown tools,
unknown evidence IDs and unsupported action names. Record observations separately
from hypotheses. An evidence-poor diagnosis stays inconclusive, regardless of
the model's claimed confidence. Low/medium/high confidence labels are estimates,
not statistical calibration.

Suggested initial limits, to tune on practice runs: 180 seconds for investigation,
12 telemetry calls, 6 model calls, 10 seconds per telemetry request, 30 seconds
per model request, and a 10-minute overall incident deadline including approval
and verification. Clamp each operation to the remaining deadline. Allow one
retry for transient read-only failures and one structured-output repair; both
consume the original budgets. Configure a provider token/spend cap as well.

**Done when:** the agent explains its diagnosis using captured evidence, tests
alternatives where possible, and terminates safely on missing data, contradictory
observations, malformed responses, provider failure or exhausted budgets.

## 4. Add approval, one action and recovery checks

Use the following sequence; all mutation stays inside the Action API adapter.

1. Select a remedy from the official runbook catalog. Validate action, service,
   parameter types/ranges and evidence-based preconditions in deterministic code.
   An unsupported remedy causes escalation. A permitted flag toggle still requires
   a telemetry-based diagnosis; it never authorises reading flag state.
2. Capture the pre-action baseline and define recovery criteria before approval.
   Show the diagnosis, evidence, exact action/parameters, expected effect, risk,
   verification criteria and any supported rollback to the human.
3. Request an explicit terminal approval bound to incident ID, proposal revision,
   a digest of the complete proposal and expiry. Default to rejection. Suggested
   expiry: 120 seconds, bounded by the remaining incident deadline. Changed
   parameters or verification criteria require a new approval.
4. Default to dry-run: record the proposal without calling any mutation endpoint.
   Live execution requires deliberate operator selection and current approval.
   An API dry-run is usable only if the official contract guarantees no mutation.
5. Before dispatch, confirm the original symptom and runbook preconditions using
   fresh telemetry. If already healthy, stale or materially changed, stop or
   re-propose. Recheck policy, approval, expiry, single-use status and the kill
   switch. Consume approval and durably record dispatch intent first. Use a
   single-process lock and local append-only audit journal; do not auto-resume
   pending actions after a crash.
6. Call only the documented Action API. Persist request ID and result. On timeout
   or interruption, mark the outcome unknown; query documented status if available,
   otherwise escalate. Never blindly repeat a mutation. Any retry must use the
   API's confirmed idempotency/reconciliation contract.
7. Verify with fresh telemetry covering the original symptom, service health and
   relevant traces/logs. Compare against the saved baseline and the alert's
   recovery thresholds. Suggested observation period: up to 120 seconds, requiring
   two consecutive 30-second healthy windows after telemetry ingestion delay.
   Require sufficient request volume, configured before the run; absent traffic,
   stale data or a missing threshold yields inconclusive, not recovered.
8. If recovery fails, record the evidence and escalate. If an official rollback
   exists, present it as a separate proposal requiring fresh approval through the
   same gate. Do not perform an unapproved automatic rollback or second remedy.

Provide a documented local kill-switch control owned by the operator; check it
throughout the workflow and immediately before every dispatch. Stopping a run
prevents further actions but cannot cancel or reverse an already accepted API
request. Persist the known outcome and escalate when it cannot be determined.

**Done when:** one authorised practice incident goes from alert to human approval,
one logged Action API call, measured recovery and a report. Dry-run, rejection,
timeout and cancellation all produce reports with no unapproved mutation.

## 5. Validate, report and submit

Write a readable report for every outcome: timeline, affected services,
investigation, evidence, diagnosis, uncertainty, proposals, approval, execution,
verification, final status and limitations. Prevention recommendations remain
suggestions; they do not edit demo code or configuration.

Keep the internal report model separate from the official exporter. Map to the
actual supplied schema, validate the final JSON and fail export visibly on schema
errors. Do not invent official field names or label provisional JSON compliant.
Preserve the evidence/audit files when an export fails.

### Minimum tests and acceptance criteria

| Test group | Required result |
| --- | --- |
| Intake and budgets | Valid alerts run; invalid services/times fail; duplicate alerts are controlled; all loops and waits terminate. |
| Telemetry | Missing, empty, stale and failed metrics/logs/traces are distinguished; bounded results retain reproducible evidence references. |
| Diagnosis | LLM timeout, malformed output, invented evidence IDs, conflicting evidence and low confidence fail safely; practice runs measure wrong diagnoses honestly. |
| Policy and approval | Unsupported actions/parameters, rejection, timeout, altered proposals, expired/replayed approval and kill-switch activation prevent dispatch. |
| Execution | API failure, duplicate requests and crash/timeout after dispatch do not trigger blind retries; unknown outcomes are recorded. |
| Verification | API success with a persisting symptom fails; missing data or inadequate traffic is inconclusive; sustained measured recovery passes. |
| Reporting | All terminal paths produce readable reports; final JSON passes the organiser's actual schema. |

Use mocked adapters for unit tests, then real local telemetry for authorised
integration runs. Test the complete negative paths as well as the successful
path. Offline bundles can establish investigation/reporting behaviour; they
cannot prove live remediation or recovery. Keep mock, offline and live results
separate in the evaluation note, including commands, run IDs, expected/observed
outcomes and failures. Do not treat upstream telemetry tests as agent tests:
their [warmup fixture](../test/telemetry/conftest.py) generates checkout traffic.

### Reproducible demonstration and deliverables

For the later implementation, record the exact dependency setup, approved Compose
launch command, discovered local ports, agent invocation and fixture/reset
procedure in the README after validating them. No agent launch command exists yet.
Run the demo in this order:

1. Human starts the approved local stack and checks telemetry readiness.
2. Human selects an authorised practice incident using the organiser's procedure.
3. Agent receives the alert and presents its evidence-backed diagnosis and plan.
4. Show a dry-run or rejected approval with no action; then explicitly approve
   a fresh live proposal on a reset practice run.
5. Show the Action API audit entry, measured verification and validated report.
6. Demonstrate kill-switch prevention and one honest inconclusive/escalated case.

Submit working source with accurate README instructions, the architecture
overview, real incident reports in both formats, an evaluation note and the team
form. Fill in actual contributions and the required tools/model/AI-assistance
declarations truthfully at submission time; do not invent entries or results now.
Record known limitations and give every member a part they can explain.

Before later integration, review the diff and tests, commit only the intended
files, and push the assigned branch to `harsha135135/opentelemetry-demo-Team_6`.
Preserve teammates' work, never force-push, and do not target the upstream demo.
PR creation and merging are separate later activities.

### Current validation status

Only repository inspection and documentation checks apply to this change.
Agent unit tests, live LLM calls, Action API execution, recovery verification and
official report-schema validation are **not run / not yet available**. The main
risks are missing organiser contracts, unverified runtime telemetry access and
the conflicting event schedule. None is concealed by a claim of implementation.
