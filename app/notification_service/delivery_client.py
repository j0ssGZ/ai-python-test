from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from notification_service.domain import FailureCategory, ProcessingError
from notification_service.extraction import Notification, strict_json
from notification_service.http_support import bounded_post


class Confirmation(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    status: Literal["delivered"]
    provider_id: str = Field(min_length=1, max_length=200)


class DeliveryClient:
    def __init__(self, client: httpx.AsyncClient, api_key: str):
        self.client = client
        self.api_key = api_key

    async def send(self, request_id: str, notification: Notification) -> str:
        code, body, delay = await bounded_post(
            self.client,
            "/v1/notify",
            self.api_key,
            notification.model_dump(),
            params={"trace_id": request_id},
        )
        if code != 200:
            category = (
                FailureCategory.INFRASTRUCTURE if code in (401, 403) else FailureCategory.DELIVERY
            )
            raise ProcessingError(
                category,
                f"notify_http_{code}",
                retryable=code == 429 or 500 <= code < 600,
                retry_after=delay,
            )
        try:
            result = Confirmation.model_validate(strict_json(body.decode("utf-8")))
            if not result.provider_id.strip():
                raise ValueError
        except (ValueError, ValidationError, ProcessingError, RecursionError) as exc:
            raise ProcessingError(FailureCategory.DELIVERY, "delivery_unknown") from exc
        return result.provider_id
