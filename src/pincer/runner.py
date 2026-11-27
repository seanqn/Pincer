"""Approve a saved local function call or continue from its committed output."""
from __future__ import annotations

import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path

from agents import Agent, FunctionTool, RunConfig, RunHooks, Runner, RunResult, RunState
from agents.items import ToolApprovalItem
from agents.tool_context import ToolContext
from agents.tracing import get_current_span, get_current_trace, trace
from openai.types.responses import ResponseFunctionToolCall

from .adapter import OpenAIAgentsTraceAdapter
from .checkpoints import (
    ToolBreakpoint,
    append_checkpoint,
    claim_run,
    connect,
    create_history,
    migrate_legacy,
    read_checkpoint_history,
)


@dataclass(frozen=True)
class RecoveryProfile:
    """Rebuild the trusted agent and config identified when the run was saved."""

    configuration_id: str
    agent: Agent
    run_config: RunConfig | None = None


def _pending_call(state: RunState) -> tuple[ToolBreakpoint, ToolApprovalItem]:
    interruptions = state.get_interruptions()
    if len(interruptions) != 1 or not isinstance(interruptions[0].raw_item, ResponseFunctionToolCall):
        raise ValueError("Expected one pending local function call")
    item = interruptions[0]
    call = item.raw_item
    return ToolBreakpoint(call.name, call.call_id, call.arguments), item


def save_breakpoint(
    path: Path,
    state: RunState,
    *,
    configuration_id: str | None = None,
    trace_adapter: OpenAIAgentsTraceAdapter | None = None,
) -> ToolBreakpoint:
    """Commit the SDK model result and pending call in a new run database."""
    breakpoint, _ = _pending_call(state)
    current_trace = get_current_trace()
    trace_id = current_trace.trace_id if current_trace else None
    create_history(
        path, breakpoint, state.to_string(strict_context=True), configuration_id=configuration_id,
        trace_id=trace_id,
        trace_path=trace_adapter.path if trace_adapter else None,
        span_id=trace_adapter.last_model_span_id(trace_id) if trace_adapter and trace_id else None,
    )
    return breakpoint


def read_breakpoint(path: Path, *, timeout: float = 5.0) -> ToolBreakpoint:
    """Read the most recent tool breakpoint without restoring SDK run state."""
    return read_checkpoint_history(path, timeout=timeout).breakpoint


class _CheckpointHooks(RunHooks):
    def __init__(
        self,
        db: sqlite3.Connection,
        state: RunState,
        attempt_id: int,
        source_id: int,
        approved: ToolBreakpoint | None,
    ) -> None:
        self.db = db
        self.state = state
        self.attempt_id = attempt_id
        self.source_id = source_id
        self.approved = approved
        self.started = False
        self.saved = False

    def _matches_approval(self, context: ToolContext, tool: FunctionTool) -> bool:
        call = ToolBreakpoint(tool.name, context.tool_call_id, context.tool_arguments)
        return call == self.approved

    async def on_tool_start(self, context, agent, tool) -> None:
        if self.started or not self._matches_approval(context, tool):
            raise ValueError("Tool dispatch requires approval from a committed breakpoint")
        self.started = True

    async def on_tool_end(self, context, agent, tool, result) -> None:
        if not self.started or self.saved or not self._matches_approval(context, tool):
            raise ValueError("Tool result does not match the claimed dispatch")
        # The pinned SDK places this call's output into the supplied state
        # before running this hook. Keep the SDK's own output representation.
        snapshot = self.state.to_string(strict_context=True)
        span = get_current_span()
        with self.db:
            head = append_checkpoint(
                self.db, self.attempt_id, "tool_result", replace(self.approved, status="ready"),
                snapshot, span.span_id if span and span.span_data.type == "function" else None,
            )
            self.db.execute(
                "UPDATE checkpoints SET status = 'completed' WHERE id = ?", (self.source_id,)
            )
            self.db.execute("UPDATE recovery_run SET head_id = ? WHERE id = 1", (head,))
        self.saved = True

    async def on_llm_start(self, context, agent, system_prompt, input_items) -> None:
        if self.approved is not None and not self.saved:
            raise ValueError("Tool result was not committed before model continuation")


def _source(
    db: sqlite3.Connection, checkpoint_id: int | None, action: str, configuration_id: str | None,
) -> sqlite3.Row:
    run = db.execute("SELECT * FROM run_position").fetchone()
    if run["configuration_id"] != configuration_id and not (
        run["configuration_id"] is None and action == "approve"
    ):
        raise ValueError("Recovery configuration does not match the saved run")
    source = db.execute(
        "SELECT * FROM checkpoints WHERE id = ?",
        (run["head_id"] if checkpoint_id is None else checkpoint_id,),
    ).fetchone()
    if source is None:
        raise ValueError(f"Checkpoint {checkpoint_id} does not exist")
    if run["attempt_status"] == "completed":
        if action == "approve":
            raise ValueError("Tool breakpoint cannot resume from status completed")
        raise ValueError("Run completed")
    # The preceding result remains safe while the model turn after it has only
    # reached an unclaimed approval. Taking it invalidates that pending call.
    if source["id"] not in (run["head_id"], run["previous_result_id"]):
        raise ValueError(f"Checkpoint {source['id']} has been superseded")
    if source["kind"] == "breakpoint":
        if source["status"] != "pending":
            raise ValueError(
                f"Tool breakpoint cannot resume from status {source['status']}. Tool outcome is unconfirmed."
            )
        if action != "approve":
            raise ValueError("Checkpoint requires approval. Use --approve or resume_breakpoint")
    elif action == "approve":
        raise ValueError("Tool result is already saved. Use recover_checkpoint without approval")
    return source


