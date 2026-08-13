"""Unified event bus: one call logs to the Python terminal *and* streams to the site.

Build instructions Step 1b requires results in two places — the terminal (for
debugging and demonstrating what happens under the hood) and the live site. Rather
than asking every feature to remember both, all instrumentation goes through
`emit()`, which does three things:

1. Writes a readable, colourised line to the Python terminal immediately.
2. Publishes the structured event to every WebSocket client watching that workspace.
3. Appends it to a bounded in-memory ring buffer so a page refresh or a late-joining
   reviewer can replay recent activity.

Durability boundary (core_resoruces.md, "Live UI state"): these events are a
*transient presentation channel*. PostgreSQL holds authoritative state. A reviewer
who reconnects re-fetches real records from the API; they never rely on replayed
events for financial truth.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from app.core.config import settings

logger = logging.getLogger("revenueproof.events")

# How many recent events per workspace stay replayable.
_RING_BUFFER_SIZE = 500
# Global channel for events not tied to one workspace (boot, health, migrations).
SYSTEM_CHANNEL = "_system"


class EventKind(StrEnum):
    """Event taxonomy. Kept small so the UI can style each kind meaningfully."""

    # Lifecycle
    SYSTEM = "system"
    # Outbound calls to a third party (Razorpay, Zoho, Drive, the model provider, ...)
    API_CALL = "api_call"
    # A LangGraph node / agent starting, finishing, or deciding
    AGENT_STEP = "agent_step"
    # A tool invoked by an agent
    TOOL_CALL = "tool_call"
    # Deterministic rule evaluation (the non-AI half of the system)
    RULE = "rule"
    # Persistence activity worth surfacing (writes, quarantine, versions)
    PERSISTENCE = "persistence"
    # Test execution, per Step 2a's "log every test you run"
    TEST = "test"
    # Anything that failed
    ERROR = "error"
    # Feature-level results destined for the site
    RESULT = "result"
    # Progress of a long-running verification run
    PROGRESS = "progress"


class Severity(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


@dataclass(slots=True)
class Event:
    kind: EventKind
    message: str
    workspace_id: str = SYSTEM_CHANNEL
    severity: Severity = Severity.INFO
    # Which feature produced this (1-8), for UI grouping and filtering.
    feature: int | None = None
    # Free-form structured payload rendered as JSON in the UI's detail panel.
    data: dict[str, Any] = field(default_factory=dict)
    # Correlates every event emitted during one verification run.
    run_id: str | None = None
    duration_ms: float | None = None
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "kind": str(self.kind),
            "severity": str(self.severity),
            "workspace_id": self.workspace_id,
            "feature": self.feature,
            "message": self.message,
            "data": _json_safe(self.data),
            "run_id": self.run_id,
            "duration_ms": self.duration_ms,
        }


def _json_safe(value: Any) -> Any:
    """Coerce arbitrary payloads into something JSON-serialisable.

    Financial payloads carry Decimal, date and UUID values constantly; losing an
    event because of a serialisation error would blind the operator at exactly the
    moment they need the trace.
    """
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        if isinstance(value, dict):
            return {str(k): _json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_json_safe(v) for v in value]
        return str(value)


# --------------------------------------------------------------------------
# Terminal rendering
# --------------------------------------------------------------------------

_RESET = "\033[0m"
_DIM = "\033[2m"
_COLOURS = {
    Severity.DEBUG: "\033[90m",
    Severity.INFO: "\033[36m",
    Severity.SUCCESS: "\033[32m",
    Severity.WARNING: "\033[33m",
    Severity.ERROR: "\033[31m",
}
_KIND_LABEL = {
    EventKind.SYSTEM: "SYS ",
    EventKind.API_CALL: "API ",
    EventKind.AGENT_STEP: "AGNT",
    EventKind.TOOL_CALL: "TOOL",
    EventKind.RULE: "RULE",
    EventKind.PERSISTENCE: "DB  ",
    EventKind.TEST: "TEST",
    EventKind.ERROR: "ERR ",
    EventKind.RESULT: "RSLT",
    EventKind.PROGRESS: "PROG",
}


def _render_terminal(event: Event) -> str:
    colour = _COLOURS.get(event.severity, "") if settings.log_pretty else ""
    reset = _RESET if settings.log_pretty else ""
    dim = _DIM if settings.log_pretty else ""

    clock = event.timestamp[11:23]
    label = _KIND_LABEL.get(event.kind, "....")
    feature = f"F{event.feature}" if event.feature else "--"
    took = f" {dim}({event.duration_ms:.0f}ms){reset}" if event.duration_ms is not None else ""

    line = f"{dim}{clock}{reset} {colour}{label}{reset} {dim}[{feature}]{reset} {event.message}{took}"

    if event.data:
        # One compact line of context keeps the terminal readable while still
        # showing the payload that makes a failure diagnosable.
        rendered = json.dumps(_json_safe(event.data), default=str)
        if len(rendered) > 400:
            rendered = rendered[:397] + "..."
        line += f"\n           {dim}{rendered}{reset}"
    return line


# --------------------------------------------------------------------------
# Event bus
# --------------------------------------------------------------------------


class EventBus:
    """Fan-out to terminal, WebSocket subscribers and a replay buffer."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._history: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=_RING_BUFFER_SIZE)
        )
        self._lock = asyncio.Lock()

    def emit(self, event: Event) -> None:
        """Publish an event. Safe to call from sync or async code.

        Never raises: instrumentation must not be able to break a financial
        calculation that is otherwise correct.
        """
        try:
            print(_render_terminal(event), flush=True)
        except Exception:  # pragma: no cover - terminal encoding edge cases
            logger.exception("failed to render event to terminal")

        payload = event.to_dict()
        self._history[event.workspace_id].append(payload)

        for queue in list(self._subscribers.get(event.workspace_id, ())):
            _offer(queue, payload)

        # Workspace events also reach operators watching the global firehose.
        if event.workspace_id != SYSTEM_CHANNEL:
            for queue in list(self._subscribers.get(SYSTEM_CHANNEL, ())):
                _offer(queue, payload)

    def history(self, workspace_id: str, limit: int = 200) -> list[dict[str, Any]]:
        return list(self._history.get(workspace_id, ()))[-limit:]

    @asynccontextmanager
    async def subscribe(self, workspace_id: str):
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        async with self._lock:
            self._subscribers[workspace_id].add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                self._subscribers[workspace_id].discard(queue)

    def subscriber_count(self, workspace_id: str) -> int:
        return len(self._subscribers.get(workspace_id, ()))


