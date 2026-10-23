"""Offline tests for Harshita's LLM_brain wired into the project (fake chat model, mocked telemetry)."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from langchain_core.messages import AIMessage, ToolMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "LLM_brain"))
from agent.brain.investigator import HarshitaBrain, investigation_tools  # noqa: E402
from models import ConfidenceLabel, Evidence, Incident, TimeWindow, ToolStatus  # noqa: E402

NOW = datetime.now(timezone.utc)
INC = Incident(id="INC-H", source="t", symptom="cart errors", detection_time=NOW, recovery_condition="x",
               observation_window=TimeWindow(start_time=NOW - timedelta(minutes=15), end_time=NOW))


class FakeChat:
    def __init__(self, *responses):
        self.responses, self.bound, self.requests = list(responses), [], []

    def bind_tools(self, tools, **kw):
        self.bound.append((tools, kw))
        return self

    def invoke(self, messages, **kw):
        self.requests.append(list(messages))
        return self.responses.pop(0)


def ai(*calls):
    return AIMessage(content="", tool_calls=[{"name": n, "args": a, "id": i} for i, n, a in calls])


def fake_telemetry(name, args):
    return Evidence(evidence_id=f"ev-{name}-1", status=ToolStatus.OK, source="jaeger", service=args.get("service"),
                    event_time_window=TimeWindow(start_time=NOW, end_time=NOW), tool=name, query="q",
                    observed_value={"error_origin_counts": {"cart": 3}})


def submit(ids, conf="high"):
    return {"suspected_component": "cart", "suspected_cause": "valkey timeout", "supporting_evidence_ids": ids,
            "conflicting_evidence_ids": [], "alternatives": [], "uncertainty": "", "confidence_label": conf,
            "next_step": "escalate"}


def test_uses_project_tools_not_free_form_queries():
    names = {t["function"]["name"] for t in investigation_tools()}
    assert {"overview", "resource_overview", "search_traces", "query_logs", "submit_diagnosis"} <= names
    assert "query_traces" not in names and "finish" not in names


def test_gate_drops_invented_ids_and_lowers_confidence():
    chat = FakeChat(ai(("c1", "search_traces", {"service": "cart"})),
                    ai(("c2", "submit_diagnosis", submit(["ev-search_traces-1", "ev-made-up"]))))
    diag, evidence = HarshitaBrain("m", chat_model=chat).investigate(INC, fake_telemetry)
    assert diag.supporting_evidence_ids == ["ev-search_traces-1"] and diag.confidence_label == ConfidenceLabel.LOW
    assert "invalid citation" in diag.uncertainty and [e.evidence_id for e in evidence] == ["ev-search_traces-1"]
    assert isinstance(chat.requests[1][-1], ToolMessage)  # tool result answered before the next request


def test_flag_queries_blocked_and_budget_end_forces_submission():
    chat = FakeChat(ai(("c1", "query_logs", {"service": "flagd"})),
                    ai(("c2", "submit_diagnosis", submit([], conf="medium"))))
    diag, evidence = HarshitaBrain("m", chat_model=chat, max_turns=1).investigate(INC, fake_telemetry)
    assert evidence == [] and diag.confidence_label == ConfidenceLabel.LOW  # blocked; no valid support -> escalate
    assert chat.bound[-1][1].get("tool_choice") == "submit_diagnosis"
