import copy
import subprocess
import sys
from pathlib import Path

import pytest

from pincer.cli import main, render
from pincer.debugger import TraceView, inspect
from pincer.trace import read_events


@pytest.fixture
def offline(tmp_path):
    example = Path(__file__).resolve().parents[1] / "examples" / "offline_trace.py"
    result = subprocess.run(
        [sys.executable, str(example)], cwd=tmp_path, capture_output=True,
        text=True, check=True, timeout=20,
    )
    return tmp_path / result.stdout.strip()


def test_offline_cli_navigation_and_read_only(offline):
    original = offline.read_bytes()
    result = subprocess.run(
        [sys.executable, "-m", "pincer.cli", "debug", str(offline)],
        input="details\nnext\nnext\nnext\nprevious\nselect 1\nselect 0\nselect nope\nresume\nlist\nquit\n",
        capture_output=True, text=True, check=True, timeout=20,
    )
    output = result.stdout
    assert 'Agent: "demo"' in output
    assert 'Selected: Step 01 - "synthetic-model"' in output
    assert 'Selected: Step 02 - "lookup"' in output
    assert 'Selected: Step 03 - "deliberate_failure"' in output
    assert 'Error: {"message": "[content omitted]"}' in output
    assert "execution stopped here" in output
    assert "Resumable state saved: no" in output
    assert "selection unchanged" in output
    assert "Usage: select <step number>" in output
    assert "execution commands are unavailable" in output
    assert "Traceback" not in result.stderr
    assert offline.read_bytes() == original


def test_step_pairing_usage_and_snapshot_command(offline, capsys):
    events = read_events(offline)
    view = TraceView(events)
    assert len(view.steps) == 3
    assert [step.candidate for step in view.steps] == [True, True, False]
    assert [step.failed for step in view.steps] == [False, False, True]
    details = view.detail(0)
    assert details.count('"input_tokens": 12') == 1
    assert details.count('"output_tokens": 4') == 1
    assert "SDK duration: unavailable" not in details
    assert "omitted or not recorded" in details
    assert "INCOMPLETE" not in view.listing()
    main(["debug", str(offline), "--list"])
    assert "PINCER DEBUGGER" in capsys.readouterr().out
    assert "trace.ended" in render(offline)


def test_interleaved_traces_and_end_usage_take_precedence(offline):
    first = read_events(offline)
    second = copy.deepcopy(first)
    for event in second:
        event["trace_id"] = "another-trace"  # deliberately reuse span IDs
    events = [event for pair in zip(first, second) for event in pair]
    for sequence, event in enumerate(events, 1):
        event["sequence"] = sequence
        if event["kind"] == "model" and event["event"] == "span.ended":
            event["details"]["usage"] = {"input_tokens": 30, "output_tokens": 10}
    view = TraceView(events)
    assert len(view.steps) == 6
    assert [step.record["trace_id"] for step in view.steps[:2]] == [first[0]["trace_id"], "another-trace"]
    assert all(view.agent(step).label == "demo" for step in view.steps)
    assert '"input_tokens": 30' in view.detail(0)
    assert '"input_tokens": 12' not in view.detail(0)


@pytest.mark.parametrize("missing", ["span.started", "span.ended"])
def test_missing_step_endpoint_is_not_a_checkpoint(offline, missing):
    events = [event for event in read_events(offline) if not (
        event["kind"] == "model" and event["event"] == missing
    )]
    view = TraceView(events)
    assert not view.steps[0].candidate
    assert "INCOMPLETE" in view.listing()
    assert "Completed boundary: no" in view.detail(0)


def test_missing_trace_end_is_visible_even_with_complete_steps(offline):
    view = TraceView(read_events(offline)[:-1])
    assert "INCOMPLETE: 1" in view.listing()
    assert view.steps[0].candidate


def test_captured_content_is_displayed_without_terminal_controls(offline):
    events = read_events(offline)
    tool = next(e for e in events if e["kind"] == "tool" and e["event"] == "span.ended")
    tool["details"]["name"] = "lookup\x1b[2J\nspoofed"
    tool["details"]["sdk_data"] = {"input": '{"topic":"pincer"}', "output": "answer\x1b[31m"}
    view = TraceView(events)
    text = view.listing() + view.detail(1)
    assert "\x1b" not in text
    assert "\\u001b[2J\\nspoofed" in text
    assert 'Output: "answer\\u001b[31m"' in text


def test_response_content_and_absent_usage(offline):
    events = read_events(offline)
    for event in events:
        if event["kind"] == "model":
            event["details"] = {"input": "question", "response": {"output": ["answer"]}}
    detail = TraceView(events).detail(0)
    assert 'Input: "question"' in detail
    assert 'Output: {"output": ["answer"]}' in detail
    assert "not recorded (not zero)" in detail


@pytest.mark.parametrize("bad", ['{"partial":', '[]', '{"schema_version":2}'])
def test_invalid_records_fail_cleanly(tmp_path, capsys, bad):
    path = tmp_path / "bad.jsonl"
    path.write_text(bad)
    with pytest.raises(SystemExit) as exc:
        main(["debug", str(path), "--list"])
    assert exc.value.code == 1
    assert "Invalid trace record at line 1" in capsys.readouterr().err


def test_duplicate_lifecycle_rejected(offline):
    events = read_events(offline)
    events.insert(3, copy.deepcopy(events[2]))
    with pytest.raises(ValueError, match="Ambiguous trace lifecycle at line 4"):
        TraceView(events)


def test_empty_trace_and_eof(tmp_path, monkeypatch, capsys):
    path = tmp_path / "empty.jsonl"
    path.write_text("")
    answers = iter(["details", "next", "select 1", "help"])

    def read_command(_):
        try:
            return next(answers)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr("builtins.input", read_command)
    inspect(TraceView(read_events(path)))
    assert "No model or tool steps recorded." in capsys.readouterr().out


def test_ctrl_c_exits_inspection(monkeypatch):
    def interrupt(_):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupt)
    inspect(TraceView([]))
