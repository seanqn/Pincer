import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from pincer.runner import read_breakpoint
from pincer.trace import read_events


def test_debugger_displays_durable_breakpoint_and_fresh_runner_dispatches_once(tmp_path):
    example = Path(__file__).resolve().parents[1] / "examples" / "tool_breakpoint.py"
    checkpoint = tmp_path / "checkpoint.sqlite3"
    paused_trace = tmp_path / "paused.jsonl"

    paused = subprocess.run(
        [sys.executable, str(example), "pause", str(tmp_path)],
        capture_output=True, text=True, check=True, timeout=20,
    )
    assert "Tool breakpoint: Paused before dispatch" in paused.stdout
    assert "Invoked local tool" not in paused.stdout
    assert read_breakpoint(checkpoint).call_id == "call_demo"
    with closing(sqlite3.connect(checkpoint)) as db:
        snapshot, = db.execute("SELECT snapshot FROM breakpoint").fetchone()
    state = json.loads(snapshot)
    call, = state["last_model_response"]["output"]
    interruption, = state["current_step"]["data"]["interruptions"]
    assert call == interruption["raw_item"]

    original = checkpoint.read_bytes()
    debugger = subprocess.run(
        [sys.executable, "-m", "pincer.cli", "debug", str(paused_trace),
         "--checkpoint", str(checkpoint), "--list"],
        capture_output=True, text=True, check=True, timeout=20,
    )
    assert 'Tool: "lookup"' in debugger.stdout
    assert 'Call ID: "call_demo"' in debugger.stdout
    assert 'Arguments: {"topic": "Pincer"}' in debugger.stdout
    assert checkpoint.read_bytes() == original

    resume_command = [sys.executable, str(example), "resume", str(tmp_path)]
    resumed = subprocess.run(resume_command, capture_output=True, text=True, check=True, timeout=20)
    assert resumed.stdout.count("Invoked local tool: lookup") == 1
    assert "Pincer records agent traces locally." in resumed.stdout
    resumed_trace, = tmp_path.glob("resumed-*.jsonl")
    model_ends = [event for event in read_events(resumed_trace) if (
        event["kind"] == "model" and event["event"] == "span.ended"
    )]
    assert len(model_ends) == 1  # The tool result gets a new turn. The saved turn is reused.
    assert read_breakpoint(checkpoint).status == "completed"

    repeated = subprocess.run(resume_command, capture_output=True, text=True, timeout=20)
    assert repeated.returncode != 0
    assert "cannot resume from status completed" in repeated.stderr
    assert "Invoked local tool" not in repeated.stdout
