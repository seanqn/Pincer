"""Pause before a local tool, inspect its call, and resume in a new process."""
import argparse
import asyncio
import sqlite3
from pathlib import Path
from uuid import uuid4

from agents import Agent, RunConfig, Runner, function_tool, set_trace_processors
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.tracing import generation_span, trace
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

from pincer import OpenAIAgentsTraceAdapter
from pincer.debugger import TraceView
from pincer.runner import RecoveryProfile, resume_breakpoint, save_breakpoint
from pincer.trace import iter_events


@function_tool(needs_approval=True, failure_error_function=None)
def lookup(topic: str) -> str:
    """Describe the supplied topic."""
    print("Invoked local tool: lookup")
    return f"{topic} records agent traces locally."


class FakeModel(Model):
    def __init__(self, *, interrupt: bool = False) -> None:
        self.interrupt = interrupt

    async def get_response(self, system_instructions, input, *args, **kwargs):
        with generation_span(model="fake"):
            for item in input:
                if item.get("type") == "function_call_output":
                    if self.interrupt:
                        raise RuntimeError("Demo interrupted after saving the tool result")
                    message = ResponseOutputMessage(
                        id="msg_demo", type="message", role="assistant", status="completed",
                        content=[ResponseOutputText(
                            type="output_text", text=item["output"], annotations=[],
                        )],
                    )
                    return ModelResponse([message], Usage(requests=1), None)
            call = ResponseFunctionToolCall(
                type="function_call", name="lookup", call_id="call_demo",
                arguments='{"topic": "Pincer"}',
            )
            return ModelResponse([call], Usage(requests=1), None)

    async def stream_response(self, *args, **kwargs):
        raise NotImplementedError("This demo uses the non-streaming runner")
        yield


def recovery_profile(*, interrupt: bool = False) -> RecoveryProfile:
    agent = Agent(name="breakpoint demo", model=FakeModel(interrupt=interrupt), tools=[lookup])
    config = RunConfig(trace_include_sensitive_data=False)
    return RecoveryProfile("pincer-breakpoint-demo-v1", agent, config)


async def run(phase: str, directory: Path) -> None:
    checkpoint_path = directory / "checkpoint.sqlite3"
    trace_name = "paused.jsonl" if phase == "pause" else f"resumed-{uuid4().hex}.jsonl"
    profile = recovery_profile(interrupt=phase == "interrupt")
    agent, config = profile.agent, profile.run_config
    processor = OpenAIAgentsTraceAdapter(directory / trace_name)
    set_trace_processors([processor])
    try:
        with trace(f"Tool breakpoint {phase}"):
            if phase == "pause":
                result = await Runner.run(agent, "Describe Pincer", max_turns=3, run_config=config)
                breakpoint = save_breakpoint(
                    checkpoint_path, result.to_state(), configuration_id=profile.configuration_id,
                    trace_adapter=processor,
                )
            else:
                result = await resume_breakpoint(
                    agent, checkpoint_path, run_config=config,
                    configuration_id=profile.configuration_id, trace_adapter=processor,
                )
    finally:
        set_trace_processors([])
        processor.shutdown()

    print(f"Trace: {processor.path}")
    if phase == "pause":
        print(f"Checkpoint: {checkpoint_path}")
        print(TraceView(iter_events(processor.path)).listing(breakpoint=breakpoint))
    else:
        print(result.final_output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("pause", "resume", "interrupt"))
    parser.add_argument("directory", type=Path, help="local directory for the trace and checkpoint")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.phase, args.directory))
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"tool breakpoint: {exc}\n")


if __name__ == "__main__":
    main()
