"""Evidence picture: rank candidate root-cause services from the evidence collected so far.

Generic by design: no service, operation or fault names. Every service that appears in the evidence is a
candidate; what counts is the kind of evidence (direct, current, old, symptom-of-another) and where it came from.
The ranking is an explainable summary for the LLM and for one consistency check, not a verdict.

Evidence tiers (highest first):
  direct   - failing spans originate in this service, calls to it went unanswered, or it shows a crash loop /
             sudden resource jump (resource flag severity >= 3)
  current  - its errors / latency / operations changed in the current window, or a log message of its started
             recently
  old      - only fading signals: recovering, log messages that stopped or have been going on for a long time,
             restarts that ended
  symptom  - its failures are explained by another service (its failing traces originate elsewhere)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

STRONG_RESOURCE_SEVERITY = 3
SOURCE_OF = {"overview": "prometheus", "resource_overview": "prometheus", "query_metrics": "prometheus",
             "service_health": "prometheus", "search_traces": "jaeger", "query_logs": "opensearch",
             "log_overview": "opensearch"}


@dataclass
class Candidate:
    service: str
    direct: list[str] = field(default_factory=list)
    current: list[str] = field(default_factory=list)
    old: list[str] = field(default_factory=list)
    symptom_of: dict[str, int] = field(default_factory=dict)
    own_origin_traces: int = 0
    sources: set[str] = field(default_factory=set)
    evidence_ids: set[str] = field(default_factory=set)
    newest_min: float | None = None  # onset: minutes since its newest log message first appeared
    old_logs_only: bool = False      # it logs, but every message started long ago or has stopped

    @property
    def started_before(self) -> bool:
        """Its problem began well before the current window (or already ended): a leftover or an older issue."""
        return self.old_logs_only and self.newest_min is None

    def add(self, kind: str, text: str, ev: Any) -> None:
        getattr(self, kind).append(text)
        if kind in ("direct", "current"):
            self.sources.add(SOURCE_OF.get(ev.tool, ev.source))
        self.evidence_ids.add(ev.evidence_id)

    def seen(self, minutes: float | None) -> None:
        if minutes is not None:
            self.newest_min = minutes if self.newest_min is None else min(self.newest_min, minutes)

    @property
    def mostly_symptom(self) -> bool:
        return bool(self.symptom_of) and sum(self.symptom_of.values()) > self.own_origin_traces

    @property
    def only_old(self) -> bool:
        return bool(self.old) and not self.direct and not self.current

    @property
    def tier(self) -> int:
        """4 direct + current, 3 direct, 2 current change seen by 2+ sources, 1 current (one source),
        0 old / symptom only. Direct causal evidence outranks corroborated side effects."""
        if self.only_old:
            return 0
        if self.direct:
            t = 4 if self.current else 3
        else:
            t = (2 if len(self.sources) >= 2 else 1) if self.current else 0
        if self.mostly_symptom:
            t = max(t - 1, 0)
        if self.started_before:
            t = max(t - 1, 0)
        return t

    def key(self) -> tuple:
        return (self.tier, len(self.sources), -(self.newest_min if self.newest_min is not None else 1e9))


def _value(ev: Any) -> dict:
    v = (ev.observed_value or {}).get("value")
    return v if isinstance(v, dict) else {}


def _trace_groups(v: dict) -> list[tuple[dict, dict, list[str], int]]:
    """(origin, latency origin, services with error spans, number of traces) from search_traces output:
    patterns when present (each with a count), otherwise individual traces (older runs)."""
    if v.get("patterns"):
        return [({"service": p.get("origin_service"), "timed_out_waiting": p.get("timed_out_waiting")},
                 p.get("latency_origin") or {}, p.get("error_services") or [], p.get("count") or 1)
                for p in v["patterns"]]
    return [(t.get("suspected_origin") or {}, t.get("latency_origin") or {},
             [e.get("service") for e in t.get("error_spans") or [] if e.get("service")], 1)
            for t in v.get("traces") or []]


def _lead(text: str) -> str:
    return re.split(r'[\s:"]', str(text).strip(), maxsplit=1)[0]


def observed_services(evidence: Iterable[Any]) -> set[str]:
    """Services the telemetry itself reports (per-service evidence, overview rows, container rows)."""
    out: set[str] = set()
    for ev in evidence:
        if ev.service:
            out.add(ev.service)
        v = _value(ev)
        out.update(r.get("service") for r in v.get("services") or [] if isinstance(r, dict))
        rows = (v.get("containers") or {}).get("rows") if isinstance(v.get("containers"), dict) else None
        out.update(r[0] for r in rows or [] if r)
        now = v.get("logging_now") if isinstance(v.get("logging_now"), list) else []
        out.update(m.get("service") for m in now + list(v.get("stopped_earlier") or []))
    out.discard(None)
    return out


def build(evidence: Iterable[Any], known: set[str] | None = None) -> dict[str, Candidate]:
    """known: the service inventory (from telemetry). Names outside it (e.g. a raw URL of a call target that
    is not a service in telemetry) are not candidates."""
    evidence = list(evidence)
    known = known or observed_services(evidence)
    cands: dict[str, Candidate] = {}

    def c(svc: str | None) -> Candidate | None:
        if not svc or svc == "unknown" or svc not in known:
            return None
        return cands.setdefault(svc, Candidate(svc))

    for ev in evidence:
        if ev.status.value != "ok":
            continue
        v = _value(ev)
        if ev.tool == "overview":
            for key in ("changed_since_previous_window", "operations_changed"):
                for x in v.get(key) or []:
                    x = str(x)
                    if x.startswith(("NO BASELINE", "no ")):
                        continue
                    if (cand := c(_lead(x))) is not None:
                        cand.add("current", x[:160], ev)
            for key in ("recovering_since_previous_window", "operations_recovering"):
                for x in v.get(key) or []:
                    if (cand := c(_lead(str(x)))) is not None:
                        cand.add("old", f"recovering: {str(x)[:140]}", ev)
        elif ev.tool == "resource_overview":
            for f in v.get("flagged") or []:
                if (cand := c(f.get("container"))) is None:
                    continue
                text = "; ".join(f.get("reasons") or [])
                strong = (f.get("severity") or 0) >= STRONG_RESOURCE_SEVERITY
                cand.add("direct" if strong else "current", f"resources: {text[:160]}", ev)
            for x in v.get("restarts_ended") or []:
                if (cand := c(_lead(str(x)))) is not None:
                    cand.add("old", f"restarts ended: {str(x)[:120]}", ev)
        elif ev.tool == "log_overview":
            now = v.get("logging_now") if isinstance(v.get("logging_now"), list) else []
            for m in now:
                if (cand := c(m.get("service"))) is None:
                    continue
                first = m.get("first_seen_min_ago")
                msg = f'log "{str(m.get("message"))[:90]}" x{m.get("count_now")}'
                if m.get("started_recently"):
                    cand.add("current", f"{msg}, first seen {first} min ago", ev)
                    cand.seen(first)
                else:
                    # The same message that began long ago is still arriving: what fails now is the older problem.
                    cand.add("old", f"{msg}, ongoing for {first}+ min (began before the current incident)", ev)
                    cand.old_logs_only = True
            for m in v.get("stopped_earlier") or []:
                # A problem that ended says nothing against a new one in the same service: listed, not demoted.
                if (cand := c(m.get("service"))) is not None:
                    cand.add("old", f'log "{str(m.get("message"))[:90]}" stopped {m.get("last_seen_min_ago")} min ago', ev)
        elif ev.tool == "search_traces":
            # Origins per trace: a span that failed because it waited too long (timeout) is a symptom of the
            # service where the time went, not an origin.
            origins: dict[str, int] = {}
            groups = _trace_groups(v)
            for o, slow, errs, n in groups:
                if not o.get("service"):
                    continue
                if o.get("timed_out_waiting") and slow.get("service"):
                    if (cand := c(o["service"])) is not None:
                        cand.symptom_of[slow["service"]] = cand.symptom_of.get(slow["service"], 0) + n
                        cand.evidence_ids.add(ev.evidence_id)
                    continue
                origins[o["service"]] = origins.get(o["service"], 0) + n
                for s in set(errs) - {o["service"]}:  # failing upstream of the origin: a symptom of it
                    if (cand := c(s)) is not None:
                        cand.symptom_of[o["service"]] = cand.symptom_of.get(o["service"], 0) + n
                        cand.evidence_ids.add(ev.evidence_id)
            if not groups:
                origins = dict(v.get("error_origin_counts") or {})
            for svc, n in origins.items():
                if (cand := c(svc)) is not None:
                    cand.own_origin_traces += n
                    cand.add("direct", f"failing spans originate here in {n} trace(s) (searched from {ev.service})", ev)
            for svc, n in (v.get("calls_with_no_response_from") or {}).items():
                if (cand := c(svc)) is not None:
                    cand.add("direct", f"{n} call(s) to it got no response (searched from {ev.service})", ev)
            for svc, n in (v.get("latency_origin_counts") or {}).items():
                name = svc.replace(" (unexplained)", "")
                if (cand := c(name)) is not None:
                    tag = " (time not explained by finished spans)" if name != svc else ""
                    cand.add("current", f"slow path ends here in {n} trace(s){tag}", ev)
        elif ev.tool == "query_logs" and ev.service:
            if (cand := c(ev.service)) is not None and (v.get("count") or 0) > 0:
                lines = v.get("lines") or []
                cand.add("current", f'{v["count"]} WARN+ log line(s) in window, e.g. "{str(lines[0].get("body"))[:80]}"'
                         if lines else f'{v["count"]} WARN+ log line(s) in window', ev)
    return cands


def rank(evidence: Iterable[Any], known: set[str] | None = None) -> list[Candidate]:
    return sorted((x for x in build(evidence, known).values() if x.direct or x.current or x.old or x.symptom_of),
                  key=lambda x: x.key(), reverse=True)


TIER_NAME = {4: "direct + current", 3: "direct", 2: "current change, 2+ sources", 1: "current, one source",
             0: "old / symptom only"}


def summary(evidence: Iterable[Any], limit: int = 6, known: set[str] | None = None) -> str:
    """Compact text for the LLM: ranked candidates with their reasons and evidence ids."""
    ranked = rank(evidence, known)
    if not ranked:
        return ""
    lines = ["EVIDENCE PICTURE (computed from the evidence collected so far; a summary to check, not a verdict):"]
    for i, x in enumerate(ranked[:limit], 1):
        lines.append(f"{i}. {x.service} [tier {x.tier}: {TIER_NAME[x.tier]}; sources: {', '.join(sorted(x.sources)) or '-'}"
                     + (f"; its newest log message began {x.newest_min} min ago" if x.newest_min is not None else "")
                     + ("; its log messages all began long ago or stopped (lowered one tier)" if x.started_before else "")
                     + ("; mostly a symptom of another service (lowered one tier)" if x.mostly_symptom else "") + "]")
        for label, items in (("direct", x.direct), ("current", x.current), ("old", x.old)):
            for it in items[:3]:
                lines.append(f"   {label}: {it}")
        if x.symptom_of:
            lines.append("   symptom: its failing traces originate in " +
                         ", ".join(f"{s} ({n})" for s, n in sorted(x.symptom_of.items(), key=lambda kv: -kv[1])))
        lines.append(f"   evidence: {', '.join(sorted(x.evidence_ids)[:6])}")
    lines.append("Several candidates in the top tier = possibly several active problems: name the best-supported one, "
                 "list the others under alternatives. A top tier of 1 or 0 = weak evidence: low confidence or inconclusive.\n"
                 "Tiers: 4 direct + current, 3 direct (failures originate there, calls to it unanswered, crash loop or "
                 "sudden resource jump), 2 current change seen by 2+ sources, 1 current change from one source, 0 old or "
                 "only a symptom of another service.")
    return "\n".join(lines)


def mentioned(component: str, ranked: list[Candidate]) -> list[Candidate]:
    comp = component.lower()
    return [x for x in ranked if re.search(rf"(?<![\w-]){re.escape(x.service.lower())}(?![\w-])", comp)]


def consistency(diagnosis: Any, evidence: Iterable[Any], known: set[str] | None = None) -> str | None:
    """Why the diagnosis looks inconsistent with the evidence picture, or None. Fires only when a clearly
    better-supported candidate exists (a higher tier), so ties and genuinely concurrent problems are accepted."""
    evidence = list(evidence)
    ranked = rank(evidence, known)
    if not ranked:
        return None
    named = mentioned(diagnosis.suspected_component, ranked)
    if not named:
        # A known service with no evidence of its own counts as the lowest tier; a component outside the
        # inventory (e.g. a network or host issue) is left to the other gates.
        inventory = known or observed_services(evidence)
        named = mentioned(diagnosis.suspected_component, [Candidate(s) for s in inventory])
        if not named:
            return None
    best_named = max(named, key=lambda x: x.key())
    top = ranked[0]
    if top.tier <= best_named.tier or top.service in {x.service for x in named}:
        return None
    why = "; ".join((top.direct + top.current)[:3])
    weak = "; ".join((best_named.old + best_named.current)[:2]) or "little evidence"
    extra = (f" Its failing traces originate in {', '.join(best_named.symptom_of)}." if best_named.mostly_symptom else "")
    return (f"the evidence picture ranks {top.service} (tier {top.tier}: {why}) above {best_named.service} "
            f"(tier {best_named.tier}: {weak}).{extra}")
