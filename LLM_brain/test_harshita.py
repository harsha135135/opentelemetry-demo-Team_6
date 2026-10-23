"""
test_harshita.py - Test script for Harshita's module.
Simulates Neha's Evidence returns (default) or uses the real telemetry tools (--live) to verify the citation gate.

    python LLM_brain/test_harshita.py           # mocked telemetry, real LLM from incident-agent/.env
    python LLM_brain/test_harshita.py --live    # Neha's real read-only tools against the running demo
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent.brain.investigator import AGENT_DIR, HarshitaBrain, project_telemetry_caller  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from models import Evidence, Incident, TimeWindow, ToolStatus  # noqa: E402

load_dotenv(AGENT_DIR / ".env")


# 1. Mock Neha's Telemetry Tool (generates real Evidence objects, same tool names as the project)
def mock_neha_telemetry(tool_name: str, args: dict) -> Evidence:
    print(f"\n[Tool Invoked] -> {tool_name} with args: {args}")
    now = datetime.now(timezone.utc)
    window = TimeWindow(start_time=now - timedelta(minutes=10), end_time=now)

    if tool_name == "search_traces":
        return Evidence(
            evidence_id="ev-search_traces-3873a079", status=ToolStatus.OK, source="jaeger",
            service=args.get("service", "frontend"), event_time_window=window, tool="search_traces",
            query=f"service={args.get('service')}&errors_only=true",
            observed_value={"error_origin_counts": {"cart": 6},
                            "traces": [{"suspected_origin": {"service": "cart", "operation": "GetCart",
                                                             "error_message": "valkey connection timeout"}}]},
            trace_ids=["3873a0791c2e4f5a8b9c0d1e2f3a4b5c"],
        )
    if tool_name in ("query_metrics", "overview", "service_health"):
        return Evidence(
            evidence_id=f"ev-{tool_name}-a1b2c3d4", status=ToolStatus.OK, source="prometheus",
            service=args.get("service"), event_time_window=window, tool=tool_name, query="fixed template",
            observed_value={"changed_since_previous_window": ["cart: error_rate up 0.000 -> 0.450"]},
        )
    if tool_name == "query_logs":
        return Evidence(
            evidence_id="ev-query_logs-99f8e7d6", status=ToolStatus.OK, source="opensearch",
            service=args.get("service", "cart"), event_time_window=window, tool="query_logs", query="WARN+",
            observed_value={"lines": [{"body": "Valkey.Redis.ConnectionTimeout: Pool exhausted"}]},
        )
    return Evidence(
        evidence_id=f"ev-{tool_name}-0000", status=ToolStatus.EMPTY, source="prometheus", service=None,
        event_time_window=window, tool=tool_name, query="", observed_value=None,
    )


# 2. Run the test
if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true", help="use Neha's real tools against the running demo")
    a = p.parse_args()
    model = os.getenv("LLM_MODEL")
    if not model:
        print("Error: set LLM_PROVIDER, LLM_MODEL and the provider's API key in incident-agent/.env")
        sys.exit(1)

    now = datetime.now(timezone.utc)
    sample_incident = Incident(
        id="INC-20261023-001",
        source="alertmanager",
        symptom="Elevated 500 status codes on /api/cart",
        affected_service="frontend",
        detection_time=now,
        observation_window=TimeWindow(start_time=now - timedelta(minutes=15), end_time=now),
        recovery_condition="frontend HTTP 500 error rate < 0.01 for 2 consecutive minutes",
    )
    caller = project_telemetry_caller(Path(__file__).resolve().parent / "runs" / "evidence") if a.live else mock_neha_telemetry

    print(f"=== Testing Harshita's SRE Brain ({os.getenv('LLM_PROVIDER', 'google_genai')} / {model}, "
          f"{'live telemetry' if a.live else 'mocked telemetry'}) ===")
    brain = HarshitaBrain(model, provider=os.getenv("LLM_PROVIDER"), max_turns=4)
    diagnosis, evidence_list = brain.investigate(sample_incident, caller)

    print("\n" + "=" * 50)
    print("DIAGNOSIS PRODUCED:")
    print("=" * 50)
    print(f"Suspected Component: {diagnosis.suspected_component}")
    print(f"Suspected Cause:     {diagnosis.suspected_cause}")
    print(f"Confidence Label:    {diagnosis.confidence_label.value}")
    print(f"Supporting Evidence: {diagnosis.supporting_evidence_ids}")
    print(f"Conflicting Evidence:{diagnosis.conflicting_evidence_ids}")
    print(f"Alternatives:        {diagnosis.alternatives}")
    print(f"Uncertainty:         {diagnosis.uncertainty}")
    print(f"Next Step:           {diagnosis.next_step}")

    # 3. Simulate Aditya's Citation Integrity Gate
    print("\n[Simulating Aditya's validate_citations() Gate]:")
    valid_ids = {e.evidence_id for e in evidence_list}
    hallucinated = set(diagnosis.supporting_evidence_ids + diagnosis.conflicting_evidence_ids) - valid_ids
    if hallucinated:
        print(f"FAILED: Found hallucinated IDs: {hallucinated}")
    else:
        print("PASSED: 100% Citation Integrity! All cited evidence IDs were produced by Neha.")