async def _continue(
    agent: Agent,
    path: Path,
    *,
    action: str,
    checkpoint_id: int | None,
    run_config: RunConfig | None,
    configuration_id: str | None,
    trace_adapter: OpenAIAgentsTraceAdapter | None,
) -> RunResult:
    if agent.handoffs or agent.mcp_servers or any(
        not isinstance(tool, FunctionTool) or tool.needs_approval is not True for tool in agent.tools
    ):
        raise ValueError("Recovery supports local function tools that require approval and no handoffs")
    with connect(path, writable=True) as db, claim_run(path):
        with db:
            db.execute("BEGIN IMMEDIATE")
            migrate_legacy(db)
            source = _source(db, checkpoint_id, action, configuration_id)

        state = await RunState.from_string(agent, source["snapshot"], strict_context=True)
        source_id = source["id"]
        approved = None
        if action == "approve":
            approved, item = _pending_call(state)
            if approved != ToolBreakpoint(source["name"], source["call_id"], source["arguments"]):
                raise ValueError("Tool breakpoint differs from saved SDK state")
            state.approve(item)

        trace_scope = nullcontext() if get_current_trace() else trace(
            f"Pincer {action} checkpoint {source_id}",
            disabled=run_config.tracing_disabled if run_config else False,
        )
        with trace_scope:
            current_trace = get_current_trace()
            trace_id = current_trace.trace_id if current_trace else None
            with db:
                db.execute("UPDATE attempts SET status = 'interrupted' WHERE status = 'running'")
                attempt_id = db.execute(
                    """INSERT INTO attempts (source_id, action, status, trace_id, trace_path)
                       VALUES (?, ?, 'running', ?, ?)""",
                    (source_id, action, trace_id,
                     str(trace_adapter.path.resolve()) if trace_adapter else None),
                ).lastrowid
                db.execute(
                    """UPDATE recovery_run SET attempt_id = ?, head_id = ?,
                       configuration_id = COALESCE(configuration_id, ?) WHERE id = 1""",
                    (attempt_id, source_id, configuration_id),
                )
                if approved is not None:
                    db.execute(
                        "UPDATE checkpoints SET status = 'dispatching' WHERE id = ?", (source_id,)
                    )

            hooks = _CheckpointHooks(db, state, attempt_id, source_id, approved)
            try:
                result = await Runner.run(agent, state, hooks=hooks, run_config=run_config)
                if approved is not None and not hooks.saved:
                    raise ValueError("Approved dispatch completed without a saved tool result")
                next_state = result.to_state()
                snapshot = next_state.to_string(strict_context=True)
                with db:
                    if result.interruptions:
                        pending, _ = _pending_call(next_state)
                        span_id = (
                            trace_adapter.last_model_span_id(trace_id)
                            if trace_adapter and trace_id else None
                        )
                        head = append_checkpoint(db, attempt_id, "breakpoint", pending, snapshot, span_id)
                        db.execute("UPDATE recovery_run SET head_id = ? WHERE id = 1", (head,))
                        db.execute("UPDATE attempts SET status = 'paused' WHERE id = ?", (attempt_id,))
                    else:
                        db.execute(
                            "UPDATE attempts SET status = 'completed', final_snapshot = ? WHERE id = ?",
                            (snapshot, attempt_id),
                        )
            except BaseException:
                with db:
                    db.execute("UPDATE attempts SET status = 'failed' WHERE id = ?", (attempt_id,))
                    if approved is not None and not hooks.saved:
                        db.execute(
                            "UPDATE checkpoints SET status = 'failed' WHERE id = ?", (source_id,)
                        )
                raise
    return result


async def resume_breakpoint(
    agent: Agent,
    path: Path,
    *,
    run_config: RunConfig | None = None,
    checkpoint_id: int | None = None,
    configuration_id: str | None = None,
    trace_adapter: OpenAIAgentsTraceAdapter | None = None,
) -> RunResult:
    """Approve and dispatch the pending call at the current checkpoint."""
    return await _continue(
        agent, path, action="approve", checkpoint_id=checkpoint_id, run_config=run_config,
        configuration_id=configuration_id, trace_adapter=trace_adapter,
    )


async def recover_checkpoint(
    agent: Agent,
    path: Path,
    *,
    checkpoint_id: int | None = None,
    run_config: RunConfig | None = None,
    configuration_id: str | None = None,
    trace_adapter: OpenAIAgentsTraceAdapter | None = None,
) -> RunResult:
    """Continue from a saved tool result without approving or dispatching a tool."""
    return await _continue(
        agent, path, action="recover", checkpoint_id=checkpoint_id, run_config=run_config,
        configuration_id=configuration_id, trace_adapter=trace_adapter,
    )
