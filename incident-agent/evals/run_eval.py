"""Scoring harness, kept separate from the agent. The agent never reads expected answers.

The operator injects each fault, then runs:
    python evals/run_eval.py --cases /path/outside/repo/cases.json --model <id> [--out runs]

cases.json (operator-held, never committed):
    [{"incident_id": "INC-1", "symptom": "<symptom shown to the agent>", "expected_component": "ad",
      "expected_terms": ["cpu"]}]
Only incident_id and symptom reach the agent. Automatic matching is a first pass; the mentor judges correctness.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]


def score(report: dict, case: dict) -> dict:
    diag = report.get("diagnosis")
    result = {"incident_id": case["incident_id"], "status": report.get("status"), "correct": False, "reason": ""}
    if report.get("status") != "DIAGNOSED" or not diag:
        result["reason"] = f"no accepted diagnosis (status {report.get('status')})"
        return result
    comp, text = diag["suspected_component"].lower(), (diag["suspected_component"] + " " + diag["suspected_cause"]).lower()
    expected = case["expected_component"].lower()
    if expected not in comp:
        result["reason"] = f"suspected component '{diag['suspected_component']}' does not match expected"
        return result
    missing = [t for t in case.get("expected_terms", []) if t.lower() not in text]
    if missing:
        result["reason"] = f"cause text lacks expected terms {missing}"
        return result
    result.update(correct=True, reason="component and terms match (mentor must still confirm)")
    return result


def run_case(case: dict, model: str, out: Path) -> dict:
    cmd = [sys.executable, str(AGENT_DIR / "cli.py"), "investigate", "--incident-id", case["incident_id"],
           "--symptom", case["symptom"], "--model", model, "--out", str(out)]  # expected answers are never passed
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=AGENT_DIR)
    path = out / case["incident_id"] / f"{case['incident_id']}_report.json"
    if not path.is_file():
        return {"incident_id": case["incident_id"], "status": "NO_REPORT", "correct": False,
                "reason": f"cli exit {proc.returncode}: {proc.stderr.strip()[:200]}"}
    return score(json.loads(path.read_text()), case)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--cases", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--out", default="runs")
    a = p.parse_args()
    cases = json.loads(Path(a.cases).read_text())
    results = [run_case(c, a.model, Path(a.out).resolve()) for c in cases]
    for r in results:
        print(f"{r['incident_id']}: {'CORRECT' if r['correct'] else 'NOT CORRECT'} [{r['status']}] {r['reason']}")
    ok = sum(r["correct"] for r in results)
    print(f"correct / tested: {ok} / {len(results)}  (CP2 needs 3 mentor-confirmed correct)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
