import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

from pincer.checkpoints import read_checkpoint_history
from pincer.debugger import TraceView
from pincer.runner import read_breakpoint
from pincer.trace import iter_events, read_events


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


@pytest.fixture
def recovery_demo(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(repository / "src"), str(repository / "tests"), environment.get("PYTHONPATH")])
    )
    environment["PINCER_TEST_DIRECTORY"] = str(tmp_path)
    subprocess.run(
        [sys.executable, "examples/tool_breakpoint.py", "pause", str(tmp_path)],
        cwd=repository, env=environment, capture_output=True, text=True, check=True, timeout=20,
    )
    database = tmp_path / "checkpoint.sqlite3"
    command = [
        sys.executable, "-m", "pincer.cli", "recover", str(database),
        "--agent", "recovery_fixture:profile",
    ]

    def run(*args, mode="continue"):
        return subprocess.run(
            [*command, *args], cwd=repository, env={**environment, "PINCER_TEST_MODE": mode},
            capture_output=True, text=True, timeout=20,
        )

    def start(*args, mode):
        return subprocess.Popen(
            [*command, *args], cwd=repository, env={**environment, "PINCER_TEST_MODE": mode},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    return database, run, start


def test_saved_result_survives_process_exit_and_claim_is_exclusive(tmp_path, recovery_demo):
    database, recover, start = recovery_demo
    denied = recover("--checkpoint", "1")
    assert denied.returncode == 1
    assert "requires approval" in denied.stderr
    assert not (tmp_path / "effects.log").exists()

    mismatch = recover("--approve", mode="different-profile")
    assert mismatch.returncode == 1
    assert "configuration does not match" in mismatch.stderr
    assert read_checkpoint_history(database).checkpoints[0].action == "Approve and continue"

    crashed = recover("--checkpoint", "1", "--approve", mode="crash-after-commit")
    assert crashed.returncode == 72, crashed.stderr
    history = read_checkpoint_history(database)
    approval, saved_result = history.checkpoints
    assert approval.action == "Checkpoint superseded"
    assert saved_result.action == "Continue from saved result"
    assert (tmp_path / "effects.log").read_text() == "Pincer\n"
    assert saved_result.span_id is not None
    assert saved_result.trace_path is not None

    view = TraceView(iter_events(saved_result.trace_path))
    view.set_checkpoint_history(history)
    step = next(i for i, span in enumerate(view.steps) if span.record["span_id"] == saved_result.span_id)
    detail = view.detail(step)
    assert "Resumable state saved: yes" in detail
    assert "Checkpoint 2 | Tool result | Continue from saved result" in detail

    older = recover("--checkpoint", "1", "--approve")
    assert older.returncode == 1
    assert "superseded" in older.stderr

    with start("--checkpoint", "2", mode="hold") as worker:
        try:
            deadline = time.monotonic() + 15
            while not (tmp_path / "model-started").exists():
                assert worker.poll() is None
                assert time.monotonic() < deadline
                time.sleep(0.02)
            second = recover("--checkpoint", "2")
            assert second.returncode == 1
            assert "already owns" in second.stderr
            (tmp_path / "release-model").touch()
            output, error = worker.communicate(timeout=15)
            assert worker.returncode == 0, error
            assert "Pincer was looked up." in output
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.communicate(timeout=5)

    history = read_checkpoint_history(database)
    dispatch, continued = history.attempts
    assert dispatch.status == "interrupted"
    assert continued.status == "completed"
    assert Path(continued.trace_path).is_file()
    assert history.checkpoints[-1].action == "Run completed"
    assert (tmp_path / "effects.log").read_text() == "Pincer\n"


def test_process_exit_during_dispatch_blocks_recovery(tmp_path, recovery_demo):
    database, recover, _ = recovery_demo
    crashed = recover("--approve", mode="crash-during-dispatch")
    assert crashed.returncode == 71, crashed.stderr

    only_checkpoint, = read_checkpoint_history(database).checkpoints
    assert only_checkpoint.action == "Dispatch claimed. Outcome unconfirmed"
    for options in ((), ("--approve",)):
        denied = recover(*options)
        assert denied.returncode == 1
        assert "outcome is unconfirmed" in denied.stderr
    assert (tmp_path / "effects.log").read_text() == "Pincer\n"


def test_next_tool_is_saved_as_a_new_approval_checkpoint(tmp_path, recovery_demo):
    database, recover, _ = recovery_demo
    paused = recover("--approve", mode="next-tool")
    assert paused.returncode == 0, paused.stderr
    assert "Paused for tool approval" in paused.stdout
    first, result, next_tool = read_checkpoint_history(database).checkpoints
    assert first.breakpoint.call_id == result.breakpoint.call_id == "call_demo"
    assert next_tool.breakpoint.call_id == "call_next"
    assert next_tool.action == "Approve and continue"
    assert next_tool.span_id is not None
    assert (tmp_path / "effects.log").read_text() == "Pincer\n"
    assert result.action == "Continue from saved result and revisit pending approval"
    revisited = recover("--checkpoint", str(result.id), mode="next-tool")
    assert revisited.returncode == 0, revisited.stderr
    history = read_checkpoint_history(database)
    assert history.checkpoints[-2].action == "Checkpoint superseded"
    replacement = history.checkpoints[-1]
    assert replacement.id > next_tool.id
    assert replacement.action == "Approve and continue"
    assert (tmp_path / "effects.log").read_text() == "Pincer\n"
    denied = recover("--approve", "--checkpoint", str(next_tool.id))
    assert denied.returncode == 1
    assert "superseded" in denied.stderr

    finished = recover("--approve", "--checkpoint", str(replacement.id), mode="next-tool")
    assert finished.returncode == 0, finished.stderr
    assert read_checkpoint_history(database).checkpoints[-1].action == "Run completed"
    assert (tmp_path / "effects.log").read_text() == "Pincer\nSecond step\n"


def test_legacy_pending_breakpoint_can_join_the_checkpoint_history(tmp_path, recovery_demo):
    database, recover, _ = recovery_demo
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("CREATE TABLE old_breakpoint AS SELECT * FROM breakpoint")
        db.execute("DROP VIEW breakpoint")
        db.execute("DROP VIEW run_position")
        db.execute("DROP TABLE recovery_run")
        db.execute("DROP TABLE checkpoints")
        db.execute("DROP TABLE attempts")
        db.execute("ALTER TABLE old_breakpoint RENAME TO breakpoint")
        db.execute("PRAGMA user_version = 0")

    assert read_breakpoint(database).status == "pending"
    assert read_checkpoint_history(database).checkpoints[0].action == "Approve and continue"
    resumed = recover("--approve")
    assert resumed.returncode == 0, resumed.stderr
    history = read_checkpoint_history(database)
    assert len(history.checkpoints) == 2
    assert history.attempts[0].status == "completed"
    assert (tmp_path / "effects.log").read_text() == "Pincer\n"
