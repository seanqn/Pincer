# Pincer

A Python foundation for recording and inspecting OpenAI Agents SDK traces locally.
Pincer includes a tracing adapter, JSON Lines storage, a terminal viewer, and a
read-only step debugger. The offline tool breakpoint demo saves a paused SDK run
and resumes its pending local function call in a new process.

## Set up in VS Code

Python 3.10+ and Python Debugger extensions required; this foundation was tested on 3.12.

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
including a marked tool failure. It does not call a model or run a
real agent. The viewer reads the file only and never contacts a provider.

## Inspect steps in the debugger

Use the same printed file path from the offline example:

```bash
pincer debug traces/offline-<printed-id>.jsonl
```

The debugger loads a saved JSONL file once. Each model request or tool invocation
is one step, pairing its start/end events by trace and span IDs. Steps are numbered
in first-observation order and shown under their parent agent. Concurrent spans
can overlap; the list is not a claim that execution was sequential.

```text
Commands: list | select <step> | details | next | previous | help | quit

pincer(debug)> select 2
Selected: Step 02 - "lookup"
...
Checkpoint assessment:
  Completed boundary: yes (start and end recorded)
  Checkpoint candidate: yes (boundary only)
  Resumable state saved: no
```

`list` shows all steps; `select 3` inspects the offline demo's deliberate failure.
`next` and `previous` navigate saved records only. `details` shows the selected
step's IDs, timing, input/output when recorded, error, and latest usage snapshot.
Start/end usage is not added together. Missing usage is unknown, not zero.
Use `help` to list commands and `quit`, Ctrl-D (EOF), or Ctrl-C to leave.

For a noninteractive overview:

```bash
pincer debug traces/offline-<printed-id>.jsonl --list
```

The original `pincer TRACE` event viewer is still available. Both commands share
the same saved-event reader. `python -m pincer.cli debug TRACE` is also supported.

A **checkpoint candidate** is only a model/tool span with both endpoints and an
end status of `ended_without_reported_error`. It does not prove success, save
resumable state, or establish that repeating an operation is safe. Failed and
incomplete steps are not marked as candidates. Missing trace/agent endpoints are
also flagged, even when individual steps have both endpoints. Malformed records
and ambiguous duplicate span events produce a line-numbered error.

The demo always has two candidate boundaries followed by one error-marked tool
step. Its enclosing agent and trace still end: displaying an error does not mean
the debugger stopped execution. Default traces omit inputs, outputs, and error
text; the debugger cannot recover that content. It neither executes steps nor
contacts a model, writes checkpoints, retries work, or enforces spending limits.

## Follow a live trace

Once the recording file exists, attach from a separate terminal:

```bash
pincer debug TRACE --follow
```

The debugger loads the history, then shows model and tool updates as events
arrive. Open spans keep their step numbers when they end. Use the same navigation
commands while the trace grows, and use `quit` or Ctrl-C to detach.

Follow watches one file for appends and keeps waiting after a trace ends. With
`--checkpoint PATH`, it also shows changes to the committed breakpoint status.
If the runner resumes into a new file, follow that file to see the resumed spans.

## Pause and resume a tool call offline

```bash
python examples/tool_breakpoint.py pause traces/breakpoint-demo
pincer debug traces/breakpoint-demo/paused.jsonl --checkpoint traces/breakpoint-demo/checkpoint.sqlite3 --list
python examples/tool_breakpoint.py resume traces/breakpoint-demo
```

The fake model requests `lookup`, and the SDK pauses before invoking the local
function. The debugger shows the tool name, call ID, arguments, and dispatch
status. The SDK can emit a tool span during its approval check.

The checkpoint commits the model response and pending call together. `resume`
approves the saved call and continues in a new process. A checkpoint can be
claimed once. If the process stops after claiming it, the debugger reports an
unconfirmed outcome. Use a new directory for a new demo run.

Checkpoint files can contain full model inputs and tool arguments. Resume only
from checkpoints you trust and use the same agent and tool configuration.

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

Only this example incurs model usage. Example shows accepting `PINCER_OPENAI_API_KEY` first, then `OPENAI_API_KEY`; the former
can be used with a supported cloud secret binding. `.env` files are ignored by Git
but are not automatically loaded.

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
- A missing start or end appears as **INCOMPLETE**. This observation gap leaves
  the execution state unknown unless the runner reports it separately.
- Tool errors handled by the framework may appear inside otherwise completed runs.
  Pincer relies on SDK error reporting and cannot infer failures hidden in outputs.

By default, prompts, outputs, trace names/metadata, and error text are omitted.
Tool/agent names, model names, IDs, timestamps, and usage remain; keep secrets out
of these labels. `capture_content=True` explicitly records SDK payloads and error
details without automatic redaction. Only enable it with data you can store locally.
SDK settings can also suppress payloads before Pincer receives them.

Files are created exclusively, and each event is written under a thread lock.
There is no multi-process shared-file support, fsync durability guarantee, rotation,
or retention policy. Partial or corrupt lines produce a line-numbered viewer error.
Recording errors are logged without payloads and counted in `write_errors`. Each
saved record carries the cumulative count so far and uses the next consecutive
sequence number. Check the adapter's final count for failures after the last saved
record. Earlier schema-v1 files may omit the count.
Callbacks write synchronously, appropriate for this small foundation rather than
high-throughput production use. A trace is not a recoverable checkpoint.
