# Investigation of zero notifications in the original k6 workload

The original zero-send result was reproduced. Three original prompts expose destructive
mock extraction behavior. The Marta prompt exposes a different issue: its message
has the same ordinary meaning after lowercasing, but failed the deliberately stricter
literal-fidelity policy. Calling all four outputs semantically inaccurate would
overstate the evidence. Parser v4 corrects this false negative by restoring only
ASCII capitalization from explicit source evidence after the AI supplies the entire
corresponding body. The final delivered message must still match the original exactly.

## Real mock reproduction and stage trace

On 2026-10-22, twelve independent calls for each of the four prompts were made to
the running original `/v1/ai/extract`, using the production system prompt and exact
user strings read from `platform/k6/test.js`. The provider and that script were
unchanged. These were 48 independent diagnostic trials, not retries of one refused
request until success. No diagnostic trial called the notification endpoint.

Each record retains source input, HTTP status/duration, the complete AI envelope,
source routing evidence, parsed/normalized candidate when available, syntax-repair
flag, and final failure category/code. Raw traces are local generated evidence in
`docs/evidence/zero-sends/real-ai-traces.json`; payloads are not logged by production.

All 48 HTTP calls returned 200. Thirty-nine candidates parsed and normalized but
failed validation; six responses failed extraction/parsing, and three were explicit
refusals. Infrastructure failures were zero. Delivery was not attempted. These
counts describe single diagnostic attempts, not eventual request outcomes.

### Prompt 1: email with `diciendo que`

Source: `Enviar email a juan@example.com diciendo que su paquete está en camino.`

