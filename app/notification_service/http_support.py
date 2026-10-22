import math
import time
from email.utils import parsedate_to_datetime

import httpx

from notification_service.domain import FailureCategory, ProcessingError


def retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0, delay) if math.isfinite(delay) else None


async def bounded_post(
    client: httpx.AsyncClient,
    path: str,
    key: str,
    payload: dict,
    *,
    max_bytes: int = 65_536,
    params: dict | None = None,
) -> tuple[int, bytes, float | None]:
    try:
        async with client.stream(
            "POST", path, headers={"X-API-Key": key}, json=payload, params=params
        ) as response:
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise ProcessingError(FailureCategory.INFRASTRUCTURE, "response_too_large")
            return (
                response.status_code,
                bytes(body),
                retry_after(response.headers.get("Retry-After")),
            )
    except httpx.RequestError as exc:
        raise ProcessingError(
            FailureCategory.INFRASTRUCTURE, "transport_error", retryable=True
        ) from exc
