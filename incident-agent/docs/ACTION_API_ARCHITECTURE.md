# Team Action API: architecture and security model

> **Team-defined API.** Team 6 designed this API because no organiser Action API contract was supplied. It is
> **not** an organiser contract and makes no claim to satisfy an organiser-specific requirement. If an official API
> appears, the agent can target it through `actions.HttpActionClient` and a catalog file (see `../ACTION_API.md`).

Machine-readable contract:

- OpenAPI 3.1: [`action_api.openapi.json`](action_api.openapi.json)
- JSON Schemas (2020-12): [`../schemas/action_api/`](../schemas/action_api/)

Both are generated from the Pydantic models in `action_api_models.py` (`python cli.py action-api export-schemas`), and
`tests/test_action_api.py::test_schema_files_and_openapi_match_models` fails if they drift.

## 1. Architecture

```mermaid
flowchart LR
    subgraph Agent process (no Docker access)
      LLM[Claude: read-only tools + proposal-only planner]
      ORCH[orchestrator / response.py<br/>policy, baseline, kill switch, verification]
      CL[action_api_client.py]
      LLM -->|tool calls, proposal| ORCH
      ORCH --> CL
    end
    H[On-call human] -->|types 'approve CODE'| ORCH
    subgraph Action API process  (python cli.py action-api serve)
      HTTP[action_server.py<br/>auth, schema, allowlist, approvals,<br/>idempotency, kill switch, audit]
      EX[action_executor.py<br/>fixed argv: docker inspect / docker restart]
      HTTP --> EX
    end
    CL -->|HTTP + bearer token, 127.0.0.1| HTTP
    HTTP -->|one-time approval code| CON[Action API console]
    CON -->|human reads code| H
    EX -->|docker restart --time N container| D[(allowlisted app containers)]
    ORCH -->|fresh PromQL, traces, logs| T[(Prometheus / Jaeger / OpenSearch)]
```

| Component | File | May do | May never do |
| --- | --- | --- | --- |
| LLM investigator | `reasoning.py` | Call read-only telemetry tools | Call the Action API, approve, run commands |
| LLM planner | `remediation.py` | Return one `propose_action` or `escalate` | Execute or approve anything |
| Agent policy and flow | `safety.py`, `response.py` | Validate the proposal, ask the human, call the API client | Access Docker; know the approval code |
| API client | `action_api_client.py` | HTTP to the Action API | Bypass server checks |
| Action API server | `action_server.py` | Authenticate, validate, issue and verify approvals, schedule execution, audit | Run anything not in the allowlist |
| Executor | `action_executor.py` | `docker inspect` and `docker restart --time N <container>` | Use a shell, take arbitrary args, touch hard-denied containers |

`action_executor.py` is imported only by `action_server.py`. The agent process never imports it.

## 2. Endpoints

All `/v1/*` endpoints require `Authorization: Bearer $ACTION_API_TOKEN`. Bodies must be `application/json`, at most
16 KB, and are validated strictly (unknown fields are rejected).

| Method and path | Purpose | Success |
| --- | --- | --- |
| `GET /health` (no auth) | Liveness, executor mode, kill-switch state | 200 `Health` |
| `GET /v1/operations` | Allowlisted operations, argument schemas, targets | 200 `OperationList` |
| `GET /v1/services` | **Read-only system state:** container state/health/restart count/uptime for app services, workers, cache, DB, collector and load generator (+ Locust users, RPS, fail ratio). Never feature flags. Also exposed to the investigator as the `container_state` tool. | 200 `SystemState` |
| `POST /v1/actions/validate` | Stateless dry-run validation of an `ActionPayload` | 200 `ValidateResponse` |
| `POST /v1/approvals` | Open an approval bound to the payload digest; print a one-time code on the console | 201 `Approval` |
| `GET /v1/approvals/{approval_id}` | Approval status (never the code) | 200 `Approval` |
| `POST /v1/approvals/{approval_id}/reject` | Record a human rejection | 200 `Approval` |
| `POST /v1/actions/execute` | Execute an approved action asynchronously | 202 `ActionRecord` (200 on idempotent replay) |
| `GET /v1/actions` | Recent action records | 200 `ActionList` |
| `GET /v1/actions/{action_id}` | Full record (payload, approval reference, result, verification) | 200 `ActionRecord` |
| `GET /v1/actions/{action_id}/result` | Polling view: status, terminal flag, result | 200 `ActionResultResponse` |
| `POST /v1/actions/{action_id}/verification` | Attach the agent's recovery verification (once) | 200 `ActionRecord` |

### Error codes (`{"error": {"code", "message", "details"}}`)

