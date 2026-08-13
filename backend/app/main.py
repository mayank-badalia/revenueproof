"""RevenueProof API entrypoint.

Start-up deliberately performs real connectivity checks against every backing
service and prints the result to the terminal. Step 2a's integration reality-check
asks whether a call actually went out; the answer should be visible before any
feature runs, not discovered halfway through a verification.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.core import cache, graph_db, llm
from app.core.config import settings
from app.core.db import dispose_engine, healthcheck as db_healthcheck
from app.core.events import EventKind, Severity, emit
from app.core.schema_init import create_schema

BANNER = r"""
  ___                             ___                __
 | _ \_____ _____ _ _ _  _ ___   | _ \_ _ ___  ___ / _|
 |   / -_) V / -_) ' \ || / -_)  |  _/ '_/ _ \/ _ \  _|
 |_|_\___|\_/\___|_||_\_,_\___|  |_| |_| \___/\___/_|
  Evidence-backed revenue verification
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(BANNER, flush=True)
    emit(EventKind.SYSTEM, f"Starting {settings.app_name} ({settings.environment})")

    # --- PostgreSQL: required. Without it there is no system of record. ---
    db_status = await db_healthcheck()
    if db_status["ok"]:
        emit(
            EventKind.SYSTEM,
            f"PostgreSQL connected (server {db_status['version']})",
            severity=Severity.SUCCESS,
        )
        await create_schema()
    else:
        emit(
            EventKind.ERROR,
            f"PostgreSQL unavailable: {db_status.get('error')}",
            severity=Severity.ERROR,
        )

    # --- Redis: degraded mode is acceptable (loses dedup, not truth). ---
    redis_status = await cache.healthcheck()
    emit(
        EventKind.SYSTEM,
        f"Redis {'connected' if redis_status['ok'] else 'unavailable'}",
        severity=Severity.SUCCESS if redis_status["ok"] else Severity.WARNING,
    )

    # --- Neo4j: required for the evidence graph, optional for classification. ---
    graph_status = await graph_db.healthcheck()
    if graph_status["ok"]:
        emit(EventKind.SYSTEM, "Neo4j connected", severity=Severity.SUCCESS)
        await graph_db.ensure_constraints()
    else:
        emit(
            EventKind.SYSTEM,
            f"Neo4j unavailable ({graph_status.get('error', '')[:80]}) — "
            "graph features will report degraded",
            severity=Severity.WARNING,
        )

    # --- LLM: optional. Deterministic rules produce the financial figures. ---
    llm_status = await llm.healthcheck()
    if llm_status["ok"]:
        emit(
            EventKind.SYSTEM,
            f"{llm_status['provider']} connected — proposer={llm_status['proposer']} "
            f"critic={llm_status['critic']}",
            severity=Severity.SUCCESS,
        )
        if not llm_status.get("proposer_available") or not llm_status.get("critic_available"):
            emit(
                EventKind.SYSTEM,
                f"A configured {llm_status['provider']} model is not in the account's "
                "model list; check the proposer/critic model settings",
                severity=Severity.WARNING,
            )
    else:
        emit(
            EventKind.SYSTEM,
            f"Language model not available ({llm_status.get('reason') or llm_status.get('error')}) — "
            "agents run deterministic-only",
            severity=Severity.WARNING,
        )

    # Refuse to look production-ready while shipping development secrets. Stated
    # at start-up, where an operator sees it, rather than discovered by whoever
    # forges the first token.
    for problem in settings.assert_production_ready():
        emit(EventKind.ERROR, f"INSECURE CONFIGURATION: {problem}",
             severity=Severity.ERROR)

    providers = settings.provider_status()
    live = [name for name, ok in providers.items() if ok]
    emit(
        EventKind.SYSTEM,
        f"Live provider credentials: {', '.join(live) if live else 'none'}",
        severity=Severity.INFO,
        providers=providers,
    )
    emit(
        EventKind.SYSTEM,
        f"API ready on http://{settings.api_host}:{settings.api_port}",
        severity=Severity.SUCCESS,
    )

    yield

    emit(EventKind.SYSTEM, "Shutting down")
    await dispose_engine()
    await cache.close_client()
    await graph_db.close_driver()


app = FastAPI(
    title="RevenueProof API",
    description=(
        "Verifies whether a startup's claimed revenue is supported by contracts, "
        "invoices, payment records, refunds and bank receipts."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# Local development frequently shifts ports when 3000 is occupied, so any
# loopback origin is allowed here. Production pins `frontend_origin` exactly.
_LOCAL_ORIGIN_PATTERN = r"^http://(localhost|127\.0\.0\.1)(:\d+)?$"

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins(),
    allow_origin_regex=_LOCAL_ORIGIN_PATTERN if settings.environment == "local" else None,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Emit every inbound request to the terminal with its status and duration."""
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000
        emit(
            EventKind.ERROR,
            f"{request.method} {request.url.path} raised {type(exc).__name__}: {exc}",
            severity=Severity.ERROR,
            duration_ms=elapsed,
        )
        raise
    elapsed = (time.perf_counter() - started) * 1000

    # Health polling and the event stream would otherwise drown the trace.
    if not request.url.path.startswith(("/health", "/api/v1/events/stream")):
        emit(
            EventKind.API_CALL,
            f"{request.method} {request.url.path} → {response.status_code}",
            severity=Severity.SUCCESS if response.status_code < 400 else Severity.WARNING,
            duration_ms=elapsed,
        )
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Fail in a contained, understandable way rather than crashing the app."""
    emit(
        EventKind.ERROR,
        f"Unhandled error on {request.method} {request.url.path}: {type(exc).__name__}: {exc}",
        severity=Severity.ERROR,
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_error",
            "detail": str(exc)[:500],
            "path": request.url.path,
        },
    )


app.include_router(api_router, prefix="/api/v1")


@app.get("/health", tags=["system"])
async def health():
    """Live status of every dependency, used by the UI's connection banner."""
    database, redis_status, graph, llm_status = (
        await db_healthcheck(),
        await cache.healthcheck(),
        await graph_db.healthcheck(),
        await llm.healthcheck(),
    )
    return {
        "status": "ok" if database["ok"] else "degraded",
        "services": {
            "postgres": database,
            "redis": redis_status,
            "neo4j": graph,
            "llm": llm_status,
        },
        "providers": settings.provider_status(),
        "environment": settings.environment,
    }
