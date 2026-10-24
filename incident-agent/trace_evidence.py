"""Tools only, no LLM: run the agent's tools in the order the prompt asks for and show, step by step, what
evidence the model would receive. Use it to check whether a wrong diagnosis comes from the tools (evidence
missing or misleading) or from the model (evidence was there, the model misread it).

    python trace_evidence.py [--id TRACE-1] [--max-suspects 5]

Writes runs/<id>/llm_payloads/NN_<tool>_<service>.json = the exact text sent to the model for that call.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from integration import make_telemetry_runner  # noqa: E402


def lead(text: str) -> str:
    return re.split(r'[\s:"]', str(text).strip(), maxsplit=1)[0]


def show(n: int, name: str, args: dict, res, out: Path, seconds: float) -> dict:
    payload = res.for_llm()
    svc = args.get("service", "fleet")
    path = out / f"{n:02d}_{name}_{svc}.json"
    path.write_text(payload, encoding="utf-8")
    print(f"\n[{n:02d}] {name}({', '.join(f'{k}={v}' for k, v in args.items()) or ''})")
    print(f"     status={res.status.value}  evidence_id={res.evidence_id}  {seconds:.1f}s  "
          f"sent to LLM: {len(payload)} chars (~{len(payload) // 4} tokens) -> {path.name}")
    if res.message:
        print(f"     message: {res.message}")
    return res.value if isinstance(res.value, dict) else {}


def bullet(label: str, items) -> None:
    if not items:
        return
    items = items if isinstance(items, list) else [items]
    print(f"     {label}:")
    for i in items[:12]:
        print(f"       - {i if isinstance(i, str) else json.dumps(i, default=str)}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--id", default=f"TRACE-{time.strftime('%H%M%S')}")
    p.add_argument("--max-suspects", type=int, default=5)
    a = p.parse_args()
    run_dir = Path("runs").resolve() / a.id
    out = run_dir / "llm_payloads"
    out.mkdir(parents=True, exist_ok=True)
    run_tool, policy, _ = make_telemetry_runner(run_dir / "evidence")
    n = 0
    results = []

    def call(name: str, **args) -> dict:
        nonlocal n
        n += 1
        t0 = time.time()
        res = run_tool(name, args)
        results.append(res)
        return show(n, name, args, res, out, time.time() - t0)

    print(f"=== {a.id}: STEP 1, fleet-wide (what the model sees first) ===")
    ov = call("overview")
    bullet("changed now (service-wide)", ov.get("changed_since_previous_window"))
    bullet("changed now (single operations)", ov.get("operations_changed"))
    bullet("recovering (earlier incident ending)", ov.get("recovering_since_previous_window"))
    bullet("operations recovering", ov.get("operations_recovering"))
    ro = call("resource_overview")
    bullet("resource flags", [f'{f["container"]}: {"; ".join(f["reasons"])}' for f in ro.get("flagged", [])] or "none")
    lo = call("log_overview")
    now = lo.get("logging_now") if isinstance(lo.get("logging_now"), list) else []
    bullet("log messages now", [f'{m["service"]} [{m["severity"]}] "{m["message"]}" x{m["count_now"]} '
                                f'first {m["first_seen_min_ago"]} min ago{"  <- STARTED RECENTLY" if m["started_recently"] else "  (baseline)"}'
                                for m in now] or "none")
    bullet("log messages that stopped (earlier incident)",
           [f'{m["service"]} "{m["message"]}" last {m["last_seen_min_ago"]} min ago' for m in lo.get("stopped_earlier", [])])

    # Suspects = what the prompt tells the model to follow up, in priority order.
    suspects: list[tuple[str, str]] = []
    def add(svc: str, why: str) -> None:
        if svc in policy.approved_services and svc not in [s for s, _ in suspects]:
            suspects.append((svc, why))
    for m in now:
        if m["started_recently"]:
            add(m["service"], "new log message")
    for x in ov.get("operations_changed") or []:
        add(lead(x), "operation changed")
    for x in ov.get("changed_since_previous_window") or []:
        add(lead(x), "service changed")
    for f in ro.get("flagged", []):
        add(f["container"], "resource flag")
    suspects = suspects[:a.max_suspects]
    print(f"\n=== STEP 2, follow-up of suspects: {', '.join(f'{s} ({w})' for s, w in suspects) or 'none'} ===")

    origins, slow = {}, {}
    for svc, _ in suspects:
        tr = call("search_traces", service=svc)
        for k, v in (tr.get("error_origin_counts") or {}).items():
            origins[k] = origins.get(k, 0) + v
        for k, v in (tr.get("latency_origin_counts") or {}).items():
            slow[k] = slow.get(k, 0) + v
        bullet("error origins", tr.get("error_origin_counts"))
        bullet("slow-path origins", tr.get("latency_origin_counts"))
        bullet("calls with no response from", tr.get("calls_with_no_response_from"))
        for t in (tr.get("traces") or [])[:2]:
            o, lat = t.get("suspected_origin") or {}, t.get("latency_origin") or {}
            print(f'       trace {t["trace_id"][:12]} age {t["age_s"]}s {t["duration_ms"]}ms: origin={o.get("service")} '
                  f'"{o.get("operation")}" err="{str(o.get("error_message"))[:90]}"'
                  + (f' | slow={lat.get("service")} "{lat.get("operation")}" {lat.get("duration_ms")}ms {lat.get("certainty", "")}' if lat else ""))
        lg = call("query_logs", service=svc)
        for line in (lg.get("lines") or [])[:4]:
            print(f'       log [{line["severity"]}] {str(line["body"])[:140]}')
        h = call("service_health", service=svc)
        s = h.get("summary") or {}
        print("       health: " + ", ".join(f"{k}={v}" for k, v in s.items() if not isinstance(v, (dict, list))))

    import candidates
    from integration import to_evidence  # noqa: F401  (evidence objects come from the saved results below)
    evs = [to_evidence(r) for r in results]
    picture = candidates.summary(evs, known=set(policy.approved_services))
    (out / "evidence_picture.txt").write_text(picture, encoding="utf-8")
    print("\n=== EVIDENCE PICTURE (what the LLM is shown each round) ===")
    print(picture or "(no candidates)")
    print("\n=== WHAT THE EVIDENCE ALONE POINTS AT (before any LLM) ===")
    print(f"  trace error origins (all suspects): {origins or 'none'}")
    print(f"  trace slow-path origins:            {slow or 'none'}")
    print(f"  new log messages:                   {[m['service'] for m in now if m['started_recently']] or 'none'}")
    print(f"  exact LLM payloads: {out}")


if __name__ == "__main__":
    main()
