import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictStr
from starlette.types import ASGIApp, Receive, Scope, Send

from notification_service.config import Settings
from notification_service.domain import BacklogFull, InvalidInput, NotFound, Status
from notification_service.repository import SQLiteRepository
from notification_service.service import RequestService


class CreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_input: StrictStr


class RequestId(BaseModel):
    id: str


class RequestStatus(RequestId):
    status: Status


class BodyLimit:
    """Bound buffering before JSON decoding, including chunked request bodies."""

    def __init__(self, app: ASGIApp, limit: int):
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        chunks = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.limit:
                await JSONResponse({"detail": "Request body too large"}, 413)(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with SQLiteRepository(settings.database_path) as repository:
            app.state.repository = repository
            app.state.service = RequestService(repository, settings)
            yield

    app = FastAPI(title="AI Notification Service", lifespan=lifespan)
    app.add_middleware(BodyLimit, limit=settings.max_body_bytes)

    @app.exception_handler(NotFound)
    async def not_found(request: Request, exc: NotFound):
        return JSONResponse({"detail": "Request not found"}, 404)

    @app.exception_handler(InvalidInput)
    async def invalid_input(request: Request, exc: InvalidInput):
        return JSONResponse(
            {"detail": "Input must be nonblank and within the configured limit"}, 422
        )

    @app.exception_handler(BacklogFull)
    async def full(request: Request, exc: BacklogFull):
        return JSONResponse({"detail": "Backlog capacity reached"}, 503, {"Retry-After": "10"})

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request: Request, exc: sqlite3.Error):
        return JSONResponse({"detail": "Storage unavailable"}, 503)

    @app.post("/v1/requests", status_code=201, response_model=RequestId)
    async def create(payload: CreateRequest, request: Request):
        record = await request.app.state.service.create(payload.user_input)
        return {"id": record.id}

    @app.post("/v1/requests/{request_id}/process", response_model=RequestStatus)
    async def activate(request_id: str, request: Request):
        record = await request.app.state.service.activate(request_id)
        code = 200 if record.status in (Status.SENT, Status.FAILED) else 202
        return JSONResponse({"id": record.id, "status": record.status}, code)

    @app.get("/v1/requests/{request_id}", response_model=RequestStatus)
    async def get(request_id: str, request: Request):
        record = await request.app.state.service.get(request_id)
        return {"id": record.id, "status": record.status}

    @app.get("/health/ready")
    async def ready(request: Request):
        await request.app.state.repository.ping()
        return {"status": "ready"}

    return app