def _offer(queue: asyncio.Queue[dict[str, Any]], payload: dict[str, Any]) -> None:
    """Non-blocking publish; a slow client drops events rather than stalling the run."""
    try:
        queue.put_nowait(payload)
    except asyncio.QueueFull:
        try:
            queue.get_nowait()  # discard oldest
            queue.put_nowait(payload)
        except (asyncio.QueueEmpty, asyncio.QueueFull):
            pass


bus = EventBus()


# --------------------------------------------------------------------------
# Ergonomic helpers used throughout the codebase
# --------------------------------------------------------------------------


def emit(
    kind: EventKind,
    message: str,
    *,
    workspace_id: str = SYSTEM_CHANNEL,
    severity: Severity = Severity.INFO,
    feature: int | None = None,
    run_id: str | None = None,
    duration_ms: float | None = None,
    **data: Any,
) -> None:
    bus.emit(
        Event(
            kind=kind,
            message=message,
            workspace_id=workspace_id,
            severity=severity,
            feature=feature,
            run_id=run_id,
            duration_ms=duration_ms,
            data=data,
        )
    )


@asynccontextmanager
async def timed(
    kind: EventKind,
    message: str,
    *,
    workspace_id: str = SYSTEM_CHANNEL,
    feature: int | None = None,
    run_id: str | None = None,
    **data: Any,
):
    """Emit a start event, then a success/failure event carrying the elapsed time.

    Used for every outbound API call and agent step so the terminal shows both that
    a call went out and what came back — the evidence Step 2a category 5 asks for.
    """
    emit(kind, f"→ {message}", workspace_id=workspace_id, severity=Severity.DEBUG,
         feature=feature, run_id=run_id, **data)
    started = time.perf_counter()
    try:
        yield
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000
        emit(
            EventKind.ERROR,
            f"✗ {message} — {type(exc).__name__}: {exc}",
            workspace_id=workspace_id,
            severity=Severity.ERROR,
            feature=feature,
            run_id=run_id,
            duration_ms=elapsed,
            **data,
        )
        raise
    else:
        elapsed = (time.perf_counter() - started) * 1000
        emit(kind, f"✓ {message}", workspace_id=workspace_id, severity=Severity.SUCCESS,
             feature=feature, run_id=run_id, duration_ms=elapsed, **data)
