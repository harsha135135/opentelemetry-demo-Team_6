# Incident Zero: evaluation note

**Team 6 · 24 October 2026.** This note covers what we tested, what worked and what failed. It keeps mocked tests,
live read-only runs and live remediation separate. Results in this note come from the run reports and audit logs in
`incident-agent/runs/`. Where the repository doesn't prove a fact, the note says so.

> **Where the reports are.** `incident-agent/.gitignore` excludes `runs/`, so the agent's original output
> (`incident-agent/runs/<ID>/<ID>_report.{md,json}`) stays on the demo laptop. All five 24 October runs (the four
> `LIVE-*` runs and DEMO-PAY-4) are copied, with their evidence, into
> [`incident-reports/`](../incident-reports/README.md), which is meant to be committed. SMOKE-001 and PRACTICE-1 to
> PRACTICE-8 exist only under `runs/`.

## 1. Mocked and in-process tests

| Item | Value |
| --- | --- |
| Command | `cd incident-agent && .venv/bin/python -m pytest -q tests` |
| Result | **229 passed** on 24 Oct 2026 (after the CPU-metric fix, the restart guard, the Grafana navigation, the alert watcher and the browser approval page). Re-run and update this line before submission. |
| Files | `test_orchestration.py`, `test_safety.py`, `test_scenarios.py`, `test_action_api.py`, `test_telemetry.py`, `test_report.py` |
| Scope | Budgets and loop termination; evidence validation and the bounded correction; policy refusals; approval rejection, expiry, replay and payload mismatch; the kill switch; idempotency; unknown outcomes; verification outcomes; prompt-injection text in telemetry. Action API scenarios A–J run against a real HTTP server on 127.0.0.1 with a fake Docker runner, plus a test that the schemas and OpenAPI file don't drift from the models. |

All network, Docker and LLM access is faked. These tests prove the **control logic**, not diagnosis quality on unseen
faults, and they don't count toward CP2.

## 2. Live read-only runs

All runs used real telemetry from the local stack and the Anthropic API. The team reports using `claude-sonnet-5-5`.
The reports don't record the model ID or a code commit.

- Times are UTC.
- "Calls" is model calls / tool calls / wall time, from each report's `Finished:` line.
- LIVE-2-PAYMENT and DEMO-PAY-4 proposed an action. No run approved or executed one.
- For the 24 October runs, the ground truth was recorded by the integrating session in
  [`incident-reports/README.md`](../incident-reports/README.md), not by the reports. Member 5 still needs to confirm
  those verdicts.

