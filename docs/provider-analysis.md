# Supplied provider analysis

This analysis comes from the immutable `provider/app.py`, `provider/responses.py`,
original README, k6 scripts and Grafana provisioning. It describes the challenge
mock, not the behavior of a production language model.

## HTTP contracts

Both endpoints require `X-API-Key: test-dev-2026`. A wrong key produces 401; an
absent key produces 403 under the supplied FastAPI security dependency. Actual
HTTPException responses use `detail`, despite the documented `error` schema.
The application classifies responses by status and never requires the error body's
wording to match the documentation.

`POST /v1/ai/extract` accepts `{"messages":[{"role":"system","content":"..."},
{"role":"user","content":"..."}]}`. The mock uses only the last user message.
The normal envelope includes `id`, `object`, `created`, `model` and `choices`;
each choice contains `index`, `message.role`, `message.content` and `finish_reason`.
The application requires one assistant choice, checks structured refusal/content
filtering, then treats its content as untrusted text. The mock's `finish_reason`
defaults to `stop` even when the generated content is truncated; envelope validity
does not establish content validity.

`POST /v1/notify` takes `{"to":"...","message":"...","type":"email|sms"}`.
Optional query parameters are `priority` and `trace_id`. The application passes
its request UUID as trace ID, which appears in access logs but is not an idempotency
key. A normal 200 returns `{"status":"delivered","provider_id":"p-..."}`.
The random four-digit provider identifier can collide. No lookup, deduplication
or actual email/SMS delivery is implemented. The application requires a strict
confirmation before persisting `sent`.

Notification latency is uniform between 0.1 and 0.5 seconds; concurrent calls are
limited to 50, with a shared 50-per-10-second sliding admission quota. Random 500s
are possible in code but the supplied `FAIL_RATE` is 0.0. Deterministic tests inject
5xx/429/timeout responses without editing this provider or depending on chance.

## Extraction before output formatting

Every extraction waits uniformly 1.5–3.0 seconds, mean 2.25 seconds. The original
input is lowercased. A regex searches for an email and another for a phone; the first
email wins over a phone if both are present. Channel selection follows keywords
and those regex matches, not the system prompt or user-authority boundaries.

If a target/channel cannot be found, the mock emits an explanation that information
is missing. This is an extraction failure, distinct from its explicit refusal
messages. Otherwise the message is the suffix after the **last** colon, or the
first 50 characters of the entire lowercased instruction when there is no colon.
This can truncate messages, include routing text, change case-sensitive content,
or select a contact mentioned inside the body as recipient. Valid JSON therefore
still requires semantic verification against the original source.

The output generator interpolates strings directly instead of JSON-encoding each
field. Quotes, backslashes and newlines inside values can corrupt even its nominal
success formats. Output-format probabilities do not equal notification-success
probabilities for arbitrary inputs.

## Stochastic output distribution

| Output family | Probability | Exact branches and treatment |
| --- | ---: | --- |
| Canonical JSON | 50% | `to`, `message`, `type`; validate every field and semantics |
| Alternative names/case | 10% | 3.3% Recipient/body/channel; 3.3% To/Message/Type; 3.4% destination/text/method; known aliases only |
| Metadata or omissions | 10% | 3.3% confidence/latency_ms; 3.3% absent type; 3.4% absent to; never infer missing fields locally |
| Markdown/prose | 10% | 3.3% json fence; 3.3% generic fence; 3.4% surrounding prose; one unambiguous object only |
| Malformed syntax | 10% | 3.3% truncated; 3.3% single quotes; 3.4% bare keys; repair only the latter two when unequivocal |
| Refusal | 10% | 3.3% personal-contact refusal; 3.3% safety/spam refusal; 3.4% sensitive-information refusal; immediately terminal |

Probabilities follow independent `random.random()` branches with nested thresholds
0.33/0.66; they are not equally rounded thirds. For a simple safely interpolated,
semantically representable input, approximately 80% of individual responses are
recoverable/complete, 10% are retryable malformed/missing output and 10% refuse.
With at most three attempts and no refusal retry, the theoretical sending fraction
under those simplifying assumptions is `0.8 * (1 + 0.1 + 0.1²) = 0.888`, refusal
fraction `0.1 * (1 + 0.1 + 0.1²) = 0.111`, exhausted extraction fraction `0.001`.
These are code-derived expectations, not guarantees or measured prompt quality.
Real batch measurements and their denominators are reported in [validation.md](validation.md).

## Safe processing boundary

1. Bound the HTTP envelope and attempt duration; validate its structure and refusal
   signals before interpreting model content.
2. Bound UTF-8 content; reject recognized refusals before attempting syntax recovery.
3. Locate one object while tracking quoted strings. Reject arrays, competing objects,
   excessive nesting, duplicate keys and ambiguous content.
4. Parse strict JSON. A bounded flat lexer permits single-quoted strings and bare
   keys; it never evaluates code or completes missing delimiters/content.
5. Normalize only documented aliases, channel case and permitted recipient formatting.
   Ignore only finite numeric confidence/latency metadata. Reject collisions and
   unexpected fields.
6. Use strict Pydantic v2 validation for a single recipient, nonempty complete
   message and email/SMS channel. No default fields or country-code invention.
7. Verify source correspondence using the conservative evidence grammar. A complete
   equal-length message may have ASCII letter case restored from the explicit
   source; every other character must already match. Record this separately from
   syntax repair. Then require exact message, recipient and channel correspondence.
   This cannot supply absent fields, complete truncation or bypass AI extraction.
8. Commit the validated result durably, then let a separately rate-limited delivery
   worker send it. Never combine fields across attempts or re-extract on delivery retry.

The versioned system prompt describes production-oriented extraction and explicit
error outcomes. The mock ignores those instructions, so prompt quality remains a
human-review and future real-model evaluation responsibility. The implementation
does not change the prompt to exploit mock formatting or special-case k6 inputs.