| HTTP | Code | Meaning |
| --- | --- | --- |
| 400 | `INVALID_REQUEST`, `UNKNOWN_OPERATION` | Schema violation (including any extra field such as `approved`); the operation is not allowlisted |
| 401 | `UNAUTHORIZED` | Missing or wrong bearer token |
| 403 | `TARGET_NOT_ALLOWED`, `APPROVAL_NOT_FOUND`, `APPROVAL_EXPIRED`, `APPROVAL_REJECTED`, `APPROVAL_LOCKED`, `APPROVAL_CODE_INVALID`, `PAYLOAD_MISMATCH` | Authorization failures; nothing runs |
| 404 | `NOT_FOUND` | Unknown path or ID |
| 409 | `APPROVAL_CONSUMED`, `IDEMPOTENCY_CONFLICT`, `INCIDENT_ACTION_EXISTS`, `VERIFICATION_EXISTS` | Replay or duplicate protection |
| 413 / 415 | `PAYLOAD_TOO_LARGE` / `UNSUPPORTED_MEDIA_TYPE` | Transport limits |
| 422 | `INVALID_ARGUMENTS` | The arguments violate the operation schema |
| 423 | `KILL_SWITCH_ACTIVE` | The kill switch blocks every execution |
| 500 | `INTERNAL_ERROR` | No internals are leaked; the client treats this as an **unknown** outcome |

### One endpoint per service?

No. Each action is the generic `POST /v1/actions/execute` with a validated `target`, checked against the allowlist.
State is the single read-only `GET /v1/services`. One endpoint per service would duplicate the same checks 14 times
and widen the attack surface. **Scale** (on the slide) is Kubernetes-only and does not apply to Docker Compose.

## 3. Supported operation

Exactly one operation is supported. It is the only one that is safe, genuinely useful and supported by the local
environment.

| Operation | Arguments | Targets | Executor |
| --- | --- | --- | --- |
| `restart_service` | `grace_period_seconds`: integer 0–30, default 10 | `ad`, `cart`, `checkout`, `currency`, `email`, `frontend`, `image-provider`, `payment`, `product-catalog`, `quote`, `recommendation`, `shipping`, and the workers `accounting` and `fraud-detection` | `docker restart --time N <container>` |

- **Optional targets, off by default:** `valkey-cart`, `astronomy-db`, `kafka` and `frontend-proxy`. Enable them
  only with `ACTION_API_EXTRA_TARGETS=valkey-cart,...`. Restarting a stateful store can lose data.
- **Hard-denied, cannot be enabled:** `flagd`, `flagd-ui`, `load-generator`, `otel-collector`, `jaeger`,
  `prometheus`, `grafana`, `opensearch`, `opamp-server`, `telemetry-docs`, `agent`, `mcp`, `chatbot` and the
  profiling containers.

**Deliberately not implemented:**

- Load-generator throttling: it changes organiser traffic and the evaluation.
- Feature-flag edits: flag state is out of bounds.
- Scaling, config edits and arbitrary Docker arguments.

**Executor modes** (`ACTION_API_EXECUTOR`, default `simulate`):

- `simulate` changes nothing, and every result says `SIMULATED`. The agent never reports recovery for a simulated
  action; the best possible outcome is `UNVERIFIED`.
- `docker` first runs `docker inspect` and requires the container labels
  `com.docker.compose.service == target` and `com.docker.compose.project == ACTION_API_COMPOSE_PROJECT`
  (default `opentelemetry-demo-team_6`). It then runs the fixed argv
  `["docker", "restart", "--time", "<0-30>", "<target>"]` without a shell, inspects again, and reports
  `succeeded` only if the container is `running`.

## 4. Security model

| Threat | Control |
| --- | --- |
| The LLM executes something | The LLM has no Action API tool. The planner returns data that deterministic code validates. |
| The LLM or agent approves its own action | The **server** creates the approval and prints the 8-character code **only on its own console**. The code is never returned by any endpoint, and only its SHA-256 hash is stored. The agent learns it only when a human types `approve <CODE>`. |
| A client claims `approved: true` | `extra="forbid"` on every request model returns 400. Authorization is the server-verified code alone. |
| Approval reused for another action | The approval is bound to the SHA-256 of the canonical `ActionPayload` (incident, operation, target, arguments, evidence IDs, reason, proposal digest). Any difference returns `PAYLOAD_MISMATCH`. |
| Replay | Approvals are single use (`consumed`, persisted across restarts). Each incident allows one action (`INCIDENT_ACTION_EXISTS`). Reusing an idempotency key returns the original record and never executes twice; reusing it with a different payload returns `IDEMPOTENCY_CONFLICT`. |
| Brute-forcing the code | 31^8 possible codes, constant-time comparison, locked after 5 failures (`APPROVAL_LOCKED`). |
| Stale approval | TTL of 30–300s (default 120). Expired approvals are refused, and the agent also enforces its own prompt TTL. |
| Emergency stop | The kill switch (`KILL_SWITCH` file or `INCIDENT_AGENT_KILL_SWITCH=1`) is checked by the agent before approval and before dispatch, and by the **server** at `execute` and again in the worker, where the record becomes `blocked`. |
| Unauthorized local callers | Bearer token of at least 24 characters, constant-time comparison. The server binds to `127.0.0.1` and refuses other hosts without `--bind-all`. |
| Secret leakage | The request log prints only method, path and status (headers are never logged). Tokens and codes never appear in state, audit or reports; tests assert this. |
| Misleading the approver with terminal escapes | Model-written free text (`reason`, `risks`, …) must not contain control characters (rejected by schema, `pattern` in the JSON Schema). All displayed free text is escaped again at print time. Both consoles print the **authoritative action block last**, so earlier text cannot redraw it. Found by the security review; regression-tested. |
| Crash during execution | State is written atomically (`os.replace`). On restart, `accepted` or `running` records become `unknown` and are never resumed. The client never retries an unknown outcome. |
| Arbitrary container access | Allowlist, hard-deny list, compose-label identity check, fixed argv, bounded grace period. |

