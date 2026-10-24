"""Deterministic fakes for scenario tests: scripted telemetry (before/after an action), scripted reasoner and planner,
and a recording Action API client. No network, no Anthropic API."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import telemetry
from actions import ActionResult, load_catalog
from contracts import CallToolDecision, FinishDecision, InconclusiveDecision, ToolCall, ToolPolicy
from integration import EvidenceRegistry
from models import ConfidenceLabel, Diagnosis, Incident, TimeWindow
from orchestrator import Dependencies, InvestigationConfig, investigate
from response import ResponseConfig, ResponseDeps
from safety import AuditJournal, KillSwitch

NOW = datetime.now(timezone.utc)
MOCK_CATALOG = load_catalog(Path(__file__).parent / "fixtures" / "mock_action_catalog.json")
SERVICES = ["ad", "cart", "checkout", "currency", "email", "frontend", "load-generator", "payment",
            "product-catalog", "quote", "recommendation", "shipping", "valkey-cart"]


class Telemetry:
    """run_tool fake. `tools[(name, service)]` -> (status, value, extra) for investigation tools; `metrics[phase][(service,
    metric)]` -> number or None for query_metrics. The phase switches to 'after' when the fake action takes effect."""

    def __init__(self, tmp: Path, tools: dict | None = None, before: dict | None = None, after: dict | None = None):
        self.tmp, self.tools, self.phase, self.n, self.calls = tmp, tools or {}, "before", 0, []
        self.metrics = {"before": before or {}, "after": after if after is not None else (before or {})}

    def __call__(self, name: str, args: Optional[dict]) -> telemetry.ToolResult:
        args = args or {}
        self.calls.append((name, dict(args)))
        svc = args.get("service")
        if name == "query_metrics" and (svc, args.get("metric")) in self.metrics[self.phase]:
            v = self.metrics[self.phase][(svc, args["metric"])]
            status, value, extra = ("ok", {"metric": args["metric"], "value": v}, {}) if v is not None else ("empty", {"metric": args["metric"]}, {})
        else:
            status, value, extra = self.tools.get((name, svc), self.tools.get((name, None), ("empty", None, {})))
        self.n += 1
        eid = f"ev-{name}-{self.phase}-{self.n:04d}"
        path = self.tmp / f"{eid}.json"
        path.write_text("{}")
        return telemetry.ToolResult(status=status, evidence_id=eid, tool=name,
                                    source="jaeger" if name == "search_traces" else "opensearch" if name == "query_logs" else "prometheus",
                                    service=svc, query=f"{name} {args}", window_start=NOW - timedelta(minutes=5), window_end=NOW,
                                    collected_at=NOW, value=value, capture_ref=str(path), **extra)


class Script:
    def __init__(self, *decisions):
        self.decisions, self.contexts = list(decisions), []

    def next_step(self, ctx):
        self.contexts.append(ctx)
        d = self.decisions.pop(0) if len(self.decisions) > 1 else self.decisions[0]
        if callable(d):
            d = d(ctx)
        if isinstance(d, BaseException):
            raise d
        return d


class Planner:
    def __init__(self, *plans):
        self.plans, self.calls = list(plans), []

    def plan(self, incident, diagnosis, evidence, catalog, inventory, feedback=None):
        self.calls.append(feedback)
        p = self.plans.pop(0) if len(self.plans) > 1 else self.plans[0]
        p = p(evidence) if callable(p) else p
        if isinstance(p, BaseException):
            raise p
        return p


class Client:
    """Recording Action API client; `effect` runs on execute to simulate what the action does to telemetry."""

    def __init__(self, tel: Optional[Telemetry] = None, result: Optional[ActionResult] = None, effect: bool = True):
        self.tel, self.result, self.effect, self.calls = tel, result or ActionResult(outcome="success", http_status=202, request_id="req-1"), effect, []

    def execute(self, op, target, arguments, idempotency_key):
        self.calls.append((op.name, target, arguments, idempotency_key))
        if self.tel and self.effect:
            self.tel.phase = "after"
        return self.result


def calls(*specs) -> CallToolDecision:
    return CallToolDecision(calls=[ToolCall(call_id=f"toolu_{i}_{n}", tool_name=n, arguments=a) for i, (n, a) in enumerate(specs)])


def finish_citing(component: str, cause: str, tools: list[str], confidence=ConfidenceLabel.MEDIUM, **kw):
    """Finish decision citing every captured evidence item produced by the named tools."""
    def build(ctx):
        ids = [e.evidence_id for e in ctx.evidence if e.tool in tools and e.status.value in ("ok", "empty")]
        return FinishDecision(diagnosis=Diagnosis(suspected_component=component, suspected_cause=cause, supporting_evidence_ids=ids,
                                                  uncertainty="mocked", confidence_label=confidence, next_step="remediate", **kw))
    return build


def inconclusive(reason="conflicting"):
    return InconclusiveDecision(reason=reason)


def incident(service: Optional[str] = None, **kw) -> Incident:
    base = dict(id="INC-T", source="test", symptom="symptom", affected_service=service, detection_time=NOW,
                observation_window=TimeWindow(start_time=NOW - timedelta(minutes=15), end_time=NOW), recovery_condition="errors < 5%")
    base.update(kw)
    return Incident(**base)


def run_investigation(tmp: Path, tel: Telemetry, *decisions, service: Optional[str] = None):
    registry = EvidenceRegistry()
    policy = ToolPolicy(approved_services=SERVICES, arg_models={**{n: s[1] for n, s in telemetry.TOOLS.items()},
                                                                 "resource_overview": __import__("resources").ResourceArgs})
    deps = Dependencies(reasoner=Script(*decisions), run_tool=tel, policy=policy, registry=registry)
    return investigate(incident(service), InvestigationConfig(), deps), registry


def response_deps(tmp: Path, tel: Telemetry, registry, planner, *, client=None, answer: str = "", interactive=True,
                  catalog=MOCK_CATALOG, kill: bool = False) -> ResponseDeps:
    ks = KillSwitch(tmp / "KILL")
    ks.set(kill)
    prompts: list[str] = []

    def input_fn(_prompt):
        if callable(answer):
            return answer(prompts)
        return answer

    return ResponseDeps(planner=planner, catalog=catalog, run_tool=tel, registry=registry, inventory=SERVICES, kill_switch=ks,
                        journal=AuditJournal(tmp / "audit.jsonl"), lock_path=tmp / ".lock", approver="tester",
                        interactive=interactive, input_fn=input_fn, print_fn=prompts.append, client=client,
                        sleep=lambda s: None)


def approve_shown_code(prompts: list[str]) -> str:
    """Type the approval code exactly as a human would read it from the prompt."""
    line = next(p for p in prompts if "approve " in p)
    return "approve " + line.split("approve ", 1)[1].split()[0]


FAST = ResponseConfig(mode="live", settle_s=0, interval_s=0, samples=2)
DRY = ResponseConfig(mode="dry_run", settle_s=0, interval_s=0)
