import copy
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from time import monotonic

import pytest

from pincer.cli import main, render
from pincer.debugger import TraceView, inspect
from pincer.trace import TraceRecordReader, iter_events, read_events


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


def test_split_span_end_updates_view_once(offline, tmp_path):
    start, end = [event for event in read_events(offline) if event["kind"] == "model"]
    path = tmp_path / "growing.jsonl"
    path.write_text(json.dumps(start) + "\n", encoding="utf-8")
    end_line = json.dumps(end) + "\n"
    split_at = len(end_line) // 2
    reader = TraceRecordReader()
    view = TraceView()

    with path.open(encoding="utf-8") as source, path.open("a", encoding="utf-8") as sink:
        for event in reader.feed(source.read()):
            view.add_event(event)
        step = view.steps[0]
        assert not step.complete

        sink.write(end_line[:split_at])
        sink.flush()
        assert list(reader.feed(source.read())) == []
        assert "INCOMPLETE: missing end" in view.listing()

        sink.write(end_line[split_at:])
        sink.flush()
        for event in reader.feed(source.read()):
            view.add_event(event)
        assert list(reader.feed(source.read())) == []

    assert view.steps == [step]
    assert view.steps[0] is step
    assert step.candidate
    assert view.listing() == TraceView(iter_events(path)).listing()


def test_saved_records_validate_lazily_and_report_line(offline, tmp_path):
    start = read_events(offline)[0]
    start.pop("write_errors", None)
    line = json.dumps(start)
    path = tmp_path / "saved.jsonl"
    path.write_text(line, encoding="utf-8")
    assert list(iter_events(path)) == [start]

    path.write_text(line + "\n" + line, encoding="utf-8")
    events = iter_events(path)
    assert next(events) == start
    with pytest.raises(ValueError, match="Invalid trace record at line 2"):
        next(events)


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


@pytest.fixture
def live_debugger():
    processes = []

    def start(path, *options):
        process = subprocess.Popen(
            [sys.executable, "-m", "pincer.cli", "debug", str(path), "--follow", *options],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        processes.append(process)
        output = Queue()

        def read_output():
            for line in process.stdout:
                output.put(line)
            output.put(None)

        Thread(target=read_output, daemon=True).start()

        def wait_for(message):
            deadline = monotonic() + 10
            seen = []
            try:
                while (remaining := deadline - monotonic()) > 0:
                    line = output.get(timeout=remaining)
                    if line is None:
                        break
                    seen.append(line)
                    if message in line:
                        return "".join(seen)
            except Empty:
                pass
            pytest.fail(f"Expected {message!r} in debugger output:\n{''.join(seen)}")

        def command(line):
            process.stdin.write(line + "\n")
            process.stdin.flush()

        return process, wait_for, command

    yield start
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()


def test_follow_updates_open_step_once_and_keeps_selection(offline, tmp_path, live_debugger):
    events = read_events(offline)
    lookup_start = next(
        i for i, event in enumerate(events)
        if event["event"] == "span.started" and event["details"].get("name") == "lookup"
    )
    history = events[:lookup_start + 1]
    lookup_end, other_tool_start = events[lookup_start + 1:lookup_start + 3]
    lookup_end["sequence"], other_tool_start["sequence"] = (
        other_tool_start["sequence"], lookup_end["sequence"]
    )
    lookup_end["details"]["output"] = "café"

    def encode(event):
        return (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")

    path = tmp_path / "growing.jsonl"
    path.write_bytes(b"".join(map(encode, history)))
    process, wait_for, command = live_debugger(path)
    assert '"lookup" | Open' in wait_for("Waiting for events")
    command("select 2")
    wait_for("Recorded result: Open")

    end_line = encode(lookup_end)
    split = end_line.index("é".encode("utf-8")) + 1
    with path.open("ab", buffering=0) as writer:
        writer.write(encode(other_tool_start) + end_line[:split])
        wait_for('Step 03 "deliberate_failure" | Open')
        command("details")
        assert 'Selected: Step 02 - "lookup"' in wait_for("Recorded result: Open")

        writer.write(end_line[split:-1])
        command("list")
        assert '"lookup" | Open' in wait_for("Commands:")

        writer.write(end_line[-1:])
        wait_for('Step 02 "lookup" | Ended; no reported error')
        command("details")
        details = wait_for(r'Output: "caf\u00e9"')
        assert 'Selected: Step 02 - "lookup"' in details
        assert 'Step 02 "lookup" | Ended' not in details

        writer.write(encode(events[-1]))
        wait_for("Trace ended:")
        command("select 3")
        wait_for("Recorded result: INCOMPLETE: missing end")

        next_trace = {**events[0], "trace_id": "next-trace", "sequence": events[-1]["sequence"] + 1}
        next_tool = {
            **other_tool_start, "trace_id": "next-trace", "sequence": next_trace["sequence"] + 1
        }
        writer.write(encode(next_trace) + encode(next_tool))
        wait_for('Step 04 "deliberate_failure" | Open')

    command("quit")
    assert process.wait(timeout=5) == 0


def test_follow_reports_appended_invalid_record_while_idle(tmp_path, live_debugger):
    path = tmp_path / "growing.jsonl"
    path.touch()
    process, wait_for, _ = live_debugger(path)
    wait_for("Waiting for events")
    path.write_bytes(b"{}\n")
    assert "Traceback" not in wait_for("Invalid trace record at line 1")
    assert process.wait(timeout=5) == 1


def test_follow_displays_committed_checkpoint_changes(tmp_path, live_debugger):
    path = tmp_path / "growing.jsonl"
    checkpoint = tmp_path / "checkpoint.sqlite3"
    path.touch()
    checkpoint.touch()
    process, wait_for, command = live_debugger(path, "--checkpoint", str(checkpoint))
    wait_for("Waiting for events")
    with closing(sqlite3.connect(checkpoint)) as db:
        with db:
            db.execute("BEGIN")
            db.execute("CREATE TABLE breakpoint (id, name, call_id, arguments, status)")
            db.execute(
                "INSERT INTO breakpoint VALUES (1, 'lookup', 'call_demo', '{}', 'pending')"
            )
        wait_for("Paused before dispatch")
        with db:
            db.execute("UPDATE breakpoint SET status = 'completed' WHERE id = 1")
        wait_for("Checkpoint resumed")
    assert path.read_bytes() == b""
    command("quit")
    assert process.wait(timeout=5) == 0
