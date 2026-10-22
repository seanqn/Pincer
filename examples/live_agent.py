"""Optional paid smoke test. Requires a key and an explicitly selected model."""
import argparse
import asyncio
import os
from uuid import uuid4

from agents import Agent, ModelSettings, Runner, RunConfig, function_tool, set_default_openai_client, set_trace_processors
from openai import AsyncOpenAI
from pincer import OpenAIAgentsTraceAdapter


@function_tool
def lookup_project() -> str:
    """Return a brief description of Pincer."""
    return "Pincer records agent execution events for local inspection."


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="An API model available to your project")
    args = parser.parse_args()
    key = os.environ.get("PINCER_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        parser.error("Set OPENAI_API_KEY (local) or PINCER_OPENAI_API_KEY (cloud).")
    processor = OpenAIAgentsTraceAdapter(f"traces/live-{uuid4().hex}.jsonl")
    set_trace_processors([processor])
    try:
        async with AsyncOpenAI(api_key=key, max_retries=0, timeout=30.0) as client:
            set_default_openai_client(client, use_for_tracing=False)
            agent = Agent(
                name="Pincer demo", model=args.model,
                instructions="Call lookup_project once, then summarize its result in one sentence.",
                tools=[lookup_project], model_settings=ModelSettings(max_tokens=256),
            )
            result = await Runner.run(agent, "What is Pincer?", max_turns=3,
                                      run_config=RunConfig(trace_include_sensitive_data=False))
            print(result.final_output)
    finally:
        processor.shutdown()
        print(f"Trace: {processor.path}; recording errors: {processor.write_errors}")
    if processor.write_errors:
        raise RuntimeError("Trace recording failed")


if __name__ == "__main__":
    asyncio.run(main())
