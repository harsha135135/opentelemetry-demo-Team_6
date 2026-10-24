# Incident Response Report: LIVE-1-BASELINE

**Status**: `ESCALATED`  
**Remediation Outcome**: `ESCALATED` (mode: `dry_run`)  
**Affected Service**: `Unknown`  
**Detection Time (UTC)**: `2026-10-24T05:50:24.281040+00:00`  
**Observation Window**: `2026-10-24T05:45:24.281040+00:00` to `2026-10-24T05:50:24.281040+00:00`

## 1. Symptom & Recovery Condition
- **Symptom**: Load generator reports about 20% of requests failing; find the cause
- **Recovery Condition**: not provided - diagnosis only (CP0-CP2, no remediation)

## 2. Investigation Timeline
| Timestamp (UTC) | State | Description |
| --- | --- | --- |
| 2026-10-24T05:50:24.331113+00:00 | `RECEIVED` | Incident received from manual-cli |
| 2026-10-24T05:50:24.331119+00:00 | `INVESTIGATING` | Investigation started |
| 2026-10-24T05:50:26.163413+00:00 | `INVESTIGATING` | Model requested 3 tool call(s):  |
| 2026-10-24T05:50:26.180701+00:00 | `INVESTIGATING` | overview({'window_minutes': 5}) -> ok [ev-overview-784e18be] |
| 2026-10-24T05:50:26.199824+00:00 | `INVESTIGATING` | resource_overview({'window_minutes': 5}) -> ok [ev-resource_overview-2f2df154] |
| 2026-10-24T05:50:26.356206+00:00 | `INVESTIGATING` | container_state({'service': None}) -> ok [ev-container_state-826b40e4] |
| 2026-10-24T05:50:29.019408+00:00 | `INVESTIGATING` | Model requested 5 tool call(s):  |
| 2026-10-24T05:50:29.076589+00:00 | `INVESTIGATING` | search_traces({'service': 'load-generator', 'window_minutes': 15, 'errors_only': True, 'limit': 10}) -> ok [ev-search_traces-a6f3746a] |
| 2026-10-24T05:50:29.107155+00:00 | `INVESTIGATING` | search_traces({'service': 'frontend', 'window_minutes': 15, 'errors_only': True, 'limit': 10}) -> empty [ev-search_traces-01a6fcce] |
| 2026-10-24T05:50:31.902431+00:00 | `INVESTIGATING` | query_logs({'service': 'ad', 'window_minutes': 10, 'min_severity': 'WARN', 'limit': 20}) -> ok [ev-query_logs-0e9838e2] |
| 2026-10-24T05:50:31.956282+00:00 | `INVESTIGATING` | query_logs({'service': 'accounting', 'window_minutes': 10, 'min_severity': 'WARN', 'limit': 20}) -> ok [ev-query_logs-a42a37f3] |
| 2026-10-24T05:50:31.965508+00:00 | `INVESTIGATING` | query_metrics({'service': 'frontend-proxy', 'metric': 'errors_by_operation', 'window_minutes': 5}) -> ok [ev-query_metrics-405cfba4] |
| 2026-10-24T05:50:34.525036+00:00 | `INVESTIGATING` | Model requested 3 tool call(s):  |
| 2026-10-24T05:50:34.549887+00:00 | `INVESTIGATING` | service_health({'service': 'ad', 'window_minutes': 5}) -> ok [ev-service_health-76a2caed] |
| 2026-10-24T05:50:34.591575+00:00 | `INVESTIGATING` | search_traces({'service': 'ad', 'window_minutes': 15, 'errors_only': True, 'limit': 10}) -> ok [ev-search_traces-8d9980bf] |
| 2026-10-24T05:50:34.603698+00:00 | `INVESTIGATING` | service_health({'service': 'accounting', 'window_minutes': 5}) -> ok [ev-service_health-03fbc603] |
| 2026-10-24T05:50:43.325155+00:00 | `INVESTIGATING` | Finish deferred: unresolved resource anomalies ['kafka', 'opensearch', 'astronomy-db'] |
| 2026-10-24T05:50:45.571669+00:00 | `INVESTIGATING` | Model requested 3 tool call(s):  |
| 2026-10-24T05:50:45.595119+00:00 | `INVESTIGATING` | service_health({'service': 'kafka', 'window_minutes': 5}) -> ok [ev-service_health-0cb68839] |
| 2026-10-24T05:50:53.245560+00:00 | `DIAGNOSED` | Diagnosis validated: load-generator |
| 2026-10-24T05:50:53.245600+00:00 | `DIAGNOSED` | Finished: diagnosed; 6 model calls, 12 tool calls, 28.9s |
| 2026-10-24T05:50:53.256485+00:00 | `ESCALATED` | Remediation finished: ESCALATED (Diagnosis confidence is low; remediation not planned. Human review required.) |