| Incident ID | Time | Symptom given to the agent | Agent conclusion | Status | Remediation outcome | Calls | Expected answer / correct? |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SMOKE-001 | 23 Oct 10:48 | Integration smoke test, not a mentor-selected incident: any service with elevated errors or latency? | Proposed "none (load-generator browser scenarios; no clear service fault)". The validator **rejected** it because the component doesn't appear in the cited evidence. | `INCONCLUSIVE` | — (diagnosis-only build) | 4 / 6 / 18.3 s | `TODO (Member 5 – ground truth kept outside repo)` |
| PRACTICE-1 | 23 Oct 12:00 | Operator-injected practice fault; no symptom given | Proposed "product-catalog and cart containers (restart or outage about 7 minutes before the check)". **Rejected**: the component phrase was not found in the cited evidence. | `INCONCLUSIVE` | — | 4 / 7 / 26.9 s | `TODO (Member 5 – ground truth kept outside repo)` |
| PRACTICE-2 | 23 Oct 12:07 | Same | `product-catalog`. Product-catalog and cart restarted; checkout got connection refused from product-catalog:3550 and zero resolver addresses for cart, so PlaceOrder failed and the frontend returned 500s. | `DIAGNOSED` (medium) | — | 5 / 9 / 20.3 s | `TODO (Member 5 – ground truth kept outside repo)` |
| PRACTICE-3 | 23 Oct 12:15 | Same | No diagnosis. The finish was deferred for unexamined anomalies (opensearch, astronomy-db), then the Anthropic request failed with `BadRequestError`. | `ESCALATED` (model_error) | — | 6 / 10 / 25.0 s | `TODO (Member 5 – ground truth kept outside repo)` |
| PRACTICE-4 | 23 Oct 12:16 | Same | No diagnosis. Anthropic 400: `tool_choice: type "tool" and "any" are not supported for this model`. | `ESCALATED` (model_error) | — | 6 / 11 / 17.0 s | `TODO (Member 5 – ground truth kept outside repo)` |
| PRACTICE-5 | 23 Oct 12:17 | Same | `ad`: CPU about 278% (about 88× the fleet median), memory at 93% of the limit. Not linked to the user-facing symptom (slow checkouts ending in 504s). Unexamined anomalies: opensearch, prometheus, astronomy-db. | `DIAGNOSED` (low) | — | 6 / 10 / 27.8 s | `TODO (Member 5 – ground truth kept outside repo)` |
| PRACTICE-6 | 23 Oct 12:21 | Same | `ad`: sustained CPU about 296% against a fleet median of about 3%, memory at 93%. Cause not confirmed; no user-visible errors yet. | `DIAGNOSED` (low) | — | 5 / 12 / 52.8 s | `TODO (Member 5 – ground truth kept outside repo)` |
| PRACTICE-7 | 23 Oct 12:23 | Same | `ad`: CPU about 300%, memory at 92%, sustained for at least 30 minutes; small user-facing impact. Unexamined anomaly: prometheus. | `DIAGNOSED` (low) | — | 6 / 12 / 44.6 s | `TODO (Member 5 – ground truth kept outside repo)` |
| PRACTICE-8 | 23 Oct 12:29 | Same | `ad`: CPU about 288%, memory at 93%. Intermittent GetAds gRPC UNAVAILABLE becomes frontend `/api/data` 500s; ad error rate about 4%. | `DIAGNOSED` (medium) | — | 5 / 11 / 59.3 s | `TODO (Member 5 – ground truth kept outside repo)` |
| LIVE-STACKDOWN-1 | 24 Oct 04:54 | Shop checkout and browsing failing; users report errors | **Abstained.** Every source was empty for 60 minutes, and the agent couldn't tell a stack outage from a broken telemetry pipeline. | `INCONCLUSIVE` | `ESCALATED` (dry-run) | 3 / 7 / 10.0 s | Per `incident-reports/README.md`: app containers removed and telemetry silent for over 60 minutes. **Correct abstention** (to confirm). |
| LIVE-A-HEALTHY-1 | 24 Oct 05:21 | Routine check: the shop may be slow; verify health | **Abstained.** All telemetry was empty: fleet overview, resources, and frontend traces, logs and health. | `INCONCLUSIVE` | `ESCALATED` (dry-run) | 3 / 6 / 7.9 s | Per `incident-reports/README.md`: stack started without the observability layer, so the collector exported to `debug` only. **Correct abstention** (to confirm). |
| LIVE-1-BASELINE | 24 Oct 05:50 | Load generator reports about 20% of requests failing; find the cause | `load-generator` (low). Its `user_ask_agent` task POSTs to `http://agent:8010`, which doesn't resolve; a separate ~5% `ad` GetAds UNAVAILABLE. Unexamined anomalies: opensearch, astronomy-db. | `ESCALATED` | `ESCALATED` (dry-run, low confidence) | 6 / 12 / 28.9 s | Per `incident-reports/README.md`: no injected fault; the optional `agent` service isn't part of `make start`. **Plausible; not acting was correct** (to confirm). |
| LIVE-2-PAYMENT | 24 Oct 05:54 | Users report checkout failing; orders are not completing | `payment` (high). The container exited; checkout's Charge calls fail with a DNS lookup error for `payment`, and the frontend returns 500 on `/api/checkout`. Proposed `restart_service payment {grace_period_seconds: 10}`, which **passed policy**. | `REJECTED` | `ESCALATED`: approval auto-rejected in a non-interactive session, nothing ran | 5 / 9 / 17.9 s | Per `incident-reports/README.md`: `docker stop payment`. **Diagnosis correct** (to confirm). |
| [DEMO-PAY-4](../incident-reports/DEMO-PAY-4/DEMO-PAY-4_report.md) | 24 Oct 06:20 | Users report checkout failing | `payment` (high). The container exited; checkout's Charge calls fail with "no route to host", so PlaceOrder fails. Proposed `restart_service payment`, which **passed policy**. **Live mode:** the server opened an approval and printed the code. | `REJECTED` | `ESCALATED`: the human's answer was rejected, nothing ran | 6 / 12 / 22.5 s | Per the integrating session: `payment` still stopped from LIVE-2-PAYMENT. **Diagnosis correct** (to confirm). |

