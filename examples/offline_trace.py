"""Exercise real SDK callbacks using synthetic spans; no API calls or key."""
import argparse
from math import isfinite
from time import sleep
from uuid import uuid4

from agents import set_trace_processors
from agents.tracing import trace, agent_span, generation_span, function_span

from pincer import OpenAIAgentsTraceAdapter


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", nargs="?", const=2.0, type=float, metavar="SECONDS",
        help="wait for the viewer to attach, then hold each model/tool span open (default: 2 seconds)",
    )
    args = parser.parse_args()
    delay = args.live if args.live is not None else 0.0
    if not isfinite(delay) or delay < 0:
        parser.error("--live SECONDS must be finite and nonnegative")

    processor = OpenAIAgentsTraceAdapter(f"traces/offline-{uuid4().hex}.jsonl")
    # Replaces the SDK's default remote exporter process-wide.
    set_trace_processors([processor])
    try:
        if args.live is not None:
            print(processor.path, flush=True)
            print(f"Attach in another terminal with: pincer debug {processor.path} --follow")
            try:
                input("Press Enter to start the trace.")
            except (EOFError, KeyboardInterrupt):
                print()
                return
        with trace("Pincer offline demo"):
            with agent_span(name="demo"):
                with generation_span(model="synthetic-model", usage={"input_tokens": 12, "output_tokens": 4}):
                    sleep(delay)
                with function_span(name="lookup", input='{"topic":"pincer"}', output="A trace adapter"):
                    sleep(delay)
                with function_span(name="deliberate_failure") as span:
                    sleep(delay)
                    span.set_error({"message": "Simulated tool failure", "data": {}})
    finally:
        processor.shutdown()
    if processor.write_errors:
        raise RuntimeError("Trace recording failed")
    if args.live is None:
        print(processor.path)


if __name__ == "__main__":
    main()