The intended body established by the grammar is `su paquete está en camino.`.
The mock has no colon to split on, so its complete candidate body is instead
`enviar email a juan@example.com diciendo que su pa` (the input's first 50 characters).
Its recipient and channel are correct. Canonical/alias parsing and normalization
succeed; semantic validation rejects `message_mismatch` because the candidate
includes routing text and loses the end of the actual message. No local completion
or deletion of the routing prefix can make this AI output a complete extraction.

Observed: ten semantic rejections, one truncated-JSON extraction failure and one
refusal. Zero accepted candidates.

### Prompt 2: SMS with a bare `que` clause

Source: `Avisar por SMS al 600-111-222 que la reserva ha sido confirmada.`

The candidate recipient normalizes safely from `600-111-222` to `600111222` and its
channel is `sms`. The mock's body is
`avisar por sms al 600-111-222 que la reserva ha si`, again an instruction prefix
rather than the complete intended `la reserva ha sido confirmada.`.

Before correction, the evidence grammar also lacked the ordinary explicit `que`
delimiter, so even a complete correct AI candidate would have been rejected as
`semantic_unverified`. Three failing general email/SMS regression examples proved
this avoidable coverage defect. Parser `conservative-v3` recognizes that delimiter
after one validated recipient, without extracting a substitute notification.
Tests retain rejection of truncation, multiple recipients and recipient substitution
by an address mentioned in the body.

Observed before correction: ten `semantic_unverified`, one invalid-field/syntax
failure and one refusal. Replaying the same captured output through v3 changes the
ten semantic codes to `message_mismatch`; none becomes sendable. Correcting grammar
does not reconstruct the mock's truncated message.

### Prompt 3: Marta and initial capitalization

Source: `Manda un correo a marta@app.com: Tienes un nuevo mensaje.`

A complete mock candidate contains exactly:

```json
{"to":"marta@app.com","message":"tienes un nuevo mensaje.","type":"email"}
```

The recipient is valid, the channel is correct, the grammar recognizes the colon,
and the period is preserved. There is no truncation, missing content or fabricated
recipient in this complete candidate. The only difference is the first character,
uppercase `T` versus lowercase `t`. Before v4, parsing and normalization preserved
that lowercase AI output; the final exact comparison rejected `message_mismatch`.

Observed: nine such semantic-policy rejections and three extraction failures
(one truncated JSON and two missing/invalid fields). No refusals appeared in this
small sample. A deterministic positive control verifies that an AI candidate with
the original uppercase `Tienes` passes the same parser and semantic validator.

**Finding:** the candidate is semantically acceptable for this ordinary sentence.
Its original rejection was a false negative caused by applying exact comparison
before a safe, narrowly defined case-normalization step. Grammar correction alone
does not resolve it; case restoration in v4 does.

Blanket `.lower()`/`.casefold()` comparison is not a safe general fix: `Abc12 is
the password.` and `abc12 is the password.` differ only at the initial character,
yet may refer to different credentials. Filenames and identifiers can also be
case-sensitive. The implemented fix does not send these lowercased values or attempt
to classify whether the prose is case-sensitive. It restores the original case
only when every character is already accounted for by the complete AI candidate.
No vocabulary whitelist or special treatment for Marta/the benchmark was added.

### Prompt 4: a time containing a colon

Source: `Recordatorio por teléfono al 699888777: cita a las 10:00`

The source grammar identifies the body `cita a las 10:00`; the mock splits on the
last colon in the entire input and returns only `00`. Recipient and channel remain
valid. Parsing/normalization succeeds, then exact content correspondence rejects
`message_mismatch`. This is substantive information loss, not a punctuation-only
format difference. The application never rebuilds the time from source text.

Observed: ten semantic rejections, one invalid-field/syntax failure and one refusal.
Zero accepted candidates.

## Case, punctuation and scope controls

The regression suite accepts intact capitalized bodies, exclamation marks, times
containing colons, quoted codes, and alias/Markdown variants. The final strict guard
still rejects nonmatching text. A separate normalization step now restores ASCII
case before that guard, preserving source passwords, filenames and identifiers.
It rejects missing punctuation that can change meaning
(`No, enviar.` versus `No enviar.`), altered number notation and incomplete times.
Punctuation is not causing the Marta rejection. Outer delimiter whitespace and
supported quote wrappers are handled separately from message content.

No general punctuation deletion or case-insensitive final comparison was introduced.
The first correction added the `que` delimiter (v3, 141 total tests). The second
added bounded case restoration and durable audit metadata (v4, 156 total tests).

## Bounded normalization and safety invariant

The real AI client now performs: envelope/refusal checks, bounded parsing and alias
normalization, Pydantic validation, optional ASCII case restoration, then strict
source correspondence. Notification delivery still uses the durably stored result.

For case restoration, the explicitly delimited source body and AI body must have
equal lengths. At every position, characters must either be identical or both be
ASCII letters with the same lowercase form. Any different digit, punctuation mark,
whitespace character, missing character, Unicode case expansion or non-ASCII case
change prevents restoration. Only qualifying case positions take their original
source representation; the source recipient/channel are never substituted for
absent or conflicting AI fields. Strict validation then checks all three fields.

This is complete-message normalization, not a fallback extractor: missing fields,
refusals, truncation and differing content remain rejected. The algorithm works
identically for arbitrary recipients and messages, including `Abc12 is the password.`
and `Upload Config.json.`, without sending the altered case returned by the mock.
No classifier or new framework is required to distinguish sensitive prose.

`case_restored` is persisted separately from the syntax `repaired` flag and counted
by the outcome verifier. Schema v3 migrates existing v1/v2 databases without changing
their payloads. The real Docker migration retained all 3,928 preexisting terminal
records and initialized their new flag to zero. Delivery retries continue to use
the exact stored normalized extraction, without another AI call.

Replaying the same 48 captured real responses through v4 accepts the nine complete
Marta candidates with source case restored. Thirty semantic rejections, six extraction
failures and three refusals remain. This isolates the correction from mock randomness;
replay is a regression diagnostic, not a substitute for the real endpoint or an
actual notification benchmark.

## Independent corpus comparison and actual notifications

The previous benchmark corpus achieved 435 independently matched sends from 480
requests, with 44 refusals and one exhausted extraction. It deliberately used
lowercase, colon-delimited messages, avoiding the mock's known destructive behavior.
That demonstrates integration and fidelity within that corpus, not general semantic
recall or coverage of natural-language phrasing. Its 90.625% send fraction should
not be generalized to the official corpus or arbitrary user input.

On the rebuilt v3 application, two additional 12-request control groups were run:

- Marta-shaped requests with an **originally lowercase** body: ten sent, two refusals.
- Independent email/SMS recipients and lowercase complete bodies: all twelve sent.

Across these 24 accepted controls, all 22 sends matched independent expected
recipient/message/channel values, all 24 public states matched durable results,
and provider access logs corroborated exactly 22 distinct trace-ID confirmations.
Terminal extraction, semantic, infrastructure and delivery failures were all zero;
refusals were two. There were 30 AI attempts and 22 notification attempts, including
six recovered extraction failures. No refusal was retried toward success.

These controls do not alter any original prompt or k6 script; they isolate the
capitalization cause and prove that delivery is functional. An initial diagnostic
client overlapped container recreation and disconnected before accepting any work;
that attempt was excluded and recorded separately. All reported controls began
after the rebuilt app was healthy. They are outcome checks, not new throughput claims.

## Verification and reproduction

The targeted regression suite first produced three failures for complete, correct
`que` clauses. After the one-rule correction, all 141 local tests passed in 2.45 s.
The first Linux suite, overlapping control work on the shared developer machine,
passed 140 and failed one existing concurrent quota test with
`sqlite3.OperationalError: database is locked` under its one-second busy timeout
(39.03 s total). An isolated rerun also passed 140 and failed the same test (36.86 s).
The failing test separately passed on macOS in 0.25 s. Both Linux failures are
retained in `linux-tests.log` and `linux-tests-isolated.log`.

The failed test races 100 quota reservations across two SQLite connections and
assumes every acquisition succeeds within the repository's one-second busy timeout.
The deployed service uses one repository connection; workers already catch storage
exceptions and retry their loop. This failure concerns contention/timing in an
existing test, not message parsing. Its precise environmental cause is unproven;
the isolated failure rules out attributing it solely to overlapping control traffic.
No timeout was increased and no failing test was removed to make the report green.
The Linux suite on that container writable filesystem was **not fully passing**.

After both corrections, all 156 tests passed locally in 3.37 s. Running the complete
final suite in Linux with only its temporary test databases on `/tmp` tmpfs passed
all 156 in 8.07 s; no test or busy timeout was changed. This establishes a passing
Linux configuration and evidence of storage/timing sensitivity, not a claim that
the earlier writable-filesystem failure disappeared or that tmpfs proves power-loss
durability. The real app continues using the persistent named volume; k6 and
durable outcome checks run against that volume. Raw failure reports are retained.

To reproduce the relevant deterministic checks:

```sh
.venv/bin/pytest -q app/tests/test_semantic_boundaries.py app/tests/test_extraction.py
.venv/bin/pytest -q
.venv/bin/ruff check app scripts
git diff --exit-code 82224da -- provider platform/k6
```

For real-provider reproduction, submit each original prompt unchanged to
`POST http://127.0.0.1:3001/v1/ai/extract`, with the public challenge API key and
the system/user message envelope in `AIClient`. Record every response, including
refusals and malformed outputs; do not sample until a preferred result appears.
Run `parse_extraction` and `validate_semantics` on each returned assistant content
separately to expose the exact boundary, with `restore_message_case` between parsing
and validation when testing v4. `replayed-after-fix.json` retains the grammar-only
replay with zero accepts; `replayed-v4.json` records the nine newly valid candidates.

The original scenario is rerun using the unchanged script and outcome-verification
procedure from [testing.md](testing.md). Each run waits for all accepted work to
drain. No endpoint bypass, mock edits, supplied-test edits, benchmark-specific
production condition or push is part of this investigation.

## Grammar-only rerun, before case restoration

Source `8438f48`, parser v3, 16 AI / 4 notification workers: 651 accepted requests,
1,953 HTTP calls, 4,557 passing checks, zero HTTP failures, zero sends. Terminal
categories: 584 semantic and 67 refusal; zero terminal extraction, infrastructure
or delivery failures. All 584 semantic codes were `message_mismatch`, confirming
that the unsupported `que` grammar was corrected but no truncated body was repaired.

There were 722 extraction attempts, including 67 recoverable extraction failures
and four recoverable infrastructure `transport_error` failures. Provider logs
recorded 718 extraction HTTP 200 responses and zero notification calls; the four
unconfirmed transport attempts must not be presented as provider confirmations.
All 651 terminal API states matched persistence. Drain span was 148.932 seconds.

HTTP mean latency was 2,011 ms and p95 4,289 ms, much slower than the historical
run; the closed-loop k6 scenario consequently admitted fewer requests. These are
observations on the current shared Docker environment, not evidence that the
one-rule grammar change caused a performance regression. The storage-sensitive
Linux test results are also retained rather than masking environment differences.

## Final unchanged-k6 run with case restoration

Source `7954745`, parser `conservative-v4`, 16 AI / 4 notification workers,
application image
`sha256:358c66a34ba56bb67d8d9cd5f1be799fc8e41ff253bc7967bb9276577ea46982`:
1,995 accepted requests, 5,985 HTTP calls, **13,965 passing checks**, zero failed
checks and zero HTTP failures. The running Python sources matched host SHA-256
hashes. All accepted requests reached a terminal state; all 1,995 public states
matched the durable record.

**450 notifications were confirmed, and every persisted sent payload matched an
independent expected recipient, complete message and channel.** Expected payloads
were manually specified for the four original inputs, not derived from AI output
or the production semantic validator. Provider access logs independently recorded
exactly 450 HTTP 200 notification confirmations for 450 distinct request trace IDs.
All were Marta requests with the exact message `Tienes un nuevo mensaje.`. The mock
acknowledgment is not proof of delivery to a real inbox.

| Original prompt | Accepted | Sent | Semantic failure | Refusal | Extraction / infrastructure / delivery terminal failures |
| --- | ---: | ---: | ---: | ---: | --- |
| Juan, `diciendo que` | 505 | 0 | 438 | 67 | 0 / 0 / 0 |
| SMS, bare `que` | 460 | 0 | 405 | 55 | 0 / 0 / 0 |
| Marta, capitalized body | 502 | 450 | 0 | 52 | 0 / 0 / 0 |
| Reminder with `10:00` | 528 | 0 | 477 | 51 | 0 / 0 / 0 |
| Total | 1,995 | 450 | 1,320 | 225 | 0 / 0 / 0 |

Marta's observed delivery fraction was 450/502 (89.64%); all its other outcomes
were explicit refusals, never converted into notifications. Across the whole
official corpus the sending fraction was 22.56%, because the other three prompts
still contain destructive mock extraction defects. This is not directly comparable
to the independent corpus's 90.625%: the workload and safe-send denominator differ.

The run used 2,230 AI attempts and 450 notification attempts. All 2,230 extraction
HTTP 200s were corroborated in provider logs. Attempt failure categories were
1,320 semantic, 225 refusal and 235 extraction; infrastructure and delivery attempt
failures were zero. Those 235 extraction failures comprised 164 invalid-field/syntax
responses and 71 truncated/missing objects. None ended as a terminal extraction
failure in this run. All 450 accepted extractions recorded `case_restored=1`; 42
also recorded syntax repair. These flags are separate, overlapping observations.

HTTP mean latency was 201.810 ms, p95 662.667 ms and maximum 806.288 ms. Mean AI
attempt duration was 2.288 s, p95 2.973 s. Mean notification duration was 0.344 s.
Mean terminal latency was 143.014 s, p95 284.915 s, maximum 316.357 s; the whole
batch drained in 326.376 s. These timings describe the current shared environment,
not a controlled throughput improvement over earlier runs. No tests, builds or
other application producers overlapped this final k6 run and drain.

Raw summaries, timestamps, independent expectations, per-prompt outcome counts,
provider logs and source/image provenance are retained under ignored
`docs/evidence/zero-sends/official-v4/`. The earlier zero-send run remains under
`official/` rather than being overwritten. The original provider and k6 files
match baseline `82224da` byte for byte.

## Scope of the correction

Two demonstrated false-rejection mechanisms were corrected: explicit Spanish
`que` clause coverage and complete-message ASCII case restoration. No missing
recipient/body/type is invented, no truncated content is reconstructed, no refusal
is retried toward a permissive answer, and the required AI endpoint is always used.
Punctuation changes, Unicode case transformations and unsupported ambiguous
language remain conservative rejection cases. The separate Linux writable-storage
contention finding remains documented; it is not hidden by the successful tmpfs
test run or by the notification outcome improvement.