**CP2 tally** (correct diagnoses / mentor-selected incidents tested): `TODO (team to fill)`. No report states that
its incident was mentor-selected. SMOKE-001 explicitly says it was not.

PRACTICE-5 to PRACTICE-8 all name `ad`. The repository doesn't record whether one injected fault stayed active
across these runs or there were several, so don't count them as independent successes until the ground truth is
checked.

### Other entries in `runs/`

| Entry | What it contains |
| --- | --- |
| `cp0_check.txt` (23 Oct 10:44 UTC) | Prometheus, Jaeger and OpenSearch answered `HTTP 200 OK`; 18 services seen; one `ok` capture each from `overview`, `query_logs`, `search_traces` and `query_metrics` in `cp0_evidence/`. Local CP0 evidence; mentor sign-off is `TODO (team to fill)`. |
| `check_ad/`, `check_ad2/` | Ad-hoc tool captures (`query_metrics`, `search_traces`) with no report. Not counted as runs. |
| `audit.jsonl` | Five `finished` events, all `ESCALATED` in dry-run: LIVE-STACKDOWN-1 at 04:53:59 and 04:54:46, LIVE-A-HEALTHY-1, LIVE-1-BASELINE and LIVE-2-PAYMENT. LIVE-2-PAYMENT also has one `approval` event, `rejected` with approver "none (non-interactive session)". The first LIVE-STACKDOWN-1 run reused the incident ID, so only the second run's report remains. |
| `action_api/audit.jsonl` | A `validated` event for **LIVE-PLAN-1** at 05:21:34: `restart_service` on `payment`, a dry-run `POST /v1/actions/validate` only, with no report. Then `approval_created` for DEMO-PAY-4 at 06:20:23; no `action_accepted` follows. |

## 3. Human-approved live remediation: not yet done

DEMO-PAY-4 went furthest. It ran in live mode with the server approval flow:
- The diagnosis was correct, and the `restart_service payment` proposal passed policy.
- The server recorded `approval_created` and printed the one-time code on its console.
- The human's answer was rejected and nothing ran. The integrating session reports the operator typed `APPROVE`
  without the code. The typed answer is deliberately not logged.
- The prompt was then changed: an answer missing the code now gets a hint and a re-prompt (up to 3 attempts within
  the TTL) instead of an immediate rejection.

Earlier, LIVE-2-PAYMENT reached the same point in a non-interactive shell, which rejects automatically. No report
contains a verification, and neither audit log contains `dispatch_intent` or `action_accepted`. The end-to-end flow
(scenario B in
[`END_TO_END_DEMO.md`](../incident-agent/docs/END_TO_END_DEMO.md): practice fault, diagnosis, approval code typed by
a human, `docker` executor, measured recovery) is **not demonstrated yet**.

| Item | Status |
| --- | --- |
| Live run with `--respond --action-api --execute` and the `docker` executor | `TODO (team to fill)` |
| Outcome (`VERIFIED_RECOVERY` or other) | `TODO (team to fill)` |
| Kill-switch demonstration on the live stack | `TODO (team to fill)`. Covered by mocked tests only. |

## 4. Failures, explained

1. **SMOKE-001: an unsupported component.** The model's answer amounted to "no clear service fault", put in the
   component field. The validator requires the suspected component to appear in the cited evidence, so it rejected
   the answer, and the run ended `INCONCLUSIVE` instead of reporting a non-diagnosis as a diagnosis. A side effect:
   "nothing is wrong" can only be expressed as inconclusive.
2. **PRACTICE-1: the component check was too strict.**
   - The validator matched the whole phrase "product-catalog and cart containers (…)" and rejected it.
   - Commit `6ec02b46` ("flexible component check", 17:47 IST, 17 minutes after this run) relaxed the check. It now
     matches service-like words inside a longer phrase.
   - PRACTICE-1 was not re-run.
