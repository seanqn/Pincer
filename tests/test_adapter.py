import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from agents import set_trace_processors
from agents.tracing import trace, agent_span, generation_span, function_span, response_span
from openai.types.responses import Response

from pincer import OpenAIAgentsTraceAdapter
from pincer.cli import render


@pytest.fixture
def recording(tmp_path):
    processor = OpenAIAgentsTraceAdapter(tmp_path / "trace.jsonl")
    set_trace_processors([processor])
    yield processor
    set_trace_processors([])
    processor.shutdown()


def records(processor):
    return [json.loads(line) for line in processor.path.read_text().splitlines()]


def test_real_sdk_lifecycle_parentage_failure_and_privacy(recording):
    with trace("secret trace name", metadata={"secret": "private"}):
        with agent_span(name="agent") as parent:
            with generation_span(model="test", input=[{"content": "private"}], usage={"input_tokens": 4}):
                pass
            with function_span(name="lookup", input="private", output="private") as tool:
                tool.set_error({"message": "private", "data": {"token": "private"}})
    events = records(recording)
    assert len(events) == 8
    assert [e["sequence"] for e in events] == list(range(1, 9))
    assert len({e["event_id"] for e in events}) == 8
    tool_end = next(e for e in events if e["kind"] == "tool" and e["event"] == "span.ended")
    assert tool_end["parent_id"] == parent.span_id
    assert tool_end["status"] == "error"
    assert tool_end["started_at"] and tool_end["ended_at"]
    assert "private" not in recording.path.read_text()
    assert events[-1]["status"] == "ended"
    assert "error" in render(recording.path)
    assert "INCOMPLETE" not in render(recording.path)
    assert recording.write_errors == 0


def test_response_usage_and_opt_in_content(recording):
    recording.capture_content = True
    response = Response.model_construct(id="resp_test", model="test", output=[],
                                        usage=None)
    with trace("response"):
        with response_span(response=response) as span:
            span.span_data.input = "visible"
            span.span_data.usage = {"input_tokens": 2}
    event = records(recording)[-2]
    assert event["kind"] == "model"
    assert event["sdk_type"] == "response"
    assert event["details"]["usage"] == {"input_tokens": 2}
    assert event["details"]["input"] == "visible"
    assert event["details"]["response"]["id"] == "resp_test"


def test_interrupted_trace(recording):
    run = trace("interrupted")
    run.start()
    assert "INCOMPLETE" in render(recording.path)
    run.finish()
    assert "INCOMPLETE" not in render(recording.path)


def test_concurrent_callbacks(recording):
    def emit(_):
        with trace("parallel"):
            with function_span(name="tool"):
                pass
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(emit, range(10)))
    events = records(recording)
    assert len(events) == 40
    assert [e["sequence"] for e in events] == list(range(1, 41))
    assert len({e["trace_id"] for e in events}) == 10


def test_write_failure_does_not_break_agent(recording):
    recording.shutdown()
    with trace("closed sink"):
        pass
    assert recording.write_errors == 2
    recording.force_flush()
    recording.shutdown()


def test_existing_file_not_overwritten(recording):
    with pytest.raises(FileExistsError):
        OpenAIAgentsTraceAdapter(recording.path)


def test_corrupt_record_reports_line(tmp_path):
    path = tmp_path / "broken.jsonl"
    path.write_text('{"partial":')
    with pytest.raises(ValueError, match="line 1"):
        render(path)


def test_runner_executes_local_tool_with_fake_model(recording):
    import asyncio
    from agents import Agent, Runner, function_tool
    from agents.models.interface import Model
    from agents.items import ModelResponse
    from agents.usage import Usage
    from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

    calls = []

    @function_tool
    def lookup() -> str:
        """Read the local demo description."""
        calls.append("lookup")
        return "Pincer"

    class FakeModel(Model):
        def __init__(self):
            self.turn = 0

        async def get_response(self, *args, **kwargs):
            self.turn += 1
            with generation_span(model="fake", usage={"input_tokens": 1, "output_tokens": 1}):
                if self.turn == 1:
                    output = [ResponseFunctionToolCall(
                        type="function_call", name="lookup", arguments="{}", call_id="call_demo")]
                else:
                    output = [ResponseOutputMessage(
                        id="msg_demo", type="message", role="assistant", status="completed",
                        content=[ResponseOutputText(type="output_text", text="Pincer", annotations=[])])]
                return ModelResponse(output=output, usage=Usage(requests=1, input_tokens=1, output_tokens=1, total_tokens=2), response_id=None)

        async def stream_response(self, *args, **kwargs):
            raise NotImplementedError("Streaming is outside this fixture")
            yield

    result = asyncio.run(Runner.run(Agent(name="demo", model=FakeModel(), tools=[lookup]), "Describe Pincer", max_turns=3))
    assert result.final_output == "Pincer"
    assert calls == ["lookup"]
    events = records(recording)
    assert len([e for e in events if e["kind"] == "model" and e["event"] == "span.ended"]) == 2
    assert any(e["kind"] == "tool" and e["event"] == "span.ended" for e in events)
    assert recording.write_errors == 0
    assert "INCOMPLETE" not in render(recording.path)
