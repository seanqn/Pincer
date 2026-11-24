Defines current project architecture and iteratively build anticipated implementations.

### Current 
Based on: commit a63f8e4b9f4cfabe79a2e8c84ebfd811ae8af204 (origin/main)

### CLI
Defines commands and corresponding trace output on the command line

#### Adapter
The adapter leverages just one SDK: OpenAI SDK. It imports the OpenAIAgentsTraceAdapter and plugs in the TracingProcessor. One adapter
owns one new file; existing files are never overwritten. Callbacks are serialized under a lock and writes are flushed, not fsynced.
Errors are logged and counted instead of interrupting the agent. Custom adapter does not perform network requests, but OpenAPI SDK
TracingProcessor does.

_record: defines dictionary typed record output from trace information,
on_<trace/span>_<start/end> functions call to _record and provide trace and span items

### Debugger
CLI debugger. Defines CLI commands and corresponding trace output. Pulls from a /traces/<source_trace>.JSONL file and offers
the following commands: list | select <step> | details | next | previous | help | quit. Supports inspection but no further
capability.

#### Next targeted changes
1. Reader and step view are incremental: currently, it loads the whol file and debugger builds the view in one pass. Reusing
   the same record validation and span-pairing rules for saved traces and live viewing is optimal. A follow (live-trace) mode
   shows a stream of trace output as it is received. In follow mode, read from the last offset, buffer an unfinished final line, and
   apply each complete record once. The first regression test should append a span end in two writes and verify that the view shows
   as open until the complete record arrives.
2. Keep trace failures visible: the adapter counts failed writes and lets the agent continue. Its sequence advances only after a
   successful write, so an event can be lost without leaving a sequence gap. A live viewer should treat a missing endpoint as an
   observation gap. The runner should explicitly report whether a step is running, failed, or safe to resume.
3. Put the breakpoint at tool dispatch: start with one fake model and one local tool, then let the runner hold the pending call while
   the debugger displays it. Make that same boundary durable by committing the model result and pending call together. This gives a
   clean progression from continuation of a paused process to durable restart and resume from the saved call.
