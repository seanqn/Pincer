"""Offline agent for exercising real process exits and concurrent recovery."""
import asyncio
import os
from pathlib import Path

from agents import function_tool
from agents.items import ModelResponse
from agents.tracing import generation_span
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall

from examples.tool_breakpoint import FakeModel, recovery_profile
from pincer.runner import RecoveryProfile


def profile():
    mode = os.environ["PINCER_TEST_MODE"]
    directory = Path(os.environ["PINCER_TEST_DIRECTORY"])

    @function_tool(needs_approval=True, failure_error_function=None)
    def lookup(topic: str) -> str:
        """Describe the supplied topic."""
        with (directory / "effects.log").open("a") as effects:
            effects.write(topic + "\n")
        if mode == "crash-during-dispatch":
            os._exit(71)
        return f"{topic} was looked up."

    class RecoveryModel(FakeModel):
        async def get_response(self, system_instructions, input, *args, **kwargs):
            outputs = [item for item in input if item.get("type") == "function_call_output"]
            if outputs and mode == "crash-after-commit":
                os._exit(72)
            if outputs and mode == "hold":
                (directory / "model-started").touch()
                while not (directory / "release-model").exists():
                    await asyncio.sleep(0.02)
            if len(outputs) == 1 and mode == "next-tool":
                with generation_span(model="fake"):
                    call = ResponseFunctionToolCall(
                        type="function_call", name="lookup", call_id="call_next",
                        arguments='{"topic":"Second step"}',
                    )
                    return ModelResponse([call], Usage(requests=1), None)
            return await super().get_response(system_instructions, input, *args, **kwargs)

    demo = recovery_profile()
    return RecoveryProfile(
        "different-profile" if mode == "different-profile" else demo.configuration_id,
        demo.agent.clone(model=RecoveryModel(), tools=[lookup]),
        demo.run_config,
    )
