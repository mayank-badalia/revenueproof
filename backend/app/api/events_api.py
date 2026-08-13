"""Live processing trace — the Processing Trace screen (§10.3).

Two transports for the same stream:
* **WebSocket** for the browser, matching the spec's stated technology.
* **SSE** as a fallback, because a corporate proxy that breaks WebSocket upgrades
  should degrade the trace rather than hide what the backend is doing.

What is streamed is an *operational* trace — actions, evidence IDs, statuses and
timings. idea_features.md §10.3 is explicit that this is not hidden chain-of-thought.
"""

from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.api.deps import DbSession, Workspace_, decode_access_token
from app.core.db import get_sessionmaker
from app.core.events import SYSTEM_CHANNEL, EventKind, Severity, bus, emit
from app.models import User, WorkspaceMember

router = APIRouter(tags=["events"])

# Emitted when no event arrives for this long, so proxies do not drop an idle socket.
HEARTBEAT_SECONDS = 25.0


async def _authorize_socket(token: str, workspace_id: str) -> bool:
    """Verify the token and membership before streaming a workspace's activity.

    WebSockets cannot carry an Authorization header from the browser, so the token
    arrives as a query parameter — but it is still verified, and membership is still
    checked. An unauthenticated socket would leak one company's processing trace.
    """
    try:
        payload = decode_access_token(token)
        user_id = uuid.UUID(payload["sub"])
    except Exception:
        return False

    async with get_sessionmaker()() as session:
        user = await session.get(User, user_id)
        if user is None or not user.is_active:
            return False
        if user.is_platform_admin:
            return True
        if workspace_id == SYSTEM_CHANNEL:
            return False  # only platform admins may watch the global firehose
        try:
            target = uuid.UUID(workspace_id)
        except ValueError:
            return False
        membership = (
            await session.execute(
                select(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == target,
                    WorkspaceMember.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        return membership is not None


@router.websocket("/events/ws/{workspace_id}")
async def event_socket(websocket: WebSocket, workspace_id: str, token: str = Query(...)):
    if not await _authorize_socket(token, workspace_id):
        await websocket.close(code=4403, reason="unauthorized")
        return

    await websocket.accept()
    emit(
        EventKind.SYSTEM,
        f"Trace viewer connected ({bus.subscriber_count(workspace_id) + 1} watching)",
        workspace_id=workspace_id,
        severity=Severity.DEBUG,
    )

    try:
        # Replay recent history so a late-joining reviewer sees context immediately.
        for historical in bus.history(workspace_id, limit=100):
            await websocket.send_text(json.dumps({"type": "event", "event": historical}))
        await websocket.send_text(json.dumps({"type": "ready"}))

        async with bus.subscribe(workspace_id) as queue:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                    await websocket.send_text(json.dumps({"type": "event", "event": event}))
                except TimeoutError:
                    await websocket.send_text(json.dumps({"type": "heartbeat"}))
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        emit(
            EventKind.ERROR,
            f"Trace socket error: {type(exc).__name__}: {exc}",
            workspace_id=workspace_id,
            severity=Severity.WARNING,
        )
    finally:
        emit(
            EventKind.SYSTEM,
            "Trace viewer disconnected",
            workspace_id=workspace_id,
            severity=Severity.DEBUG,
        )


@router.get("/events/stream/{workspace_id}")
async def event_stream(workspace_id: str, token: str = Query(...)):
    """Server-sent events fallback for the same trace."""
    if not await _authorize_socket(token, workspace_id):
        return StreamingResponse(
            iter(['data: {"type":"error","detail":"unauthorized"}\n\n']),
            media_type="text/event-stream",
            status_code=403,
        )

    async def generate():
        for historical in bus.history(workspace_id, limit=100):
            yield f"data: {json.dumps({'type': 'event', 'event': historical})}\n\n"
        async with bus.subscribe(workspace_id) as queue:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                    yield f"data: {json.dumps({'type': 'event', 'event': event})}\n\n"
                except TimeoutError:
                    yield 'data: {"type":"heartbeat"}\n\n'

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/workspaces/{workspace_id}/events")
async def recent_events(ctx: Workspace_, limit: int = Query(200, ge=1, le=500)):
    """Recent trace history over plain HTTP, for the initial page render."""
    return {"events": bus.history(str(ctx.workspace_id), limit=limit)}


@router.get("/workspaces/{workspace_id}/audit")
async def audit_log(ctx: Workspace_, session: DbSession, limit: int = Query(100, ge=1, le=500)):
    """The durable audit trail, plus a live integrity check of its hash chain."""
    from app.models import AuditEvent
    from app.services.audit import verify_chain

    rows = (
        (
            await session.execute(
                select(AuditEvent)
                .where(AuditEvent.workspace_id == ctx.workspace_id)
                .order_by(AuditEvent.sequence.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    integrity = await verify_chain(session, ctx.workspace_id)
    return {
        "integrity": integrity,
        "events": [
            {
                "sequence": row.sequence,
                "timestamp": row.created_at.isoformat(),
                "actor": f"{row.actor_type}:{row.actor_id}",
                "action": row.action,
                "object_type": row.object_type,
                "object_id": row.object_id,
                "reason": row.reason,
                "before_state": row.before_state,
                "after_state": row.after_state,
                "event_hash": row.event_hash,
            }
            for row in rows
        ],
    }
