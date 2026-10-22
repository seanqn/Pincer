"""Read saved traces without running the agent or contacting a provider."""
import argparse
import json
from pathlib import Path


def render(path: str | Path) -> str:
    lines = []
    active = set()
    with Path(path).open(encoding="utf-8") as stream:
        for number, raw in enumerate(stream, 1):
            try:
                event = json.loads(raw)
                if event["schema_version"] != 1:
                    raise ValueError("unsupported schema version")
                key = (event["trace_id"], event["span_id"])
                if event["event"].endswith("started"):
                    active.add(key)
                else:
                    active.discard(key)
                label = event["details"].get("name", event["details"].get("model", event["kind"]))
                # JSON escaping prevents payloads/names injecting terminal controls.
                lines.append(
                    f'{event["sequence"]:04d} {event["event"]:13} '
                    f'{json.dumps(label, ensure_ascii=True)} '
                    f'{event["status"]} '
                    f'trace={json.dumps(event["trace_id"])} '
                    f'span={json.dumps(event["span_id"])}'
                )
                if event["error"]:
                    lines.append("     error: " + json.dumps(event["error"], ensure_ascii=True))
                if "usage" in event["details"]:
                    lines.append("     usage: " + json.dumps(event["details"]["usage"]))
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"Invalid trace record at line {number}") from exc
    if active:
        lines.append(f"INCOMPLETE: {len(active)} trace/span start(s) have no recorded end.")
    return "\n".join(lines) if lines else "Empty trace: no events recorded."


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    args = parser.parse_args()
    try:
        print(render(args.trace))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"pincer: {exc}\n")
