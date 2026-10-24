# Incident Zero — On-Call Agent Web Interface

A web-based Command Center for the AI On-Call Engineer, built with **Python & Streamlit**.

## Features

- **🚨 1. Raise New Alert:**
  - One-click presets for common Astronomy Shop failure scenarios (Cart Valkey pool exhaustion, Payment gateway outage, Frontend 503 spike, Shipping RPC deadline exceeded).
  - Configurable parameters: Incident ID, Source (`alertmanager`, `manual-ui`, `pagerduty`, etc.), Affected Service, Observation Window, Recovery Target, and Remediation Mode (`dry_run` vs `live`).
  - Safe & isolated: Does not modify any teammate code.
- **🔬 2. Live Investigation & Recovery:**
  - Visual status pipeline: `RECEIVED` ➔ `INVESTIGATING` ➔ `DIAGNOSED` ➔ `ACTION_PROPOSED` ➔ `AWAITING_APPROVAL` ➔ `EXECUTING` ➔ `VERIFYING` ➔ `RECOVERED`.
  - Step-by-step progress tracking with real-time reasoning feedback.
  - **Evidence Registry:** Live inspection of metrics, traces, and logs queries and their observed values.
  - **Root Cause & Diagnosis:** Suspected component, cause, confidence ratings, and disproved alternatives.
  - **Human-in-the-Loop Approval Gate:** Displays proposed action, parameters, risks, and requires typing the SHA-256 digest `approve <code>` challenge before dispatch.
  - **Recovery Verification:** Baseline vs post-remediation telemetry comparison.
  - **Artifacts:** Downloadable and previewable markdown and JSON incident reports.
- **⚙️ System Health & Controls:**
  - Real-time Prometheus, Jaeger, OpenSearch, and Action API connectivity indicators.
  - Emergency Operator **Kill Switch** toggle.
- **📁 3. History & Audit:**
  - Browse past runs from `incident-agent/runs/` and export incident reports.

---

## How to Run

From the repository root or from `incident-agent/`:

```bash
# Using the project venv:
.venv/bin/streamlit run incident-agent/UserInterface/app.py --server.port 8501
```

Or from within `incident-agent/UserInterface/`:

```bash
cd incident-agent/UserInterface
../../.venv/bin/streamlit run app.py
```

Open your browser at:
`http://localhost:8501`

---

## Teammate Collaboration Safety

All UI code lives strictly in `incident-agent/UserInterface/`. It imports contracts and models from `incident-agent/` (`models.py`, `endpoints.py`, `safety.py`, `orchestrator.py`, etc.) as a read-only client, ensuring **zero merge conflicts** with other branches (`feature/harsha`, `feature/neha`, `feature/harshita`, `feature/deepika`).
