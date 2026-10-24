"""Replay the deterministic evidence picture over saved runs (no LLM, no live demo).

    python evals/replay_ranking.py [--truth evals/replay_truth.local.json] [--runs runs]

truth file (operator-held, git-ignored, never read by the agent):
    {"INC-BLIND-2": "cart", "INC-LIVE-003": null, ...}      # null = no real incident expected
Shows, per run: the injected origin, the top-ranked candidates, and what the LLM answered then.
Older runs were collected by older tool versions, so some evidence (log_overview, per-operation data) is missing.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import candidates  # noqa: E402
from models import IncidentReport  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--truth", default=str(Path(__file__).with_name("replay_truth.local.json")))
    p.add_argument("--runs", default=str(Path(__file__).resolve().parents[1] / "runs"))
    a = p.parse_args()
    truth = json.loads(Path(a.truth).read_text(encoding="utf-8-sig"))
    hits = total = 0
    for run_id, expected in truth.items():
        path = Path(a.runs) / run_id / f"{run_id}_report.json"
        if not path.is_file():
            print(f"{run_id}: no saved report")
            continue
        rep = IncidentReport.model_validate_json(path.read_text(encoding="utf-8"))
        ranked = candidates.rank(rep.evidence)
        top = ranked[0] if ranked else None
        if expected is None:
            ok = top is None or top.tier <= 1
            verdict = "OK (weak/no evidence)" if ok else f"FALSE ALARM risk: {top.service} tier {top.tier}"
        else:
            ok = top is not None and top.service == expected
            verdict = "TOP=TRUTH" if ok else "MISS"
        hits += ok
        total += 1
        llm = rep.diagnosis.suspected_component if rep.diagnosis else rep.status.value
        tools = sorted({e.tool for e in rep.evidence})
        print(f"{run_id:14} truth={expected or 'none':16} {verdict:28} llm-said={llm[:40]}")
        for x in ranked[:3]:
            print(f"      {x.service:16} tier {x.tier} sources={len(x.sources)} onset={x.newest_min} "
                  f"direct={len(x.direct)} current={len(x.current)} old={len(x.old)}"
                  + (" symptom" if x.mostly_symptom else "") + (" started-before" if x.started_before else ""))
        if "log_overview" not in tools:
            print("      (old run: no log_overview evidence)")
    print(f"\ntop candidate = truth: {hits} / {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