3. **PRACTICE-3 and PRACTICE-4: model API errors.**
   - PRACTICE-4 recorded an Anthropic 400 because the request set `tool_choice`.
   - PRACTICE-3 recorded only `BadRequestError`, so its cause isn't proven to be the same.
   - Both runs escalated instead of guessing.
   - The reasoner no longer sends `tool_choice` (`reasoning.py`: the last turn offers only terminal tools).
     PRACTICE-5 to PRACTICE-8, run minutes later, show no model error.
4. **PRACTICE-5 and PRACTICE-7: unexamined anomalies.**
   - The anomaly gate deferred the finish once. The model then finished without examining some flagged containers,
     so the reports list them as limitations.
   - PRACTICE-5's diagnosis also doesn't link `ad` to the user-facing symptom.
   - Both are `low` confidence, which the planner never acts on.
5. **LIVE-STACKDOWN-1 and LIVE-A-HEALTHY-1: abstained correctly while telemetry was missing.**
   - Every source returned `empty`, so the agent made no diagnosis and planned no action. This is the intended
     behaviour.
   - According to `incident-reports/README.md`, in LIVE-A-HEALTHY-1 the stack had been started without the
     observability layer: a telemetry-pipeline outage.
   - By the same source, in LIVE-STACKDOWN-1 the application containers had been removed for more than 60 minutes.
     That is beyond the 60-minute silence lookback, so it looked the same as a pipeline outage.
   - The model said, correctly, that it couldn't tell the two cases apart from telemetry.
   - Neither run called `container_state`, which reads Docker state independently of the pipeline. LIVE-2-PAYMENT
     shows that tool separating "container exited" from "no telemetry".
   - LIVE-A-HEALTHY-1 therefore **does not** demonstrate scenario A (a healthy system). It shows abstention during
     an outage.
6. **LIVE-1-BASELINE: a real environmental fault, not an incident.** The load generator's failures come from a
   task that calls an optional service `make start` doesn't run. The agent found it but labelled it `low`, so
   nothing was planned. It also left two flagged containers (opensearch, astronomy-db) unexamined within the 12-call
   budget.

## 5. Defects found in hardening (24 Oct)

1. **CPU under-reported.** The live measurements for `ad` were:

   | Source | `ad` CPU |
   | --- | --- |
   | `docker stats` | 404.7% |
   | `rate(container_cpu_usage_nanoseconds_total)` | 399.8% |
   | `container_cpu_utilization_ratio` | 47.5% |

   So the ratio we used is a lifetime average in this setup. Below the 100% flag threshold, a CPU spike was missed.
   Fixed by switching to the CPU-time rate. Neha found this first on `feature/neha2`.
2. **Restart proposed for faults it cannot fix.** A restart is now refused without process-level evidence about the
   target, so application or flag-driven errors escalate.
3. **Approval prompt.** DEMO-PAY-4: the operator typed `APPROVE` without the code. The prompt now explains where the
   code is and asks again, up to 3 times within the TTL.

## 6. Limits of this evaluation

- Confidence labels are model-reported and **not calibrated**. Fourteen runs are too few to measure accuracy.
- Telemetry tools are live-only, so a run can't look at a past incident window. Windows are at most 60 minutes.
  `container_state` always reads the present state, so the reports flag it as queried outside the incident window.
- Container silence is detected only after 150 s without samples, and within a 60-minute lookback. A container that
  has been down for longer drops out instead of being flagged.
- After the 24 October `make start`, the integrating session saw no `container_uptime` series from `docker_stats`
  for `cart`, `product-catalog`, `accounting` and `fraud-detection`, although they ran normally. Uptime and silence
  checks can't judge those containers from metrics; `container_state` still sees them.
- New reports are validated against the team-defined schema
  [`incident-agent/schemas/incident_report.schema.json`](../incident-agent/schemas/incident_report.schema.json).
  LIVE-1-BASELINE, LIVE-2-PAYMENT and DEMO-PAY-4 carry `schema_version` `team6.incident-report/1.0`. LIVE-STACKDOWN-1,
  LIVE-A-HEALTHY-1, SMOKE-001 and PRACTICE-1 to PRACTICE-8 were written before the schema was added, and weren't
  checked against it.
