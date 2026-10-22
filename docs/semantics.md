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
See `ROUTING` in `extraction.py` for the exact accepted grammar. A body may be
quoted. Its complete content must equal the AI candidate, including capitalization,
numbers, punctuation and internal whitespace. Only outer separator whitespace is
ignored. Recipient formatting normalization removes permitted phone separators;
it never supplies a country code. Email domains use email-validator normalization.

This grammar only validates an AI candidate; it never creates or replaces one.
Valid natural language outside that evidence grammar is rejected as
`semantic_unverified`. This is a deliberate coverage limitation, not a claim to
understand arbitrary language or prove immunity to every prompt injection.
Adding more patterns requires general examples and adversarial tests, not special
cases for benchmark prompts. There is no secondary model or local extraction fallback.

The mock lowercases all input, takes the suffix after the last colon, or truncates
the complete instruction to 50 characters. Even capitalization can matter (for
example, a password). Therefore case changes are not silently accepted. All four
original k6 inputs have a deterministic message-fidelity problem under this policy.
The benchmark may finish with no notifications; results must distinguish semantic
failures from refusals and extraction/infrastructure errors rather than claim
successful delivery from HTTP checks. Separate lowercase, explicitly delimited
inputs exercise real successful extraction and sending without changing k6.

Failure families are `extraction`, `semantic`, `refusal`, `infrastructure`, and
`delivery`. They remain separate in durable outcomes and benchmark reports.
The test corpus is deterministic; the real provider remains stochastic.
