from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .checkpointing import postgres_checkpointer
from .database import DatabaseSettings, PostgresDatabase
from .paper_trading import PostgresPaperLedger
from .rag import LocalKnowledgeIndex, PostgresKnowledgeIndex


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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database = PostgresDatabase(DatabaseSettings.from_env()) if ledger is None else None
        app.state.ledger = ledger or PostgresPaperLedger(database)
        app.state.knowledge = knowledge_index or PostgresKnowledgeIndex(database)
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
        response = await call_next(request)
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
