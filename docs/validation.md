# Measured validation results

Measurements were taken on 2026-10-22 against the supplied, unmodified provider.
Raw evidence is retained locally under ignored `docs/evidence/`. These are observed
results, not synthetic performance targets. Reproduction commands are in
[testing.md](testing.md).

These are the original measurements. The later [zero-notification investigation](zero-notifications.md)
records parser-v3/v4 corrections, resolution of case-only false negatives, additional regressions
and a separate unchanged-k6 rerun. Historical measurements below are preserved.

## Environment and provenance

- Python 3.12; Docker Engine 29.7.2, Compose 5.5.1, 10 allocated CPUs and
  8,319,504,384 bytes of allocated Docker memory. Shared macOS developer machine;
  unrelated idle containers were not stopped. No resource-isolated capacity claim.
- One application process, SQLite WAL on a named Docker volume, unchanged mock,
  InfluxDB and Grafana. Host port 15000 forwards to the original internal port 5000.
- Worker comparisons used application image `e501f6b761cd21167e1dd4bf9e4326667a0c8d1982696373a74fb220d755909f`.
  The original k6 run used source `0cf7d98`, image
  `9db425da4dc4429359cc357250116d775da5c44255205d1f932fa97408f18dae`.
  The intervening application changes bounded empty-body buffering and rejected
  invalid Unicode input; they did not change workers or valid extraction behavior.
- Final application source `0b192bd` adds more explicit free-text refusal patterns
  and increments the parser version to `conservative-v2`. Its running image ID is
  `sha256:6d88add118637e7431de8bc7a3d235a7af28a39f70860664ed574dedbcc77e4f`.
  Full load measurements were **not** repeated after that narrow safety change.
  The complete deterministic suite, Linux suite and real-provider smoke verification
  were repeated. No supported mock response category changes under this update.
- The final image's Python sources match host SHA-256 hashes. Protected provider
  and original k6 files match baseline `82224da` byte for byte. Image IDs, package
  versions and file hashes are in `environment-final.json`.

## Deterministic verification

125 tests passed on macOS in 2.82 seconds and inside Linux Docker in 3.13 seconds.
The suite includes actual subprocess SIGKILL tests for both processing stages,
transaction cancellation, stale-owner fencing, durable intermediate results,
quota/cooldown recovery, failure taxonomy, adversarial parsing and independent
outcome verification. It does not require the stochastic provider.

Coverage of `notification_service`: 704/753 statements (93.49%) and 184/220 branches
(83.64%); combined coverage 91.26%. The thin `main.py` launcher is not included in
that figure; its startup is exercised by Docker. Coverage is not proof of correctness.
Ruff lint/format, Compose configuration, whitespace checks and protected-source
comparisons passed. All four Mermaid diagrams rendered with Mermaid CLI.

## Worker comparison with real successful notifications

Eight isolated batches of 60 requests compared 8/2, 16/2, 16/4 and 24/4 extraction/
notification workers. Each batch started without backlog and after startup quota
cooldown. Repetition two reversed the configuration order. The corpus used new
email/SMS recipients and complete lowercase bodies explicitly separated by a colon;
no production behavior was specialized for it. Every extraction called the AI.

| AI / notify | Repeat | Sent | Refusal | Extraction failure | Total span, s | Terminal p95, s |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 8 / 2 | 1 | 56 | 4 | 0 | 22.472 | 20.719 |
| 8 / 2 | 2 | 54 | 6 | 0 | 19.632 | 18.334 |
| 16 / 2 | 1 | 54 | 6 | 0 | 15.189 | 13.561 |
| 16 / 2 | 2 | 56 | 4 | 0 | 14.698 | 14.137 |
| 16 / 4 | 1 | 57 | 3 | 0 | 14.553 | 13.902 |
| 16 / 4 | 2 | 53 | 7 | 0 | 14.216 | 13.250 |
| 24 / 4 | 1 | 52 | 7 | 1 | 13.421 | 12.481 |
| 24 / 4 | 2 | 53 | 7 | 0 | 13.907 | 12.986 |

Span is first creation to last terminal completion, including batch admission.
Terminal p95 includes both sent and failed requests. Every batch's first backlog
sample contained 60 pending records and its final sample contained zero. One-second
polling produced the intervening timestamped backlog series; it is not an exact
continuous maximum. `processing` includes retry-waiting requests, not just active
workers. The verifier uses the lower empirical order statistic for percentiles.

Across 480 accepted requests: **435 sent (90.625%), 44 refusals, one exhausted
extraction**, zero semantic, infrastructure or delivery terminal failures. There
were 541 extraction attempts and 435 notification attempts; 62 extraction errors
and 44 refusals across attempts. Twenty-eight accepted extractions required syntax
repair. Refusals terminated immediately rather than being retried toward success.

All 435 persisted sent payloads matched independent expected recipient, message
and channel values. For these already-completed batches, the oracle was reconstructed
from the generator's ordered accepted IDs and input index, not AI output; original
reports were preserved and an additive verification report was written. The current
generator saves this oracle before verification for future runs. Provider access
logs independently corroborated exactly 435 distinct `trace_id` acknowledgments;
all 480 public API states matched durable results. The mock does not deliver to an
actual inbox, so this is provider acknowledgment and payload fidelity evidence.

