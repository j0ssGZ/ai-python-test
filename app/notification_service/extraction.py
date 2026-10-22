"""Bounded syntax recovery and conservative evidence checks, never replacement extraction."""

import json
import math
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from notification_service.domain import FailureCategory as Category
from notification_service.domain import ProcessingError

PARSER_VERSION = "conservative-v1"
MAX_OUTPUT_BYTES = 65_536
ALIASES = {
    "to": "to",
    "recipient": "to",
    "destination": "to",
    "message": "message",
    "body": "message",
    "text": "message",
    "type": "type",
    "channel": "type",
    "method": "type",
}
EMAIL = TypeAdapter(EmailStr)
REFUSAL = re.compile(
    r"refus(?:ed|al)|no tengo permitido|viola.{0,30}pol[ií]ticas|potential spam|"
    r"cannot comply|not allowed|do not send|no enviar",
    re.I,
)


def normalize_recipient(value: str, channel: str) -> str:
    value = value.strip()
    if channel == "email":
        if any(c.isspace() or c in "<>" for c in value):
            raise ValueError("Only a single plain email address is supported")
        return str(EMAIL.validate_python(value))
    if not re.fullmatch(r"\+?[0-9 ()-]+", value):
        raise ValueError("Invalid phone syntax")
    normalized = re.sub(r"[ ()-]", "", value)
    if not re.fullmatch(r"\+?[0-9]{7,15}", normalized):
        raise ValueError("Invalid phone length")
    return normalized


class Notification(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    to: str = Field(min_length=1, max_length=320)
    message: str = Field(min_length=1, max_length=8_000)
    type: Literal["email", "sms"]

    @field_validator("message")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip() or any(ord(c) < 32 and c not in "\n\r\t" for c in value):
            raise ValueError("Empty message or unsupported control characters")
        return value

    @model_validator(mode="after")
    def recipient(self) -> "Notification":
        object.__setattr__(self, "to", normalize_recipient(self.to, self.type))
        return self


@dataclass(frozen=True)
class ParsedExtraction:
    notification: Notification
    repaired: bool


def invalid(code: str, *, retryable: bool = True) -> ProcessingError:
    return ProcessingError(Category.EXTRACTION, code, retryable=retryable)


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise invalid("duplicate_key", retryable=False)
        result[key] = value
    return result


def strict_json(text: str):
    def reject_constant(value):
        raise invalid("nonfinite_value", retryable=False)

    return json.loads(text, object_pairs_hook=unique_pairs, parse_constant=reject_constant)


def candidate_object(raw: str) -> tuple[str, str]:
    start = None
    end = None
    depth = 0
    quote = None
    escaped = False
    for i, char in enumerate(raw):
        if start is None:
            if char == "{":
                start, depth = i, 1
            continue
        if end is not None:
            if char in "{}[]":
                raise invalid("multiple_objects", retryable=False)
            continue
        if escaped:
            escaped = False
        elif quote:
            if char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "{":
            depth += 1
            if depth > 2:
                raise invalid("nested_output", retryable=False)
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
    if start is None or end is None:
        raise invalid("missing_or_truncated_object")
    outside = raw[:start] + raw[end:]
    if "[" in outside or "]" in outside:
        raise invalid("array_output", retryable=False)
    return raw[start:end], outside


def repair_flat_object(text: str) -> dict:
    """Accept only flat key:value pairs, quoted strings, and numeric metadata.

    A lexer preserves string contents; no eval, global quote substitution, missing
    delimiter completion, adjacent strings, comments, or executable expressions.
    """
    token = re.compile(
        r"""\s*("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[A-Za-z_][A-Za-z_0-9]*|-?[0-9]+(?:\.[0-9]+)?|[{},:])"""
    )
    tokens = []
    pos = 0
    while pos < len(text):
        match = token.match(text, pos)
        if match is None:
            if text[pos:].strip():
                raise invalid("unrepairable_syntax")
            break
        tokens.append(match.group(1))
        pos = match.end()
    if not tokens or tokens[0] != "{" or tokens[-1] != "}":
        raise invalid("unrepairable_syntax")

    def string(value: str) -> str:
        if value.startswith('"'):
            return json.loads(value)
        if not value.startswith("'"):
            raise invalid("invalid_string")
        # Convert delimiters only; JSON escape semantics remain enforced.
        out = []
        i = 1
        while i < len(value) - 1:
            c = value[i]
            if c == "\\":
                i += 1
                following = value[i]
                out.append("'" if following == "'" else "\\" + following)
            else:
                out.append('\\"' if c == '"' else c)
            i += 1
        return json.loads('"' + "".join(out) + '"')

    pairs = []
    i = 1
    while i < len(tokens) - 1:
        if i + 3 >= len(tokens) or tokens[i + 1] != ":":
            raise invalid("unrepairable_syntax")
        key_token, value_token = tokens[i], tokens[i + 2]
        key = string(key_token) if key_token[0] in "\"'" else key_token
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key):
            raise invalid("invalid_key")
        if value_token[0] in "\"'":
            value = string(value_token)
        elif key in ("confidence", "latency_ms"):
            value = json.loads(value_token)
        else:
            raise invalid("invalid_string")
        pairs.append((key, value))
        i += 3
        if i == len(tokens) - 1:
            break
        if tokens[i] != "," or i + 1 == len(tokens) - 1:
            raise invalid("unrepairable_syntax")
        i += 1
    return unique_pairs(pairs)


