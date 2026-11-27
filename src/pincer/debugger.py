"""Read-only navigation of individual steps in a saved or growing trace."""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Thread

from prompt_toolkit import PromptSession
from prompt_toolkit.application import in_terminal, set_app

from .checkpoints import Checkpoint, CheckpointHistory, ToolBreakpoint, read_checkpoint_history
from .trace import TraceFollower, iter_events

COMMANDS = "Commands: list | select <step> | details | next | previous | help | quit"
PROMPT = "pincer(debug)> "
FOLLOW_INTERVAL = 0.1


def quoted(value) -> str:
    """Escape untrusted trace content, including terminal control characters."""
    return json.dumps(value, ensure_ascii=True)


def describe_breakpoint(breakpoint: ToolBreakpoint) -> str:
    status = {
        "pending": "Paused before dispatch",
        "dispatching": "Dispatch claimed. Outcome unconfirmed",
        "completed": "Checkpoint resumed",
        "failed": "Runner reported a failure",
    }[breakpoint.status]
    try:
        arguments = json.loads(breakpoint.arguments)
    except ValueError:
        arguments = breakpoint.arguments
    return "\n".join([
        f"Tool breakpoint: {status}",
        "Tool: " + quoted(breakpoint.name),
        "Call ID: " + quoted(breakpoint.call_id),
        "Arguments: " + quoted(arguments),
        "SDK tool spans can include approval checks. The breakpoint reports dispatch status.",
    ])


def _checkpoint_label(checkpoint: Checkpoint) -> str:
    kind = "Tool result" if checkpoint.kind == "tool_result" else "Tool approval"
    return f"Checkpoint {checkpoint.id} | {kind} | {checkpoint.action}"


@dataclass
class Span:
    start: dict | None = None
    end: dict | None = None
    number: int | None = None

    @property
    def record(self) -> dict:
        return self.end if self.end is not None else self.start

    @property
    def details(self) -> dict:
        return {
            **(self.start["details"] if self.start else {}),
            **(self.end["details"] if self.end else {}),
        }

    @property
    def label(self):
        return self.details.get("name", self.details.get("model", self.record["kind"]))

    @property
    def missing_endpoint(self) -> str | None:
        if self.start is None:
            return "start"
        if self.end is None:
            return "end"
        return None

    @property
    def complete(self) -> bool:
        return self.missing_endpoint is None

    @property
    def failed(self) -> bool:
        return any(
            event and (event["error"] or event["status"] == "error")
            for event in (self.start, self.end)
        )

    @property
    def candidate(self) -> bool:
        return (
            self.complete and not self.failed
            and self.end["status"] == "ended_without_reported_error"
        )

    @property
    def result(self) -> str:
        parts = []
        if self.failed:
            parts.append("Error reported")
        elif self.end:
            parts.append(
                "Ended; no reported error" if self.end["status"] == "ended_without_reported_error"
                else "End status: " + quoted(self.end["status"])
            )
        missing = self.missing_endpoint
        if missing:
            parts.append(f"INCOMPLETE: missing {missing} (observation gap)")
        return "; ".join(parts)


