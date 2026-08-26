from __future__ import annotations

import logging
import os
import secrets
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import MemorySessionAuth, PostgresSessionAuth
from .brokers import BrokerApiError, GrowwBrokerClient, GrowwSettings
from .checkpointing import postgres_checkpointer
from .database import DatabaseSettings, PostgresDatabase
from .intraday import IntradayService
from .observability import Metrics, configure_logging
from .paper_trading import PostgresPaperLedger
from .rag import LocalKnowledgeIndex, PostgresKnowledgeIndex
from .research_status import regime_router_status

logger = logging.getLogger("kiwit.api")


class HaltRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class ResumeRequest(BaseModel):
    operator: str = Field(min_length=2, max_length=100)


class SearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    limit: int = Field(default=6, ge=1, le=12)


class LoginRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class SignalReviewRequest(BaseModel):
    reason: str = Field(default="Reviewed in kiwiT dashboard", min_length=2, max_length=500)


class PaperSessionRequest(BaseModel):
    amount: Decimal = Field(gt=0, le=1_000_000, max_digits=12, decimal_places=2)
    loss_pct: Decimal = Field(gt=0, le=25, max_digits=8, decimal_places=4)
    profit_pct: Decimal = Field(gt=0, le=100, max_digits=8, decimal_places=4)


def _valid_api_key(supplied: str) -> bool:
    expected = os.getenv("KIWIT_API_KEY", "")
    previous = os.getenv("KIWIT_PREVIOUS_API_KEY", "")
    current_match = len(expected) >= 24 and secrets.compare_digest(supplied, expected)
    previous_match = len(previous) >= 24 and secrets.compare_digest(supplied, previous)
    return current_match or previous_match


def _require_authenticated(request: Request, x_kiwit_api_key: Annotated[str | None, Header()] = None) -> None:
    if _valid_api_key(x_kiwit_api_key or ""):
        return
    token = request.cookies.get("kiwit_session", "")
    if token and request.app.state.auth.authenticate(token):
        return
    logger.warning("authentication_failed")
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")


