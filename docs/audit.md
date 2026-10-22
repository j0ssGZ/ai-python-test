# Engineering audit and residual risks

Scope: application, deterministic tests, Docker integration, supplied provider,
original k6, Grafana provisioning, dependency audit and outcome-verification tools.
This is an engineering review with reproducible evidence, not an independent
penetration test or a certification for public production deployment.

The later [zero-notification investigation](zero-notifications.md) adds parser-v4
case restoration, explicit `que` clause coverage and current test/load evidence.
Milestone counts and dependency findings below describe the original delivery.

## SQLite concurrency finding: CLOSED

The [focused SQLite investigation](sqlite-concurrency.md) reproduced the historical
quota test's BEGIN IMMEDIATE failure. It required cross-connection writer fairness
within a one-second busy budget while queuing 50 transactions on each connection.
Under controlled CPU pressure, the original test failed 3/3 on ordinary container
storage; transaction traces show repeated writer reacquisition, native SQLITE_BUSY,
correct persisted quota and no leaked transaction. The same mechanism also occurs
in tmpfs, so moving tests to memory is not the correction.

The fix synchronizes two competing writers in 50 rounds and retains all 100 quota
decisions, with stronger persistence/deferral assertions. Separate regressions cover
the deployed single-connection burst, actual busy error propagation, cancellation
during blocked BEGIN and real HTTP 503/recovery. No production code or SQLite setting
changed. All 160 tests pass on macOS and Linux overlayfs, tmpfs and a persistent
Docker volume, plus 50 additional focused passes per configuration (200 total).
These suites include SIGKILL, persisted retries and worker-concurrency checks.

Closure applies to the reproduced test defect, not a guarantee that every external
writer obtains SQLite's lock within one second. The exact historical CPU/I/O delay
was not recorded. Single-process deployment and explicit busy/503 behavior remain
documented limits; no silent retry or hidden lock error was introduced. Provider
and original k6 files are unchanged. See the focused report for configurations,
adverse-schedule comparisons, raw evidence locations and reproduction commands.

## Requirements and evaluation boundaries

The original README explicitly requires FastAPI on internal port 5000, `app/main.py`
exporting `app`, the three request endpoints, required AI extraction using separate
system/user messages, normalization/validation, notification of valid results and
the provider API key header. Human evaluation explicitly values prompt robustness,
error handling and pipeline architecture. No original numeric latency, delivery-rate
threshold, retry policy or persistence technology is specified.

The original k6 script checks HTTP statuses, JSON and allowed public states. It
allows queued/processing/failed and never verifies the sent payload or waits for
completion. Grafana visualization does not add a business correctness assertion.
Bounded workers, durable activation/results, failure taxonomy, leases, exact
message fidelity and no refusal retries are design choices and subsequently
approved requirements, not hidden claims about the original benchmark contract.

## Milestone acceptance and evidence

| Milestone | Implemented acceptance | Verification |
| --- | --- | --- |
| 1: foundation | Exact contracts; strict configuration; durable admission/activation; separate registration and processing | Initial 17 tests; persistence, invalid input, repeated activation and concurrent capacity |
| 2: extraction | Versioned prompt; mandatory AI endpoint; bounded parsing; Pydantic v2; conservative semantics; explicit refusals | Initial total 76 tests; aliases, syntax noise, missing data, wrong content and adversarial inputs |
| 3: pipeline | Separate bounded pools; durable intermediate result; independent retry budgets; shared provider quota | Initial total 96 tests; real async integration through controlled HTTP transports |
| 4: recovery | Cancellation-safe transactions; leases; stale-owner fencing; shutdown; no AI repetition after durable extraction | Initial total 111 tests; competing connections and actual SIGKILL in both stages |
| 5: deployment | Non-root Docker app; named volume; unchanged original k6; independent outcome verifier; measured worker comparison | 480-request comparison, original 2,749-iteration run, provider logs and Docker crash/recreation |
| 6: audit/docs | Final adversarial fixes; independent gold oracle; English runbooks/guarantees/diagrams; honest limitations | Final 125 tests on macOS and Linux; lint/format; rendered Mermaid; source hashes and dependency audit |

Atomic Conventional Commits were created after verification on personal-fork
`main`. No provider/k6 changes, benchmark-specific application branches, upstream
mutation, neighbor imports or pushes were required. The backend repository informed
transaction cleanup, leases and recovery patterns through read-only inspection.