**Limitation:** both processes run as the same OS user on the demo laptop. The privilege separation is architectural
(separate process, the only importer of the executor, no LLM path), **not OS-enforced**. A production deployment would
run the Action API under a dedicated user or behind a Docker socket proxy that permits only `restart` on labelled
containers.

The server checks that evidence IDs are well formed. It cannot check that they exist; the agent's policy does that
(`safety.evaluate`), together with checks that the target is the diagnosed component and that the confidence is not
low.

## 5. Human approval flow

1. The agent's policy accepts the proposal and the baseline is captured. Both the agent and the server check the kill
   switch.
2. The agent calls `POST /v1/approvals` with the exact payload. The server validates it and prints this to **its
   console**:
   ```text
   APPROVAL REQUEST apr-3f0c9a1b2c3d4e5f  incident INC-PAY-1
     action:   restart_service target=payment arguments={'grace_period_seconds': 10}
     ONE-TIME APPROVAL CODE: K7MQ-9XD2
   ```
3. The agent terminal shows the full summary: incident, diagnosis, evidence, request, risks, rollback, verification
   criteria and baseline.
4. The human compares the two screens. To approve, they type `approve K7MQ-9XD2` in the agent terminal. Anything else,
   a timeout, or a non-interactive session rejects, and the agent records the rejection with
   `POST /v1/approvals/{id}/reject`.
5. The server verifies the code, the payload digest, expiry, single use and the kill switch at `execute`.

Dry-run (the default) never opens a server approval. It uses the agent's local digest code and calls
`POST /v1/actions/validate` to show that the server would accept the payload.

## 6. Execution flow

`execute` returns `202` with `action_id` and status `accepted`. A worker thread then:

1. Re-checks the kill switch (a block makes the record `blocked`).
2. Sets the record to `running`.
3. Calls the executor.
4. Sets a terminal status: `succeeded`, `failed` or `simulated`.

The client polls `GET /v1/actions/{id}/result` for up to 120s. A timeout or lost contact is an **unknown** outcome:
recorded, never retried, and verified from telemetry only.

## 7. Verification (agent side, `verify.py`)

1. A baseline is captured **before** approval, using the checks from the proposal plus defaults: the affected
   service's `error_rate` and the target's `container_last_seen_age_s` (availability).
2. After dispatch, the agent waits `--settle-s` (default 120s, so `rate()` windows cover post-action time only), then
   takes `--samples` samples (default 2) `--interval-s` apart.
3. Each check is judged on its own:
   - A check recovers only if it is within its bound in **every** sample.
   - Error and latency checks need ≥ 0.05 req/s of traffic, otherwise the result is `no_data`.
   - Checks that were already healthy at baseline only guard against regression.
4. Unjudged observations go into the report: p95 latency, error-trace count and error-log count for the target and
   the affected service, before and after.
5. Outcome:

| Outcome | Condition |
| --- | --- |
| `VERIFIED_RECOVERY` | Every symptomatic check recovered and the executor was not simulated |
| `PARTIAL_RECOVERY` | Some checks recovered or improved |
| `NOT_RECOVERED` | No improvement |
| `UNVERIFIED` | Missing data or insufficient traffic, or a simulated executor |
| `ACTION_FAILED` | The executor reported failure |
| `ESCALATED` | Refused or unavailable API, rejection, kill switch, no safe action |
| `DRY_RUN` | Nothing sent |

The result is posted to `POST /v1/actions/{id}/verification`, so the server record holds the full story. An HTTP 2xx
from the API is never treated as recovery.

## 8. Failure handling

