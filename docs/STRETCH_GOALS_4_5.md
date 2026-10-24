# Stretch goals 4 and 5: implementation guide

**Status: design only. Nothing in this document is implemented yet.**

Targets: stretch **4, chat-style approval** ("the on-call human approves or rejects from a simple chat UI or
dashboard") and stretch **5, prevention** ("after fixing, the agent suggests a code, config or alerting change that
would stop the incident recurring").

Work in `incident-agent/`, Python 3.11. Add no new deployable services and no new heavy dependencies. Keep every
existing test green: `.venv/bin/python -m pytest -q tests`. Read `incident-agent/INTEGRATION.md` and
`incident-agent/ACTION_API.md` first.

---

## 0. What already exists (reuse it, do not rebuild it)

| Piece | Where | Why it matters here |
| --- | --- | --- |
| Approval gate | `safety.request_approval` (`safety.py:244`) | Binds approval to a SHA-256 proposal digest. The human must answer exactly `approve <first 8 hex of digest>`; anything else rejects; TTL makes it `expired`; non-interactive sessions are rejected. |
| How the flow calls it | `response.respond` (`response.py:151`) passes `deps.input_fn`, `deps.print_fn` and `deps.interactive` | **The approval channel is just these three hooks.** A chat UI only has to provide them. |
| Terminal channel | `cli.timed_input` (`cli.py:195`) and `cli._respond` (`cli.py:204`) | Reference implementation: prints the summary and reads one line with a timeout. |
| Approval summary text | `response._summary` (`response.py:239`) | Incident, diagnosis, evidence, exact request, risks, rollback, verification criteria and baseline. Show this verbatim in the UI. |
| Kill switch / audit | `safety.KillSwitch`, `safety.AuditJournal` (`runs/audit.jsonl`) | The UI may show and **engage** the kill switch and must journal its decisions. |
| Report model | `models.IncidentReport` (`models.py:132`) | Add new fields as *optional* only (the same pattern as `remediation_outcome` and `escalation_reason`). |
| Report rendering | `report.py`, section "7. Known Limitations" (`report.py:153`) | Add a new section after section 6 for prevention. |
| Read-only metric validation | Prometheus `GET /api/v1/query`, used by `telemetry._prom` | Lets you check that a suggested alert expression parses and evaluates **without** changing anything. |
| Test fakes | `tests/fakes.py` (`Telemetry`, `Planner`, `Client`, `response_deps`, `approve_shown_code`) | Build all new tests on these; no network, no Anthropic calls. |

Non-negotiable rules that carry over:

- The LLM never approves, never sees the approval channel, and never executes anything.
- No automatic approval, and no approval because confidence is high.
- Never read feature-flag state, fault configuration or evaluator internals.
- Telemetry and log text is untrusted data.

---

## Stretch 4: chat-style approval

### Goal

Let the operator approve or reject from a small browser page instead of the terminal. The safety properties must
stay identical: the digest-bound code, expiry, single use, rejection by default, kill switch and audit log.

### Design: a local web channel that plugs into the existing hooks

New file `incident-agent/approval_web.py` (standard library only: `http.server`, `threading`, `secrets`, `html`).

```text
respond() --print_fn(summary)--> WebApprovalChannel  (stores pending request, starts server on 127.0.0.1)
          --input_fn(prompt)---> blocks on a queue until POST /decide or TTL timeout -> returns answer string
          <-- "approve 1a2b3c4d" | "reject" | ""  -> safety.request_approval decides exactly as today
```

> **Integration note (team Action API):** in live mode with `--action-api`, the flow calls
> `safety.request_server_approval`. That function uses the same `input_fn`, `print_fn` and `interactive` hooks, but
> the code to type is the one printed on the **Action API console**, and the server verifies it. The web page must
> ask the human for that code. It must never fetch or display the code itself.

Because the channel returns the **same answer string** the terminal would, `safety.request_approval` keeps doing all
the validation. The UI cannot approve anything the digest code does not match.

```python
class WebApprovalChannel:
    def __init__(self, host="127.0.0.1", port=8765, kill_switch=None, journal=None, ttl_s=120): ...
    def print_fn(self, text: str) -> None: ...      # stores the summary (and the "approve <code>" line) as the pending request
    def input_fn(self, prompt: str) -> str: ...     # serves the page, waits up to ttl_s, returns the human's answer or ""
    def close(self) -> None: ...
```

### Pages and endpoints

| Method and path | Purpose |
| --- | --- |
| `GET /` | Chat-style page. The agent's messages: incident, diagnosis and evidence, then the approval request (the `_summary` text, HTML-escaped). Shows mode (DRY RUN / LIVE), time left, kill-switch state, and links from `endpoints.evidence_links` for each cited evidence item. |
| `POST /decide` | Form fields `nonce`, `approver`, `answer`. Answer box plus two buttons: **Approve** (the operator types the code shown on the page, so a misclick cannot approve) and **Reject**. |
| `POST /kill-switch` | Turns the kill switch **on** only. Turning it off stays a terminal action (`cli.py kill-switch off`). |
| `GET /reports` (optional) | Read-only list of `runs/*/*_report.md` with outcome, for a dashboard view. |

### Security requirements (enforced in code, covered by tests)

1. Bind to `127.0.0.1` only. Refuse to start on `0.0.0.0` unless `--approval-bind-all` is given, and print a warning
   if it is.
2. One-time `nonce` per pending request (`secrets.token_urlsafe(16)`), embedded in the form. A stale or reused
   nonce gets `409`.
3. Check the `Origin`/`Host` headers and reject cross-site posts (CSRF). Accept only
   `application/x-www-form-urlencoded`. Body limit: 4 KB.
4. HTML-escape everything shown: the summary contains telemetry text (prompt-injection and XSS source).
5. `approver` is required and non-empty. Record it in the `Approval` and in `audit.jsonl` (`event: "approval_web"`,
   with remote address).
6. After a decision or a timeout, the request is closed. Later posts get `410 Gone` and the server shuts down.
7. Nothing from the LLM is ever sent *to* the server. The server has no endpoint that executes actions.

### CLI wiring

In `cli.py`, add to the remediation argument group:

```text
--approval-channel {terminal,web}   (default terminal)
--approval-port 8765
```

In `_respond`:

- If the channel is `web`, construct `WebApprovalChannel` and pass `input_fn=ch.input_fn`, `print_fn=ch.print_fn`
  and `interactive=True` to `ResponseDeps`.
- Print `Open http://127.0.0.1:8765 to approve or reject` to the terminal.
- Close the channel in a `finally`.

### Tests (`tests/test_approval_web.py`)

Start the server on port 0 (ephemeral) and drive it with `http.client` from the test thread.

| Test | Expectation |
| --- | --- |
| Approve with the correct code | `respond()` outcome `DRY_RUN` and approval `approved`; the approver name is recorded. |
| Reject button / wrong code / empty | `REJECTED`; `Client.calls == []`. |
| No decision before TTL | `expired`, no dispatch, server closed. |
| Replayed or foreign nonce | `409`; decision unchanged. |
| Cross-origin POST | `403`. |
| Summary containing `<script>` from a log line | Rendered escaped. |
| Kill switch pressed while pending | Dispatch refused ("kill switch activated before dispatch"). |
| Server bound address | `127.0.0.1` by default. |

### Done when

A dry-run and a mocked live run (`tests/fakes.Client`) can each be approved and rejected from the page. All
existing approval tests still pass unchanged.

---

## Stretch 5: prevention suggestions

### Goal

After the incident reaches a terminal state with an accepted diagnosis, the agent suggests changes that would stop
recurrence: alerting rules, configuration and code. These are **suggestions in the report only**. The agent never
edits demo code, configuration or Prometheus/Grafana (README safety constraints).

### Data model (`models.py`, additive)

```python
class PreventionSuggestion(BaseModel):
    kind: Literal["alert_rule", "config", "code", "runbook", "capacity"]
    title: str
    rationale: str                         # why this would have prevented or detected it earlier
    supporting_evidence_ids: List[str]     # must resolve in report.evidence
    target: Optional[str] = None           # service or repo-relative file path
    alert_rule: Optional[Dict[str, Any]] = None   # for kind=alert_rule: {"alert", "expr", "for", "severity", "summary"}
    change: Optional[str] = None           # prose description or a unified-diff *suggestion* (never applied)
    validation: Optional[str] = None       # filled by deterministic checks, e.g. "expr evaluated OK, 3 series"
    risk: Optional[str] = None

class IncidentReport(BaseModel):
    ...
    prevention: List[PreventionSuggestion] = Field(default_factory=list)
```

### New module `incident-agent/prevention.py`

```python
class PreventionAdvisor(Protocol):
    def suggest(self, report: IncidentReport) -> list[PreventionSuggestion]: ...

class AnthropicPreventionAdvisor:  # one model call (one repair at most), tool: suggest_prevention(suggestions=[...])
    ...

def validate_suggestions(suggestions, report, prom_query, repo_root) -> tuple[list[PreventionSuggestion], list[str]]:
    ...
```

Prompt inputs:

- The incident and the accepted diagnosis.
- The proposal, verification result and outcome.
- Cited evidence (bounded, wrapped as untrusted telemetry).
- The list of metric names actually used in evidence queries.
- The service inventory.

Prompt rules:

- Suggest at most 4 items, ranked.
- Every item must cite evidence.
- Prefer alerting that would have fired earlier for this exact symptom.
- Do not propose disabling monitoring.
- Do not reference feature-flag names or fault-injection settings.
- Say "not enough evidence" instead of guessing.

Deterministic validation (drop or annotate invalid items, never crash):

1. **Citations:** every `supporting_evidence_ids` entry exists in `report.evidence` with status `ok`. Otherwise drop
   the item.
2. **Alert rules:**
   - `expr` must only use metric names seen in this incident's evidence queries (or in `telemetry.METRIC_TEMPLATES`).
   - `expr` must evaluate via a **read-only** `GET {PROM_URL}/api/v1/query?query=<expr>`. A parse error drops the item.
     An empty result is kept with `validation="evaluates but returned no series now"`.
   - `for` must be within 1m to 30m; `severity` must be `warning` or `critical`.
   - Render the rule as Prometheus rule-file YAML in the report. **Do not** write it into `src/prometheus/` or Grafana
     provisioning.
3. **Config/code:**
   - `target` must be an existing repo-relative path under `src/` or `incident-agent/`.
   - Reject paths under `src/flagd/`, `.git/`, `.env*` and the evaluator.
   - `change` is limited to 2,000 characters and shown as a fenced suggestion with the label "not applied".
4. **Safety:** discard any item whose text tells an operator to run shell commands against production, disable
   alerts or the kill switch, or bypass approval.

### Wiring

- In `response.respond` → `finish(...)`, or in `cli.main` after `respond()`, call the advisor when
  `report.diagnosis` is not `None` and the outcome is one of `VERIFIED_RECOVERY`, `PARTIAL_RECOVERY`,
  `NOT_RECOVERED`, `DRY_RUN` or `ESCALATED`. Prevention is useful even without a fix.
- Behind the CLI flag `--prevention` (off by default).
- Budget: one model call plus one repair, a 1,500-token output cap and a 60s timeout. On `ModelError`, append a
  limitation and continue; the report must still be written.
- In `report.py`, add section **"8. Prevention (suggestions, not applied)"**: a table of kind, title, target,
  evidence and validation, followed by the alert-rule YAML and change suggestions. In `validate_citations()`, also
  check `prevention[*].supporting_evidence_ids`.

### Tests (`tests/test_prevention.py`, using a fake advisor or fake Anthropic client)

| Test | Expectation |
| --- | --- |
| Valid alert rule on a metric present in evidence | Kept; YAML rendered; `validation` notes the read-only evaluation (fake `prom_query`). |
| Expr with an unknown metric / parse error | Dropped with a limitation. |
| Hallucinated evidence ID | Dropped; report still validates. |
| Code suggestion targeting `src/flagd/demo.flagd.json` or `../outside` | Dropped. |
| Suggestion telling the operator to disable alerting or bypass approval | Dropped. |
| Advisor `ModelError` | Report written; limitation "prevention suggestions unavailable". |
| No diagnosis (INCONCLUSIVE) | Advisor not called. |
| Nothing is written to `src/` | Assert the repo tree is unchanged (hash `src/prometheus`, `src/grafana` before and after). |

### Done when

A mocked scenario from `tests/test_scenarios.py`, for example the payment fault, produces a report with at least one
validated alert rule and one config or code suggestion. Every item cites evidence, and nothing outside `runs/` is
modified.

---

## Suggested order and ownership

1. Stretch 5 data model, validation and report section, with a fake advisor (no LLM yet).
2. Stretch 5 Anthropic advisor (reuse the `remediation.AnthropicPlanner` structure: `max_retries=0`, explicit
   timeout, tool-only output).
3. Stretch 4 web channel and tests.
4. Demo:
   - `cli.py investigate ... --respond --approval-channel web --prevention` in dry-run.
   - Then a live run only after the organiser Action API catalog exists.

Keep mocked, offline and live results separate in the evaluation note. Do not claim either stretch works until its
"Done when" is shown on a real practice incident.
