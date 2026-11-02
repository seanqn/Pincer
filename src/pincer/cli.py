"""Read saved traces without running the agent or contacting a provider."""
import argparse
import json
import sys
from pathlib import Path

from .trace import read_events


def render(path: str | Path) -> str:
    lines = []
    active = set()
    for event in read_events(path):
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
            f'{json.dumps(event["status"], ensure_ascii=True)[1:-1]} '
            f'trace={json.dumps(event["trace_id"])} '
            f'span={json.dumps(event["span_id"])}'
        )
        if event["error"]:
            lines.append("     error: " + json.dumps(event["error"], ensure_ascii=True))
        if "usage" in event["details"]:
            lines.append("     usage: " + json.dumps(event["details"]["usage"]))
    if active:
        lines.append(f"INCOMPLETE: {len(active)} trace/span start(s) have no recorded end.")
    return "\n".join(lines) if lines else "Empty trace: no events recorded."


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "debug":
        from .debugger import main as debug_main
        return debug_main(argv[1:])
    parser = argparse.ArgumentParser(
        description=__doc__, epilog="Inspect individual steps: pincer debug TRACE"
    )
    parser.add_argument("trace", type=Path)
    args = parser.parse_args(argv)
    try:
        print(render(args.trace))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"pincer: {exc}\n")


if __name__ == "__main__":
    main()
