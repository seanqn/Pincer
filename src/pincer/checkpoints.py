"""SQLite storage for local runner checkpoints and continuation attempts."""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ToolBreakpoint:
    name: str
    call_id: str
    arguments: str
    status: str = "pending"


@dataclass(frozen=True)
class Checkpoint:
    id: int
    kind: str
    breakpoint: ToolBreakpoint
    trace_id: str | None
    span_id: str | None
    trace_path: str | None
    action: str


@dataclass(frozen=True)
class CheckpointHistory:
    checkpoints: tuple[Checkpoint, ...]
    breakpoint: ToolBreakpoint
    attempts: tuple[ContinuationAttempt, ...] = ()


@dataclass(frozen=True)
class ContinuationAttempt:
    id: int
    source_id: int
    action: str
    status: str
    trace_path: str | None


def _private_file(path: Path) -> None:
    os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))


@contextmanager
def connect(path: Path, *, writable: bool = False, timeout: float = 5.0):
    mode = "rw" if writable else "ro"
    uri = path.resolve().as_uri() + "?mode=" + mode
    with closing(sqlite3.connect(uri, uri=True, timeout=timeout)) as db:
        db.row_factory = sqlite3.Row
        if writable:
            db.execute("PRAGMA synchronous = EXTRA")
            db.execute("PRAGMA foreign_keys = ON")
        yield db


@contextmanager
def claim_run(path: Path):
    # SQLite releases this independent process lock when a worker exits. Holding
    # it across execution leaves the checkpoint database free for durable writes
    # and debugger reads. The lock file must stay at the same path between runs.
    resolved = path.resolve()
    lock = resolved.with_name(resolved.name + ".lock")
    try:
        _private_file(lock)
    except FileExistsError:
        pass
    with connect(lock, writable=True, timeout=0) as db:
        try:
            db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if str(exc) == "database is locked":
                raise ValueError("A runner already owns this checkpoint database") from exc
            raise
        yield


def _has_history(db: sqlite3.Connection) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'checkpoints'"
    ).fetchone() is not None


def _create_schema(db: sqlite3.Connection, *, legacy: bool = False) -> None:
    if legacy:
        db.execute("DROP TABLE breakpoint")
    db.execute("""CREATE TABLE attempts (
        id INTEGER PRIMARY KEY,
        source_id INTEGER REFERENCES checkpoints (id),
        action TEXT NOT NULL CHECK (action IN ('start', 'approve', 'recover')),
        status TEXT NOT NULL CHECK (status IN ('paused', 'running', 'interrupted', 'failed', 'completed')),
        trace_id TEXT,
        trace_path TEXT,
        final_snapshot TEXT
    )""")
    db.execute("""CREATE TABLE checkpoints (
        id INTEGER PRIMARY KEY,
        attempt_id INTEGER NOT NULL REFERENCES attempts (id),
        kind TEXT NOT NULL CHECK (kind IN ('breakpoint', 'tool_result')),
        name TEXT NOT NULL,
        call_id TEXT NOT NULL,
        arguments TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'dispatching', 'completed', 'failed', 'ready')),
        span_id TEXT,
        snapshot TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE recovery_run (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        configuration_id TEXT,
        head_id INTEGER NOT NULL REFERENCES checkpoints (id),
        attempt_id INTEGER NOT NULL REFERENCES attempts (id)
    )""")
    db.execute("""CREATE VIEW breakpoint AS
        SELECT id, name, call_id, arguments, status, snapshot FROM checkpoints WHERE id = 1
    """)
    db.execute("""CREATE VIEW run_position AS
        SELECT run.configuration_id, run.head_id, attempt.status AS attempt_status,
               CASE head.kind WHEN 'breakpoint' THEN head.id ELSE owner.source_id END AS breakpoint_id,
               CASE WHEN head.status = 'pending' AND attempt.status = 'paused' THEN
                   CASE attempt.action
                       WHEN 'recover' THEN attempt.source_id
                       WHEN 'approve' THEN (
                           SELECT result.id FROM checkpoints AS result
                           WHERE result.attempt_id = attempt.id AND result.kind = 'tool_result'
                       )
                   END
               END AS previous_result_id
        FROM recovery_run AS run
        JOIN checkpoints AS head ON head.id = run.head_id
        JOIN attempts AS owner ON owner.id = head.attempt_id
        JOIN attempts AS attempt ON attempt.id = run.attempt_id
    """)
    db.execute("PRAGMA user_version = 1")