class TraceView:
    def __init__(self, events: Iterable[dict] = (), *, following: bool = False):
        self.spans: dict[tuple[str, str | None], Span] = {}
        self.steps: list[Span] = []
        self.following = following
        self.write_errors = 0
        self.checkpoint_history: CheckpointHistory | None = None
        self.checkpoints_by_span: dict[tuple[str, str], list[Checkpoint]] = {}
        self._event_count = 0
        for event in events:
            self.add_event(event)

    def set_checkpoint_history(self, history: CheckpointHistory | None) -> None:
        self.checkpoint_history = history
        self.checkpoints_by_span.clear()
        if history is not None:
            for checkpoint in history.checkpoints:
                if checkpoint.trace_id and checkpoint.span_id:
                    key = (checkpoint.trace_id, checkpoint.span_id)
                    self.checkpoints_by_span.setdefault(key, []).append(checkpoint)

    def checkpoint_summary(self) -> str:
        if self.checkpoint_history is None:
            return ""
        lines = ["Committed checkpoints:"]
        for checkpoint in self.checkpoint_history.checkpoints:
            line = f"  {_checkpoint_label(checkpoint)} | Tool {quoted(checkpoint.breakpoint.name)}"
            if checkpoint.trace_path is not None:
                line += f" | Trace {quoted(checkpoint.trace_path)}"
            lines.append(line)
        if self.checkpoint_history.attempts:
            lines.append("Continuation attempts:")
        for attempt in self.checkpoint_history.attempts:
            status = "running or interrupted" if attempt.status == "running" else attempt.status
            line = f"  Attempt {attempt.id} | Checkpoint {attempt.source_id} | {attempt.action} | {status}"
            if attempt.trace_path is not None:
                line += f" | Trace {quoted(attempt.trace_path)}"
            lines.append(line)
        return "\n".join(lines)

    def add_event(self, event: dict) -> Span:
        """Apply one validated record in input order and return its span."""
        number = self._event_count + 1
        key = (event["trace_id"], event["span_id"])
        span = self.spans.get(key)
        starting = event["event"].endswith("started")
        if span is None:
            span = Span()
            self.spans[key] = span
            # Keep first-observation order, including overlapping spans.
            if event["kind"] in ("model", "tool"):
                self.steps.append(span)
                span.number = len(self.steps)
        else:
            if span.end is not None or starting:
                raise ValueError(f"Ambiguous trace lifecycle at line {number}: duplicate or reversed event")
            if any(span.record[field] != event[field] for field in ("kind", "parent_id")):
                raise ValueError(f"Inconsistent span identity at line {number}")
        if starting:
            span.start = event
        else:
            span.end = event
        self.write_errors = max(self.write_errors, event.get("write_errors", 0))
        self._event_count = number
        return span

    def is_open(self, span: Span) -> bool:
        if not self.following or span.missing_endpoint != "end":
            return False
        trace = self.spans.get((span.record["trace_id"], None))
        return trace is None or trace.end is None

    def span_result(self, span: Span) -> str:
        if self.is_open(span):
            return "Error reported; Open" if span.failed else "Open"
        return span.result

    def agent(self, step: Span) -> Span | None:
        parent_id = step.record["parent_id"]
        seen = set()
        while parent_id is not None and parent_id not in seen:
            seen.add(parent_id)
            parent = self.spans.get((step.record["trace_id"], parent_id))
            if parent is None:
                break
            if parent.record["kind"] == "agent":
                return parent
            parent_id = parent.record["parent_id"]
        return None

    def listing(self, selected: int = 0, *, breakpoint: ToolBreakpoint | None = None) -> str:
        lines = [
            "PINCER DEBUGGER - following trace" if self.following else "PINCER DEBUGGER - saved trace",
            "Mode: inspection only | Execution control: unavailable",
            "Order: first observation; overlapping steps may run concurrently.",
            "Checkpoint candidates mark observed span boundaries.",
        ]
        if self.checkpoint_history is not None:
            lines.extend(["", self.checkpoint_summary()])
            breakpoint = self.checkpoint_history.breakpoint
        if breakpoint is not None:
            lines.extend(["", describe_breakpoint(breakpoint)])
        open_spans = sum(self.is_open(span) for span in self.spans.values())
        incomplete = sum(not span.complete for span in self.spans.values()) - open_spans
        if self.write_errors:
            lines.append(f"Recording errors reported by adapter: {self.write_errors}")
        if incomplete:
            lines.append(f"INCOMPLETE: {incomplete} trace/span lifecycle(s) have an observation gap.")
        if open_spans:
            lines.append(f"Open trace/span lifecycles: {open_spans}")
        if not self.steps:
            lines.append("No model or tool steps recorded.")
        group = None
        for number, step in enumerate(self.steps, 1):
            agent = self.agent(step)
            next_group = (step.record["trace_id"], agent.record["span_id"] if agent else None)
            if next_group != group:
                lines.extend([
                    "", "Trace: " + quoted(next_group[0]),
                    "Agent: " + (quoted(agent.label) + " (" + quoted(next_group[1]) + ")" if agent else "not recorded"),
                    "   STEP  TYPE   NAME  | RECORDED RESULT | CHECKPOINT CANDIDATE",
                ])
                group = next_group
            marker = ">" if number == selected + 1 else " "
            lines.append(
                f"{marker}  {number:02d}    {step.record['kind']:5}  {quoted(step.label)}"
                f" | {self.span_result(step)} | {'yes (boundary only)' if step.candidate else 'no'}"
            )
            key = (step.record["trace_id"], step.record["span_id"])
            for checkpoint in self.checkpoints_by_span.get(key, ()):
                lines.append("      " + _checkpoint_label(checkpoint))
        lines.extend(["", COMMANDS])
        return "\n".join(lines)

    def detail(self, index: int) -> str:
        step = self.steps[index]
        record = step.record
        checkpoints = self.checkpoints_by_span.get((record["trace_id"], record["span_id"]), ())
        details = step.details
        agent = self.agent(step)
        lines = [
            f"Selected: Step {index + 1:02d} - {quoted(step.label)}",
            "Type: " + record["kind"],
            "Trace: " + quoted(record["trace_id"]),
            "Span: " + quoted(record["span_id"]),
            "Parent span: " + quoted(record["parent_id"]),
            "Agent: " + (quoted(agent.label) if agent else "not recorded"),
            "Recorded result: " + self.span_result(step),
            "SDK start: " + quoted(record.get("started_at")),
            "SDK end: " + quoted(record.get("ended_at")),
        ]
        try:
            duration = datetime.fromisoformat(record["ended_at"]) - datetime.fromisoformat(record["started_at"])
            elapsed = f"{duration.total_seconds() * 1000:.3f} ms" if step.complete and duration.total_seconds() >= 0 else "unavailable"
        except (KeyError, TypeError, ValueError):
            elapsed = "unavailable"
        lines.append("SDK duration: " + elapsed)
        if record["kind"] == "model":
            # One snapshot per step, never sum the same usage on start and end.
            lines.append("Usage (latest recorded snapshot): " + (
                quoted(details["usage"]) if details.get("usage") is not None else "not recorded (not zero)"
            ))
        sdk_data = details.get("sdk_data")
        sdk_data = sdk_data if isinstance(sdk_data, dict) else {}
        for title, key in (("Input", "input"), ("Output", "output")):
            if key in details:
                content = quoted(details[key])
            elif key in sdk_data:
                content = quoted(sdk_data[key])
            elif key == "output" and "response" in details:
                content = quoted(details["response"])
            else:
                content = "omitted or not recorded in trace"
            lines.append(f"{title}: {content}")
        if step.failed:
            error = (step.end or {}).get("error") or (step.start or {}).get("error")
            lines.append("Error: " + (quoted(error) if error else "details not recorded"))
            lines.append("A recorded error does not establish that execution stopped here.")
        lines.extend([
            "", "Checkpoint assessment:",
            "  Completed boundary: " + ("yes (start and end recorded)" if step.complete else "no"),
            "  Checkpoint candidate: " + ("yes (boundary only)" if step.candidate else "no"),
            "  Resumable state saved: " + ("yes" if checkpoints else "no"),
            "  No reported error does not prove success or that repeating a step is safe.",
        ])
        lines.extend("  " + _checkpoint_label(checkpoint) for checkpoint in checkpoints)
        return "\n".join(lines)


