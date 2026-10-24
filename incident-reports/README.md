# Incident reports (produced by the agent)

Each folder contains the agent's report in JSON and in readable Markdown, plus the raw evidence captures the report
cites. The JSON follows our **team-defined** schema
[`incident-agent/schemas/incident_report.schema.json`](../incident-agent/schemas/incident_report.schema.json); no
organiser schema was supplied.

LIVE-2-PAYMENT and LIVE-1-BASELINE carry `schema_version: team6.incident-report/1.0`. LIVE-A-HEALTHY-1 and
LIVE-STACKDOWN-1 were produced before the schema existed: their JSON has no `schema_version` field and otherwise
validates against the same model.

`local_capture_reference` paths inside the JSON point to where the evidence was captured on the operator laptop. The
same files are copied here under `evidence/`.

| Report | Ground truth (how the fault was created) | Agent result | Correct? |
| --- | --- | --- | --- |
| [LIVE-2-PAYMENT](LIVE-2-PAYMENT/LIVE-2-PAYMENT_report.md) | Operator ran `docker stop payment` on the local stack | Diagnosed `payment` exited, **high** confidence (container state, silent metrics, checkout traces). Proposed `restart_service payment`; approval was not given in a non-interactive session, so nothing was executed. | Diagnosis **correct**; remediation pending a human-approved run |
| [LIVE-1-BASELINE](LIVE-1-BASELINE/LIVE-1-BASELINE_report.md) | No injected fault. The load generator showed ~20% failures | Found load-generator calls to the optional `agent` service failing DNS (that service is not part of `make start`), plus ~5% `ad` errors. **Low** confidence, so no action. | Plausible; environmental, not an incident. Abstaining from action was correct. |
| [LIVE-A-HEALTHY-1](LIVE-A-HEALTHY-1/LIVE-A-HEALTHY-1_report.md) | Stack started without the observability layer: the collector exported to `debug` only | "All telemetry empty: pipeline outage or stack down, cannot tell which". Inconclusive. | Correct abstention |
| [LIVE-STACKDOWN-1](LIVE-STACKDOWN-1/LIVE-STACKDOWN-1_report.md) | App containers removed; telemetry silent for over 60 minutes | Same honest abstention | Correct abstention |

Practice-incident runs from 23 October (operator-injected faults; the reports do not record whether they were mentor-selected) are summarised in
[`docs/EVALUATION_NOTE.md`](../docs/EVALUATION_NOTE.md).
