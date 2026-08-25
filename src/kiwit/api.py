from __future__ import annotations

import logging
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .checkpointing import postgres_checkpointer
from .database import DatabaseSettings, PostgresDatabase
from .observability import Metrics, configure_logging
from .paper_trading import PostgresPaperLedger
from .rag import LocalKnowledgeIndex, PostgresKnowledgeIndex

logger = logging.getLogger("kiwit.api")


class HaltRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class ResumeRequest(BaseModel):
    operator: str = Field(min_length=2, max_length=100)


class SearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    limit: int = Field(default=6, ge=1, le=12)


def _require_api_key(x_kiwit_api_key: Annotated[str | None, Header()] = None) -> None:
    expected = os.getenv("KIWIT_API_KEY", "")
    if len(expected) < 24:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "API authentication is not configured")
    if x_kiwit_api_key is None or not secrets.compare_digest(x_kiwit_api_key, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key")


def create_app(*, ledger: Any | None = None, knowledge_index: Any | None = None) -> FastAPI:
    owns_index = knowledge_index is None
    configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database = PostgresDatabase(DatabaseSettings.from_env()) if ledger is None else None
        app.state.database = database
        app.state.ledger = ledger or PostgresPaperLedger(database)
        app.state.knowledge = knowledge_index or PostgresKnowledgeIndex(database)
        app.state.metrics = Metrics()
        yield
        if owns_index and isinstance(app.state.knowledge, LocalKnowledgeIndex):
            app.state.knowledge.close()

    app = FastAPI(
        title="kiwiT Control Plane", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None,
        lifespan=lifespan,
    )
    static = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))[:128]
        started = time.monotonic()
        app.state.metrics.begin()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
        finally:
            duration = time.monotonic() - started
            route = request.scope.get("route")
            route_name = getattr(route, "path", "unmatched")
            app.state.metrics.finish(request.method, route_name, status_code, duration)
            logger.info(
                "request_completed",
                extra={"request_id": request_id, "method": request.method, "path": request.url.path,
                       "status_code": status_code, "duration_ms": round(duration * 1000, 2)},
            )
        response.headers["X-Request-ID"] = request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; connect-src 'self'; style-src 'self'; script-src 'self'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    protected = [Depends(_require_api_key)]

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "kiwit-api", "execution": "paper-only"}

    @app.get("/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready(request: Request) -> dict[str, Any]:
        database = request.app.state.database
        if database is None:  # dependency-injected tests/local operation
            return {"status": "ok", "database": "injected"}
        try:
            result = database.healthcheck()
        except Exception as error:
            request.app.state.metrics.readiness_failed()
            logger.exception("readiness_failed")
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database unavailable") from error
        return {"status": "ok", "database": "connected", "schema_version": result["schema_version"]}

    @app.get("/metrics", dependencies=protected, response_class=PlainTextResponse)
    def metrics(request: Request) -> str:
        return request.app.state.metrics.render()

    @app.get("/dashboard", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(static / "dashboard.html")

    @app.get("/api/v1/paper/accounts/{account_id}", dependencies=protected)
    def account_status(account_id: str, request: Request) -> dict[str, Any]:
        try:
            return request.app.state.ledger.account_status(account_id)
        except KeyError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "paper account not found") from error

    @app.post("/api/v1/paper/accounts/{account_id}/halt", dependencies=protected)
    def halt(account_id: str, body: HaltRequest, request: Request) -> dict[str, Any]:
        request.app.state.ledger.halt(account_id, "API_OPERATOR_HALT", body.reason)
        return request.app.state.ledger.account_status(account_id)

    @app.post("/api/v1/paper/accounts/{account_id}/resume", dependencies=protected)
    def resume(account_id: str, body: ResumeRequest, request: Request) -> dict[str, Any]:
        request.app.state.ledger.release_halt(account_id, body.operator)
        return request.app.state.ledger.account_status(account_id)

    @app.post("/api/v1/research/search", dependencies=protected)
    def search(body: SearchRequest, request: Request) -> dict[str, Any]:
        hits = request.app.state.knowledge.search(body.query, limit=body.limit)
        return {
            "query": body.query,
            "hits": [
                {
                    "chunk_id": hit.chunk_id, "citation": hit.citation, "content": hit.content,
                    "score": hit.score, "source_type": hit.source_type,
                }
                for hit in hits
            ],
        }

    @app.get("/api/v1/workflows/{thread_id}", dependencies=protected)
    def workflow_state(thread_id: str) -> dict[str, Any]:
        with postgres_checkpointer() as saver:
            saved = saver.get_tuple({"configurable": {"thread_id": thread_id}})
        if saved is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "workflow not found")
        values = saved.checkpoint.get("channel_values", {})
        proposal = values.get("proposal", {})
        return {
            "thread_id": thread_id,
            "status": values.get("status"),
            "proposal_id": proposal.get("proposal_id"),
            "message": values.get("message"),
            "has_fill": "fill" in values,
            "checkpoint_id": saved.config.get("configurable", {}).get("checkpoint_id"),
        }

    return app


app = create_app()