def _inspect_command(
    command: list[str], view: TraceView, selected: int, breakpoint: ToolBreakpoint | None
) -> int:
    if not command:
        return selected
    if command == ["help"]:
        print(COMMANDS)
    elif command == ["list"]:
        print(view.listing(selected, breakpoint=breakpoint))
    elif command in (["next"], ["previous"], ["details"]) or command[0] == "select":
        if not view.steps:
            print("No model or tool steps recorded.")
            return selected
        target = selected
        if command[0] == "select":
            try:
                if len(command) != 2:
                    raise ValueError
                target = int(command[1]) - 1
            except ValueError:
                print("Usage: select <step number>")
                return selected
        elif command == ["next"]:
            target += 1
        elif command == ["previous"]:
            target -= 1
        if not 0 <= target < len(view.steps):
            print(f"Choose a step from 1 to {len(view.steps)}; selection unchanged.")
            return selected
        print(view.detail(target))
        return target
    else:
        print("Unknown command. Navigation only; execution commands are unavailable.")
        print(COMMANDS)
    return selected


def inspect(view: TraceView, *, breakpoint: ToolBreakpoint | None = None) -> None:
    selected = 0
    print(view.listing(selected, breakpoint=breakpoint))
    while True:
        try:
            command = input(PROMPT).strip().split()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if command == ["quit"]:
            return
        selected = _inspect_command(command, view, selected, breakpoint)


