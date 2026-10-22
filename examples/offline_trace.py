"""Exercise real SDK callbacks using synthetic spans; no API calls or key."""
from uuid import uuid4
from agents import set_trace_processors
from agents.tracing import trace, agent_span, generation_span, function_span
from pincer import OpenAIAgentsTraceAdapter

processor = OpenAIAgentsTraceAdapter(f"traces/offline-{uuid4().hex}.jsonl")
# Replaces the SDK's default remote exporter process-wide.
set_trace_processors([processor])
try:
    with trace("Pincer offline demo"):
        with agent_span(name="demo"):
            with generation_span(model="synthetic-model", usage={"input_tokens": 12, "output_tokens": 4}):
                pass
            with function_span(name="lookup", input='{"topic":"pincer"}', output="A trace adapter"):
                pass
            with function_span(name="deliberate_failure") as span:
                span.set_error({"message": "Simulated tool failure", "data": {}})
finally:
    processor.shutdown()
if processor.write_errors:
    raise RuntimeError("Trace recording failed")
print(processor.path)
