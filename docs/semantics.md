# Extraction safety and semantic scope

The AI endpoint is mandatory for every extraction attempt. The application sends
the original input as the user message and a versioned system prompt separately.
The supplied mock ignores system instructions. Its tests demonstrate integration
and response handling, **not production prompt quality**.

The parser accepts one bounded object, known aliases, Markdown wrappers, numeric
confidence/latency metadata, and unequivocal single-quote or bare-key syntax.
It never executes code, fills absent fields, joins separate answers, completes
truncation, or retries an explicit refusal to obtain a different decision.
Duplicate keys/aliases, conflicting objects, ambiguous escaping and unexpected
fields fail. Syntax repair and semantic acceptance are separate decisions.

Semantic validation is intentionally fail-closed. The initial evidence grammar
supports explicit English/Spanish routing instructions with a channel, one plain
recipient and a complete body introduced by a colon or a supported saying marker.
The grammar also accepts an explicit Spanish `que` body marker after
the single recipient. It still requires the complete AI-provided body to match.
See `ROUTING` in `extraction.py` for the exact accepted grammar. A body may be
quoted. Its complete content must equal the normalized AI candidate, including
capitalization, numbers, punctuation and internal whitespace. Parser `conservative-v4`
can restore source ASCII letter case only when the AI already supplied the entire
body with equal length and a corresponding character at every position. Every
difference must be an ASCII letter's case; punctuation, digits, whitespace and
non-ASCII characters must match exactly. The complete normalized message uses the
source's case, preserving case-sensitive identifiers instead of ignoring them.
This normalization is recorded durably as `case_restored`, separately from syntax
repair. Only outer separator whitespace is ignored. Recipient formatting
normalization removes permitted phone separators;
it never supplies a country code. Email domains use email-validator normalization.

This grammar verifies an AI candidate and permits only the bounded case restoration
described above; it cannot create a missing candidate or complete absent content.
Valid natural language outside that evidence grammar is rejected as
`semantic_unverified`. This is a deliberate coverage limitation, not a claim to
understand arbitrary language or prove immunity to every prompt injection.
Adding more patterns requires general examples and adversarial tests, not special
cases for benchmark prompts. There is no secondary model or local extraction fallback.

The mock lowercases all input, takes the suffix after the last colon, or truncates
the complete instruction to 50 characters. Even capitalization can matter (for
example, a password). Therefore case differences are restored from explicit source
evidence, never silently accepted as equivalent delivered content. Three original
k6 inputs have substantive truncation defects; the Marta case can be accepted after
case restoration. Results must distinguish semantic
failures from refusals and extraction/infrastructure errors rather than claim
successful delivery from HTTP checks. Separate lowercase, explicitly delimited
inputs exercise real successful extraction and sending without changing k6.

Literal fidelity is stricter than natural-language semantic equivalence. In the
original Marta example, changing `Tienes` to `tienes` preserves the ordinary meaning.
Before v4, rejecting it was a false negative caused by requiring raw AI case fidelity.
It was not fabricated or truncated content. V4 restores the original `Tienes` after
verifying the full candidate; it does not send altered password/filename case.
The independent lowercase
corpus avoids this limitation and does not measure broad language coverage. See
[the zero-notification investigation](zero-notifications.md) for real-provider traces,
the demonstrated grammar correction and new outcome verification.

Failure families are `extraction`, `semantic`, `refusal`, `infrastructure`, and
`delivery`. They remain separate in durable outcomes and benchmark reports.
The test corpus is deterministic; the real provider remains stochastic.
