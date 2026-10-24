import sys, pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    """telemetry.py loads the developer's .env on import; tests must never reach a real LLM or use real keys."""
    for var in ("LLM_PROVIDER", "LLM_MODEL", "LLM_FALLBACK_MODEL", "LLM_THINKING_BUDGET",
                "GOOGLE_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GRAFANA_URL", "")  # never post test results to a running Grafana