def _poll_checkpoints(path: Path | None, previous: CheckpointHistory | None) -> CheckpointHistory | None:
    if path is None or not path.exists():
        return previous
    try:
        return read_checkpoint_history(path, timeout=0)
    except sqlite3.OperationalError as exc:
        # The file can be visible before the runner commits the schema, or
        # briefly locked during a status update. Try again on the next poll.
        if str(exc) in {"no such table: breakpoint", "database is locked"}:
            return previous
        raise


def _piped_commands() -> Callable[[], Awaitable[str | None]]:
    """Read redirected stdin without blocking the trace or terminal loop."""
    loop = asyncio.get_running_loop()
    commands: asyncio.Queue[str | None | OSError | ValueError] = asyncio.Queue()

    def read() -> None:
        try:
            try:
                for line in sys.stdin:
                    loop.call_soon_threadsafe(commands.put_nowait, line)
            except (OSError, ValueError) as exc:
                loop.call_soon_threadsafe(commands.put_nowait, exc)
            else:
                loop.call_soon_threadsafe(commands.put_nowait, None)
        except RuntimeError:
            if not loop.is_closed():
                raise

    async def receive() -> str | None:
        command = await commands.get()
        if isinstance(command, (OSError, ValueError)):
            raise command
        return command

    # On quit or a trace error, the process can exit even if stdin is idle.
    Thread(target=read, daemon=True).start()
    return receive


async def follow(path: Path, *, checkpoint: Path | None = None) -> None:
    with path.open("rb") as stream:
        source = TraceFollower(stream)
        view = TraceView(source.read_available(), following=True)
        view.set_checkpoint_history(_poll_checkpoints(checkpoint, None))
        print(view.listing(), flush=True)
        print("Waiting for events. Quit or press Ctrl-C to stop following.", flush=True)

        prompt = PromptSession(PROMPT) if sys.stdin.isatty() and sys.stdout.isatty() else None
        read_command = prompt.prompt_async if prompt is not None else _piped_commands()

        async def publish(text: str) -> None:
            if prompt is None:
                print(text, flush=True)
            else:
                with set_app(prompt.app):
                    async with in_terminal():
                        print(text, flush=True)

        async def watch() -> None:
            while True:
                await asyncio.sleep(FOLLOW_INTERVAL)
                updates = []
                previous_errors = view.write_errors
                for event in source.read_available():
                    span = view.add_event(event)
                    if span.number is not None:
                        updates.append(
                            f"Step {span.number:02d} {quoted(span.label)} | {view.span_result(span)}"
                        )
                    elif event["event"] == "trace.ended":
                        updates.append(f"Trace ended: {quoted(event['trace_id'])}")
                if view.write_errors != previous_errors:
                    updates.append(f"Recording errors reported by adapter: {view.write_errors}")
                previous = view.checkpoint_history
                latest = _poll_checkpoints(checkpoint, previous)
                if latest is not None and latest != previous:
                    view.set_checkpoint_history(latest)
                    updates.append(view.checkpoint_summary())
                    if previous is None or previous.breakpoint != latest.breakpoint:
                        updates.append(describe_breakpoint(latest.breakpoint))
                if updates:
                    await publish("\n".join(updates))

        async def interact() -> None:
            selected = 0
            while True:
                try:
                    line = await read_command()
                except (EOFError, KeyboardInterrupt):
                    return
                if line is None:
                    return
                command = line.strip().split()
                if command == ["quit"]:
                    return
                selected = _inspect_command(command, view, selected, None)
                sys.stdout.flush()

        tasks = [asyncio.create_task(watch()), asyncio.create_task(interact())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pincer debug", description=__doc__)
    parser.add_argument("trace", type=Path, help="JSONL trace file")
    parser.add_argument("--checkpoint", type=Path, help="include committed checkpoints and recovery actions")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="print the step list and exit")
    mode.add_argument("--follow", action="store_true", help="watch a growing trace file for new steps")
    args = parser.parse_args(argv)
    try:
        if args.follow:
            try:
                asyncio.run(follow(args.trace, checkpoint=args.checkpoint))
            except KeyboardInterrupt:
                print()
            return
        view = TraceView(iter_events(args.trace))
        if args.checkpoint is not None:
            view.set_checkpoint_history(read_checkpoint_history(args.checkpoint))
        if args.list:
            print(view.listing())
        else:
            inspect(view)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"pincer debug: {quoted(str(exc))}\n")


if __name__ == "__main__":
    main()
