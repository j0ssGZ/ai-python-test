import httpx

from notification_service.domain import FailureCategory, ProcessingError
from notification_service.extraction import (
    ParsedExtraction,
    parse_extraction,
    restore_message_case,
    strict_json,
    validate_semantics,
)
from notification_service.http_support import bounded_post
from notification_service.prompt import SYSTEM_PROMPT


class AIClient:
    def __init__(self, client: httpx.AsyncClient, api_key: str):
        self.client = client
        self.api_key = api_key

    async def extract(self, user_input: str) -> ParsedExtraction:
        code, body, delay = await bounded_post(
            self.client,
            "/v1/ai/extract",
            self.api_key,
            {
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_input},
                ]
            },
        )
        if code != 200:
            raise ProcessingError(
                FailureCategory.INFRASTRUCTURE,
                f"ai_http_{code}",
                retryable=code == 429 or 500 <= code < 600,
                retry_after=delay,
            )
        try:
            data = strict_json(body.decode("utf-8"))
            choices = data["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError
            choice = choices[0]
            message = choice["message"]
            if choice.get("finish_reason") == "content_filter" or message.get("refusal"):
                raise ProcessingError(FailureCategory.REFUSAL, "ai_refused")
            if choice.get("finish_reason", "stop") != "stop":
                raise ValueError
            if message.get("role") != "assistant" or not isinstance(message["content"], str):
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
            raise ProcessingError(
                FailureCategory.EXTRACTION, "invalid_ai_envelope", retryable=True
            ) from exc
        result = restore_message_case(user_input, parse_extraction(message["content"]))
        validate_semantics(user_input, result.notification)
        return result
