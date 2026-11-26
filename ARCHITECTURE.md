
## 1. Project Structure

```
Pincer/
├── docs/                       # Project documentation (Empty as of now)
├── examples/
    ├── live_agent.py           # Exemplification of real tracing
    ├── offline_trace.py        # Exemplification of offline tracing
    └── tool_breakpoint.py      # Exemplification of breakpoint
├── src/
    ├── pincer/
        ├── __init__.py
        ├── adapter.py          # Defines adapter(s) trace processing
        ├── cli.py              # Defines CLI output
        ├── runner.py           # Defines breakpoint and resume capabilites
        ├── debugger.py         # Defines the foundational debugger logic and debug subcommands
        └── trace.py            # Shared reader for v1 schema, does not call a trace
├── tests/                      # Includes tests for implementations, per-component (i.e. test_adapter, test_debugger)
├── traces/                     # Destination for traces in JSONL
├── .gitignore                  # Specifies intentionally untracked files to ignore
├── README.md                   # Project overview and quick start guide
├── AGENTS.md                   # Context for agentic workflows
└── ARCHITECTURE.md             # This document, architectural concerns
```


## 2. High-Level System Diagram
```
Adapters (Model API) --> Pincer [Adapter Logic <--> Debugger <-- Runner(Breakpoint/Resume)]
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
- Live trace viewing
- Testing with actual traces
- Agnostic adapter, route more SDKs into the project, including for local LLMs

Horizon:
- Debugger frontend (out of CLI)

## 7. Project Identification

Project Name: Pincer

Repository URL: github.com/seanqn/Pincer

Primary Contact/Team: Sean Quinn

Date of Last Update: [2026-11-25]