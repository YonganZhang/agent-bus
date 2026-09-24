#!/usr/bin/env python3
"""Small unit tests for supervisor capture helpers."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import cli_bridge  # noqa: E402
import event_ledger  # noqa: E402
import supervisor  # noqa: E402


class SuffixAfterTest(unittest.TestCase):
    def test_ignores_tmux_blank_fill_and_keeps_command_line(self) -> None:
        before = "bash-5.1$\n\n\n"
        after = "bash-5.1$ echo DONE\nDONE\nbash-5.1$\n\n\n"
        self.assertIn("echo DONE", supervisor.suffix_after(before, after))
        self.assertIn("DONE", supervisor.suffix_after(before, after))

    def test_returns_tail_when_screen_redraw_breaks_alignment(self) -> None:
        before = "old screen\nold prompt\n"
        after = "fresh screen\nnew output\nnew prompt\n"
        got = supervisor.suffix_after(before, after)
        self.assertIn("fresh screen", got)
        self.assertIn("new output", got)


TARGET = cli_bridge.Target(name="worker", pane="s:1.0", expected_command="claude", pane_id="%7")
INFO = {
    "pane": "s:1.0", "pane_id": "%7", "pane_pid": 70, "pane_start_time": "1", "foreground_pid": 71,
    "foreground_start_time": "2", "command": "claude", "cwd": "/work", "title": "claude",
}


class SupervisorLedgerTest(unittest.TestCase):
    """Collect/start behaviour against a temporary ledger with tmux mocked out."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        ledger = root / "event-ledger"
        self.patches = [
            mock.patch.object(supervisor, "JOBS", root / "secretary-jobs"),
            mock.patch.object(event_ledger, "LEDGER", ledger),
            mock.patch.object(event_ledger, "EVENTS_FILE", ledger / "events.jsonl"),
            mock.patch.object(event_ledger, "JOBS_DIR", ledger / "jobs"),
            mock.patch.object(event_ledger, "COUNTER_FILE", ledger / "next-event-id.txt"),
            mock.patch.object(event_ledger, "OFFSETS_FILE", ledger / "event-offsets.json"),
            mock.patch.object(event_ledger, "LOCK_FILE", ledger / ".lock"),
            mock.patch.object(supervisor, "target_for", return_value=TARGET),
            mock.patch.object(supervisor, "target_for_job", return_value=TARGET),
            mock.patch.object(cli_bridge, "target_info", return_value=INFO),
            mock.patch.object(supervisor, "git_context", return_value={"root": "", "branch": "", "head": ""}),
            mock.patch.object(supervisor, "baseline_repo", return_value={"is_git": False}),
            mock.patch.object(supervisor, "git_capture", return_value={}),
        ]
        for patch in self.patches:
            patch.start()
        self.screen = "> \n"
        self.capture = mock.patch.object(supervisor, "capture_pane", side_effect=self.fake_capture)
        self.capture.start()

    def tearDown(self) -> None:
        self.capture.stop()
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def fake_capture(self, _pane: str, out: Path, history: int = 5000) -> str:
        out.write_text(self.screen, encoding="utf-8")
        return self.screen

    def start(self, task: str = "", *, task_file: str | None = None, job: str = "job-1") -> mock.MagicMock:
        with mock.patch.object(
            cli_bridge, "send_to_target", return_value={"sent": True, "verified": False, "delivery": "submitted"}
        ) as send, contextlib.redirect_stdout(io.StringIO()):
            supervisor.cmd_start(
                argparse.Namespace(
                    target="worker", task=task or None, task_file=task_file, repo="", id=job, wait=0,
                    history=100, allow_newline=False, allow_pane_cwd_mismatch=False, yes=True,
                )
            )
        return send

    def collect(self, job: str = "job-1") -> dict:
        with contextlib.redirect_stdout(io.StringIO()):
            supervisor.cmd_collect(argparse.Namespace(id=job, history=100))
        return event_ledger.get_job(job) or {}

    def test_marker_quoted_by_the_echoed_prompt_does_not_complete_a_working_job(self) -> None:
        # Before: the first COMPLETION_STATUS match (inside the prompt echo)
        # turned a still-working job into `completed`.
        task = "Refactor parser; finish with COMPLETION_STATUS: COMPLETE"
        self.start(task)
        self.screen = f"> {task}\n\n* Working… (41s · esc to interrupt)\n"
        self.assertEqual(self.collect()["status"], "waiting_user")
        self.screen += "done\nCOMPLETION_STATUS: COMPLETE\n"
        self.assertEqual(self.collect()["status"], "completed")

    def test_collect_never_replaces_an_existing_terminal_status(self) -> None:
        # Before: a marker overwrote `interrupted` with `completed`.
        self.start("Do the thing")
        event_ledger.upsert_job("job-1", status="interrupted", message="Ctrl-C verified")
        self.screen = "> Do the thing\nCOMPLETION_STATUS: COMPLETE\n"
        job = self.collect()
        self.assertEqual(job["status"], "interrupted")
        self.assertIn("kept terminal status interrupted", job["message"])

    def test_invalid_task_text_is_rejected_before_any_job_exists(self) -> None:
        # Before: the ledger job was created first, then the send-time check
        # failed and left a `failed` attempt behind.
        with self.assertRaisesRegex(SystemExit, "control character"):
            self.start("bad \x1b[31m text")
        self.assertIsNone(event_ledger.get_job("job-1"))
        self.assertFalse((supervisor.JOBS / "job-1").exists())

    def test_multiline_task_is_spilled_into_the_job_directory(self) -> None:
        task = "step one\nstep two\n" + "x" * 50
        send = self.start(task)
        typed = send.call_args.args[1]
        self.assertNotIn("\n", typed)
        spilled = list((supervisor.JOBS / "job-1").glob("task-*.md"))
        self.assertEqual(len(spilled), 1)
        self.assertIn(str(spilled[0]), typed)
        self.assertEqual(spilled[0].read_text(encoding="utf-8").rstrip("\n"), task.rstrip("\n"))
        job = json.loads((supervisor.JOBS / "job-1" / "job.json").read_text(encoding="utf-8"))
        self.assertEqual(job["task"], task)
        self.assertEqual(job["delivered_text"], typed)

    def test_task_file_is_read_and_short_single_line_is_typed_as_is(self) -> None:
        path = Path(self.tmp.name) / "task.txt"
        path.write_text("echo from-file\n", encoding="utf-8")
        send = self.start(task_file=str(path))
        self.assertEqual(send.call_args.args[1], "echo from-file")

    def test_continue_spills_long_followups_into_the_job_directory(self) -> None:
        self.start("first")
        event_ledger.upsert_job("job-1", status="running")
        with mock.patch.object(cli_bridge, "send_to_target", return_value={}) as send, \
                contextlib.redirect_stdout(io.StringIO()):
            supervisor.cmd_continue(
                argparse.Namespace(id="job-1", text="line a\nline b", text_file=None, allow_newline=False, yes=True)
            )
        typed = send.call_args.args[1]
        self.assertIn(str(supervisor.JOBS / "job-1" / "continue-"), typed)
        job = json.loads((supervisor.JOBS / "job-1" / "job.json").read_text(encoding="utf-8"))
        self.assertEqual(job["events"][-1]["delivered_text"], typed)
        self.assertEqual(supervisor.delivered_prompts(job), [job["delivered_text"], typed])


if __name__ == "__main__":
    unittest.main()
