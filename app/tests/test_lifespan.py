import asyncio
import json
from unittest.mock import patch

import httpx
import pytest
from notification_service.api import create_app
from notification_service.config import Settings
from notification_service.rate_limiter import RateLimiter


@pytest.mark.parametrize("source_body", ["ready", "Ready"])
async def test_api_lifespan_processes_through_both_http_endpoints(tmp_path, source_body):
    calls = []
    sent = asyncio.Event()

    def respond(request):
        calls.append(request)
        if request.url.path == "/v1/ai/extract":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
                                    {"to": "test@example.org", "message": "ready", "type": "email"}
                                ),
                            }
                        }
                    ]
                },
            )
        assert request.url.path == "/v1/notify"
        assert json.loads(request.content)["message"] == source_body
        sent.set()
        return httpx.Response(200, json={"status": "delivered", "provider_id": "p-confirmed"})

    app = create_app(
        Settings(database_path=str(tmp_path / "jobs.db"), provider_api_key="key"),
        provider_transport=httpx.MockTransport(respond),
    )
    with patch.object(RateLimiter, "start", new=no_startup_delay):
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://app"
            ) as client:
                response = await client.post(
                    "/v1/requests",
                    json={"user_input": "Send email to test@example.org: " + source_body},
                )
                request_id = response.json()["id"]
                await client.post(f"/v1/requests/{request_id}/process")
                await asyncio.wait_for(sent.wait(), 2)
                # Graceful drain includes the final durable completion.
                await app.state.workers.stop()
                assert (await client.get(f"/v1/requests/{request_id}")).json()["status"] == "sent"
                async with app.state.repository.db.execute(
                    "SELECT case_restored,extracted_json FROM requests WHERE id=?", (request_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                assert row["case_restored"] == int(source_body != "ready")
                assert json.loads(row["extracted_json"])["message"] == source_body
                assert (await client.get("/health/ready")).status_code == 503
    assert [r.url.path for r in calls] == ["/v1/ai/extract", "/v1/notify"]
    assert all(r.headers["X-API-Key"] == "key" for r in calls)


async def no_startup_delay(self):
    pass


async def test_workers_require_explicit_credentials(tmp_path):
    app = create_app(Settings(database_path=str(tmp_path / "jobs.db")))
    with pytest.raises(RuntimeError, match="API_KEY"):
        async with app.router.lifespan_context(app):
            pass
