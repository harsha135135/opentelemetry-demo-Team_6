"""Step 1: threshold detection. Invented service names: thresholds are generic."""
import detector as d


def table(**svcs):
    return {name: m for name, m in svcs.items()}


def test_error_and_latency_thresholds_against_own_baseline():
    t = table(**{"svc-a": {"err": 0.30, "err_base": 0.01, "rate": 0.5},        # errors jumped (60 requests)
                 "svc-b": {"err": 0.06, "err_base": 0.05, "rate": 5.0},        # high but normal for it
                 "svc-g": {"err": 0.04, "err_base": 0.0, "rate": 7.0},         # partial fault: 4% from ~0
                 "svc-c": {"p95": 4200.0, "p95_base": 900.0, "rate": 2.0},     # latency 4.7x
                 "svc-f": {"p95": 1900.0, "p95_base": 500.0, "rate": 0.03},    # 4x, but from ~5 requests
                 "svc-d": {"p95": 1500.0, "p95_base": 1000.0},                 # slow but its usual
                 "svc-e": {"err": 0.50, "err_base": 0.0, "rate": 0.01},        # 1 failed call: a blip
                 "svc-h": {"err": 0.667, "err_base": 0.167, "rate": 0.05}})    # 4 of 6 requests: too few
    found = {(b.service, b.text.split()[0]) for b in d.breaches(t)}
    assert found == {("svc-a", "error"), ("svc-c", "p95"), ("svc-g", "error")}


def test_needs_two_checks_then_cools_down_but_new_services_still_alert():
    now = [0.0]
    det = d.Detector(clock=lambda: now[0])
    a = [d.Breach("svc-a", "x")]
    assert det.check(a) == []                     # first sighting: could be a blip
    assert [b.service for b in det.check(a)] == ["svc-a"]
    now[0] = 60
    both = [d.Breach("svc-a", "x"), d.Breach("svc-b", "y")]
    det.check(both)                               # svc-b first sighting; svc-a in cooldown
    assert [b.service for b in det.check(both)] == ["svc-b"]  # a second fault overlapping the first still alerts
    now[0] = d.COOLDOWN_S + 1                    # svc-a cooled down; svc-b (alerted at 60 s) still quiet
    assert [b.service for b in det.check(both)] == ["svc-a"]  # still breaching after its cooldown: alert again


def test_alert_text_states_values_not_causes():
    text = d.alert_text([d.Breach("svc-a", "error rate of svc-a is 30.0% (its 30-min baseline 1.0%)")])
    assert text.startswith("Threshold alert: ") and "30.0%" in text