## 3. Evidence Collection
| ID | Source | Service | Status | Tool / Query | Observed / Capture |
| --- | --- | --- | --- | --- | --- |
| `ev-overview-784e18be` | `prometheus` | `-` | `ok` | `overview: {"request_rate": "sum by (service_name) (rate(traces_span_metrics_calls_total[5m]))", "error_rate": "(sum by (service_name) (rate(traces_span_metrics_calls_total{status_code=\"STATUS_CODE_ERROR\"}[5m])) or sum by (service_name) (rate(traces_span_metrics_calls_total[5m])) * 0) / sum by (service_name) (rate(traces_span_metrics_calls_total[5m]))", "p95_ms": "histogram_quantile(0.95, sum by (le, service_name) (rate(traces_span_metrics_duration_milliseconds_bucket[5m])))"}` | {'value': {'services': [{'service': 'load-generator', 're... (`/Users/harshamac/developer/bosch/opentelemetry-demo-Team_6/incident-agent/runs/LIVE-1-BASELINE/evidence/ev-overview-784e18be.json`) |
| `ev-resource_overview-2f2df154` | `prometheus` | `-` | `ok` | `resource_overview: cpu: container_cpu_utilization_ratio; cpu_prior: avg_over_time(container_cpu_utilization_ratio[5m] offset 10m); mem: container_memory_percent_ratio; mem_growth: deriv(container_memory_usage_total_bytes[5m]) * 300 / container_memory_usage_total_bytes; uptime: container_uptime_seconds; last_seen_age: time() - max_over_time(timestamp(container_uptime_seconds)[60m:30s])` | {'value': {'fleet': {'cpu_median_pct': 0.0175762504519528... (`/Users/harshamac/developer/bosch/opentelemetry-demo-Team_6/incident-agent/runs/LIVE-1-BASELINE/evidence/ev-resource_overview-2f2df154.json`) |
| `ev-container_state-826b40e4` | `health` | `-` | `ok` | `container_state: GET /v1/services` | {'value': {'services': [{'service': 'accounting', 'state'... (`/Users/harshamac/developer/bosch/opentelemetry-demo-Team_6/incident-agent/runs/LIVE-1-BASELINE/evidence/ev-container_state-826b40e4.json`) |
| `ev-search_traces-a6f3746a` | `jaeger` | `load-generator` | `ok` | `search_traces: GET /jaeger/ui/api/v3/traces {"query.service_name": "load-generator", "query.num_traces": 40, "query.start_time_min": "2026-10-24T05:35:29Z", "query.start_time_max": "2026-10-24T05:50:29Z"}` | {'value': {'traces_scanned': 40, 'error_origin_counts': {... (`/Users/harshamac/developer/bosch/opentelemetry-demo-Team_6/incident-agent/runs/LIVE-1-BASELINE/evidence/ev-search_traces-a6f3746a.json`) |
| `ev-search_traces-01a6fcce` | `jaeger` | `frontend` | `empty` | `search_traces: GET /jaeger/ui/api/v3/traces {"query.service_name": "frontend", "query.num_traces": 40, "query.start_time_min": "2026-10-24T05:35:29Z", "query.start_time_max": "2026-10-24T05:50:29Z"}` | {'value': {'traces_scanned': 40, 'edges': {'frontend-prox... (`/Users/harshamac/developer/bosch/opentelemetry-demo-Team_6/incident-agent/runs/LIVE-1-BASELINE/evidence/ev-search_traces-01a6fcce.json`) |
| `ev-query_logs-0e9838e2` | `opensearch` | `ad` | `ok` | `query_logs: source=otel-logs-* | where resource.service.name = 'ad' and `@timestamp` >= '2026-10-24 05:40:29' and `severity.number` >= 13 | sort - `@timestamp` | head 40 | fields `@timestamp`, severity, body, traceId, _id` | {'value': {'min_severity': 'WARN', 'count': 5, 'lines': [... (`/Users/harshamac/developer/bosch/opentelemetry-demo-Team_6/incident-agent/runs/LIVE-1-BASELINE/evidence/ev-query_logs-0e9838e2.json`) |
| `ev-query_logs-a42a37f3` | `opensearch` | `accounting` | `ok` | `query_logs: source=otel-logs-* | where resource.service.name = 'accounting' and `@timestamp` >= '2026-10-24 05:40:31' and `severity.number` >= 13 | sort - `@timestamp` | head 40 | fields `@timestamp`, severity, body, traceId, _id` | {'value': {'min_severity': 'WARN', 'count': 1, 'lines': [... (`/Users/harshamac/developer/bosch/opentelemetry-demo-Team_6/incident-agent/runs/LIVE-1-BASELINE/evidence/ev-query_logs-a42a37f3.json`) |
| `ev-query_metrics-405cfba4` | `prometheus` | `frontend-proxy` | `ok` | `query_metrics: topk(10, sum by (span_name) (rate(traces_span_metrics_calls_total{service_name="frontend-proxy",status_code="STATUS_CODE_ERROR"}[5m])))` | {'value': {'metric': 'errors_by_operation', 'unit': 'erro... (`/Users/harshamac/developer/bosch/opentelemetry-demo-Team_6/incident-agent/runs/LIVE-1-BASELINE/evidence/ev-query_metrics-405cfba4.json`) |
| `ev-service_health-76a2caed` | `derived` | `ad` | `ok` | `service_health: request_rate, error_rate, p95_latency_ms, container_cpu, container_memory, container_memory_trend, container_uptime, container_last_seen_age_s` | {'value': {'summary': {'request_rate': 0.369543, 'error_r... (`/Users/harshamac/developer/bosch/opentelemetry-demo-Team_6/incident-agent/runs/LIVE-1-BASELINE/evidence/ev-service_health-76a2caed.json`) |
| `ev-search_traces-8d9980bf` | `jaeger` | `ad` | `ok` | `search_traces: GET /jaeger/ui/api/v3/traces {"query.service_name": "ad", "query.num_traces": 40, "query.start_time_min": "2026-10-24T05:35:34Z", "query.start_time_max": "2026-10-24T05:50:34Z"}` | {'value': {'traces_scanned': 40, 'error_origin_counts': {... (`/Users/harshamac/developer/bosch/opentelemetry-demo-Team_6/incident-agent/runs/LIVE-1-BASELINE/evidence/ev-search_traces-8d9980bf.json`) |
| `ev-service_health-03fbc603` | `derived` | `accounting` | `ok` | `service_health: request_rate, error_rate, p95_latency_ms, container_cpu, container_memory, container_memory_trend, container_uptime, container_last_seen_age_s` | {'value': {'summary': {'request_rate': 0.086995, 'error_r... (`/Users/harshamac/developer/bosch/opentelemetry-demo-Team_6/incident-agent/runs/LIVE-1-BASELINE/evidence/ev-service_health-03fbc603.json`) |
| `ev-service_health-0cb68839` | `derived` | `kafka` | `ok` | `service_health: request_rate, error_rate, p95_latency_ms, container_cpu, container_memory, container_memory_trend, container_uptime, container_last_seen_age_s` | {'value': {'summary': {'request_rate': 'empty', 'error_ra... (`/Users/harshamac/developer/bosch/opentelemetry-demo-Team_6/incident-agent/runs/LIVE-1-BASELINE/evidence/ev-service_health-0cb68839.json`) |

**Navigate to the evidence** (backend URLs from the operator's .env):
- `ev-overview-784e18be`: <http://localhost:9090/query?g0.expr=sum+by+%28service_name%29+%28rate%28traces_span_metrics_calls_total%5B5m%5D%29%29&g0.tab=table>
- `ev-overview-784e18be`: <http://localhost:9090/query?g0.expr=%28sum+by+%28service_name%29+%28rate%28traces_span_metrics_calls_total%7Bstatus_code%3D%22STATUS_CODE_ERROR%22%7D%5B5m%5D%29%29+or+sum+by+%28service_name%29+%28rate%28traces_span_metrics_calls_total%5B5m%5D%29%29+%2A+0%29+%2F+sum+by+%28service_name%29+%28rate%28traces_span_metrics_calls_total%5B5m%5D%29%29&g0.tab=table>
- `ev-overview-784e18be`: <http://localhost:9090/query?g0.expr=histogram_quantile%280.95%2C+sum+by+%28le%2C+service_name%29+%28rate%28traces_span_metrics_duration_milliseconds_bucket%5B5m%5D%29%29%29&g0.tab=table>
- `ev-resource_overview-2f2df154`: <http://localhost:9090/query?g0.expr=container_cpu_utilization_ratio&g0.tab=table>
- `ev-resource_overview-2f2df154`: <http://localhost:9090/query?g0.expr=avg_over_time%28container_cpu_utilization_ratio%5B5m%5D+offset+10m%29&g0.tab=table>
- `ev-resource_overview-2f2df154`: <http://localhost:9090/query?g0.expr=container_memory_percent_ratio&g0.tab=table>
- `ev-search_traces-a6f3746a`: <http://localhost:50076/jaeger/ui/trace/336abcaa820b9817e4829bb87541bb2c>
- `ev-search_traces-a6f3746a`: <http://localhost:50076/jaeger/ui/trace/a84be8f5e124993f31d665dc70dd7f84>
- `ev-search_traces-01a6fcce`: <http://localhost:50076/jaeger/ui/search?service=frontend>
- `ev-query_metrics-405cfba4`: <http://localhost:9090/query?g0.expr=topk%2810%2C+sum+by+%28span_name%29+%28rate%28traces_span_metrics_calls_total%7Bservice_name%3D%22frontend-proxy%22%2Cstatus_code%3D%22STATUS_CODE_ERROR%22%7D%5B5m%5D%29%29%29&g0.tab=table>
- `ev-search_traces-8d9980bf`: <http://localhost:50076/jaeger/ui/trace/885edd1a17d36af6aebf2ae78102fbcc>
- `ev-search_traces-8d9980bf`: <http://localhost:50076/jaeger/ui/trace/ebcef62f2a4716edac540ea45f18f302>
- `ev-search_traces-8d9980bf`: <http://localhost:50076/jaeger/ui/trace/8955dc118d731807bf9a405bdceae181>

## 4. Diagnosis
- **Suspected Component**: `load-generator`
- **Suspected Root Cause**: The most likely cause of the load-generator failures is its "user_ask_agent" task. It POSTs to http://agent:8010/prompt, and the hostname "agent" does not resolve (NameResolutionError). Each call fails after about 4s. This looks like a missing or misconfigured "agent" dependency or load-generator target. A smaller, separate fault is the ad service's intermittent gRPC UNAVAILABLE on GetAds (~5% errors), which shows up as HTTP 500s on /api/data through frontend-proxy and frontend-web.
- **Confidence**: `low`
- **Uncertainty / Notes**: Only 2 of 40 sampled traces showed the agent DNS failure, so I haven't shown it accounts for the full ~20% fail ratio. Part of the rest is probably the ad errors. Every container started about 3 minutes ago, so I have no pre-restart baseline. The ad memory growth of 50% is probably warm-up. Kafka sits at ~86% of its memory limit with a flat-to-falling trend. It showed no request or error metrics, and its CPU is low. I treat it as probably unrelated, but I can't rule out a link to the accounting Kafka "Consume error". For opensearch and astronomy-db, my follow-up calls failed because the tool budget ran out. I only have their resource_overview flags (opensearch 86% memory, astronomy-db memory growth of 21%). I did not check either one, so they remain unverified and are not ruled out. The accounting service reports no container metrics, so I couldn't check its resources. Its 15s p95 and 4% errors are unexplained, apart from one Consume error log. The data is live, not from the historical incident window. [Unresolved resource anomaly without follow-up evidence: opensearch, astronomy-db.]
- **Supporting Evidence**: `ev-search_traces-a6f3746a`, `ev-overview-784e18be`, `ev-container_state-826b40e4`, `ev-search_traces-8d9980bf`, `ev-query_logs-0e9838e2`, `ev-service_health-76a2caed`, `ev-query_metrics-405cfba4`
- **Conflicting Evidence**: `ev-search_traces-01a6fcce`, `ev-service_health-03fbc603`, `ev-query_logs-a42a37f3`, `ev-resource_overview-2f2df154`, `ev-service_health-0cb68839`
- **Alternative Hypotheses**: The ad service's UNAVAILABLE errors as the main cause. This seems less likely, because they are only ~5% of ad traffic and show up as small error rates at frontend and frontend-proxy., Kafka memory pressure causing the accounting consume errors. Kafka's memory trend is flat to falling, and this is unproven., Opensearch or astronomy-db resource problems. I couldn't follow up on either, so this is unchecked.
- **Next Step**: Confirm whether an "agent" service is meant to be deployed and why DNS for host "agent" fails. Check the load-generator config for its agent endpoint. Then investigate why ad's GetAds returns UNAVAILABLE. Check opensearch and astronomy-db health, and the accounting-to-Kafka consume error.

## 5. Action Proposals & Approvals
No remediation actions proposed.

**Escalation**: Diagnosis confidence is low; remediation not planned. Human review required.

## 6. Verification
Verification not performed or pending.

## 7. Known Limitations & Escalation Notes
- No recovery condition supplied; recovery is out of scope for CP0-CP2.
- Resource anomalies flagged but not followed up with evidence: opensearch, astronomy-db
- Confidence labels are model-reported and not calibrated; no evaluation was run.
- Telemetry tools are live-only; queried windows are recorded per evidence item.
- 1 evidence item(s) were queried outside the incident observation window: ev-container_state-826b40e4
- Diagnosis confidence is low; remediation not planned. Human review required.