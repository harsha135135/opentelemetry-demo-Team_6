# Team form (DRAFT: every member must confirm their own row before submission)

**Team name:** Team 6

**Project:** AI On-Call Engineer for Cloud-Native Incident Response (Bosch Incident Zero Hackathon 2026)

**Repository:** https://github.com/harsha135135/opentelemetry-demo-Team_6 (branch `feature/harsha`)

## Members

From the [README](../README.md#team-members) team table.

| No. | Name | GitHub | Role (README) |
| --- | --- | --- | --- |
| 1 | Harshavardhan M | harsha135135 | CLI, orchestrator and integration through CP2 |
| 2 | Padarthi Neha Sai | pnehasai | Read-only telemetry: metrics, logs, traces and permitted health information |
| 3 | Harshita C | Harshita100 | Bounded LLM tool calling and evidence-backed diagnosis |
| 4 | Deepika Raman | deepika-raman | Architecture sketch and safety explanation |
| 5 | Aditya R | adityatr64 | Authorised incident testing and checkpoint reports |

## Who built which part (to confirm)

The rows below are drawn from git authorship (`git log --format='%h %an %s'`, including the team branches) and the
README roles. Git author names map to members as follows:

- `Harshavardhan M` → 1
- `pnehasai` → 2
- `Harshita Chhaparia` / `Harshita` → 3
- `Deepika_Raman` → 4
- `Aditya R` → 5

**Every row is marked "to confirm"; each member must correct their own.**

| Member | What git shows | Status |
| --- | --- | --- |
| Harshavardhan M | See the list below the table. | to confirm |
| Padarthi Neha Sai | Read-only telemetry tools in `telemetry.py`: Prometheus metrics, Jaeger traces, OpenSearch logs, health, evidence capture, feature-flag filtering. Also `tests/test_telemetry.py` and the initial `requirements.txt` and `.env.example` (`5545e435`, `6032313b`, `5338dae5`; `2c5d1d80` on `feature/neha`). | to confirm |
| Harshita C | The Claude investigator prototype with a citation gate, in `LLM_brain/agent/brain/investigator.py` and `LLM_brain/test_harshita.py` (`d9909a96`, imported unchanged for reference), plus `LLM_brain/agent/brain/models.py` on `feature/harshita` (`8feb8435`). This investigation prompt and its two-source confidence cap were adopted into the agent in `3701749b`. | to confirm |
| Deepika Raman | The only commit in the history is a README edit (`8be6721e`). README role: architecture sketch and safety explanation. `guardrails.py` is labelled a placeholder for Member 4. Contribution: `TODO (Deepika to describe)`. | to confirm |
| Aditya R | Shared Pydantic models, the report generator and its tests: `models.py`, `report.py`, `tests/test_report.py` (`54d59df3`, from the `adi` branch), plus a report fix (`f8c8510c`). The team integration contract, `docs/TEAM_INTEGRATION_CONTRACT.md` (`35bac1c9`), names Aditya as its author. The `adi` branch also holds `capture_run.py`, `telemetry_client.py`, `verify.py`, `intake.py` and tests that are **not** merged into `feature/harsha`. Running the practice incidents and recording ground truth: `TODO (Aditya to confirm)`. | to confirm |

**Harshavardhan M:**

- Orchestrator and CLI: `orchestrator.py`, `cli.py`.
- Anthropic tool loop: `reasoning.py`, labelled a placeholder for Member 3's interface.
- Evidence integration: `integration.py`, `resources.py`.
- Validator: `guardrails.py`, labelled a placeholder for Member 4.
- Remediation flow: `remediation.py`, `safety.py`, `response.py`, `verify.py`, `actions.py`, `endpoints.py`.
- Team Action API: `action_server.py`, `action_executor.py`, `action_api_client.py`, `action_api_models.py`.
- Report export: `report_io.py`.
- Most tests.
- Design and integration docs.
- Commits: `a98841ff`, `75ef5c86`, `58aa6841`, `3701749b`, `6ec02b46`, `5d97078e`, `499060ac`, `527e2bb0`.

Small README edits on `main` were also made by Neha, Harshita, Deepika and Aditya.

## Tools and AI models used

| Category | Entry |
| --- | --- |
| Agent LLM (runtime) | Anthropic Claude API, model `claude-sonnet-5-5` in the live runs (configurable with `--model` / `ANTHROPIC_MODEL`). It is used by the investigator (`reasoning.py`) and the proposal-only planner (`remediation.py`) through the `anthropic` Python SDK. |
| Other tools | Docker and Docker Compose, `uv` (Python environments), Git and GitHub. The stack itself (OpenTelemetry Collector, Prometheus, Jaeger, OpenSearch, Grafana) comes from the upstream demo. |

## AI coding assistants used

`TODO (team must declare truthfully per hackathon rules)`

Each member declares every AI coding assistant they used, and for what.

## Third-party libraries

Agent, from [`incident-agent/requirements.txt`](../incident-agent/requirements.txt):

| Library | Version constraint | Used for |
| --- | --- | --- |
| `anthropic` | `>=0.40` | Claude API client |
| `httpx` | `>=0.27` | HTTP client for telemetry backends and the Action API |
| `pydantic` | `>=2.6` | Shared models, request validation, JSON Schema generation |
| `python-dotenv` | `>=1.0` | Loading `incident-agent/.env` |
| `pytest` | `>=8.0` | Tests |

The Action API server uses only the Python standard library (`http.server`) plus `pydantic`.

**Base application:** the upstream [OpenTelemetry Demo](https://github.com/open-telemetry/opentelemetry-demo)
(Apache-2.0, see [`LICENSE`](../LICENSE)), including its services and observability stack. Team 6's code lives in
`incident-agent/` beside it.
