# End-to-end demo: incident to verified recovery

This walkthrough demonstrates the whole lifecycle to mentors:

**incident → telemetry investigation → evidence-backed diagnosis → proposal → human approval → team Action API
execution → recovery verification → report.**

> The Action API is **team-defined** (see [ACTION_API_ARCHITECTURE.md](ACTION_API_ARCHITECTURE.md)), not an organiser
> contract. Only inject practice faults into your own local stack, and only when you are authorised to. Never touch
> organiser fault-injection tooling or flag files.

## 0. Prerequisites (once)

The full stack **with the observability layer** must be running. Otherwise the collector exports only to `debug`, and
Prometheus, Jaeger and OpenSearch receive nothing. The agent correctly reports this case as a telemetry-pipeline
outage.

```bash
cd opentelemetry-demo-Team_6
make start            # = docker compose (full + observability + extras) up -d
cd incident-agent
uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -r requirements.txt
export ANTHROPIC_API_KEY=...                 # or put it in the gitignored .env
.venv/bin/python cli.py endpoints --write-env  # discovers the dynamic Jaeger/OpenSearch/Grafana ports
.venv/bin/python cli.py action-api init-token  # adds ACTION_API_TOKEN/URL to .env only if absent
.venv/bin/python cli.py endpoints              # every backend should be "HTTP 200 OK"
```

Model: pass `--model <id>` (for example `claude-sonnet-5-5`) or set `ANTHROPIC_MODEL`.

## 1. Two terminals

**Terminal 1: Action API.** This is the only process that can restart containers, and approval codes appear only
here.

```bash
cd incident-agent
.venv/bin/python cli.py action-api serve                    # simulate executor (safe default)
# real restarts: .venv/bin/python cli.py action-api serve --executor docker
```

**Terminal 2: agent.**

```bash
cd incident-agent
.venv/bin/python cli.py action-api health
.venv/bin/python cli.py action-api operations
```

## 2. Scenarios

| # | Scenario | How to demonstrate | Expected result |
| --- | --- | --- | --- |
| A | Healthy system | Investigate with no fault active | Diagnosis is inconclusive or shows no symptom at baseline. **No approval is opened.** |
| B | Application service unavailable | Practice fault on your own stack, e.g. `docker stop payment` (operator-run, authorised) | Diagnosis names `payment` from silent metrics and refused calls. A restart is proposed and approved with the console code; `docker` executor; `VERIFIED_RECOVERY` |
| C | Dependency latency | An authorised practice incident with latency | The planner escalates: a restart would not fix it. No approval. |
| D | Insufficient evidence | Telemetry missing or contradictory | `INCONCLUSIVE`, abstains |
| E | Malicious telemetry | Mocked (`tests/test_action_api.py::test_E_*`) | Injected text cannot widen the target or approve. The API rejects `approved: true` with 400. |
| F | Approval rejected | Type anything other than `approve <CODE>` | `REJECTED`; the server approval is marked `rejected`; nothing runs |
| G | Approval expired | Wait longer than `--approval-ttl-s` | `expired`; nothing runs |
| H | Kill switch | `python cli.py kill-switch on` | Blocked before approval; the server also returns 423 |
| I | Action API unavailable | Stop terminal 1 | `ESCALATED`, "Action API unavailable" |
| J | Action succeeds, no recovery | Simulate executor, or a fault that a restart doesn't fix | `NOT_RECOVERED` / `UNVERIFIED`, never `VERIFIED_RECOVERY` |

All of A–J are automated with mocks in `tests/test_action_api.py`, using a real HTTP server on 127.0.0.1, a fake Docker
runner and scripted telemetry and LLM.

### Scenario B, step by step

```bash
# Operator, own stack only: create the practice fault
docker stop payment
# Wait at least 3 minutes before investigating. A container counts as "silent" (stopped reporting) only after its
# last metric sample is older than 150 s (SILENT_AFTER_S); earlier, the diagnosis rests on checkout traces alone.
sleep 180

# Terminal 2: investigate, plan and request approval through the team API (live)
.venv/bin/python cli.py investigate --incident-id DEMO-B-$(date +%H%M) \
    --symptom "Checkout fails for users; payments not completing" --service checkout \
    --model claude-sonnet-5-5 --respond --action-api --execute \
    --settle-s 120 --interval-s 30 --samples 2
```

1. The agent investigates live telemetry: overview, resource_overview (payment silent), traces (checkout →
   payment connection refused) and logs.
2. It prints the approval summary: diagnosis, cited evidence IDs, `restart_service payment {grace_period_seconds: 10}`,
   risks, verification criteria and baseline.
3. **Terminal 1** prints `ONE-TIME APPROVAL CODE: XXXX-XXXX`. Check that the action matches, then type
   `approve XXXX-XXXX` in terminal 2.
4. The API restarts `payment`. The agent waits about 2.5 min, re-queries Prometheus, traces and logs, and prints
   the outcome.

Use a new `--incident-id` for every run: each incident allows one action, enforced by both the agent and the server.

## 3. What to show the mentors

```bash
.venv/bin/python cli.py report DEMO-B-1234               # diagnosis, evidence (with links), proposal, approval, verification
.venv/bin/python cli.py audit --incident-id DEMO-B-1234  # agent journal: approval_opened, dispatch_intent, result
.venv/bin/python cli.py audit --server                   # API journal: approval_created, action_accepted/finished, verification
.venv/bin/python cli.py action-api actions               # server records with result + verification
```

| Question | Where to find the answer |
| --- | --- |
| Exactly which telemetry the LLM queried | Report section 2 (timeline: every tool call with arguments and evidence ID) and section 3 (evidence table plus Prometheus/Jaeger links). Raw captures are in `runs/<id>/evidence/`. |
| Why that action | Proposal reason, cited evidence and policy checks (timeline `Proposal passed policy`) |
| How approval was enforced | Server `approval_created` → agent `approval` → server `action_accepted` with `approval_id`. The code exists only on terminal 1. |
| Whether recovery was verified | Report section 6: per-check baseline and samples, observations, outcome. The server record also has `verification`. |

## 4. Dry-run and safety demos (no state change)

```bash
.venv/bin/python cli.py investigate ... --respond --action-api      # dry-run: local code + server /validate only
.venv/bin/python cli.py kill-switch on && .venv/bin/python cli.py action-api health   # kill_switch_active: true
.venv/bin/python cli.py kill-switch off
.venv/bin/python -m pytest -q tests                                  # all mocked and in-process tests
```

## 5. Honest limits

- Mocked tests prove the control logic, not diagnosis quality on unseen faults.
- A real verified recovery needs the human-run scenario B above.
- `simulate` mode can never produce `VERIFIED_RECOVERY`.
- Confidence labels are not calibrated.
