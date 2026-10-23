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

Member 3–5 assignments follow the team-table order and should be confirmed by
the team. Preserve existing modules and use the shared Pydantic contracts for
incident inputs, tool results, evidence and diagnoses.

The current shared contracts are `incident-agent/models.py` (updated: `Evidence.service` is optional,
`source` may be `"derived"`, and `trace_ids`/`log_ids` were added) and `incident-agent/contracts.py`
(orchestrator, reasoning and guardrail interfaces). See `incident-agent/INTEGRATION.md` and `docs/TEAM_INTEGRATION_CONTRACT.md`.

## Current Priority: CP0, CP1 and CP2

Checkpoint presentation: **23 October 2026, 5:30 PM IST**.
The immediate goal is a reproducible, read-only investigation and diagnosis.
CP3, remediation execution, optional features and cosmetic improvements are
deferred. The full workflow and deliverables below describe the later destination.

| Checkpoint | Required demonstration | Responsibility |
| --- | --- | --- |
| CP0 — Setup | Docker services and observability backends are operational; telemetry endpoints respond; setup commands are reproducible. | Harsha integrates setup; Neha verifies telemetry access; Member 5 records results. |
| CP1 — Eyes | Investigate one authorised practice incident with real telemetry, show symptoms and evidence, and present a concise architecture sketch. | Harsha connects CLI and orchestration; Neha supplies read-only tools; Member 4 explains architecture and safety; Member 5 captures the run. |
| CP2 — Brain | Bounded LLM tool calling selects useful investigation steps and produces a root-cause diagnosis supported by real evidence. Demonstrate correct diagnoses on at least three mentor-selected authorised practice incidents. | Member 3 owns diagnosis; Harsha integrates it; Neha supports queries; Member 5 checks and reports each result; Member 4 explains boundaries. |

Before CP2, the working flow must be:

**Alert → targeted telemetry queries → evidence → diagnosis or honest escalation
→ checkpoint report.**

- Harsha: validate inputs, connect shared contracts and modules, bound the
  orchestration loop, and provide one reproducible CLI demonstration path.
- Neha: return real, timestamped telemetry with source/query references;
  distinguish missing data from healthy results.
- Member 3: bound model/tool calls and time, validate structured output, compare
  hypotheses, cite captured evidence and report uncertainty.
- Member 4: explain components, data flow, read-only access and why the agent
  cannot mutate the demo or read feature-flag state for diagnosis.
- Member 5: obtain the mentor-selected incidents, record expected versus observed
  diagnoses, separate mocked tests from live runs, and document failures honestly.

Do not hard-code incident answers, fabricate evidence, inspect feature flags for
diagnosis or modify the demo. Implementation and code commits must stay within
the organiser-confirmed coding window.

This README update does not verify any checkpoint. Record CP0/CP1/CP2 as passed
only after the required behaviour is demonstrated. For CP2, report the number
correctly diagnosed out of the number of real mentor-selected incidents tested;
mocked tests do not count toward the three incidents.

## Problem Statement

Build an AI-powered incident response agent that investigates
failures in the OpenTelemetry Demo microservices application.

The agent must identify root causes using real telemetry,
recommend safe remediation actions, obtain human approval,
execute permitted actions, and verify system recovery.

## Technology Stack

- Python 3.11
- Docker and Docker Compose
- OpenTelemetry
- Grafana
- Jaeger
- OpenTelemetry Collector
- LLM API (to be confirmed)
- Git and GitHub

## Local Setup

Clone the repository:

    git clone https://github.com/harsha135135/opentelemetry-demo-Team_6.git

Enter the repository:

    cd opentelemetry-demo-Team_6

Download Docker images:

    docker compose pull

Start the application:

    docker compose up -d

Check container status:

    docker compose ps

Stop the application:

    docker compose down

## Local Services

| Service | URL |
| --- | --- |
| Astronomy Shop | <http://localhost:8080> |
| Grafana | <http://localhost:8080/grafana/> |
| Jaeger | <http://localhost:8080/jaeger/ui/> |
| Load Generator | <http://localhost:8080/loadgen/> |
| Feature Flags | <http://localhost:8080/feature/> |

URLs depend on the Docker Compose configuration.

## Agent Workflow

1. Receive an incident alert.
2. Collect logs, metrics, traces and health information.
3. Investigate the affected microservices.
4. Identify the probable root cause.
5. Produce an evidence-backed diagnosis.
6. Propose a safe remediation action.
7. Obtain explicit human approval.
8. Execute the approved action through the Action API.
9. Verify recovery.
10. Generate an incident report.

## Safety Constraints

- Do not inspect feature flags to discover incident causes.
- Do not modify the demo or scoring harness.
- Do not execute state-changing actions without approval.
- Use only organiser-approved remediation actions.
- Do not fabricate evidence.
- Escalate uncertain diagnoses.
- Record actions and verification results.

## Hackathon Deliverables

- [ ] Working AI On-Call Engineer
- [ ] Source code and setup instructions
- [ ] Human approval gate and kill switch
- [ ] One-page architecture overview
- [ ] Incident reports in the required JSON schema
- [ ] Evaluation note with successes and failures
- [ ] Team contribution and AI tools declaration
- [ ] Final live demonstration

## Current Status

Environment setup in progress.

Agent implementation and testing are pending.

## Design and Implementation Guides

- [Design guide and architecture overview](docs/ARCHITECTURE.md)
- [Simple implementation guide](docs/IMPLEMENTATION_GUIDE.md)

These guides describe the planned solution. No incident-response modules have
been implemented. The implementation guide also explains the Compose layers
needed for telemetry; the base startup command above does not include them.

## References

- <https://github.com/open-telemetry/opentelemetry-demo>
- <https://opentelemetry.io/docs/>
- <https://docs.docker.com/>

---

Bosch Incident Zero Hackathon 2026 — Team 6
