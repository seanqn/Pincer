# Pincer

A Python foundation for recording and inspecting OpenAI Agents SDK traces locally.
This iteration provides a tracing adapter, JSON Lines storage, and a terminal viewer.
It does **not** implement checkpoint restoration, replay, or resume.

## Set up in VS Code

Open this repository folder in VS Code. Install Microsoft's Python and Python
Debugger extensions. Python 3.10+ is required; this foundation was tested on 3.12.

```bash
python -m venv .venv
```

Activate the environment:

- macOS/Linux: `source .venv/bin/activate`
- Windows PowerShell: `.venv\Scripts\Activate.ps1`

Then install the project and test dependencies:

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

Use **Python: Select Interpreter** to select `.venv` if VS Code does not select it
automatically. The included debugger configuration runs the offline example.
`openai-agents` is pinned to the version tested against the public processor API;
transitive dependencies are not locked yet.

## Try it without billing or a key

```bash
python examples/offline_trace.py
pincer traces/offline-<printed-id>.jsonl
```

Use the actual path printed by the example. Each run creates a new file. The demo
emits synthetic model/tool spans through the **real SDK tracing callbacks**,
including a deliberately marked tool failure. It does not call a model or run a
real agent. The viewer reads the file only; it never contacts a provider.

## Attach to your agent

```python
from agents import set_trace_processors
from pincer import OpenAIAgentsTraceAdapter

processor = OpenAIAgentsTraceAdapter("traces/my-run.jsonl")
set_trace_processors([processor])
try:
    # Run your existing Agent with Runner.run(...) / Runner.run_sync(...).
    # Ensure async runs have finished before shutting down the processor.
    ...
finally:
    processor.shutdown()

assert processor.write_errors == 0
```

Registration is explicit and process-wide. `set_trace_processors` replaces other
processors, including the SDK's default OpenAI trace exporter. Importing Pincer
alone changes nothing. Use `add_trace_processor` instead only if you deliberately
want to retain existing exporters, including any remote export behavior.
Keep SDK tracing enabled: disabling it also prevents Pincer from receiving events.

The public SDK interfaces are documented at
[Tracing](https://openai.github.io/openai-agents-python/tracing/) and
[TracingProcessor](https://openai.github.io/openai-agents-python/ref/tracing/processor_interface/).

## Optional live API smoke test

Only this example incurs model usage. Configure API billing and set a key in your
terminal environment. Never put the key in source, launch configuration, or chat.
The example accepts `PINCER_OPENAI_API_KEY` first, then `OPENAI_API_KEY`; the former
can be used with a supported cloud secret binding. `.env` files are ignored by Git
but are **not automatically loaded**.

```bash
python examples/live_agent.py --model YOUR_MODEL_ID
```

Select a model available to your project supporting Responses API function tools
and the output-token limit. The example asks the agent to call a harmless local
lookup tool. It limits execution to three model turns, requests at most 256 output
tokens per call, disables client retries, and sets a 30-second request timeout.
These are usage controls, not a dollar spending cap. Inspect the saved trace to
confirm the model actually called the tool. Model behavior is not guaranteed.
No live API request is part of the default tests.

## Trace contract (schema version 1)

Each line is an event with an ID, file-local sequence, UTC observation timestamp,
trace/span/parent IDs, normalized kind, original SDK type, status, details, and
SDK span timestamps when available. `function` maps to `tool`; `generation` and
`response` map to `model`; other SDK span types retain their names. Usage is
included when exposed by the SDK; absent usage does not mean zero usage.

- `trace.started` / `trace.ended` delimit an observed trace, not business success.
- `span.started` records entry; `span.ended` reports an SDK error if supplied.
- `ended_without_reported_error` does not prove the operation succeeded.
- Unmatched starts appear as **INCOMPLETE** in the viewer. A missing end does not
  prove the external operation failed or is safe to repeat.
- Tool errors handled by the framework may appear inside otherwise completed runs.
  Pincer relies on SDK error reporting and cannot infer failures hidden in outputs.

By default, prompts, outputs, trace names/metadata, and error text are omitted.
Tool/agent names, model names, IDs, timestamps, and usage remain; keep secrets out
of these labels. `capture_content=True` explicitly records SDK payloads and error
details without automatic redaction. Only enable it with data you can store locally.
SDK settings can also suppress payloads before Pincer receives them.

Files are created exclusively, and each event is flushed under a thread lock.
There is no multi-process shared-file support, fsync durability guarantee, rotation,
or retention policy. Partial/corrupt lines produce a line-numbered viewer error;
recording I/O errors are logged without payloads and counted in `write_errors`.
Callbacks write synchronously, appropriate for this small foundation rather than
high-throughput production use. A trace is not a recoverable checkpoint.
