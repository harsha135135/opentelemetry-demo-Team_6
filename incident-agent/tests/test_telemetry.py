"""Unit tests for telemetry.py with mocked backends (no Docker needed).
Run: python -m pytest tests -q
"""
import json

import httpx
import pytest

import telemetry as t


def _use(handler, monkeypatch, tmp_path, inventory=("frontend", "checkout", "payment")):
    monkeypatch.setattr(t, "_client", httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(t, "EVIDENCE_DIR", tmp_path)
    monkeypatch.setattr(t, "_inventory", (t.time.time(), list(inventory)))


def test_rejects_bad_and_unknown_services(monkeypatch, tmp_path):
    _use(lambda r: httpx.Response(200, json={}), monkeypatch, tmp_path)
    for bad in ["payment; drop", "flagd", "PAYMENT", "nosuchservice", ""]:
        with pytest.raises(t.TelemetryInputError):
            t.validate_service(bad)
    with pytest.raises(t.TelemetryInputError):
        t.query_metrics("payment", "rm -rf", 5)
    with pytest.raises(t.TelemetryInputError):
        t.query_metrics("payment", "error_rate", 999)
    with pytest.raises(t.TelemetryInputError):
        t.run_tool("delete_everything", {})


def test_unreachable_backend_is_unavailable_not_healthy(monkeypatch, tmp_path):
    def down(request):
        raise httpx.ConnectError("refused", request=request)
    _use(down, monkeypatch, tmp_path)
    res = t.query_metrics("payment", "error_rate", 5)
    assert res.status == t.ToolStatus.UNAVAILABLE
    assert (tmp_path / f"{res.evidence_id}.json").exists()


def test_nan_error_rate_is_empty(monkeypatch, tmp_path):
    body = {"status": "success", "data": {"result": [{"metric": {}, "value": [0, "NaN"]}]}}
    _use(lambda r: httpx.Response(200, json=body), monkeypatch, tmp_path)
    res = t.query_metrics("payment", "error_rate", 5)
    assert res.status == t.ToolStatus.EMPTY
    assert "cannot judge" in res.message


def test_metric_ok_and_query_uses_template(monkeypatch, tmp_path):
    seen = {}

    def handler(request):
        seen["q"] = request.url.params["query"]
        return httpx.Response(200, json={"status": "success",
                                         "data": {"result": [{"metric": {}, "value": [0, "0.42"]}]}})
    _use(handler, monkeypatch, tmp_path)
    res = t.run_tool("query_metrics", {"service": "payment", "metric": "error_rate", "window_minutes": 5})
    assert res.status == t.ToolStatus.OK and res.value["value"] == 0.42
    assert 'service_name="payment"' in seen["q"] and "STATUS_CODE_ERROR" in seen["q"] and "[5m]" in seen["q"]
    assert "or vector(0)" in seen["q"]  # zero errors with traffic = 0, not empty


def _span(sid, parent, name, error=False, msg="", events=None, kind=None, attrs=None):
    return {"traceId": "0af7651916cd43dd8448eb211c80319c", "spanId": sid, "parentSpanId": parent,
            "name": name, "kind": kind, "startTimeUnixNano": "1000000", "endTimeUnixNano": "51000000",
            "status": {"code": "STATUS_CODE_ERROR", "message": msg} if error else {},
            "events": events or [], "attributes": attrs or [
                {"key": "rpc.grpc.status_code", "value": {"intValue": "13"}},
                {"key": "feature_flag.key", "value": {"stringValue": "secret"}}]}


def _rs(service, spans):
    return {"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]},
            "scopeSpans": [{"spans": spans}]}


def test_traces_find_origin_and_hide_flags(monkeypatch, tmp_path):
    flag_event = {"name": "feature_flag.evaluation", "attributes": []}
    body = {"result": {"resourceSpans": [
        _rs("frontend", [_span("00000000000000a1", "", "POST /api/checkout", True, "500")]),
        _rs("checkout", [_span("00000000000000b2", "00000000000000a1", "PlaceOrder", True, "charge failed",
                               [flag_event])]),
        _rs("payment", [_span("00000000000000c3", "00000000000000b2", "charge", True, "Invalid token")]),
        _rs("flagd", [_span("00000000000000d4", "00000000000000c3", "ResolveBoolean")]),
    ]}}
    _use(lambda r: httpx.Response(200, json=body), monkeypatch, tmp_path)
    res = t.search_traces("checkout", 15)
    assert res.status == t.ToolStatus.OK
    tr = res.value["traces"][0]
    assert tr["suspected_origin"]["service"] == "payment"
    assert res.value["error_origin_counts"] == {"payment": 1}
    assert "checkout -> payment" in res.value["edges"]
    assert res.trace_ids == ["0af7651916cd43dd8448eb211c80319c"]
    assert "feature_flag" not in json.dumps(res.value)
    assert "flagd" not in json.dumps(res.value)


def test_unanswered_call_points_at_callee(monkeypatch, tmp_path):
    # paymentUnreachable shape: checkout's outgoing Charge call fails and payment records nothing
    charge = [{"key": "rpc.method", "value": {"stringValue": "oteldemo.PaymentService/Charge"}}]
    body = {"result": {"resourceSpans": [
        _rs("checkout", [_span("00000000000000b2", "", "oteldemo.CheckoutService/PlaceOrder", True, "failed", kind=2),
                         _span("00000000000000b3", "00000000000000b2", "oteldemo.PaymentService/Charge", True,
                               "name resolver error: produced zero addresses", kind=3, attrs=charge)]),
    ]}}
    _use(lambda r: httpx.Response(200, json=body), monkeypatch, tmp_path)
    res = t.search_traces("checkout", 15)
    origin = res.value["traces"][0]["suspected_origin"]
    # the caller only observed the failure: the callee is named, marked as suspected
    assert origin["service"] == "payment" and origin["certainty"] == "suspected" and origin["observed_in"] == "checkout"
    assert origin["callee_responded"] is False and origin["called_service"] == "payment"
    assert res.value["error_origin_counts"] == {"payment": 1}
    assert res.value["calls_with_no_response_from"] == {"payment": 1}


def test_calls_to_flag_service_never_reach_the_model(monkeypatch, tmp_path):
    # live shape: payment's client call to flagd errors and flagd's own spans are filtered out
    flag_call = [{"key": "rpc.service", "value": {"stringValue": "flagd.evaluation.v2.Service"}}]
    body = {"result": {"resourceSpans": [
        _rs("payment", [_span("00000000000000c3", "", "charge", True, "Invalid token", kind=2),
                        _span("00000000000000c4", "00000000000000c3", "flagd.evaluation.v2.Service/ResolveBoolean",
                              True, "deadline", kind=3, attrs=flag_call)]),
    ]}}
    _use(lambda r: httpx.Response(200, json=body), monkeypatch, tmp_path)
    res = t.search_traces("payment", 15)
    assert res.value["traces"][0]["suspected_origin"]["operation"] == "charge"
    assert "flagd" not in res.for_llm() and "calls_with_no_response_from" not in res.value


@pytest.mark.parametrize("msg", ["Error: Product Catalog Fail Feature Flag Enabled", "feature-flags on", "FEATURE.FLAG x"])
def test_flag_wording_in_error_messages_is_redacted(monkeypatch, tmp_path, msg):
    # productCatalogFailure's real gRPC error text names the flag; the model must not see it
    body = {"result": {"resourceSpans": [
        _rs("product-catalog", [_span("00000000000000d1", "", "oteldemo.ProductCatalogService/GetProduct", True, msg, kind=2)]),
    ]}}
    _use(lambda r: httpx.Response(200, json=body), monkeypatch, tmp_path, inventory=("frontend", "product-catalog"))
    res = t.search_traces("product-catalog", 15)
    origin = res.value["traces"][0]["suspected_origin"]
    assert origin["service"] == "product-catalog" and origin["error_message"] == t.FLAG_REDACTED
    assert "flag" not in res.for_llm().replace(t.FLAG_REDACTED, "").lower()


def test_propagated_flag_text_does_not_hide_the_real_origin(monkeypatch, tmp_path):
    # live productCatalogFailure shape: frontend's GetProduct client span carries the callee's error text
    propagated = [{"key": "rpc.method", "value": {"stringValue": "oteldemo.ProductCatalogService/GetProduct"}},
                  {"key": "rpc.grpc.status_message", "value": {"stringValue": "Error: Product Catalog Fail Feature Flag Enabled"}}]
    body = {"result": {"resourceSpans": [
        _rs("frontend", [_span("00000000000000a1", "", "executing api route (pages) /api/products", True, "13 INTERNAL", kind=1),
                         _span("00000000000000a2", "00000000000000a1", "oteldemo.ProductCatalogService/GetProduct", True,
                               "", kind=3, attrs=propagated)]),
        _rs("product-catalog", [_span("00000000000000b1", "00000000000000a2", "oteldemo.ProductCatalogService/GetProduct",
                                      True, "Error: Product Catalog Fail Feature Flag Enabled", kind=2)]),
    ]}}
    _use(lambda r: httpx.Response(200, json=body), monkeypatch, tmp_path, inventory=("frontend", "product-catalog"))
    res = t.search_traces("product-catalog", 3)
    assert res.value["error_origin_counts"] == {"product-catalog": 1}
    assert "flag" not in res.for_llm().replace(t.FLAG_REDACTED, "").lower()


def test_answered_call_is_not_marked_unreachable(monkeypatch, tmp_path):
    body = {"result": {"resourceSpans": [
        _rs("checkout", [_span("00000000000000b3", "", "oteldemo.PaymentService/Charge", True, "x", kind=3)]),
        _rs("payment", [_span("00000000000000c3", "00000000000000b3", "charge", True, "Invalid token", kind=2)]),
    ]}}
    _use(lambda r: httpx.Response(200, json=body), monkeypatch, tmp_path)
    res = t.search_traces("checkout", 15)
    assert res.value["traces"][0]["suspected_origin"]["service"] == "payment"
    assert "calls_with_no_response_from" not in res.value


def test_overview_reports_changes_not_levels(monkeypatch, tmp_path):
    # recommendation is slow in both windows (baseline); payment's errors jumped; cart stopped
    now = {"request_rate": {"recommendation": 1.4, "payment": 0.2},
           "error_rate": {"recommendation": 0.0, "payment": 0.4},
           "p95_ms": {"recommendation": 4800.0, "payment": 180.0}}
    before = {"request_rate": {"recommendation": 1.4, "payment": 0.2, "cart": 2.0},
              "error_rate": {"recommendation": 0.0, "payment": 0.0, "cart": 0.0},
              "p95_ms": {"recommendation": 4700.0, "payment": 170.0, "cart": 90.0}}

    def handler(request):
        q = request.url.params["query"]
        key = "error_rate" if "STATUS_CODE_ERROR" in q else "p95_ms" if "histogram_quantile" in q else "request_rate"
        data = (before if "offset" in q else now)[key]
        return httpx.Response(200, json={"status": "success", "data": {"result": [
            {"metric": {"service_name": s}, "value": [0, str(v)]} for s, v in data.items()]}})
    _use(handler, monkeypatch, tmp_path)
    res = t.overview(5)
    changed = res.value["changed_since_previous_window"]
    assert any(c.startswith("payment: error_rate up") for c in changed)
    assert any(c.startswith("cart: no traffic now") for c in changed)
    assert not any(c.startswith("recommendation") for c in changed)  # slow, but that is its baseline
    assert res.value["services"][-1]["service"] == "recommendation"


def test_overview_separates_recovering_previous_incident_from_current(monkeypatch, tmp_path):
    # payment's incident was switched off (errors falling); product-catalog's just started
    now = {"error_rate": {"payment": 0.05, "product-catalog": 0.30}, "request_rate": {"payment": 0.2, "product-catalog": 6.0}}
    before = {"error_rate": {"payment": 0.50, "product-catalog": 0.0}, "request_rate": {"payment": 0.2, "product-catalog": 6.0}}

    def handler(request):
        q = request.url.params["query"]
        key = "error_rate" if "STATUS_CODE_ERROR" in q else "p95" if "histogram_quantile" in q else "request_rate"
        data = {} if key == "p95" else (before if "offset" in q else now)[key]
        return httpx.Response(200, json={"status": "success", "data": {"result": [
            {"metric": {"service_name": s}, "value": [0, str(v)]} for s, v in data.items()]}})
    _use(handler, monkeypatch, tmp_path)
    res = t.overview()
    assert res.value["changed_since_previous_window"] == ["product-catalog: error_rate up 0.000 -> 0.300"]
    assert res.value["recovering_since_previous_window"] == ["payment: error_rate down 0.500 -> 0.050"]
    assert res.value["services"][0]["service"] == "product-catalog"  # the current incident ranks first
    assert "[3m]" in res.query  # short "now" window by default


def test_overview_flags_incident_older_than_two_windows_as_ongoing(monkeypatch, tmp_path):
    # product-catalog has failed for ~8 min: same rate now and in the previous window, but not in the 30 min baseline
    rates = {"now": 0.034, "before": 0.033, "baseline": 0.004}

    def handler(request):
        q = request.url.params["query"]
        if "STATUS_CODE_ERROR" not in q:
            return httpx.Response(200, json={"status": "success", "data": {"result": [
                {"metric": {"service_name": "product-catalog"}, "value": [0, "6.0"]}]}})
        which = "baseline" if "[30m]" in q else "before" if "offset" in q else "now"
        return httpx.Response(200, json={"status": "success", "data": {"result": [
            {"metric": {"service_name": "product-catalog"}, "value": [0, str(rates[which])]}]}})
    _use(handler, monkeypatch, tmp_path)
    changed = t.overview().value["changed_since_previous_window"]
    assert changed == ["product-catalog: error_rate elevated 0.034 vs 30 min baseline 0.004 (ongoing)"]


def test_overview_says_no_baseline_instead_of_no_change(monkeypatch, tmp_path):
    def handler(request):
        if "offset" in request.url.params["query"]:  # previous window: nothing (history gap)
            return httpx.Response(200, json={"status": "success", "data": {"result": []}})
        return httpx.Response(200, json={"status": "success", "data": {"result": [
            {"metric": {"service_name": "cart"}, "value": [0, "1.0"]}]}})
    _use(handler, monkeypatch, tmp_path)
    res = t.overview(5)
    assert res.value["baseline_available"] is False and "baseline missing" in res.message
    assert res.value["changed_since_previous_window"][0].startswith("NO BASELINE")


def test_traces_404_is_empty(monkeypatch, tmp_path):
    _use(lambda r: httpx.Response(404, json={"error": "not found"}), monkeypatch, tmp_path)
    assert t.search_traces("payment", 15).status == t.ToolStatus.EMPTY


def test_logs_unconfigured_is_unavailable(monkeypatch, tmp_path):
    _use(lambda r: httpx.Response(200, json={}), monkeypatch, tmp_path)
    monkeypatch.setattr(t, "OPENSEARCH_URL", "")
    assert t.query_logs("payment").status == t.ToolStatus.UNAVAILABLE


def test_logs_parse_and_withhold_flag_lines(monkeypatch, tmp_path):
    body = {"schema": [{"name": "@timestamp"}, {"name": "severity.text"}, {"name": "body"}, {"name": "traceId"}],
            "datarows": [["2026-10-23 09:00:00", "ERROR", "Payment request failed. Invalid token.",
                          "0af7651916cd43dd8448eb211c80319c"],
                         ["2026-10-23 09:00:01", "WARN", "flagd evaluation paymentFailure=1", ""]]}
    _use(lambda r: httpx.Response(200, json=body), monkeypatch, tmp_path)
    monkeypatch.setattr(t, "OPENSEARCH_URL", "http://os:9200")
    res = t.query_logs("payment", 10, "WARN")
    assert res.status == t.ToolStatus.OK and res.value["count"] == 2  # the line is kept, its flag text is not
    assert res.value["lines"][1]["body"] == t.FLAG_REDACTED and "withheld" in res.message
    assert "paymentFailure" not in json.dumps(res.model_dump(mode="json")["value"])


def test_logs_read_nested_severity(monkeypatch, tmp_path):
    body = {"schema": [{"name": "severity"}, {"name": "@timestamp"}, {"name": "body"}, {"name": "traceId"},
                       {"name": "_id"}],
            "datarows": [[{"number": 17, "text": "error"}, "2026-10-23 09:00:00", "Payment request failed.", "",
                          "oDRmaKEBC5GIrfqg5U64"]]}
    _use(lambda r: httpx.Response(200, json=body), monkeypatch, tmp_path)
    monkeypatch.setattr(t, "OPENSEARCH_URL", "http://os:9200")
    res = t.query_logs("payment", 10, "WARN")
    assert res.value["lines"][0]["severity"] == "error"
    assert res.log_ids == ["oDRmaKEBC5GIrfqg5U64"]


def test_health_does_not_expose_uncitable_sub_ids(monkeypatch, tmp_path):
    body = {"status": "success", "data": {"result": [{"metric": {}, "value": [0, "1.5"]}]}}
    _use(lambda r: httpx.Response(200, json=body), monkeypatch, tmp_path)
    res = t.service_health("payment", 5)
    assert res.status == t.ToolStatus.OK and res.source == "derived"
    assert "ev-query_metrics" not in res.for_llm()  # only res.evidence_id is citable
    assert "sub_evidence" in (tmp_path / f"{res.evidence_id}.json").read_text()  # still auditable on disk


def test_inventory_includes_containers_without_spans(monkeypatch, tmp_path):
    def handler(request):
        if "container_name" in request.url.path:
            return httpx.Response(200, json={"data": ["valkey-cart", "flagd"]})
        if "service_name" in request.url.path:
            return httpx.Response(200, json={"data": ["cart"]})
        return httpx.Response(200, json={"services": ["cart"]})
    _use(handler, monkeypatch, tmp_path)
    assert t.service_inventory(refresh=True) == ["cart", "valkey-cart"]  # flagd still excluded
    assert t.validate_service("valkey-cart") == "valkey-cart"


def test_for_llm_sends_the_full_result_never_cut(monkeypatch, tmp_path):
    lines = [{"time": f"t{i}", "severity": "warn", "body": "Payment request failed. " * 10, "trace_id": "x" * 32}
             for i in range(20)]
    res = t.ToolResult(status=t.ToolStatus.OK, evidence_id="ev-1", tool="query_logs", source="opensearch",
                       service="payment", query="q", window_start=t._now(), window_end=t._now(), collected_at=t._now(),
                       value={"min_severity": "WARN", "count": 20, "lines": lines})
    data = json.loads(res.for_llm())  # valid JSON, however long
    assert data["value"] == res.value and "truncated" not in res.for_llm()


def test_tool_specs_are_valid_for_llm():
    names = {s["function"]["name"] for s in t.tool_specs()}
    assert names == {"overview", "log_overview", "query_metrics", "search_traces", "query_logs", "service_health"}


# ---- cartFailure shapes (seen live): a rare operation fails slowly; callers time out

def _timed(sid, parent, name, start_ms, dur_ms, kind=2, error=False, msg="", attrs=None):
    sp = _span(sid, parent, name, error, msg, kind=kind, attrs=attrs or [])
    sp["startTimeUnixNano"] = str(int(start_ms * 1e6))
    sp["endTimeUnixNano"] = str(int((start_ms + dur_ms) * 1e6))
    return sp


def test_overview_finds_a_failing_operation_hidden_in_a_busy_service(monkeypatch, tmp_path):
    # cart serves ~4 req/s (GetCart, AddItem); EmptyCart is ~0.2% of it and now fails and takes 15 s
    def handler(request):
        q = request.url.params["query"]
        earlier = "offset" in q
        err, p95 = "STATUS_CODE_ERROR" in q, "histogram_quantile" in q
        if "SPAN_KIND_SERVER" not in q:  # service-wide: cart looks unchanged
            v = 0.0 if err else 30.0 if p95 else 4.0
            rows = [{"metric": {"service_name": "cart"}, "value": [0, str(v)]}]
        else:
            ops = {"GetCart": (0.0, 40.0, 3.0),
                   "EmptyCart": (0.0 if earlier else 1.0, 10.0 if earlier else 15000.0, 0.03)}
            rows = [{"metric": {"service_name": "cart", "span_name": f"POST /oteldemo.CartService/{op}"},
                     "value": [0, str(e if err else p if p95 else r)]} for op, (e, p, r) in ops.items()]
        return httpx.Response(200, json={"status": "success", "data": {"result": rows}})
    _use(handler, monkeypatch, tmp_path, inventory=("cart", "checkout"))
    v = t.overview().value
    assert v["changed_since_previous_window"][0].startswith("no service-wide change")
    assert len(v["operations_changed"]) == 1
    assert v["operations_changed"][0].startswith(
        'cart "POST /oteldemo.CartService/EmptyCart": error_rate up 0.000 -> 1.000; p95 up 10 -> 15000 ms')


def test_operation_needs_an_actual_failed_call():
    # 50% errors on an operation called 0.001 req/s = no real failure in 3 min: not a change
    r = {"error_rate": 0.5, "error_rate_before": 0.0, "request_rate": 0.001, "window_s": 180}
    assert t._changes(r, operation=True) == []
    r["request_rate"] = 0.05  # ~4.5 failed calls
    assert t._changes(r, operation=True) == ["error_rate up 0.000 -> 0.500"]


def test_timeout_is_traced_to_where_the_time_went(monkeypatch, tmp_path):
    # frontend-proxy gives up after 15 s (504); checkout waits on cart's EmptyCart, which hangs
    gw = [{"key": "http.status_code", "value": {"stringValue": "504"}}]
    empty = [{"key": "rpc.method", "value": {"stringValue": "oteldemo.CartService/EmptyCart"}}]
    body = {"result": {"resourceSpans": [
        _rs("load-generator", [_timed("00000000000000a0", "", "user_checkout", 0, 15200, kind=1)]),
        _rs("frontend-proxy", [_timed("00000000000000a1", "00000000000000a0", "POST", 10, 15000, error=True, attrs=gw)]),
        _rs("checkout", [_timed("00000000000000b1", "00000000000000a1", "PlaceOrder", 20, 14900),
                         _timed("00000000000000b2", "00000000000000b1", "oteldemo.CartService/EmptyCart", 300, 14600,
                                kind=3, attrs=empty)]),
        _rs("cart", [_timed("00000000000000c1", "00000000000000b2", "POST /oteldemo.CartService/EmptyCart", 310, 14590)]),
    ]}}
    _use(lambda r: httpx.Response(200, json=body), monkeypatch, tmp_path, inventory=("checkout", "cart", "frontend-proxy"))
    res = t.search_traces("checkout")
    tr = res.value["traces"][0]
    assert tr["suspected_origin"]["service"] == "frontend-proxy" and tr["suspected_origin"]["timed_out_waiting"] is True
    assert tr["latency_origin"]["service"] == "cart" and "EmptyCart" in tr["latency_origin"]["operation"]
    assert res.value["latency_origin_counts"] == {"cart": 1}
    assert tr["root"] == "load-generator: user_checkout"


def test_slow_time_not_explained_by_finished_spans_is_marked(monkeypatch, tmp_path):
    # checkout's call to cart is still hanging: its span is not exported yet, only short calls are visible
    body = {"result": {"resourceSpans": [
        _rs("frontend", [_timed("00000000000000a1", "", "POST /api/checkout", 0, 15000),
                         _timed("00000000000000a2", "00000000000000a1", "GetCart", 5, 30, kind=3)]),
    ]}}
    _use(lambda r: httpx.Response(200, json=body), monkeypatch, tmp_path, inventory=("frontend",))
    tr = t.search_traces("frontend").value["traces"][0]
    assert tr["latency_origin"]["certainty"] == "unexplained" and "not exported" in tr["latency_origin"]["note"]


def test_log_overview_separates_new_baseline_and_stopped(monkeypatch, tmp_path):
    now = t._now()

    def ts(minutes_ago):
        return (now - t.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S.%f")[:23]
    cols = [{"name": "n"}, {"name": "first"}, {"name": "last"}, {"name": "resource.service.name"},
            {"name": "severity.text"}, {"name": "body"}]
    hist = {"schema": cols, "datarows": [
        [7, ts(5), ts(1), "cart", "Error", "Wasn't able to connect to redis"],            # started 5 min ago
        [900, ts(32.9), ts(0.5), "load-generator", "WARN", "agent:8010 unreachable"],    # all along: baseline
        [250, ts(32), ts(20), "frontend", "error", "API request failed"],                 # earlier incident, ended
        [3, ts(4), ts(1), "product-catalog", "error", "Product Catalog Fail Feature Flag Enabled"],
        [99, ts(10), ts(0.1), "otelcol-contrib", "warn", "unable to open connection to broker"],
    ]}
    now_rows = {"schema": [{"name": "n"}, {"name": "resource.service.name"}, {"name": "severity.text"}, {"name": "body"}],
                "datarows": [[2, "cart", "Error", "Wasn't able to connect to redis"],
                             [80, "load-generator", "WARN", "agent:8010 unreachable"],
                             [1, "product-catalog", "error", "Product Catalog Fail Feature Flag Enabled"],
                             [9, "otelcol-contrib", "warn", "unable to open connection to broker"]]}

    def handler(request):
        return httpx.Response(200, json=hist if "min(" in json.loads(request.content)["query"] else now_rows)
    _use(handler, monkeypatch, tmp_path)
    monkeypatch.setattr(t, "OPENSEARCH_URL", "http://os:9200")
    res = t.log_overview()
    v = res.value
    assert res.status == t.ToolStatus.OK and res.source == "opensearch"
    assert [m["service"] for m in v["logging_now"][:2]] == ["product-catalog", "cart"]  # newest first
    first = v["logging_now"][1]
    assert first["service"] == "cart" and first["started_recently"] is True and 4.5 < first["first_seen_min_ago"] < 5.5
    lg = next(m for m in v["logging_now"] if m["service"] == "load-generator")
    assert lg["started_recently"] is False
    assert [m["service"] for m in v["stopped_earlier"]] == ["frontend"]
    assert not any(m["service"] == "otelcol-contrib" for m in v["logging_now"])
    assert "flag" not in res.for_llm().replace(t.FLAG_REDACTED, "").lower()


def test_error_traces_are_asked_for_explicitly_in_a_busy_service(monkeypatch, tmp_path):
    # live: 40 sampled frontend traces held none of the 28%-failing route; Jaeger's error filter finds it
    ok = {"result": {"resourceSpans": [_rs("frontend", [_span("00000000000000a1", "", "GET /api/cart", kind=2)])]}}
    bad = {"result": {"resourceSpans": [_rs("frontend", [
        dict(_span("00000000000000b1", "", "GET /api/recommendations", True, "14 UNAVAILABLE", kind=2),
             traceId="1af7651916cd43dd8448eb211c80319c")])]}}

    def handler(request):
        return httpx.Response(200, json=bad if "query.attributes" in request.url.params else ok)
    _use(handler, monkeypatch, tmp_path)
    res = t.search_traces("frontend")
    assert res.status == t.ToolStatus.OK and res.value["error_origin_counts"] == {"frontend": 1}
    assert res.value["traces_scanned"] == 2 and 'query.attributes' in res.query


def test_per_operation_metrics_never_show_flag_service_calls(monkeypatch, tmp_path):
    # live INC-DEMO-17: ad's p95 by operation listed "flagd.evaluation.v2.Service/EventStream" (15000 ms)
    rows = [{"metric": {"span_name": "flagd.evaluation.v2.Service/EventStream"}, "value": [0, "15000"]},
            {"metric": {"span_name": "oteldemo.AdService/GetAds"}, "value": [0, "46"]}]
    _use(lambda r: httpx.Response(200, json={"status": "success", "data": {"result": rows}}), monkeypatch, tmp_path,
         inventory=("ad",))
    res = t.query_metrics("ad", "p95_latency_by_operation", 3)
    assert res.value["series"] == [{"span_name": "oteldemo.AdService/GetAds", "value": 46.0}]
    assert "flagd" not in res.for_llm()
