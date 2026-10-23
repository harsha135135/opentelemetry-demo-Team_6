# Orchestrator and CLI (Harsha) - CP0 to CP2, read-only

    Alert -> cli.py -> orchestrator.investigate -> reasoning.LLMReasoner via LangChain (one request per next_step)
          -> guardrails.validate_tool_request -> telemetry.run_tool (Neha) -> integration.to_evidence -> EvidenceRegistry
          -> guardrails.validate_diagnosis -> report_io.write_report (report.py, Aditya) -> JSON + Markdown

`reasoning.py` follows Harshita's (Member 3) LLM_brain design; `guardrails.py` is a placeholder for Member 4. Replace behind `contracts.py`.
`models.py` change: Evidence.service optional, source may be "derived", added trace_ids/log_ids (trace_or_log_id kept).
Telemetry tools are live-only (windows end at call time). Reports are internal/provisional: no official schema was available.

## Setup
    cd incident-agent && uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -r requirements.txt
    copy .env.example .env      # then set OPENSEARCH_URL, LLM_PROVIDER, LLM_MODEL and the provider's API key (never commit .env)
    # OpenSearch port: docker port opensearch 9200

## LLM (LangChain, free tier by default)
Default provider is Google Gemini (`LLM_PROVIDER=google_genai`, key in `GOOGLE_API_KEY`, a Flash model in `LLM_MODEL`).
Groq also works (`LLM_PROVIDER=groq`, `GROQ_API_KEY`). Requests use temperature 0 and no client retries; a run is
bounded by 6 model calls, 12 tool calls, 180 s and 60k tokens. Check the key and model first:

    .venv/bin/python cli.py check-llm

## CP0 check
    .venv/bin/python telemetry.py check

## Investigate a live incident
    .venv/bin/python cli.py investigate --incident-id INC-001 --symptom "<symptom>" [--service checkout]

## Tests (mocked, no network)
    .venv/bin/python -m pytest -q tests

## Known limitations
- checkout and product-catalog run at their 20 MiB memory limits in the shipped demo and often show high CPU or
  failing health checks with no incident. That is the demo's baseline (its config must not be changed); the tools
  report changes against the previous window so this baseline is not mistaken for the incident.
- After a Docker or Prometheus restart, Prometheus may not serve older data until storage compaction finishes.
  overview and resource_overview then report "NO BASELINE" rather than "no change". Wait ~10 minutes after a restart
  before injecting an incident.
- Free-tier LLM quotas are small and change without notice; a run that hits a rate limit ends as ESCALATED (model error).