def append_checkpoint(
    db: sqlite3.Connection,
    attempt_id: int,
    kind: str,
    breakpoint: ToolBreakpoint,
    snapshot: str,
    span_id: str | None = None,
) -> int:
    return db.execute(
        """INSERT INTO checkpoints (attempt_id, kind, name, call_id, arguments, status, span_id, snapshot)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (attempt_id, kind, breakpoint.name, breakpoint.call_id, breakpoint.arguments,
         breakpoint.status, span_id, snapshot),
    ).lastrowid


def _start_history(
    db: sqlite3.Connection,
    breakpoint: ToolBreakpoint,
    snapshot: str,
    *,
    configuration_id: str | None = None,
    trace_id: str | None = None,
    trace_path: Path | None = None,
    span_id: str | None = None,
) -> None:
    attempt = db.execute(
        "INSERT INTO attempts (action, status, trace_id, trace_path) VALUES ('start', 'paused', ?, ?)",
        (trace_id, str(trace_path.resolve()) if trace_path is not None else None),
    ).lastrowid
    head = append_checkpoint(db, attempt, "breakpoint", breakpoint, snapshot, span_id)
    db.execute("INSERT INTO recovery_run VALUES (1, ?, ?, ?)", (configuration_id, head, attempt))


def create_history(
    path: Path,
    breakpoint: ToolBreakpoint,
    snapshot: str,
    *,
    configuration_id: str | None,
    trace_id: str | None,
    trace_path: Path | None,
    span_id: str | None,
) -> None:
    _private_file(path)
    with connect(path, writable=True) as db, db:
        db.execute("BEGIN IMMEDIATE")
        _create_schema(db)
        _start_history(
            db, breakpoint, snapshot, configuration_id=configuration_id,
            trace_id=trace_id, trace_path=trace_path, span_id=span_id,
        )


def _legacy_breakpoint(db: sqlite3.Connection, *, restore: bool = False) -> sqlite3.Row:
    columns = "name, call_id, arguments, status" + (", snapshot" if restore else "")
    row = db.execute(f"SELECT {columns} FROM breakpoint WHERE id = 1").fetchone()
    if row is None:
        raise ValueError("No committed tool breakpoint found")
    return row


def migrate_legacy(db: sqlite3.Connection) -> None:
    if _has_history(db):
        return
    row = _legacy_breakpoint(db, restore=True)
    _create_schema(db, legacy=True)
    _start_history(
        db, ToolBreakpoint(row["name"], row["call_id"], row["arguments"], row["status"]),
        row["snapshot"],
    )
    if row["status"] == "completed":
        db.execute("UPDATE attempts SET status = 'completed'")


def _checkpoint_action(
    kind: str, status: str, *, current: bool, completed: bool = False, previous_result: bool = False,
) -> str:
    if completed:
        return "Run completed"
    if previous_result:
        return "Continue from saved result and revisit pending approval"
    if not current:
        return "Checkpoint superseded"
    if kind == "tool_result":
        return "Continue from saved result"
    return {
        "pending": "Approve and continue",
        "dispatching": "Dispatch claimed. Outcome unconfirmed",
        "failed": "Dispatch failed. Outcome unconfirmed",
        "completed": "Checkpoint resumed",
    }[status]


def read_checkpoint_history(path: Path, *, timeout: float = 5.0) -> CheckpointHistory:
    """Read display fields only, from one committed view of the database."""
    with connect(path, timeout=timeout) as db:
        db.execute("BEGIN")
        if not _has_history(db):
            breakpoint = ToolBreakpoint(*_legacy_breakpoint(db))
            checkpoint = Checkpoint(
                1, "breakpoint", breakpoint, None, None, None,
                _checkpoint_action("breakpoint", breakpoint.status, current=True),
            )
            return CheckpointHistory((checkpoint,), breakpoint)
        rows = db.execute("""
            SELECT c.id, c.kind, c.name, c.call_id, c.arguments, c.status, c.span_id,
                   source.trace_id, source.trace_path,
                   run.head_id, run.attempt_status, run.previous_result_id, run.breakpoint_id
            FROM checkpoints AS c
            JOIN attempts AS source ON source.id = c.attempt_id
            CROSS JOIN run_position AS run
            ORDER BY c.id
        """).fetchall()
        attempts = tuple(
            ContinuationAttempt(*row) for row in db.execute("""
                SELECT id, source_id, action, status, trace_path
                FROM attempts WHERE source_id IS NOT NULL ORDER BY id
            """)
        )
    checkpoints = tuple(
        Checkpoint(
            row["id"], row["kind"],
            ToolBreakpoint(row["name"], row["call_id"], row["arguments"], row["status"]),
            row["trace_id"], row["span_id"], row["trace_path"],
            _checkpoint_action(
                row["kind"], row["status"], current=row["id"] == row["head_id"],
                completed=row["attempt_status"] == "completed",
                previous_result=row["id"] == row["previous_result_id"],
            ),
        )
        for row in rows
    )
    if not checkpoints:
        raise ValueError("No committed checkpoints found")
    breakpoint = next(item.breakpoint for item in checkpoints if item.id == rows[0]["breakpoint_id"])
    return CheckpointHistory(checkpoints, breakpoint, attempts)