def parse_extraction(raw: str) -> ParsedExtraction:
    try:
        size = len(raw.encode("utf-8"))
    except UnicodeError as exc:
        raise invalid("invalid_unicode", retryable=False) from exc
    if size > MAX_OUTPUT_BYTES:
        raise invalid("output_too_large", retryable=False)
    if REFUSAL.search(raw.split("{", 1)[0]):
        raise ProcessingError(Category.REFUSAL, "ai_refused")
    # Refusals in surrounding prose take precedence; quoted message content is data.
    if "{" not in raw:
        if REFUSAL.search(raw):
            raise ProcessingError(Category.REFUSAL, "ai_refused")
        if "unable to identify" in raw.lower():
            raise invalid("missing_information", retryable=False)
        raise invalid("unstructured_output")
    text, outside = candidate_object(raw)
    if REFUSAL.search(outside):
        raise ProcessingError(Category.REFUSAL, "ai_refused")
    repaired = False
    try:
        try:
            data = strict_json(text)
        except (json.JSONDecodeError, ValueError):
            data = repair_flat_object(text)
            repaired = True
        if not isinstance(data, dict):
            raise invalid("invalid_object")
        if "error" in data:
            if set(data) != {"error", "missing_fields"} or not isinstance(data["error"], str):
                raise invalid("conflicting_error_output", retryable=False)
            code = data["error"]
            if code == "refused":
                raise ProcessingError(Category.REFUSAL, "ai_refused")
            if code in ("ambiguous_request", "unsupported_channel"):
                raise ProcessingError(Category.SEMANTIC, code)
            raise invalid(
                "missing_information" if code == "missing_information" else "invalid_error",
                retryable=False,
            )
        normalized = {}
        for name, value in data.items():
            key = name.strip().lower()
            if key in ("confidence", "latency_ms"):
                if (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not math.isfinite(value)
                ):
                    raise invalid("invalid_metadata")
                continue
            if key not in ALIASES:
                raise invalid("unknown_field", retryable=False)
            key = ALIASES[key]
            if key == "type" and isinstance(value, str):
                value = value.strip().lower()
            if key in normalized:
                raise invalid("duplicate_alias", retryable=False)
            normalized[key] = value
        return ParsedExtraction(Notification.model_validate(normalized), repaired)
    except (ValueError, TypeError, ValidationError, RecursionError) as exc:
        raise invalid("invalid_fields_or_syntax") from exc


# This deliberately small grammar establishes evidence for an AI candidate. It
# never supplies a missing candidate field or substitutes any extracted content.
ROUTING = re.compile(
    r"^\s*(?:send|enviar|env[ií]a|manda|mandar|avisar|recordatorio)\s+"
    r"(?:(?:an?|un|una|por)\s+)?"
    r"(?P<channel>e-?mail|mail|correo(?: electr[oó]nico)?|sms|tel[eé]fono)\s+"
    r"(?:to|a|al|para)\s+(?P<recipient>[^:\n]+?)\s*"
    r"(?::|\s+(?:saying|diciendo(?: que)?|indicando(?: que)?)\s+)"
    r"(?P<body>.*)$",
    re.I | re.S,
)


def validate_semantics(source: str, notification: Notification) -> None:
    match = ROUTING.fullmatch(source)
    if match is None:
        raise ProcessingError(Category.SEMANTIC, "semantic_unverified")
    channel = "sms" if match["channel"].lower() in ("sms", "teléfono", "telefono") else "email"
    try:
        recipient = normalize_recipient(match["recipient"], channel)
    except ValueError as exc:
        raise ProcessingError(Category.SEMANTIC, "ambiguous_recipient") from exc
    body = match["body"].strip()
    if len(body) >= 2 and body[0] == body[-1] and body[0] in "\"'":
        body = body[1:-1]
    if not body or notification.message != body:
        raise ProcessingError(Category.SEMANTIC, "message_mismatch")
    if channel != notification.type or recipient != notification.to:
        raise ProcessingError(Category.SEMANTIC, "recipient_or_channel_mismatch")
