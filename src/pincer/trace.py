"""Shared reader for saved schema-v1 events. Reading never executes a trace."""
import json
from pathlib import Path


def read_events(path: str | Path) -> list[dict]:
    events = []
    sequence = 0
    with Path(path).open(encoding="utf-8") as stream:
        for number, raw in enumerate(stream, 1):
            try:
                event = json.loads(raw)
                if not isinstance(event, dict) or event["schema_version"] != 1:
                    raise ValueError("unsupported record")
                if type(event["sequence"]) is not int or event["sequence"] <= sequence:
                    raise ValueError("invalid sequence")
                if event["event"] not in (
                    "trace.started", "trace.ended", "span.started", "span.ended"
                ):
                    raise ValueError("unknown event")
                for key in ("trace_id", "kind", "status"):
                    if not isinstance(event[key], str) or not event[key]:
                        raise ValueError("invalid label")
                for key in ("span_id", "parent_id"):
                    if event[key] is not None and (
                        not isinstance(event[key], str) or not event[key]
                    ):
                        raise ValueError("invalid identifier")
                if event["event"].startswith("span.") != (event["span_id"] is not None):
                    raise ValueError("invalid span identifier")
                if not isinstance(event["details"], dict):
                    raise ValueError("invalid details")
                if event["error"] is not None and not isinstance(event["error"], dict):
                    raise ValueError("invalid error")
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"Invalid trace record at line {number}") from exc
            events.append(event)
            sequence = event["sequence"]
    return events
