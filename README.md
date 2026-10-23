# Bosch Incident Zero Hackathon — Team 6

## Project

AI On-Call Engineer for Cloud-Native Incident Response

## Team Members

| No. | Name | GitHub Username | Responsibility |
| --- | --- | --- | --- |
| 1 | Harshavardhan M | harsha135135 | [Role] |
| 2 | Padarthi Neha Sai | pnehasai | [Role] |
| 3 | Harshita C | Harshita100 | [Role] |
| 4 | [Member 4] | [GitHub Username] | [Role] |
| 5 | [Member 5] | [GitHub Username] | [Role] |

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
