#!/usr/bin/env python3
"""Unit tests for the Codex app-server wrapper helpers."""

from __future__ import annotations

import sys
import os
import contextlib
import io
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import codex_app  # noqa: E402


class CodexAppHelperTest(unittest.TestCase):
    def test_text_from_item_reads_text_content(self) -> None:
        item = {"type": "agentMessage", "content": [{"type": "text", "text": "DONE"}]}
        self.assertEqual(codex_app.text_from_item(item), "DONE")

    def test_text_from_item_ignores_non_text_content(self) -> None:
        item = {"type": "agentMessage", "content": [{"type": "image", "url": "x"}], "text": "fallback"}
        self.assertEqual(codex_app.text_from_item(item), "fallback")

    def test_active_turn_excludes_terminal_statuses(self) -> None:
        self.assertTrue(codex_app.is_active_turn({"status": "running"}))
        self.assertFalse(codex_app.is_active_turn({"status": "completed"}))
        self.assertFalse(codex_app.is_active_turn({"status": "failed"}))
        self.assertFalse(codex_app.is_active_turn({"status": "interrupted"}))

    def test_thread_public_view_compacts_long_preview(self) -> None:
        view = codex_app.thread_public_view(
            {"id": "t1", "name": "员工", "cwd": "/repo", "preview": "x" * 500},
            preview_width=40,
        )
        self.assertEqual(view["id"], "t1")
        self.assertEqual(view["name"], "员工")
        self.assertLessEqual(len(view["preview"]), 40)

    def test_problem_kind_classifies_common_wait_failures(self) -> None:
        self.assertEqual(codex_app.problem_kind("AuthRequired OAuth login needed"), "auth_required")
        self.assertEqual(codex_app.problem_kind("429 RESOURCE_EXHAUSTED quota"), "rate_limited")
        self.assertEqual(codex_app.problem_kind("Connection refused by proxy"), "network")
        self.assertEqual(codex_app.problem_kind("idle_timeout reached by Secretary Bus"), "")
        self.assertEqual(codex_app.problem_kind("requestId abc402def wrote 1402 bytes"), "")
        self.assertEqual(codex_app.problem_kind("normal proxy settings and capacity planning"), "")

    def test_command_failure_summary_records_failed_command(self) -> None:
        item = {
            "type": "commandExecution",
            "status": "failed",
            "exitCode": 1,
            "command": "python post.py",
            "aggregatedOutput": "Traceback\nOSError: [Errno 30] Read-only file system: '/tmp/bus/messages'",
        }
        summary = codex_app.command_failure_summary(item)
        self.assertEqual(summary["kind"], "command_failed")
        self.assertEqual(summary["exit_code"], 1)
        self.assertEqual(summary["problem_kind"], "filesystem")
        self.assertIn("Read-only file system", summary["output_tail"])

    def test_command_failure_summary_ignores_successful_command(self) -> None:
        item = {"type": "commandExecution", "status": "completed", "exitCode": 0, "command": "true"}
        self.assertEqual(codex_app.command_failure_summary(item), {})

    def test_stream_keeps_completed_turn_and_records_failed_command_as_warning(self) -> None:
        # Regression (finding 7): a failed shell command inside a turn used to
        # override diagnosis to command_failed, so --fail-on-error failed a
        # successfully completed turn.
        class FakeApp(codex_app.AppServer):
            def __init__(self) -> None:
                self.stderr_lines = []
                self.events = iter(
                    [
                        {
                            "method": "item/completed",
                            "params": {
                                "threadId": "t1",
                                "turnId": "turn1",
                                "item": {
                                    "type": "commandExecution",
                                    "status": "failed",
                                    "exitCode": 1,
                                    "command": "python post.py",
                                    "aggregatedOutput": "OSError: [Errno 30] Read-only file system",
                                },
                            },
                        },
                        {"method": "item/agentMessage/delta", "params": {"threadId": "t1", "turnId": "turn1", "delta": "DONE"}},
                        {"method": "turn/completed", "params": {"threadId": "t1", "turnId": "turn1", "turn": {"status": "completed"}}},
                    ]
                )

            def _read_line(self, timeout: float) -> dict[str, object] | None:
                return next(self.events, None)

            def _read_turn_status(self, thread_id: str, turn_id: str) -> dict[str, object]:
                return {}

        with tempfile.TemporaryDirectory() as tmp:
            outcome = FakeApp().stream_until_complete("t1", "turn1", Path(tmp), timeout=5, idle_timeout=0)
            self.assertEqual(outcome["status"], "completed")
            self.assertEqual(outcome["diagnosis"], "completed")
            self.assertEqual(outcome["warnings"][0]["kind"], "command_failed")
            status = codex_app.read_json(Path(tmp) / "status.json", {})
            self.assertEqual(status["status"], "completed")
            self.assertEqual(status["diagnosis"], "completed")
            self.assertEqual(status["warnings"][0]["kind"], "command_failed")
            self.assertNotIn("errors", status)

    def test_stream_confirms_turn_completion_when_thread_becomes_idle(self) -> None:
        class FakeApp(codex_app.AppServer):
            def __init__(self) -> None:
                self.stderr_lines = []
                self.events = iter(
                    [
                        {"method": "item/agentMessage/delta", "params": {"threadId": "t1", "turnId": "turn1", "delta": "OK"}},
                        {"method": "thread/status/changed", "params": {"threadId": "t1", "status": {"type": "idle"}}},
                    ]
                )
                self.status_reads = 0

            def _read_line(self, timeout: float) -> dict[str, object] | None:
                return next(self.events, None)

            def _read_turn_status(self, thread_id: str, turn_id: str) -> dict[str, object]:
                self.status_reads += 1
                return {"status": "completed", "completed": True, "error": None}

        with tempfile.TemporaryDirectory() as tmp:
            app = FakeApp()
            outcome = app.stream_until_complete("t1", "turn1", Path(tmp), timeout=5, idle_timeout=0)
            self.assertEqual(outcome["status"], "completed")
            self.assertEqual(outcome["diagnosis"], "completed")
            self.assertEqual(outcome["reply"], "OK")
            self.assertEqual(app.status_reads, 1)

    def test_stream_does_not_complete_on_idle_if_turn_is_still_active(self) -> None:
        class FakeApp(codex_app.AppServer):
            def __init__(self) -> None:
                self.stderr_lines = []
                self.events = iter(
                    [
                        {"method": "item/agentMessage/delta", "params": {"threadId": "t1", "turnId": "turn1", "delta": "partial"}},
                        {"method": "thread/status/changed", "params": {"threadId": "t1", "status": {"type": "idle"}}},
                    ]
                )
                self.status_reads = 0
                self.requests: list[tuple[str, dict[str, object]]] = []

            def _read_line(self, timeout: float) -> dict[str, object] | None:
                return next(self.events, None)

            def _read_turn_status(self, thread_id: str, turn_id: str) -> dict[str, object]:
                self.status_reads += 1
                return {"status": "inProgress", "completed": False, "error": None}

            def request(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
                self.requests.append((method, params or {}))
                return {}

        with tempfile.TemporaryDirectory() as tmp:
            app = FakeApp()
            outcome = app.stream_until_complete("t1", "turn1", Path(tmp), timeout=0.2, idle_timeout=0)
            self.assertEqual(outcome["status"], "timed_out")
            self.assertEqual(outcome["reply"], "partial")
            self.assertGreaterEqual(app.status_reads, 1)
            self.assertIn(("turn/interrupt", {"threadId": "t1", "turnId": "turn1"}), app.requests)

    def test_latest_run_for_thread_falls_back_to_latest_matching_run(self) -> None:
        index = {
            "threads": {},
            "runs": {
                "r1": {"thread_id": "t1", "updated_at": "2026-01-01T00:00:00+0000"},
                "r2": {"thread_id": "t1", "updated_at": "2026-01-01T00:01:00+0000"},
            },
        }
        run_id, run = codex_app.latest_run_for_thread(index, "t1")
        self.assertEqual(run_id, "r2")
        self.assertEqual(run["thread_id"], "t1")

    def test_resolve_turn_id_falls_back_to_running_local_run(self) -> None:
        class FakeApp:
            def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
                return {"data": []}

        with tempfile.TemporaryDirectory() as tmp:
            old_index = codex_app.RUN_INDEX
            old_runs = codex_app.RUNS
            try:
                codex_app.RUNS = Path(tmp) / "runs"
                codex_app.RUN_INDEX = Path(tmp) / "index.json"
                run_dir = codex_app.RUNS / "r1"
                run_dir.mkdir(parents=True)
                (run_dir / "status.json").write_text(
                    '{"thread_id":"t1","turn_id":"turn-local","status":"running","diagnosis":"running"}\n',
                    encoding="utf-8",
                )
                codex_app.save_index(
                    {
                        "runs": {
                            "r1": {
                                "thread_id": "t1",
                                "turn_id": "turn-index",
                                "run_dir": str(run_dir),
                                "status": "running",
                                "updated_at": "2026-01-01",
                            }
                        },
                        "threads": {"t1": {"last_run_id": "r1"}},
                    }
                )
                turn_id, turn = codex_app.resolve_turn_id(FakeApp(), "t1")
                self.assertEqual(turn_id, "turn-local")
                self.assertIsNone(turn)
            finally:
                codex_app.RUN_INDEX = old_index
                codex_app.RUNS = old_runs

    def test_app_config_args_support_no_mcp_and_custom_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_home = os.environ.get("CODEX_HOME")
            try:
                Path(tmp, "config.toml").write_text(
                    "[mcp_servers.alpha]\ncommand = \"a\"\n\n[mcp_servers.beta]\nurl = \"https://example.invalid\"\n",
                    encoding="utf-8",
                )
                os.environ["CODEX_HOME"] = tmp
                args = Namespace(no_mcp=True, codex_config=["model=\"x\""])
                self.assertEqual(
                    codex_app.app_config_args(args),
                    ["model=\"x\"", "mcp_servers.alpha.enabled=false", "mcp_servers.beta.enabled=false"],
                )
            finally:
                if old_home is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = old_home

    def test_app_config_args_rejects_non_key_value_override(self) -> None:
        with self.assertRaises(SystemExit):
            codex_app.app_config_args(Namespace(no_mcp=False, codex_config=["not-a-kv"]))

    def test_git_diff_snapshot_includes_untracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.local"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
            (repo / "new.txt").write_text("new\n", encoding="utf-8")
            snapshot = codex_app.git_diff_snapshot(repo, Path(tmp) / "out")
            diff = Path(snapshot["git-diff-current.patch"]).read_text(encoding="utf-8")
            self.assertIn("+new", diff)

    def test_git_diff_snapshot_includes_committed_changes_since_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.local"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
            baseline = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            (repo / "README.md").write_text("base\ncommitted\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "change"], cwd=repo, check=True)
            snapshot = codex_app.git_diff_snapshot(repo, Path(tmp) / "out", baseline_head=baseline)
            diff = Path(snapshot["git-diff-current.patch"]).read_text(encoding="utf-8")
            self.assertIn("+committed", diff)

    def test_remove_tree_rejects_paths_outside_runs_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_runs = codex_app.RUNS
            try:
                codex_app.RUNS = Path(tmp) / "runs"
                outside = Path(tmp) / "outside"
                outside.mkdir()
                (outside / "file.txt").write_text("keep\n", encoding="utf-8")
                with self.assertRaises(SystemExit):
                    codex_app.remove_tree(outside)
                self.assertTrue(outside.exists())
            finally:
                codex_app.RUNS = old_runs

    def test_prune_runs_removes_matching_thread_index_only_with_yes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_index = codex_app.RUN_INDEX
            old_runs = codex_app.RUNS
            try:
                codex_app.RUNS = Path(tmp) / "runs"
                codex_app.RUN_INDEX = Path(tmp) / "index.json"
                codex_app.save_index(
                    {
                        "runs": {},
                        "threads": {
                            "t-test": {"display_name": "秘书测试-旧记录"},
                            "t-real": {"display_name": "真实项目"},
                        },
                    }
                )
                codex_app.cmd_prune_runs(Namespace(status="", name_prefix="秘书测试", yes=False))
                self.assertIn("t-test", codex_app.load_index()["threads"])
                codex_app.cmd_prune_runs(Namespace(status="", name_prefix="秘书测试", yes=True))
                threads = codex_app.load_index()["threads"]
                self.assertNotIn("t-test", threads)
                self.assertIn("t-real", threads)
            finally:
                codex_app.RUN_INDEX = old_index
                codex_app.RUNS = old_runs

    def test_doctor_local_reports_idle_diagnosis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_index = codex_app.RUN_INDEX
            old_runs = codex_app.RUNS
            try:
                codex_app.RUNS = Path(tmp) / "runs"
                codex_app.RUN_INDEX = Path(tmp) / "index.json"
                run_dir = codex_app.RUNS / "r1"
                run_dir.mkdir(parents=True)
                (run_dir / "status.json").write_text(
                    '{"status":"running","diagnosis":"idle_no_events","idle_seconds":130,"last_event_method":"turn/start"}\n',
                    encoding="utf-8",
                )
                codex_app.save_index(
                    {
                        "runs": {"r1": {"thread_id": "t1", "run_dir": str(run_dir), "updated_at": "2026-01-01"}},
                        "threads": {"t1": {"last_run_id": "r1"}},
                    }
                )
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    codex_app.cmd_doctor(
                        Namespace(
                            thread_id="t1",
                            run_id="",
                            run_dir="",
                            local=True,
                            json=True,
                            timeout=1,
                            turns=20,
                            no_mcp=False,
                            codex_config=[],
                        )
                    )
                self.assertIn("idle_no_events", buf.getvalue())
                self.assertIn("inspect_then_interrupt_or_continue", buf.getvalue())
            finally:
                codex_app.RUN_INDEX = old_index
                codex_app.RUNS = old_runs

    def test_doctor_local_accepts_run_dir_without_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            (run_dir / "status.json").write_text(
                '{"thread_id":"t-start","turn_id":"turn-start","status":"starting_app_server","diagnosis":"starting_app_server"}\n',
                encoding="utf-8",
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                codex_app.cmd_doctor(
                    Namespace(
                        thread_id="",
                        run_id="",
                        run_dir=str(run_dir),
                        local=True,
                        json=True,
                        timeout=1,
                        turns=20,
                        no_mcp=False,
                        codex_config=[],
                    )
                )
            payload = buf.getvalue()
            self.assertIn("starting_app_server", payload)
            self.assertIn("t-start", payload)
            self.assertIn("turn-start", payload)
            self.assertIn(str(run_dir), payload)

    def test_doctor_local_reports_app_server_died(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            (run_dir / "status.json").write_text(
                '{"thread_id":"t-dead","turn_id":"turn-dead","status":"app_server_died","diagnosis":"app_server_died"}\n',
                encoding="utf-8",
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                codex_app.cmd_doctor(
                    Namespace(
                        thread_id="",
                        run_id="",
                        run_dir=str(run_dir),
                        local=True,
                        json=True,
                        timeout=1,
                        turns=20,
                        no_mcp=False,
                        codex_config=[],
                    )
                )
            payload = buf.getvalue()
            self.assertIn("app_server_died", payload)
            self.assertIn("check_app_server_startup_or_retry_with_no_mcp", payload)

    def test_doctor_local_reports_command_failed_recommendation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            (run_dir / "status.json").write_text(
                '{"thread_id":"t-fail","turn_id":"turn-fail","status":"completed","diagnosis":"command_failed","errors":[{"kind":"command_failed","exit_code":7,"output_tail":"FAIL"}]}\n',
                encoding="utf-8",
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                codex_app.cmd_doctor(
                    Namespace(
                        thread_id="",
                        run_id="",
                        run_dir=str(run_dir),
                        local=True,
                        json=True,
                        timeout=1,
                        turns=20,
                        no_mcp=False,
                        codex_config=[],
                    )
                )
            payload = buf.getvalue()
            self.assertIn("command_failed", payload)
            self.assertIn("inspect_failed_command", payload)

    def test_doctor_local_reports_detached_uncontrolled_recommendation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            (run_dir / "status.json").write_text(
                '{"thread_id":"t-detached","turn_id":"turn-detached","status":"detached","diagnosis":"detached_uncontrolled"}\n',
                encoding="utf-8",
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                codex_app.cmd_doctor(
                    Namespace(
                        thread_id="",
                        run_id="",
                        run_dir=str(run_dir),
                        local=True,
                        json=True,
                        timeout=1,
                        turns=20,
                        no_mcp=False,
                        codex_config=[],
                    )
                )
            payload = buf.getvalue()
            self.assertIn("detached_uncontrolled", payload)
            self.assertIn("restart_with_wait", payload)
            self.assertNotIn("persistent_controller", payload)


if __name__ == "__main__":
    unittest.main()
