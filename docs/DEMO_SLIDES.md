# Incident Zero: Team 6 demo slides (4 slides)

Slide contents for the 5-minute demo. **Live** results (real stack, real Claude, real Docker) are kept separate from
**mocked or in-process tests**. Update slide 4 only from observed runs (`incident-reports/`, audit logs).

---

## Slide 1: From alert to verified recovery, with a human in the loop

**Subtitle:** The LLM only reads and proposes. Deterministic code and a human decide, and one separate process can act.

**Flow** (draw as 7 boxes with arrows):

| # | Step | What happens |
| --- | --- | --- |
| 1 | **Alert** | The watcher (`cli.py watch`) polls Prometheus. A breach holding for 2 checks opens an incident by itself. The CLI can also start one by hand. |
| 2 | **Investigate** | Claude, in a bounded loop (12 tool calls, 6 model calls, 180 s), calls 7 **read-only** tools. |
| 3 | **Diagnose** | Deterministic code validates the diagnosis against the evidence it cites. |
| 4 | **Propose** | One Action API operation, or an escalation. |
| 5 | **Approve** | Grafana links to the approval page, which shows the exact Action API JSON. The human enters the one-time code shown only on the Action API terminal. |
| 6 | **Act** | The team Action API runs `docker restart` on an allowlisted container. |
| 7 | **Verify** | Fresh telemetry is compared with the pre-approval baseline, then a report and Grafana pages are written. |

**Three lanes underneath:**
- **Read-only tools:**
  - `overview` and `resource_overview`;
  - `query_metrics`, `service_health`, `search_traces` and `query_logs`;
  - `container_state`.

  The data comes from Prometheus, Jaeger, OpenSearch and Docker state.
- **Team Action API, a separate process:**
  - bearer authentication;
  - an allowlisted `restart_service`;
  - approval bound to the payload digest, usable once;
  - idempotency keys;
  - a kill switch;
  - an audit log.
- **Grafana:**
  - an index and a root-cause and verification page per incident;
  - Explore links to the exact metrics, traces and logs;
  - the Action API request JSON.

*Speaker note:* the Action API is team-defined, because no organiser contract was supplied.

---

## Slide 2: Every claim must cite evidence the agent actually captured

**Rules, enforced by code rather than the model:**
1. **Cited IDs must exist.** Every tool result is saved with an evidence ID. Invented or failed results cannot
   support a cause.
2. **The component must appear in its evidence.** An unsupported component gets one bounded correction, and then the
   agent abstains.
3. **High confidence needs two independent sources.** Confidence labels are estimates, not proof (not calibrated).
4. **Separate cause from symptom.** Follow trace error chains to the deepest failing service. Container state tells
   "stopped" apart from "erroring".
5. **Missing data is not evidence.** Silent or empty telemetry is reported as such. Flag state is never read.

**Live example card: LIVE-2-PAYMENT** (operator ran `docker stop payment`)
- **Diagnosis:** `payment`, high confidence. The container exited; checkout's Charge calls fail to resolve `payment`,
  so orders do not complete.
- **Evidence:**
  - `container_state`: payment exited, unhealthy
  - `resource_overview`: payment stopped reporting
  - `search_traces` (checkout): DNS lookup for payment fails
  - `overview`: checkout and frontend errors
- **Verdict:** correct, from three sources.

*Speaker note:* the same pipeline abstained correctly when all telemetry was empty (LIVE-A-HEALTHY-1,
LIVE-STACKDOWN-1). It did not confuse missing data with a fault.

---

## Slide 3: The agent proposes, a human approves, a separate process acts

**Controls:**
- **Allowlist and strict schema:**
  - one operation, `restart_service`, with 14 app and worker targets;
  - flagd, flagd-ui, the load generator, the collector and observability containers are hard-denied.
- **A restart needs process-level evidence** about the target:
  - the container is stopped, unhealthy or restarting;
  - it stopped reporting;
  - it shows a CPU or memory anomaly;
  - callers get refused connections or failed name resolution.

  Otherwise the agent escalates. Flag, configuration, latency and application faults are not fixed by a restart.
- **Out-of-band approval:**
  - the Action API prints a one-time code bound to the SHA-256 of the exact payload;
  - it is used once and expires (120–300 s);
  - it locks after 5 bad codes.

  The agent and the LLM never see it.
- **Kill switch and dry-run by default:**
  - the kill switch is checked by the agent and again by the server;
  - one action per incident;
  - an uncertain outcome is never retried.
- **Recovery is measured:**
  - API success is not recovery;
  - each check is compared with the pre-approval baseline;
  - a simulated action can never verify.

**Action API request** (code card; team-defined schema `schemas/action_api/ActionPayload.schema.json`):
```json
{
  "incident_id": "DEMO-PAY-4",
  "operation": "restart_service",
  "target": "payment",
  "arguments": {"grace_period_seconds": 10},
  "evidence_ids": ["ev-container_state-367a03e6", "ev-resource_overview-7e23fb72", "..."],
  "reason": "payment exited/unhealthy ...",
  "proposal_digest": "defa1aea..."
}
```

---

## Slide 4: Evaluation, live outcomes kept separate from mocked tests

**LIVE** (real stack, real Claude):

| Run | Ground truth | Outcome |
| --- | --- | --- |
| LIVE-2-PAYMENT | `docker stop payment` | `payment` exited, **high** confidence. A restart was proposed, but no approval was given in a non-interactive shell, so nothing ran. |
| DEMO-PAY-4 | `payment` still stopped | Correct diagnosis. The operator typed `APPROVE` without the code, so the **gate refused** and nothing ran. |
| LIVE-1-BASELINE | no injected fault | The load generator calls a missing optional service. Low confidence, so **no action**. |
| LIVE-A-HEALTHY-1 | collector exporting to debug only | **Abstained**: a pipeline outage and a stack outage could not be told apart. |
| LIVE-STACKDOWN-1 | app containers removed | **Abstained**: all telemetry was silent. |

**CP3, approved restart and recovery: NOT YET SHOWN.** The human-approved run (a real `docker restart`, measured
recovery, the action log) is scheduled. *Replace this box only after the run has been observed*, with the outcome,
the action ID and the verification checks.

**MOCKED / IN-PROCESS: 229 tests pass.**
- Scenarios A–J run over a real local HTTP Action API, with a fake Docker runner and scripted telemetry and LLM.
- Also covered: approval replay, expiry and lockout, the kill switch, prompt injection, and terminal-escape injection.
- These tests prove the control logic, **not** live recovery or diagnosis quality on unseen faults.

**Next / future scope:**
- CP3 approved run.
- A flagd fault, diagnosed and escalated because a restart cannot fix it.
- A flag-toggle operation with safeguards.
- Calibrated confidence.
- Incident memory and a reviewer agent.
- OS-level privilege separation.