## Findings fixed during implementation and review

- Cancellation during `BEGIN IMMEDIATE`, and repeated cancellation during rollback,
  could otherwise leak a transaction across tasks. Cleanup keeps the connection
  lock until rollback actually finishes; regression tests exercise both boundaries.
- A final 429 still has to delay other workers even when that request cannot retry.
  Shared cooldown is persisted independently of the request's remaining budget.
- A worker waking from quota waiting must recheck shutdown before claiming work.
- Invalid Unicode input could reach SQLite and produce a 500 instead of a bounded
  validation response. A regression first exposed it; input encoding is validated.
- Body buffering now uses a bounded byte buffer rather than retaining every chunk
  object, including potentially many empty chunks.
- An outcome verifier that only reuses production semantics can share its bug.
  Independent expected payloads now provide an additional oracle. A regression
  intentionally supplies conflicting expected content and requires verification
  failure, even when the production validator accepts the payload.
- Explicit free-text refusals such as `I cannot assist` and `No puedo ayudar`
  surrounding otherwise valid JSON were insufficiently recognized. Five failing
  adversarial regressions preceded the correction. Parser version `conservative-v2`
  expands refusal detection while treating quoted notification content as data.
  Finite text patterns still do not establish universal refusal recognition.

## Failure-mode matrix

| Trigger | Outcome / category | Recovery and limitation |
| --- | --- | --- |
| Missing ID, invalid/oversized input | 404, 422 or 413; no accepted work | Caller corrects input; no fabricated defaults |
| Full queue or unavailable SQLite | 503 | Caller may retry; admission commits atomically |
| AI connection timeout, 429 or 5xx | Infrastructure attempt failure | Bounded retry; durable schedule; 429 shared cooldown |
| Invalid AI credentials | Infrastructure terminal failure | Operator fixes configuration; no silent retry loop |
| Truncated/malformed output, missing fields | Extraction failure | Only bounded unequivocal repair; stochastic incomplete output may retry |
| Duplicate fields/aliases or multiple objects | Extraction failure | Ambiguity is terminal; never merge answers |
| Valid JSON but changed/truncated message | Semantic terminal failure | Only complete ASCII case differences can be restored; no missing content is reconstructed |
| Unsupported/ambiguous routing language | Semantic terminal failure | Coverage limitation; add general tested grammar only |
| Structured or recognized prose refusal | Refusal terminal failure | No further extraction or notification |
| Notify 429 or 5xx | Delivery attempt failure | Persisted quota/backoff; reuse extraction; possible remote uncertainty |
| Notify transport failure/deadline | Infrastructure attempt failure | Bounded retry can duplicate an already accepted remote send |
| Invalid HTTP 200 notification confirmation | Delivery terminal `delivery_unknown` | Do not automatically resend; external result may be unknown |
| Crash after extraction commit | Notify stage remains durable | Reuse exact stored candidate; no AI repeat |
| Crash during active attempt | Infrastructure abandoned attempt | Expired lease reclaimed within budget; exhausted final attempt fails |
| Late result from old worker | Fenced write rejected | Cannot undo an external side effect already performed |
| Crash after provider accepts, before local commit | Uncertain delivery | Provider lacks idempotency/lookup; exactly-once unavailable |
| Permanent database/disk loss | Accepted work can be lost | Named volume is not a backup; operational backups required |

## Dependency audit: unresolved findings

`pip-audit==2.9.0` against the development environment on 2026-10-22 returned exit
code 1: **eight advisories in two packages**. Seven affect runtime Starlette 0.36.3;
one affects development-only pytest 8.4.2. Full advisory identifiers and reported
fix versions are retained in `docs/evidence/dependency-audit.json`.