def create_app(
    *, ledger: Any | None = None, knowledge_index: Any | None = None, broker_client: Any | None = None,
    auth_service: Any | None = None,
) -> FastAPI:
    owns_index = knowledge_index is None
    configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database = PostgresDatabase(DatabaseSettings.from_env()) if ledger is None else None
        app.state.database = database
        app.state.ledger = ledger or PostgresPaperLedger(database)
        app.state.knowledge = knowledge_index or PostgresKnowledgeIndex(database)
        if broker_client is not None:
            app.state.broker = broker_client
        else:
            try:
                app.state.broker = GrowwBrokerClient(GrowwSettings.from_env())
            except ValueError:
                app.state.broker = None
        app.state.intraday = IntradayService(database, app.state.broker) if database is not None else None
        app.state.metrics = Metrics()
        app.state.auth = auth_service or (
            PostgresSessionAuth(database) if database is not None
            else MemorySessionAuth("disabled@example.invalid", secrets.token_urlsafe(32))
        )
        app.state.auth.bootstrap_admin()
        app.state.login_failures = defaultdict(deque)
        yield
        if owns_index and isinstance(app.state.knowledge, LocalKnowledgeIndex):
            app.state.knowledge.close()

    app = FastAPI(
        title="kiwiT Control Plane", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None,
        lifespan=lifespan,
    )
    allowed_hosts = [host.strip() for host in os.getenv("KIWIT_ALLOWED_HOSTS", "testserver,localhost,127.0.0.1").split(",")]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    static = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > 65_536:
                    return JSONResponse({"detail": "request body too large"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "invalid content length"}, status_code=400)
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

    protected = [Depends(_require_authenticated)]

    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok", "service": "kiwit-api", "execution": "paper-only",
            "release": os.getenv("KIWIT_RELEASE_SHA", "development"),
        }

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

    @app.get("/api/v1/research/regime-router", dependencies=protected)
    def regime_router_evidence() -> dict[str, Any]:
        return regime_router_status()

    @app.get("/api/v1/intraday/status", dependencies=protected)
    def intraday_status(request: Request) -> dict[str, Any]:
        service = request.app.state.intraday
        if service is None:
            return {"execution": "paper-only", "available": False, "freshness": None, "signals": [], "counts": {}}
        result = service.list_signals()
        result["available"] = True
        result["freshness"] = service.freshness()
        result["session"] = service.session_status()
        return result

    @app.post('/api/v1/intraday/session/run', dependencies=protected)
    def start_paper_session(body: PaperSessionRequest, request: Request) -> dict[str, Any]:
        service = request.app.state.intraday
        if service is None:
            raise HTTPException(503, 'Paper session service unavailable')
        user = request.app.state.auth.authenticate(request.cookies.get('kiwit_session', ''))
        try:
            return service.start_session(body.amount, body.loss_pct, body.profit_pct,
                                         user.email if user else 'api-key-operator')
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.post('/api/v1/intraday/session/stop', dependencies=protected)
    def stop_paper_session(request: Request) -> dict[str, Any]:
        service = request.app.state.intraday
        if service is None:
            raise HTTPException(503, 'Paper session service unavailable')
        user = request.app.state.auth.authenticate(request.cookies.get('kiwit_session', ''))
        return service.stop_session(user.email if user else 'api-key-operator') or {'state':'idle'}

    @app.post("/api/v1/intraday/signals/{signal_id}/approve", dependencies=protected)
    def approve_intraday_signal(signal_id: uuid.UUID, body: SignalReviewRequest, request: Request) -> dict[str, Any]:
        service = request.app.state.intraday
        if service is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "intraday service unavailable")
        user = request.app.state.auth.authenticate(request.cookies.get("kiwit_session", ""))
        reviewer = user.email if user else "api-key-operator"
        try:
            return service.review(signal_id, True, reviewer, body.reason)
        except KeyError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "signal not found") from error
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error

    @app.post("/api/v1/intraday/signals/{signal_id}/reject", dependencies=protected)
    def reject_intraday_signal(signal_id: uuid.UUID, body: SignalReviewRequest, request: Request) -> dict[str, Any]:
        service = request.app.state.intraday
        if service is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "intraday service unavailable")
        user = request.app.state.auth.authenticate(request.cookies.get("kiwit_session", ""))
        reviewer = user.email if user else "api-key-operator"
        try:
            return service.review(signal_id, False, reviewer, body.reason)
        except KeyError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "signal not found") from error
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error

    @app.get("/login", include_in_schema=False)
    def login_page(request: Request) -> Any:
        token = request.cookies.get("kiwit_session", "")
        if token and request.app.state.auth.authenticate(token):
            return RedirectResponse("/dashboard", status_code=303)
        return FileResponse(static / "login.html")

    @app.post("/api/v1/auth/login")
    def login(body: LoginRequest, request: Request) -> JSONResponse:
        address = request.client.host if request.client else "unknown"
        now = datetime.now(UTC)
        failures = request.app.state.login_failures[address]
        cutoff = now - timedelta(minutes=5)
        while failures and failures[0] < cutoff:
            failures.popleft()
        if len(failures) >= 5:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many login attempts; retry later")
        authenticated = request.app.state.auth.login(body.email, body.password, address)
        if authenticated is None:
            failures.append(now)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid email or password")
        failures.clear()
        token, user = authenticated
        response = JSONResponse({"status": "authenticated", "email": user.email, "role": user.role})
        response.set_cookie(
            "kiwit_session", token, max_age=12 * 60 * 60, httponly=True,
            secure=os.getenv("KIWIT_SECURE_COOKIES", "true").lower() == "true", samesite="strict", path="/",
        )
        return response

    @app.post("/api/v1/auth/logout")
    def logout(request: Request) -> JSONResponse:
        request.app.state.auth.logout(request.cookies.get("kiwit_session", ""))
        response = JSONResponse({"status": "signed_out"})
        response.delete_cookie("kiwit_session", path="/", secure=True, httponly=True, samesite="strict")
        return response

    @app.get("/api/v1/auth/me", dependencies=protected)
    def current_user(request: Request) -> dict[str, str]:
        user = request.app.state.auth.authenticate(request.cookies.get("kiwit_session", ""))
        return {"email": user.email, "role": user.role} if user else {"email": "service", "role": "api_key"}

    @app.get("/dashboard", include_in_schema=False)
    def dashboard(request: Request) -> Any:
        token = request.cookies.get("kiwit_session", "")
        if not token or not request.app.state.auth.authenticate(token):
            return RedirectResponse("/login", status_code=303)
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

    @app.get("/api/v1/paper/accounts/{account_id}/operations", dependencies=protected)
    def paper_operations(account_id: str, request: Request) -> dict[str, Any]:
        ledger = request.app.state.ledger
        if not hasattr(ledger, "operational_report"):
            status_data = ledger.account_status(account_id)
            return {
                "account_id": account_id, "status": "collecting_evidence",
                "automation": {"enabled": False, "blocked_reason": "No approved strategy", "target_sessions": 40,
                               "completed_sessions": 0, "operator_action": "Approve a validated strategy first."},
                "summary": {"initial_cash": status_data.get("cash_balance", "0"), "current_equity": status_data.get("cash_balance", "0"),
                            "realized_pnl": status_data.get("realized_pnl", "0"), "max_drawdown_pct": "0", "trade_count": 0,
                            "fees": "0", "turnover": "0", "open_positions": len(status_data.get("positions", [])), "active_incidents": 0},
                "equity_curve": [], "incidents": [], "review": {"decision": "insufficient_evidence", "checks": []},
                "failure_tests": [],
            }
        try:
            return ledger.operational_report(account_id)
        except KeyError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "paper account not found") from error

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

    def require_broker(request: Request) -> Any:
        broker = request.app.state.broker
        if broker is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Groww broker is not configured")
        return broker

    @app.get("/api/v1/broker/status", dependencies=protected)
    def broker_status(request: Request) -> dict[str, Any]:
        return {"broker": "groww", "configured": request.app.state.broker is not None, "execution": "disabled"}

    @app.get("/api/v1/broker/profile", dependencies=protected)
    def broker_profile(request: Request) -> dict[str, Any]:
        try:
            profile = require_broker(request).profile()
        except BrokerApiError as error:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error
        return {"broker": "groww", "profile": profile}

    @app.get("/api/v1/broker/holdings", dependencies=protected)
    def broker_holdings(request: Request) -> dict[str, Any]:
        try:
            holdings = require_broker(request).holdings()
        except BrokerApiError as error:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error
        return {"broker": "groww", "holdings": holdings}

    @app.get("/api/v1/broker/positions", dependencies=protected)
    def broker_positions(request: Request, segment: str = "CASH") -> dict[str, Any]:
        try:
            positions = require_broker(request).positions(segment)
        except ValueError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
        except BrokerApiError as error:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error
        return {"broker": "groww", "positions": positions}

    @app.get("/api/v1/broker/margin", dependencies=protected)
    def broker_margin(request: Request) -> dict[str, Any]:
        try:
            margin = require_broker(request).margin()
        except BrokerApiError as error:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error
        return {"broker": "groww", "margin": margin}

    @app.get("/api/v1/broker/quotes/{trading_symbol}", dependencies=protected)
    def broker_quote(request: Request, trading_symbol: str, segment: str = "CASH", exchange: str = "NSE") -> dict[str, Any]:
        try:
            quote = require_broker(request).quote(trading_symbol, segment, exchange)
        except ValueError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
        except BrokerApiError as error:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error
        return {"broker": "groww", "quote": quote}

    @app.get("/api/v1/broker/orders/{groww_order_id}", dependencies=protected)
    def broker_order(request: Request, groww_order_id: str, segment: str = "CASH") -> dict[str, Any]:
        try:
            order = require_broker(request).order_status(groww_order_id, segment)
        except ValueError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
        except BrokerApiError as error:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error
        return {"broker": "groww", "order": order}

    return app


app = create_app()
