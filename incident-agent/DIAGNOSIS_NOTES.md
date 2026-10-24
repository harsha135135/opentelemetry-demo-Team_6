# Incident Zero agent: diagnosis problems, fixes and results

Team 6, read-only AI on-call agent for the OpenTelemetry demo (Astronomy Shop).
Scope: CP1 "Eyes" (telemetry tools) and CP2 "Brain" (diagnose the incident a mentor injects with a feature flag).
Repo: https://github.com/pnehasai/opentelemetry-demo-Team_6-neha (branch `main`; all fixes below are in commit `f88899a1`).

## 1. How the agent works (one paragraph)

An alert comes in -> the orchestrator runs a bounded loop (max 8 LLM calls, 16 tool calls, 240 s, 60k tokens) ->
the LLM (Gemini free tier through LangChain; `gemini-3.1-flash-lite`, fallback `gemini-3.5-flash-lite`) calls read-only tools:
`overview` (all services, Prometheus span metrics), `resource_overview` (all containers: CPU, memory, restarts),
`log_overview` (all services' WARN+ log messages, OpenSearch), then per-service `search_traces` (Jaeger),
`query_logs`, `query_metrics`, `service_health`. Every result is saved as evidence with an ID. The LLM must finish with
a diagnosis that cites evidence IDs; guardrails reject uncited, ungrounded or flag-mentioning diagnoses.
The agent never sees feature-flag names or state: flag wording is removed from all telemetry before the LLM sees it.

Debug tool (no LLM): `python trace_evidence.py --id X` runs the same tools and saves the exact text the LLM would get
(`runs/X/llm_payloads/`). This separates "tools gave wrong/missing evidence" from "LLM misread good evidence".

## 1b. Update after review: evidence picture instead of separate overrides

A reviewer pointed out that separate "send back" checks (stale, newest problem, ignored resource flag) behave like
patches, and that the prompt hardcoded the service list. Changes:

- **`candidates.py` (new, generic):** builds an *evidence picture* from all evidence collected so far. Every service
  that telemetry reports is a candidate (inventory comes from telemetry, not a list). Per candidate: **direct**
  evidence (failing spans originate there, calls to it unanswered, crash loop / sudden resource jump), **current**
  changes, **old** signals (recovering, log messages that began long ago or stopped, restarts that ended), and
  **symptom-of** (its failures originate in another service; a timeout is a symptom of where the time went).
  Tiers: 4 direct + current, 3 direct, 2 current from 2+ sources, 1 current from one source, 0 old/symptom.
  Lowered one tier if mostly a symptom, or if the same message that began long ago is still arriving.
  No service, operation or fault names anywhere in the logic (tests use invented names: svc-a, gateway, db-x).
- **LLM gets the picture every round** plus the live service list; the prompt no longer lists services.
- **One consistency check replaces the three overrides:** a diagnosis is sent back once only if another candidate
  has a *higher* tier. Ties (several live problems) are accepted; the model may keep its answer if it explains why.
- **Confidence capped by evidence tier:** tier ≤ 1 for the named component -> low.
- **Replay harness** `evals/replay_ranking.py` ranks the evidence of saved real runs against ground truth held in a
  git-ignored file the agent never reads. Result: top candidate = truth in **8 / 14** saved runs. All misses but
  one come from runs collected by older tool versions that could not see the fault (CPU bug, no log/operation
  data); fresh runs are needed to measure the current tools. The remaining miss (INC-BLIND-4) is a genuine tie.
- **Correction:** failedReadinessProbe *is* visible: cart logs ERROR "Health check … completed … with message"
  (seen in INC-DEMO-9). Demo issue #8 below was wrong.

## 2. Constraints that shape the design

- We may not modify the organisers' demo (code, compose, collector, flags). Only `incident-agent/` changes.
- The agent may not read flag state. It must diagnose from telemetry only.
- During the demo, the interviewer may switch on several flags a few minutes apart, so the agent must tell the
  newest incident apart from earlier ones that are still fading.
- Free-tier LLM: weak at ranking several signals; overloaded at times (503/504), so a fallback model answers.

## 3. Problems found and what we did

Status: ✅ fixed and verified live · 🟡 fixed, verified by test/replay only · ⚠️ limitation remains

### A. Seeing the failure at all (tools)

| # | Problem | Fix | Status |
|---|---|---|---|
| A1 | Tool output was cut at 3,500-6,000 chars, which broke JSON and hid evidence | Full results always sent (60k-token budget kept) | ✅ |
| A2 | A failure in one rarely called operation (cart `EmptyCart`, 0.2% of cart's calls) vanished in the service-wide error rate | `overview` also compares every server operation on its own (`operations_changed`) | ✅ |
| A3 | Timeouts blamed on the caller that gave up (frontend-proxy 504) | Traces follow the longest child ("slow path") to where the time went (`latency_origin`); a timed-out span is marked `timed_out_waiting` | ✅ |
| A4 | A downstream call still hanging has no span yet, so the slow path ends early | Marked `certainty: unexplained` with a note instead of blaming the last visible service | 🟡 |
| A5 | Trace sampling missed failures: 40 sampled frontend traces held none of a 31%-failing route | `search_traces` also asks Jaeger for error traces explicitly (`error=true`) | ✅ |
| A6 | Failed call with no answer from the callee (paymentUnreachable) named the caller as origin | Names the callee as `suspected`, with `observed_in` = caller | 🟡 |
| A7 | Flag-related error text made the trace filter drop frontend's span, so product-catalog was never the origin | Flag check now uses only the call's target; flag text is redacted, span kept | ✅ |
| A8 | No log view across services: the agent only checked logs of services it already suspected | New `log_overview` tool: every WARN+ message grouped, with first/last seen time | ✅ |
| A9 | CPU metric `container_cpu_utilization_ratio` is a lifetime average since container start (ad at 400% showed 26%); every CPU incident was invisible | CPU computed from the CPU-time counter: `rate(container_cpu_usage_nanoseconds_total[3m]) / 1e7` | ✅ |
| A10 | Crash loop (recommendation restarting every few minutes) only showed as "restarted recently" | `resource_overview` counts restarts in 30 min and projects memory to its limit: "CRASH LOOP: 4 restarts…, ~13 min to its limit" | ✅ |

### B. Old incidents and leftovers (the main problem in rapid demos)

| # | Problem | Fix | Status |
|---|---|---|---|
| B1 | 15-minute windows mixed an earlier, already-ended incident into the current one | Default windows **15 -> 3 min** for every tool and the CLI | ✅ |
| B2 | An incident older than two 3-min windows looked like "no change" | Also compare with the 30 min before ("elevated … (ongoing)") | ✅ |
| B3 | A fading incident looked like a current one | Changes labelled `recovering` (errors/latency falling) and listed separately | ✅ |
| B4 | LLM still diagnosed a recovering service (product-catalog after its flag was off) | Deterministic check: if the diagnosed service is listed only as recovering/stopped, the diagnosis is sent back once | ✅ |
| B5 | Log messages couldn't be dated | `log_overview` gives `first_seen_min_ago` / `last_seen_min_ago`; `started_recently` = first seen in the last 15 min; `stopped_earlier` = messages that ended | ✅ |
| B6 | Restart count over 30 min kept saying "CRASH LOOP" up to 30 min after crashes ended; "restarted recently" lingered ~2 h | A restart counts as current only in the last 10 min; older ones listed as `restarts_ended` ("stable now") | ✅ |
| B7 | **cartFailure keeps failing after the flag is off** (over an hour). Cause is in the demo: `EmptyCart` calls queue behind one connection lock (`ValkeyCartStore.cs:87`) and fail one every ~2 min. LLM blamed cart while paymentFailure was the new incident | "Newest problem" check: if the diagnosis names a service whose errors are old while another service's errors started ≤5 min ago, it is sent back once ("the alert is about the newest change") | 🟡 replay on INC-DEMO-10 |
| B8 | paymentFailure stops immediately when off, but 1-2 stray slow/failed calls afterwards still counted as "error rate up" | (a) a service-wide error rise needs **≥2 failed calls** in the window (operations ≥1); (b) a service whose error messages already stopped counts as "old" in the newest-problem check | 🟡 replay on INC-DEMO-13 |
| B9 | A long-running CPU spike (ad high for 15+ min) lifted its own recent average, so it stopped being flagged | CPU also compared with the container's level 30-60 min ago; the lower baseline is used | ✅ |

### C. Noise and over-confidence

| # | Problem | Fix | Status |
|---|---|---|---|
| C1 | Demo baseline already has slow, erroring, memory-heavy services; fixed limits flagged them all | Flags only on change vs each container's own history; observability containers never flagged | ✅ |
| C2 | Memory sawtooth (product-catalog swings 75-93% of its 20 MiB) flagged as growth | Memory flagged only above its prior 30-min **peak** | ✅ |
| C3 | HIGH confidence from a latency-only change (demo latency drifts 2-3x on its own) | If no new errors, no new logs and no resource flags anywhere: confidence capped to LOW | 🟡 |
| C4 | HIGH confidence on one source | HIGH needs agreeing "ok" evidence from 2 backends | ✅ |
| C5 | LLM noticed ad at 345% CPU but blamed checkout latency | If a strong resource flag (severity ≥3) was checked but neither named nor cited, the diagnosis is sent back once (contention explained in prompt) | ✅ |
| C6 | "High confidence" next to "cause unknown" looked contradictory | Output labels: confidence = where/how it fails; uncertainty = why (unknown from telemetry) | ✅ |
| C7 | Correlated attribute stated as cause ("fails for gold users": the demo tags every forced failure gold) | Prompt: attributes seen only on failures are correlations, reported as observed | 🟡 |

### D. Flag safety (the agent must not learn the answer from flags)

| # | Leak | Fix | Status |
|---|---|---|---|
| D1 | Calls to the flag service in traces | Excluded by call target | ✅ |
| D2 | product-catalog's error text literally says "…Feature Flag Enabled" | Any flag wording redacted with a neutral marker | ✅ |
| D3 | Flag service operation in per-operation latency (`flagd…EventStream`) | Filtered from `query_metrics` | ✅ |
| D4 | Diagnosis mentioning flags | Guardrail rejects it | ✅ |
| D5 | Prompt examples taken from incidents we tested (cart, product-catalog, "gold") could look like hardcoding | Replaced with neutral examples; no rule anywhere maps a symptom to a service | ✅ |

### E. LLM and loop reliability

| # | Problem | Fix | Status |
|---|---|---|---|
| E1 | Claude API not allowed (limits) | LangChain; Gemini free tier, Groq as alternative | ✅ |
| E2 | `gemini-3.5-flash` quota ~20 requests/day; 503 "high demand", 504 timeouts | Primary `gemini-3.1-flash-lite`; one retry then fallback `gemini-3.5-flash-lite`; 20 s per-request timeout | ✅ |
| E3 | Empty replies (hidden "thinking" used all tokens) | Thinking budget capped separately | ✅ |
| E4 | Budget ran out while re-querying logs | Prompt: empty result is an answer; follow-up gate never spends the last calls; 8 model / 16 tool calls | ✅ |
| E5 | Tests hit the real LLM | Test fixture removes keys | ✅ |

## 4. About the user's specific points

- **"Changed 15 min to 3 min"**: done (B1). Smaller than 3 min is not useful for metrics: Prometheus gets new
  span metrics about once a minute, so a 30-second window is usually empty. Traces and logs could use shorter windows,
  but some failures (cart hang) produce nothing for 2+ minutes anyway.
- **"Errors don't stop after turning the flag off, not even after 10 minutes"**: true only for some flags:

  | Flag | After switching off | Why |
  |---|---|---|
  | paymentFailure, adFailure, productCatalogFailure (most flags) | stops immediately; at most a few stray calls | flag checked on every request |
  | cartFailure | keeps failing for 1 h+ | demo bug: lock queue (B7) |
  | recommendationCacheFailure | stops crashing, but leaked memory is only freed by a restart | demo design |
  | adHighCpu | stops immediately | - |

- **"We can't reset the failure 30-60 s after turning the flag off"**: correct. We may not change demo code.
  The only fast reset is restarting the container (`docker restart cart` / `recommendation`), which changes no code;
  ask the organisers whether that is allowed. Otherwise the agent itself must ignore leftovers: B3-B8.
- **"Failed-call thresholds for services like payment"**: done (B8a): ≥2 failed calls for a service-wide rise,
  ≥1 for a single operation. Can be raised; trade-off: paymentFailure at 10% gives only ~2-3 failures in 3 min
  at this demo's traffic, so a higher threshold could hide a real low-rate incident.
- **"The agent should not read leftover failures from the previous incident"**: it cannot simply skip them (a leftover
  is real, current telemetry, and the agent has no flag access). Our approach is to date every signal
  (recovering / started_recently / stopped_earlier / restarts_ended) and to send back diagnoses that name an older
  problem when a newer one exists (B4, B7, B8).

## 5. Results on the live demo

| Run | Flag on (truth) | Agent said | Verdict | What changed after |
|---|---|---|---|---|
| INC-PAY-002 | paymentFailure | payment, Invalid token | ✅ | - |
| INC-DEMO-8 | productCatalogFailure | product-catalog, gRPC INTERNAL | ✅ | - |
| INC-LIVE-003 | none | false alarm | ✅ | - |
| INC-BLIND-1 | cartFailure | product-catalog (old incident) | ❌ | A2, A3, A8, B4 |
| INC-BLIND-2 | cartFailure | cart can't reach its store | ✅ | - |
| INC-BLIND-3/4 | recommendationCacheFailure | cart (leftover) | ❌ | A10, B6; tools-only trace (TRACE-FLAG-1) did show recommendation correctly |
| INC-DEMO-9 | failedReadinessProbe | recommendation crash loop + payment | ❌* | *flag has no visible effect in Docker Compose; agent reported real leftovers; B6 |
| INC-DEMO-10 | paymentFailure | cart (primary) + payment | ❌ half | B7 |
| INC-DEMO-12 | paymentFailure | payment, Invalid token | ✅ | C6, C7 |
| INC-DEMO-13 | adFailure | payment + ad | ❌ half | B8 (replay: check now points at ad) |
| INC-DEMO-14 | adHighCpu | product-catalog latency + memory | ❌ | A9, C2, C3 |
| INC-DEMO-15 | adHighCpu | checkout latency (ad noticed) | ❌ | C5 |
| INC-DEMO-16 | adHighCpu | product-catalog latency | ❌ | B9 |
| INC-DEMO-17 | adHighCpu | ad, extremely high CPU | ✅ | D3 |

Progress: early runs were right only for loud, single errors. After the fixes the tools find the right evidence
in every case tested (verified with `trace_evidence.py`), and the agent got cartFailure, paymentFailure and adHighCpu
right. Still not re-run live with the LLM after their fixes: recommendationCacheFailure and adFailure.

## 6. Still open

1. **The free LLM is the weakest link.** Several wrong answers had the right evidence in front of the model.
   Deterministic "send back once" checks (B4, B7, B8, C5) compensate. A stronger model (Groq llama-3.3-70b, or
   gemini-3.5-flash when its daily quota allows) would likely need fewer of them; not yet tested.
2. **Leftovers from demo bugs** (cartFailure queue, recommendation memory) stay real telemetry. The agent can only
   rank them as older; with two genuinely live problems it should name the newest and list the other.
3. **Not testable from telemetry**: kafka flags (kafka not running),
   AI-agent flags (agent service not running), emitRawPii and cartMetricCardinalityOverflow (not failures).
   imageSlowLoad mostly affects the browser; weak signal.
4. **Untested flags**: adManualGc, emailMemoryLeak, intlShippingSlowdown, productCatalogLockContention,
   loadGeneratorFloodHomepage, paymentUnreachable (live, after A6).
5. Thresholds (2x latency, ≥2 failed calls, 15-min "recent", 10-min restart, 5-min "newest") are judgement calls,
   tuned on a few runs only. Confidence labels are not calibrated.
6. The checks that send a diagnosis back each fire at most once; a stubborn model can still finish wrong
   (it is then recorded as a limitation in the report).

## 7. Issues found in the organisers' demo (to report; not changed by us)

1. checkout and product-catalog run at their 20 MiB memory limits (`GOMEMLIMIT=16MiB`), CPU-bound and unhealthy with no incident.
2. product-catalog's failure message says "Product Catalog Fail Feature Flag Enabled" (reveals the injected fault in telemetry).
3. load-generator calls `agent:8010/prompt`, which is not running in this mode: constant ~3% errors.
4. OpenSearch runs at ~920 MiB of 1 GiB; slow or timed-out log queries at times.
5. After a Prometheus restart, older data is not queryable until compaction finishes ("overlapping blocks").
6. The flag UI rewrites `src/flagd/demo.flagd.json` without its final newline.
7. (once, uncertain) payment missing from container metrics for a while.
8. ~~failedReadinessProbe has no observable effect in Docker Compose~~ (withdrawn: cart logs health-check errors).
9. cartFailure does not stop when switched off: `EmptyCart` calls queued behind the store's connection lock keep failing about every 2 min for over an hour.
10. (possible config issue) `container_cpu_utilization_ratio` behaves as a lifetime average, so it cannot show CPU spikes.

## 8. Questions for a reviewer (e.g. another LLM)

1. Are the "send back once" checks (stale, newest problem, ignored strong flag, latency-only cap) a sound way to
   compensate for a weak model, or do they risk steering the model away from a correct answer when two incidents are live?
2. Better ways to separate leftover failures from a new incident without flag access? (e.g. weighting by first-seen
   time, requiring a change point, comparing error *messages* rather than rates)
3. Are the thresholds reasonable for low-traffic services (checkout/payment at ~0.1-0.2 req/s)?
4. Is a 3-min window right given 60 s metric resolution and incidents injected minutes apart?
5. Which signals would catch the untested flags (GC pauses, international-only shipping delay, DB lock contention)?