| Package | Advisory | Reported fixed version | Relevant surface |
| --- | --- | --- | --- |
| Starlette | [GHSA-86qp-5c8j-p5mr](https://github.com/advisories/GHSA-86qp-5c8j-p5mr) | 1.0.1 | Host-dependent URL reconstruction |
| Starlette | [GHSA-82w8-qh3p-5jfq](https://github.com/advisories/GHSA-82w8-qh3p-5jfq) | 1.3.1 | URL-encoded form limits |
| Starlette | [GHSA-jp82-jpqv-5vv3](https://github.com/advisories/GHSA-jp82-jpqv-5vv3) | 1.3.0 | URL authority/path interpretation |
| Starlette | [GHSA-f96h-pmfr-66vw](https://github.com/advisories/GHSA-f96h-pmfr-66vw) | 0.40.0 | Multipart resource exhaustion |
| Starlette | [GHSA-2c2j-9gv5-cj73](https://github.com/advisories/GHSA-2c2j-9gv5-cj73) | 0.47.2 | Multipart upload blocks event loop |
| Starlette | [GHSA-wqp7-x3pw-xc5r](https://github.com/advisories/GHSA-wqp7-x3pw-xc5r) | 1.1.0 | Windows StaticFiles path handling |
| Starlette | [GHSA-x746-7m8f-x49c](https://github.com/advisories/GHSA-x746-7m8f-x49c) | 1.1.0 | Class-based HTTPEndpoint method dispatch |
| pytest | [GHSA-6w46-j5rx-g56g](https://github.com/advisories/GHSA-6w46-j5rx-g56g) | 9.0.3 | Local temporary-directory handling |

The supplied FastAPI pin constrains its older Starlette dependency. The challenge
runtime baseline was retained; these findings were not suppressed or described as
fixed. Application routes do not use forms, StaticFiles, class-based HTTPEndpoint,
or host-derived URL authorization, and JSON bodies are capped before decoding.
That narrows exposed surfaces; it is not proof that the old dependency is safe.
Before public deployment, upgrade a compatible FastAPI/Starlette set and pytest,
then rerun contracts, security regressions and performance measurements. The
provider is immutable for this challenge and needs separate production replacement.
Operating-system/container-image vulnerabilities were not scanned.

## Security and operational limits

- Prompt text is untrusted. Separate system instructions, bounded parsing, strict
  validation and source correspondence reduce injection impact. No model output
  selects executable code, SQL or a network destination. This is not a general
  abuse classifier; a faithfully represented abusive request still requires an
  effective upstream safety policy and product authorization.
- The mock ignores the system prompt and its random refusals are not a reliable
  content-safety policy. Real-model evaluation needs held-out multilingual intents,
  explicit refusal cases, ambiguous/multiple recipients and indirect instructions.
  Score exact fields, message fidelity, safe rejection and over-rejection separately.
- The service deliberately supports a narrow explicit-routing grammar. Unsupported
  but valid requests can be rejected. Case-sensitive content is preserved; Unicode
  confusables and international phone policy are not solved by stripping separators.
- The assessment API is unauthenticated and exposes no tenant ownership boundary.
  UUIDs are identifiers, not authorization. Public deployments need authenticated
  admission, access control and per-client abuse quotas, including slow-body limits.
- The original stack publishes HTTP services, a development key and anonymous
  Grafana administration. Keep it local. TLS and restricted exposure are required
  for production. Private credentials must not be committed or included in logs.
- SQLite contains original inputs and validated payloads. Directory permissions,
  retention, backup/restore and disk monitoring remain operational responsibilities.
  Terminal history grows without automatic cleanup; readiness is not a progress SLA.
- Single-process initialization is the supported deployment. Multiple connections
  are tested for claim/quota contention; multi-host/NFS storage and simultaneous
  schema migrations are not supported. No horizontal-scale claim is made.
- A lease uses wall time. Clock jumps, suspension and uncooperative I/O can delay
  recovery. Total HTTP deadlines bound normal attempts; abrupt machine/storage loss
  is broader than a process SIGKILL test.
- Direct dependencies are pinned, but transitive dependencies and supplied image
  tags are not fully locked. Record resolved versions; immutable images and a full
  dependency lock are follow-up deployment work.

## Observability interpretation

Report accepted/activated/terminal counts, oldest pending age, stage attempts,
retries, lease expirations, failure families, semantic acceptance, confirmed sends
and independent payload matches. HTTP availability and request duration are separate.
This implementation supplies structured categorized logs, durable history and
verification scripts; it does not add a new telemetry stack or persistent alerting.

The original Grafana scorecard has duplicate panel ID 7 and coarse proxy metrics:
its external-overhead calculation includes non-process API calls, provider hits
count extraction responses rather than successful semantic extraction, and its
ratio to process calls is not a unique-intent conversion rate. URL labels include
request IDs, increasing cardinality. The provider writes Influx points synchronously
inside async middleware and silently ignores export failures. Influx retention is
one hour. Export raw evidence promptly and use the durable outcomes as the business
record; no misleading scorecard panel was used to claim successful notifications.
