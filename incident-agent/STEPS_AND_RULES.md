# Steps 1-4 and rules: what the agent does and where

Checked against the organisers' slides ("What a Team's Solution Should Do", "Deliverables and Rules",
"Setup, Guidelines and Rules"). Steps 5-7 and the stretch goals are not covered yet.

## Steps

| Step (slide) | What we do | Where |
|---|---|---|
| **1 Detect the incident**: receive an error-rate or latency threshold alert on the demo's metrics and start an investigation automatically | `python cli.py watch` polls Prometheus every 30 s. Alert per service vs its own 30-min baseline: error rate ≥ 2 % and > 3× baseline + 1 pt with ≥ 3 failed calls (partial faults such as one failing product stay in single digits); p95 ≥ 1 s and ≥ 3× baseline from ≥ 20 requests. Must hold on 2 consecutive checks. Breaching services are grouped into one alert; a service that breaches later raises a new alert (overlapping faults). The alert is posted to Grafana and the investigation starts by itself. | `detector.py`, `cli.py watch` |
| **2 Investigate with tools**: the agent decides which tool to call next, not one big prompt | Bounded LLM tool loop (LangChain; ≤ 8 model requests, ≤ 16 tool calls, 240 s, 60k tokens). Read-only tools: `overview`, `resource_overview`, `log_overview`, `search_traces`, `query_logs`, `query_metrics`, `service_health` (traces, metrics, logs, container health). Each result is evidence with an ID. After each round code adds an *evidence picture* (ranked candidates) as a summary to check. | `orchestrator.py`, `reasoning.py`, `telemetry.py`, `resources.py`, `candidates.py` |
| **3 Diagnose with evidence**: failing component, cause, confidence, exact traces / log lines / metric values; invented evidence penalised | `finish` returns component, observed/likely cause, unknowns, confidence, alternatives, next step and cited evidence IDs. Guardrails reject uncited or unknown IDs, evidence that is not `ok`, components not in the evidence, and any flag wording. Confidence capped: high needs 2 sources; weak evidence -> low. Several active faults: best-supported one + "also active" alternatives. Not confident -> `inconclusive` (escalate). | `guardrails.py`, `orchestrator.py`, `candidates.py` |
| **4 Propose a safe fix**: only allowlisted runbook actions in the Action API, with a short reason | Optional `proposed_fix` in `finish` (operation, target, reason), validated by code against the **team Action API** (Harsha, `feature/harsha`): the allowlist is `action_catalog.json` (mirrors its `GET /operations`: today only `restart_service`, its allowed targets and `grace_period_seconds`). Refused when: not an Action API operation, target not allowed / not in the live inventory / not the diagnosed component, reason missing, confidence low, or - for a restart - no evidence of a process-level problem in that service (crash loop / restarts, sudden CPU or memory jump, unanswered calls): a restart does not fix code, configuration or dependency faults, so the agent escalates. Output: `ActionProposal` in the report + `runs/<id>/action_payload.json` **validated against `schemas/action_api/ActionPayload.schema.json`**, ready for steps 5-6. **Proposed only, never executed.** Not offered: *toggle a feature flag* (choosing a flag needs flag settings, out of bounds), *throttle load generator* / *scale* (no such Action API operation). | `runbooks.py`, `action_catalog.json`, `schemas/action_api/` |
| **Demo on Grafana** | Team 6's own Grafana (port 3001), separate from the demo's: alerts, diagnoses and proposals as annotations; Incidents table; one page per incident with diagnosis, evidence table, evidence picture and metrics around the incident (IST). | `grafana/`, `grafana_publish.py`, `grafana_dashboards.py` |

## Rules

| Rule (slides) | Status |
|---|---|
| Do not edit the demo's source/config, faultctl or the scoring harness | ✅ Only `incident-agent/` changes. Our Grafana is a separate container reading Prometheus through the host port. |
| Do not read feature-flag settings (flagd files, flag UI, flag API) to diagnose | ✅ The agent never queries flag services; flag wording is removed from all telemetry; diagnoses mentioning flags are rejected. (Developers checked flags only to label test runs; that ground truth lives in `evals/*.local.json`, never read by the agent.) |
| No action without human approval; agent changes the demo only through the Action API | ✅ Nothing is executed. Proposals are Action API payloads (team API schema). Steps 5-6 (approval gate, kill switch, dispatch, verification) exist on `feature/harsha` and are not merged here yet. The team API is team-defined: **no organiser Action API contract has been supplied**. |
| No hard-coded answers to the practice incidents | ✅ No service, fault or flag names in the decision logic or prompt (a test enforces this for the prompt); ranking tests use invented service names. |
| Nothing outside your own laptop | ✅ Everything local. Only outbound call: the LLM API (Gemini). |
| Output per incident follows the **provided JSON schema** | âŒ **Schema not received.** Reports use the team's internal format (`runs/<id>/<id>_report.json` + `.md`). Needs the organisers' schema. |
| Read the LLM tool-calling primer | âš ï¸ No primer in the repo or any team branch. Our loop uses the same pattern as the demo's own agent (`src/agent`: LangChain, tools bound to the model, `tool_calls` answered by ID, bounded loop); ours adds explicit budgets and a terminal `finish` / `inconclusive` tool. |
| Python 3.11 | âš ï¸ Developed on 3.13; nothing 3.13-specific is used, but not tested on 3.11. |
| Code written on the day from 09:15; commits before then not allowed | âš ï¸ **Check with the organisers**: this repo has commits from 23 Oct. |
| Be honest about what does not work | See `DIAGNOSIS_NOTES.md` (limitations, demo issues, evaluation). |

## Run it

```powershell
docker compose -f grafana/compose.yaml up -d      # once: our Grafana on http://localhost:3001
.\.venv\Scripts\python.exe cli.py watch           # step 1-4 automatically on threshold alerts
# or by hand:
.\.venv\Scripts\python.exe cli.py investigate --incident-id INC-1 --symptom "Alert: ..."
```