**Selected default: 16 AI / 4 notification workers.** Mean batch span fell from
21.052 seconds at 8/2 to 14.384 at 16/4. Moving to 24/4 reduced span to 13.664 but
also delivered fewer notifications; aggregate confirmed sends per span were about
3.82/s at 16/4 and 3.84/s at 24/4. That is insufficient evidence to justify 50%
more extraction concurrency. Two delivery workers were already competitive
(14.944-second mean at 16/2); four provide latency headroom without increasing the
4.5 attempts/s admission target. Two repetitions and random outcomes do not prove
optimality. Larger repeated trials are needed for production sizing.

## Original k6: HTTP success versus business outcomes

The unchanged 40-second scenario ramps to 200 configured VUs (199 observed maximum).
It completed 2,749 iterations, 8,247 HTTP requests and **19,243 passing checks**, with
zero failed checks and zero HTTP failures. The original script has no thresholds;
its exit code alone was not used as evidence.

- Global HTTP duration: mean 2.600 ms, p95 5.851 ms, maximum 89.263 ms.
- Process endpoint, exported from InfluxDB: 2,749 observations, mean 2.589 ms,
  p95 6.307 ms, p99 44.991 ms, maximum 89.263 ms.
- After complete drain: **zero sent; 2,749 failed**. Terminal families were
  2,453 semantic, 290 refusal, six extraction, zero infrastructure and zero delivery.
- Semantic codes: 1,808 `message_mismatch`, 645 `semantic_unverified`.
  Extraction codes: two `invalid_fields_or_syntax`, four `missing_or_truncated_object`.
- 3,048 extraction attempts, zero notification attempts. Attempt failures were
  2,453 semantic, 290 refusal and 305 extraction; retried extraction failures are
  therefore not confused with six terminal extraction failures.
- Provider access logs independently recorded 3,048 successful extraction HTTP
  responses and no notification call. A successful extraction HTTP response can
  contain a refusal, malformed output or a semantically wrong candidate.
- Every one of the 2,749 public terminal statuses matched persistence.

The original corpus cannot produce a faithful complete message under the mock's
lowercasing/last-colon/50-character logic and the application's exact-fidelity
policy. The result demonstrates conservative rejection, **not successful business
delivery**. `integrity_passed=true` describes reconciled evidence, while
`all_sent=false` describes the outcome. See [semantic limitations](semantics.md).

Mean extraction attempt duration was 2.258 s, p95 2.937 s, p99 2.997 s and maximum
3.037 s. Mean terminal latency was 190.938 s, p95 383.075 s and maximum 422.899 s.
First extraction waiting time averaged 167.912 s and reached 348.394 s. The entire
batch drained 433.252 s after first creation. The first post-run sample, taken
approximately 51 seconds after k6 returned, still contained 2,173 pending records;
it is not a measurement of backlog exactly at load-test completion.

At mean AI latency 2.25 s, 16 workers have an ideal ceiling near 7.11 attempts/s,
before overhead and retries. Around 200 VUs with 1.5 s deliberate sleeps can issue
roughly 133 iterations/s when HTTP admission is fast. Asynchrony removes AI latency
from the process endpoint; it does not remove the throughput mismatch. The observed
3,048 attempts over the 433.252-second span correspond to about 7.04 attempts/s.
For sustained overload, the 10,000-record admission bound eventually returns 503;
raising worker counts alone cannot overcome the separate 5/s notification quota.
Queue age and end-to-end latency must be operational metrics, not just HTTP p95.

## Recovery and separate provider check

The original `health-check.js`, executed separately after application backlog
drained, completed ten iterations, 20 HTTP requests and all 50 checks, with zero
HTTP failures. It directly tests the provider, not application extraction quality.

A Docker SIGKILL experiment killed the app with 12 active extraction leases. After
restart all 12 reached terminal state: 11 matched the independent oracle and were
confirmed sent; one was refused. Twelve abandoned attempts were classified as
infrastructure failures in history, while the only terminal failure was refusal.
There were 25 extraction attempts and 11 notification attempts. The batch completed
in 36.519 seconds, consistent with 30-second lease expiry and bounded retry work.
No accepted request was lost. This does not demonstrate exactly-once external effects.

Rebuilding and force-recreating the app preserved all 3,241 existing terminal
records, including payloads, attempts counters and provider IDs. Every public state
was queried again: 446 sent and 2,795 failed. API readiness, both OpenAPI endpoints
and Grafana health returned HTTP 200. A separate final-image smoke batch finished
with 11 independently matched sends and one refusal out of 12 requests, using 14
extraction attempts and 11 notification attempts. It completed in 5.052 seconds;
all 12 public statuses matched. Evidence is under `recovery/release-smoke-*`.

## Interpretation limits

No mock-based result measures production prompt adherence or resistance to all
prompt injections. The narrow semantic evidence grammar sacrifices language
coverage for fidelity. Stochastic mock trials are not deterministic acceptance
tests. Shared hardware, small samples, mutable upstream image tags and unpinned
transitive dependencies limit exact reproduction; preserve the recorded versions.
Dependency findings and remaining deployment risks are in [audit.md](audit.md).
