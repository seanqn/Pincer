"""Save and resume a run paused before one local function call."""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from agents import Agent, RunConfig, Runner, RunResult, RunState
from agents.items import ToolApprovalItem
from openai.types.responses import ResponseFunctionToolCall


@dataclass(frozen=True)
class ToolBreakpoint:
    name: str
    call_id: str
    arguments: str
    status: str = "pending"


def _pending_call(state: RunState) -> tuple[ToolBreakpoint, ToolApprovalItem]:
    interruptions = state.get_interruptions()
    if len(interruptions) != 1:
        raise ValueError("Expected one pending local function call")
    item = interruptions[0]
    if not isinstance(item.raw_item, ResponseFunctionToolCall):
        raise ValueError("Expected one pending local function call")
    call = item.raw_item
    return ToolBreakpoint(call.name, call.call_id, call.arguments), item


def save_breakpoint(path: Path, state: RunState) -> ToolBreakpoint:
    """Commit the SDK model result and pending call in a new checkpoint."""
    breakpoint, _ = _pending_call(state)
    snapshot = state.to_string()
    # The snapshot can include the full model input and output.
    os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA synchronous = EXTRA")
        db.execute("BEGIN")
        db.execute("""CREATE TABLE breakpoint (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            name TEXT NOT NULL,
            call_id TEXT NOT NULL,
            arguments TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'dispatching', 'completed', 'failed')),
            snapshot TEXT NOT NULL
        )""")
        db.execute(
            "INSERT INTO breakpoint VALUES (1, ?, ?, ?, 'pending', ?)",
            (breakpoint.name, breakpoint.call_id, breakpoint.arguments, snapshot),
        )
    return breakpoint


def _load_breakpoint(path: Path, *, include_snapshot: bool = False, timeout: float = 5.0) -> tuple:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=timeout)) as db:
        columns = "name, call_id, arguments, status"
        if include_snapshot:
            columns += ", snapshot"
        row = db.execute(f"SELECT {columns} FROM breakpoint WHERE id = 1").fetchone()
    if row is None:
        raise ValueError("No committed tool breakpoint found")
    return row


def read_breakpoint(path: Path, *, timeout: float = 5.0) -> ToolBreakpoint:
    """Read the debugger's display fields without restoring SDK run state."""
    return ToolBreakpoint(*_load_breakpoint(path, timeout=timeout))


async def resume_breakpoint(
    agent: Agent, path: Path, *, run_config: RunConfig | None = None
) -> RunResult:
    """Approve a call from trusted local storage and resume it at most once."""
    name, call_id, arguments, status, snapshot = _load_breakpoint(path, include_snapshot=True)
    if status != "pending":
        raise ValueError(f"Tool breakpoint cannot resume from status {status}")
    state = await RunState.from_string(agent, snapshot)
    breakpoint, item = _pending_call(state)
    if breakpoint != ToolBreakpoint(name, call_id, arguments):
        raise ValueError("Tool breakpoint differs from saved SDK state")
    state.approve(item)

    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True)) as db:
        db.execute("PRAGMA synchronous = EXTRA")
        # Commit the claim before invoking the tool, including across processes.
        with db:
            claimed = db.execute(
                "UPDATE breakpoint SET status = 'dispatching' WHERE id = 1 AND status = 'pending'"
            )
            if claimed.rowcount != 1:
                raise ValueError("Tool breakpoint has already been claimed")
        try:
            result = await Runner.run(agent, state, run_config=run_config)
        except Exception:
            with db:
                db.execute("UPDATE breakpoint SET status = 'failed' WHERE id = 1")
            raise
        with db:
            db.execute("UPDATE breakpoint SET status = 'completed' WHERE id = 1")
    return result
