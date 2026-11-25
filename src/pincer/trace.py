"""Incremental reader for schema-v1 JSONL events."""
import json
from collections.abc import Iterator
from pathlib import Path


class TraceRecordReader:
    """Decode complete records from successive chunks of trace text."""

    def __init__(self) -> None:
        self._pending = ""
        self._line_number = 0
        self._sequence = 0

    def feed(self, text: str) -> Iterator[dict]:
        """Yield complete records. Consume the iterator before feeding more text."""
        lines = (self._pending + text).split("\n")
        self._pending = lines.pop()
        for raw in lines:
            yield self._decode(raw)

    def finish(self) -> Iterator[dict]:
        """At permanent EOF, accept a final record without a newline."""
        if self._pending:
            raw = self._pending
            self._pending = ""
            yield self._decode(raw)

    def _decode(self, raw: str) -> dict:
        self._line_number += 1
        try:
            event = json.loads(raw)
            if not isinstance(event, dict) or event["schema_version"] != 1:
                raise ValueError("unsupported record")
            if type(event["sequence"]) is not int or event["sequence"] <= self._sequence:
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
            raise ValueError(f"Invalid trace record at line {self._line_number}") from exc
        self._sequence = event["sequence"]
        return event


def iter_events(path: str | Path) -> Iterator[dict]:
    reader = TraceRecordReader()
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            yield from reader.feed(line)
        yield from reader.finish()


def read_events(path: str | Path) -> list[dict]:
    return list(iter_events(path))
