
## 1. Project Structure

```
Pincer/
├── .venv/                      # Necessary virtual environment dependencies and configurations
├── .vscode/                    # Launch and Settings JSON files for VSCode
├── docs/                       # Project documentation
├── examples/
    ├── live_agent.py           # Exemplification of live tracing
    └──  offline_trace.py       # Exemplification of offline tracing
├── src/
    ├── pincer/
        ├── __init__.py
        ├── adapter.py          # Defines adapter(s) trace procesing
        ├── cli.py              # Defines CLI output
        ├── debugger.py         # Defines the foundational debugger logic and debug subcommands
        └── trace.py            # Shared reader for v1 schema, does not call a trace
    └── pincer_trace.egg-info
├── tests/                      # Includes tests for implementations, per-component (i.e. test_adapter, test_debugger)
├── traces/                     # Destination for traces in JSONL
├── .gitignore                  # Specifies intentionally untracked files to ignore
├── README.md                   # Project overview and quick start guide
└── ARCHITECTURE.md             # This document, architectural concerns
```


## 2. High-Level System Diagram
```
Adapters (Model API) --> Pincer [Adapter Logic <--> Debugger]
```                      

## 3. External Integrations / APIs

- OpenAI: Agents SDK, particularly the TracingProcessor
- TBI: Claude, Gemini, and Local LLM integration

## 4. Deployment & Infrastructure

CI/CD Pipeline: git, GitHub, Linear (documenation and issue tracking)

## 5. Development & Testing Environment

Using Python's venv for isolated development

Local Setup Instructions: Root level README.md includes sufficient .venv setup

Testing Frameworks: PyTest

## 6. Future Considerations / Roadmap

Next-Steps Roadmap:
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

## 7. Project Identification

Project Name: Pincer

Repository URL: github.com/seanqn/Pincer

Primary Contact/Team: Sean Quinn

Date of Last Update: [2026-11-24]