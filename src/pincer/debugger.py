"""Read-only navigation of individual steps in a saved trace."""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .trace import read_events

COMMANDS = "Commands: list | select <step> | details | next | previous | help | quit"


def quoted(value) -> str:
    """Escape untrusted trace content, including terminal control characters."""
    return json.dumps(value, ensure_ascii=True)


@dataclass
class Span:
    start: dict | None = None
    end: dict | None = None

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
    def complete(self) -> bool:
        return self.start is not None and self.end is not None

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
        if not self.complete:
            parts.append("INCOMPLETE: missing " + ("start" if self.start is None else "end"))
        return "; ".join(parts)


class TraceView:
    def __init__(self, events: list[dict]):
        self.spans: dict[tuple[str, str | None], Span] = {}
        for number, event in enumerate(events, 1):
            key = (event["trace_id"], event["span_id"])
            span = self.spans.setdefault(key, Span())
            starting = event["event"].endswith("started")
            if (starting and (span.start or span.end)) or (not starting and span.end):
                raise ValueError(f"Ambiguous trace lifecycle at line {number}: duplicate or reversed event")
            if span.start and any(
                span.start[field] != event[field] for field in ("kind", "parent_id")
            ):
                raise ValueError(f"Inconsistent span identity at line {number}")
            if starting:
                span.start = event
            else:
                span.end = event
        # Dictionary insertion order preserves first observation, including overlap.
        self.steps = [span for span in self.spans.values() if span.record["kind"] in ("model", "tool")]

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

    def listing(self, selected: int = 0) -> str:
        lines = [
            "PINCER DEBUGGER - saved trace",
            "Mode: inspection only | Execution control: unavailable",
            "Order: first observation; overlapping steps may run concurrently.",
            "Checkpoint candidates are boundaries only; resumable state is not saved.",
        ]
        incomplete = sum(not span.complete for span in self.spans.values())
        if incomplete:
            lines.append(f"INCOMPLETE: {incomplete} trace/span lifecycle(s) have a missing start or end.")
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
                f" | {step.result} | {'yes (boundary only)' if step.candidate else 'no'}"
            )
        lines.extend(["", COMMANDS])
        return "\n".join(lines)

    def detail(self, index: int) -> str:
        step = self.steps[index]
        record = step.record
        details = step.details
        agent = self.agent(step)
        lines = [
            f"Selected: Step {index + 1:02d} - {quoted(step.label)}",
            "Type: " + record["kind"],
            "Trace: " + quoted(record["trace_id"]),
            "Span: " + quoted(record["span_id"]),
            "Parent span: " + quoted(record["parent_id"]),
            "Agent: " + (quoted(agent.label) if agent else "not recorded"),
            "Recorded result: " + step.result,
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
            "  Resumable state saved: no",
            "  No reported error does not prove success or that repeating a step is safe.",
        ])
        return "\n".join(lines)


def inspect(view: TraceView) -> None:
    selected = 0
    print(view.listing(selected))
    while True:
        try:
            command = input("pincer(debug)> ").strip().split()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not command:
            continue
        if command == ["quit"]:
            return
        if command == ["help"]:
            print(COMMANDS)
        elif command == ["list"]:
            print(view.listing(selected))
        elif command in (["next"], ["previous"], ["details"]) or command[0] == "select":
            if not view.steps:
                print("No model or tool steps recorded.")
                continue
            target = selected
            if command[0] == "select":
                try:
                    if len(command) != 2:
                        raise ValueError
                    target = int(command[1]) - 1
                except ValueError:
                    print("Usage: select <step number>")
                    continue
            elif command == ["next"]:
                target += 1
            elif command == ["previous"]:
                target -= 1
            if not 0 <= target < len(view.steps):
                print(f"Choose a step from 1 to {len(view.steps)}; selection unchanged.")
                continue
            selected = target
            print(view.detail(selected))
        else:
            print("Unknown command. Navigation only; execution commands are unavailable.")
            print(COMMANDS)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pincer debug", description=__doc__)
    parser.add_argument("trace", type=Path, help="completed JSONL file (loaded once)")
    parser.add_argument("--list", action="store_true", help="print the step list and exit")
    args = parser.parse_args(argv)
    try:
        view = TraceView(read_events(args.trace))
        if args.list:
            print(view.listing())
        else:
            inspect(view)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"pincer debug: {quoted(str(exc))}\n")


if __name__ == "__main__":
    main()
