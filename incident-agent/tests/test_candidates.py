"""Evidence picture: generic ranking. Service names are invented on purpose: nothing in candidates.py may know
any real service, operation or fault, so these cases stand in for hidden scenarios."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import candidates
from models import ConfidenceLabel, Diagnosis

NOW = datetime.now(timezone.utc)
KNOWN = {"gateway", "svc-a", "svc-b", "svc-c", "db-x"}


def ev(tool, value, service=None, source="prometheus", i=[0]):
    i[0] += 1
    return SimpleNamespace(evidence_id=f"ev-{tool}-{i[0]}", tool=tool, service=service, source=source,
                           status=SimpleNamespace(value="ok"), observed_value={"value": value})


def diag(component):
    return Diagnosis(suspected_component=component, suspected_cause="c", supporting_evidence_ids=["x"],
                     uncertainty="u", confidence_label=ConfidenceLabel.LOW, next_step="n")


def trace(origin, errs, slow=None, timed_out=False):
    return {"suspected_origin": {"service": origin, **({"timed_out_waiting": True} if timed_out else {})},
            "latency_origin": {"service": slow} if slow else None,
            "error_spans": [{"service": s} for s in errs], "age_s": 30}


def test_direct_origin_outranks_its_callers_symptoms():
    evidence = [
        ev("overview", {"changed_since_previous_window": ["svc-a: error_rate up 0.000 -> 0.300",
                                                           "svc-b: error_rate up 0.000 -> 0.500"]}),
        ev("search_traces", {"traces": [trace("svc-b", ["svc-a", "svc-b"])] * 3,
                             "error_origin_counts": {"svc-b": 3}}, service="svc-a", source="jaeger"),
    ]
    ranked = candidates.rank(evidence, KNOWN)
    assert ranked[0].service == "svc-b" and ranked[0].tier == 4
    a = next(x for x in ranked if x.service == "svc-a")
    assert a.mostly_symptom and a.tier < ranked[0].tier
    assert "svc-b" in candidates.consistency(diag("svc-a"), evidence, KNOWN)  # blaming the caller is re-checked


def test_timeout_is_a_symptom_of_where_the_time_went():
    evidence = [ev("search_traces", {"traces": [trace("gateway", ["gateway"], slow="db-x", timed_out=True)] * 2,
                                     "latency_origin_counts": {"db-x": 2}}, service="gateway", source="jaeger")]
    ranked = {x.service: x for x in candidates.rank(evidence, KNOWN)}
    assert not ranked["gateway"].direct and ranked["gateway"].symptom_of == {"db-x": 2}
    assert ranked["db-x"].current


def test_problem_that_began_long_ago_ranks_below_one_that_just_started():
    logs = ev("log_overview", {"logging_now": [
        {"service": "svc-c", "message": "old failure", "count_now": 3, "first_seen_min_ago": 40, "started_recently": False},
        {"service": "svc-a", "message": "new failure", "count_now": 3, "first_seen_min_ago": 1.5, "started_recently": True}]},
        source="opensearch")
    traces = ev("search_traces", {"traces": [trace("svc-c", ["svc-c"]), trace("svc-a", ["svc-a"])],
                                  "error_origin_counts": {"svc-c": 1, "svc-a": 1}}, service="gateway", source="jaeger")
    ranked = candidates.rank([logs, traces], KNOWN)
    assert ranked[0].service == "svc-a"
    c = next(x for x in ranked if x.service == "svc-c")
    assert c.started_before and c.tier == 2  # direct (3) lowered one tier, still listed (not removed)


def test_a_problem_that_stopped_does_not_hide_a_new_one_in_the_same_service():
    logs = ev("log_overview", {"logging_now": [], "stopped_earlier": [
        {"service": "svc-b", "message": "earlier failure", "last_seen_min_ago": 9}]}, source="opensearch")
    res = ev("resource_overview", {"flagged": [{"container": "svc-b", "reasons": ["cpu 340% is 70x its prior"],
                                                "severity": 3}]})
    b = candidates.rank([logs, res], KNOWN)[0]
    assert b.service == "svc-b" and b.tier == 3 and not b.started_before


def test_concurrent_problems_with_equal_evidence_are_not_overridden():
    evidence = [ev("search_traces", {"traces": [trace("svc-a", ["svc-a"]), trace("svc-b", ["svc-b"])],
                                     "error_origin_counts": {"svc-a": 1, "svc-b": 1}}, service="gateway", source="jaeger"),
                ev("overview", {"changed_since_previous_window": ["svc-a: error_rate up 0 -> 0.2",
                                                                   "svc-b: error_rate up 0 -> 0.2"]})]
    assert candidates.consistency(diag("svc-b"), evidence, KNOWN) is None
    assert candidates.consistency(diag("svc-a"), evidence, KNOWN) is None


def test_call_targets_outside_the_inventory_are_not_candidates():
    evidence = [ev("search_traces", {"calls_with_no_response_from": {"http://10.0.0.9:8010/x": 5}, "traces": []},
                   service="gateway", source="jaeger")]
    assert candidates.rank(evidence, KNOWN) == []


def test_weak_evidence_is_labelled_weak():
    evidence = [ev("overview", {"operations_changed": ['svc-a "GET /x": p95 up 500 -> 1200 ms']})]
    top = candidates.rank(evidence, KNOWN)[0]
    assert top.tier == 1
    assert "weak evidence" in candidates.summary(evidence, known=KNOWN)


def test_nothing_named_from_the_inventory_is_left_to_other_gates():
    evidence = [ev("resource_overview", {"flagged": [{"container": "svc-a", "reasons": ["crash loop"], "severity": 4}]})]
    assert candidates.consistency(diag("network partition"), evidence, KNOWN) is None
    assert "svc-a" in candidates.consistency(diag("svc-c"), evidence, KNOWN)  # known but without evidence