| Failure | Behaviour |
| --- | --- |
| API down when opening the approval | `ESCALATED` ("Action API unavailable"); no approval and no action |
| API down at execute (connect error) | Refused, request not sent, `ESCALATED` |
| Timeout after sending, 5xx, or lost polling contact | `unknown` outcome, never retried, verification from telemetry, `UNVERIFIED` or measured outcome with a note |
| Wrong code, expired, rejected, locked, payload mismatch | Server refuses, nothing runs, `ESCALATED` |
| Container missing or identity mismatch | `failed`, then `ACTION_FAILED` |
| Kill switch between approval and execution | Agent refuses dispatch, or the server returns 423, or the worker sets `blocked` |
| Executor exception | `failed` with the exception type only |
| Server restart mid-action | The record becomes `unknown`; consumed approvals stay consumed |

## 9. Request and response examples

```http
POST /v1/approvals
Authorization: Bearer <token>
Content-Type: application/json

{"payload": {"incident_id": "INC-PAY-1", "operation": "restart_service", "target": "payment",
             "arguments": {"grace_period_seconds": 10},
             "evidence_ids": ["ev-resource_overview-1a2b3c4d", "ev-search_traces-5e6f7a8b"],
             "reason": "payment stopped reporting; checkout Charge calls get connection refused",
             "proposal_digest": "<64 hex>"},
 "requested_by": "alice", "ttl_seconds": 120}
```

```json
201 {"approval_id": "apr-3f0c9a1b2c3d4e5f", "status": "pending", "incident_id": "INC-PAY-1",
     "operation": "restart_service", "target": "payment", "payload_digest": "0d469df9...",
     "requested_by": "alice", "created_at": "2026-10-24T05:30:00Z", "expires_at": "2026-10-24T05:32:00Z",
     "failed_attempts": 0, "code_delivery": "action-api-console"}
```

```http
POST /v1/actions/execute
{"payload": { ...identical payload... }, "approval_id": "apr-3f0c9a1b2c3d4e5f",
 "approval_code": "K7MQ-9XD2", "idempotency_key": "INC-PAY-1-0d469df92341282c"}
```

```json
202 {"action_id": "act-9c8b7a6d5e4f3a2b", "incident_id": "INC-PAY-1", "operation": "restart_service",
     "target": "payment", "arguments": {"grace_period_seconds": 10}, "evidence_ids": ["..."], "reason": "...",
     "payload_digest": "0d469df9...", "proposal_digest": "...", "idempotency_key": "INC-PAY-1-0d469df92341282c",
     "status": "accepted", "executor": "docker", "created_at": "...", "started_at": null, "finished_at": null,
     "result": null, "verification": null,
     "audit": {"approved_by_code_holder": true, "approval_id": "apr-3f0c9a1b2c3d4e5f", "requested_by": "alice",
               "remote_addr": "127.0.0.1"}}
```

```json
GET /v1/actions/act-9c8b7a6d5e4f3a2b/result
200 {"action_id": "act-9c8b7a6d5e4f3a2b", "status": "succeeded", "terminal": true, "finished_at": "...",
     "result": {"success": true, "simulated": false, "detail": "container restarted and running",
                "status_before": "exited", "status_after": "running", "exit_code": 0, "duration_s": 11.4}}
```

```json
403 {"error": {"code": "APPROVAL_CODE_INVALID", "message": "invalid approval code", "details": []}}
423 {"error": {"code": "KILL_SWITCH_ACTIVE", "message": "kill switch is active: no state-changing actions", "details": []}}
```

## 10. Audit trail

| Log | Location | Events |
| --- | --- | --- |
| Server log | `runs/action_api/audit.jsonl` | `validated`, `approval_created`, `approval_rejected`, `refused_approval`, `refused_kill_switch`, `action_accepted`, `action_finished`, `idempotent_replay`, `verification_recorded`, `action_marked_unknown_after_restart` |
| Agent journal | `runs/audit.jsonl` | `policy_refused`, `approval_opened`, `approval`, `dispatch_intent` (written **before** the request), `dispatch_result`, `dispatch_refused`, `blocked_kill_switch`, `finished` |
| Incident report | `runs/<id>/<id>_report.{md,json}` | Links the two: `approval.request_id` holds the server `action_id` |

## 11. Limitations compared with an organiser API

- The API is team-defined and has not been reviewed or endorsed by the organisers. If an official API is supplied,
  use it.
- There is one operation (restart). Faults caused by code, configuration, feature flags, network or load are not
  fixable through it; the planner must escalate.
- Privilege separation is by process, not by OS user. The bearer token is static per session.
- State lives in a local JSON file (single node, no HA). Polling-based results; there are no callbacks.
- The default demo uses the simulate executor. Real restarts require `--executor docker` and a human approval.
