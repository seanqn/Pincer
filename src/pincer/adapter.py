"""Translate public SDK tracing callbacks to a versioned local event stream."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from agents.tracing import TracingProcessor

logger = logging.getLogger(__name__)


class OpenAIAgentsTraceAdapter(TracingProcessor):
    """Write one JSON object per callback, without network export.

    One adapter owns one new file; existing files are never overwritten. Callbacks
    serialize under a lock. Writes are flushed but not fsynced. Errors are logged
    and counted instead of interrupting the agent. Inspect ``write_errors`` after
    running. Do not close this processor while its agents are still running.
    """

    def __init__(self, path: str | Path, *, capture_content: bool = False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("x", encoding="utf-8")
        self.capture_content = capture_content
        self._lock = Lock()
        self._sequence = 0
        self.write_errors = 0

    def _record(self, event: str, item: Any) -> None:
        with self._lock:
            try:
                is_span = event.startswith("span.")
                data = item.span_data if is_span else None
                sdk_type = data.type if data is not None else "trace"
                error = item.error if is_span else None
                details: dict[str, Any] = {}
                if data is not None:
                    for key in ("name", "model", "usage"):
                        value = getattr(data, key, None)
                        if value is not None:
                            details[key] = value
                    response = getattr(data, "response", None)
                    if response is not None:
                        details["model"] = response.model
                        if response.usage is not None:
                            details["usage"] = response.usage.model_dump(mode="json")
                    if self.capture_content:
                        details["sdk_data"] = data.export()
                        if sdk_type == "response":
                            details["input"] = data.input
                            details["response"] = (
                                response.model_dump(mode="json") if response else None
                            )
                record = {
                    "schema_version": 1,
                    "event_id": str(uuid4()),
                    "sequence": self._sequence + 1,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "source": "openai-agents",
                    "event": event,
                    "trace_id": item.trace_id,
                    "span_id": item.span_id if is_span else None,
                    "parent_id": item.parent_id if is_span else None,
                    "kind": {"function": "tool", "generation": "model", "response": "model"}.get(sdk_type, sdk_type),
                    "sdk_type": sdk_type,
                    "status": (
                        "started" if event.endswith("started") else
                        "ended" if not is_span else
                        "error" if error else "ended_without_reported_error"
                    ),
                    "started_at": item.started_at if is_span else None,
                    "ended_at": item.ended_at if is_span else None,
                    "error": (error if self.capture_content else {"message": "[content omitted]"}) if error else None,
                    "details": details,
                }
                encoded = json.dumps(record, ensure_ascii=True, allow_nan=False)
                self._file.write(encoded + "\n")
                self._file.flush()
                self._sequence += 1
            except Exception:
                self.write_errors += 1
                # Exception text may include user payloads; do not log it.
                logger.error("Pincer could not record a trace event; inspect write_errors.")

    def on_trace_start(self, trace):
        self._record("trace.started", trace)

    def on_trace_end(self, trace):
        self._record("trace.ended", trace)

    def on_span_start(self, span):
        self._record("span.started", span)

    def on_span_end(self, span):
        self._record("span.ended", span)

    def force_flush(self):
        with self._lock:
            if not self._file.closed:
                self._file.flush()

    def shutdown(self):
        with self._lock:
            if not self._file.closed:
                self._file.close()
