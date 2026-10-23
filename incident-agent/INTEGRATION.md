# Orchestrator and CLI (Harsha) - CP0 to CP2, read-only

    Alert -> cli.py -> orchestrator.investigate -> reasoning.AnthropicReasoner (one request per next_step)
          -> guardrails.validate_tool_request -> telemetry.run_tool (Neha) -> integration.to_evidence -> EvidenceRegistry
          -> guardrails.validate_diagnosis -> report_io.write_report (report.py, Aditya) -> JSON + Markdown

`reasoning.py` and `guardrails.py` are placeholders for Member 3 and Member 4; replace them behind `contracts.py`.
`models.py` change: Evidence.service optional, source may be "derived", added trace_ids/log_ids (trace_or_log_id kept).
Telemetry tools are live-only (windows end at call time). Reports are internal/provisional: no official schema was available.

## Setup
    cd incident-agent && uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -r requirements.txt
    export ANTHROPIC_API_KEY=...   # never commit; or put it in the gitignored .env
    export OPENSEARCH_URL=http://localhost:$(docker port opensearch 9200 | head -1 | cut -d: -f2)

## CP0 check
    .venv/bin/python telemetry.py check

## Investigate a live incident
    .venv/bin/python cli.py investigate --incident-id INC-001 --symptom "<symptom>" [--service checkout] --model <model-id>

## Tests (mocked, no network)
    .venv/bin/python -m pytest -q tests
