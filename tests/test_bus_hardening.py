#!/usr/bin/env python3
"""Audit regressions: delivery safety, long tasks, Codex long turns, targets hygiene."""

from __future__ import annotations

import json
import sys
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cli_bridge  # noqa: E402
import provider_state  # noqa: E402

TARGET = cli_bridge.Target(name="worker", pane="session:1.0", expected_command="node", pane_id="%9")
INFO = {"pane": "session:1.0", "pane_id": "%9", "pane_pid": 90, "pane_start_time": "1",
        "foreground_pid": 91, "foreground_start_time": "2", "command": "node", "cwd": "/work", "title": "codex"}


RULE60 = "─" * 60
INPUT_BOX = "\n".join([RULE60, "❯", RULE60, "  ⏵⏵ bypass permissions on"])


def _permission(selected: int) -> str:
    rows = ["Yes", "Yes, and don't ask again for git commands in /repo", "No"]
    lines = [f"{'   ❯' if i == selected else '    '} {i + 1}. {text}" for i, text in enumerate(rows)]
    return "\n".join([RULE60, " Bash command", "", "   git push", "", " Do you want to proceed?", *lines, "", " Esc to cancel"])


PERMISSION_SELECTED_0 = _permission(0)
PERMISSION_SELECTED_1 = _permission(1)


def snap(state: str, output: str = "") -> dict[str, object]:
    return {"provider": "codex", "state": {"value": state}, "last_assistant": "", "last_output": output or state}


class DeliverySafetyTest(unittest.TestCase):
    def test_followup_never_types_into_an_open_prompt(self) -> None:
        # steer/continue used to paste + Enter here, confirming the highlighted option.
        with mock.patch.object(cli_bridge, "target_info", return_value=INFO), \
             mock.patch.object(cli_bridge, "provider_snapshot", return_value=snap("needs_input")), \
             mock.patch.object(cli_bridge.tmux_delivery, "paste_and_submit") as deliver:
            with self.assertRaisesRegex(SystemExit, "refusing to type into it"):
                cli_bridge.send_to_target(TARGET, "please continue", True, True, False, delivery_mode="followup")
        deliver.assert_not_called()

    def test_error_words_echoed_from_the_task_are_not_a_connectivity_failure(self) -> None:
        task = "debug why the worker logs connection refused on port 8080"
        with mock.patch.object(cli_bridge, "target_info", return_value=INFO), \
             mock.patch.object(cli_bridge, "provider_snapshot",
                               side_effect=[snap("idle"), snap("idle", "› " + task), snap("busy", task)]), \
             mock.patch.object(cli_bridge.tmux_delivery, "paste_and_submit"), \
             mock.patch.object(cli_bridge.time, "sleep"):
            result = cli_bridge.send_to_target(TARGET, task, True, True, False)
        self.assertEqual(result["delivery"], "accepted")

    def test_new_connection_error_after_submit_still_fails(self) -> None:
        with mock.patch.object(cli_bridge, "target_info", return_value=INFO), \
             mock.patch.object(cli_bridge, "provider_snapshot",
                               side_effect=[snap("idle"), snap("idle", "stream error: tls handshake eof")]), \
             mock.patch.object(cli_bridge.tmux_delivery, "paste_and_submit"), \
             mock.patch.object(cli_bridge.time, "sleep"):
            with self.assertRaisesRegex(SystemExit, "connectivity error"):
                cli_bridge.send_to_target(TARGET, "fix the parser", True, True, False)

    def test_paste_placeholder_in_the_input_box_counts_as_still_in_composer(self) -> None:
        rule = "─" * 40
        screen = "\n".join(["● earlier reply", rule, "❯ [Pasted text #1 +42 lines]", rule, "  ⏵⏵ bypass permissions on",
                            "  ● main"] + [f"  ◯ fast-worker  step {i}" for i in range(12)])
        with mock.patch.object(cli_bridge, "tmux",
                               return_value=mock.Mock(returncode=0, stdout=screen)):
            self.assertTrue(cli_bridge.composer_contains_prompt("%9", "a long task " * 40))


class AutoApproveBeforeSendTest(unittest.TestCase):
    """Owner's standing authorization: permission prompts are answered, work questions are not."""

    def fake_tmux(self, screens: list[str], sent: list[str]):
        def tmux(*args, check=True):
            if args[0] == "capture-pane":
                return mock.Mock(returncode=0, stdout=screens[0])
            if args[0] == "send-keys":
                sent.append(args[-1])
                if args[-1] == "Down":
                    screens[0] = PERMISSION_SELECTED_1
                elif args[-1] == "C-m":
                    screens[0] = "● running\n" + INPUT_BOX
            return mock.Mock(returncode=0, stdout="")
        return tmux

    def test_permission_prompt_is_answered_then_followup_proceeds(self) -> None:
        screens, sent = [PERMISSION_SELECTED_0], []
        with mock.patch.object(cli_bridge, "target_info", return_value=INFO), \
             mock.patch.object(cli_bridge, "provider_snapshot", side_effect=[snap("needs_input"), snap("busy")]), \
             mock.patch.object(cli_bridge, "auto_approve_enabled", return_value=True), \
             mock.patch.object(cli_bridge, "tmux", side_effect=self.fake_tmux(screens, sent)), \
             mock.patch.object(cli_bridge.time, "sleep"), \
             mock.patch.object(cli_bridge.tmux_delivery, "paste_and_submit") as deliver:
            result = cli_bridge.send_to_target(TARGET, "also check the tests", True, True, False, delivery_mode="followup")
        self.assertEqual(sent, ["Down", "C-m"])
        deliver.assert_called_once()
        self.assertEqual(result["delivery"], "queued-or-steered")

    def test_work_question_is_left_for_the_user(self) -> None:
        question = "\n".join(["─" * 60, " Which database should we use?", "", "   ❯ 1. PostgreSQL", "     2. SQLite",
                              "", " Enter to select"])
        sent: list[str] = []
        with mock.patch.object(cli_bridge, "target_info", return_value=INFO), \
             mock.patch.object(cli_bridge, "provider_snapshot", return_value=snap("needs_input")), \
             mock.patch.object(cli_bridge, "auto_approve_enabled", return_value=True), \
             mock.patch.object(cli_bridge, "tmux", side_effect=self.fake_tmux([question], sent)), \
             mock.patch.object(cli_bridge.tmux_delivery, "paste_and_submit") as deliver:
            with self.assertRaisesRegex(SystemExit, "refusing to type into it"):
                cli_bridge.send_to_target(TARGET, "go on", True, True, False, delivery_mode="followup")
        self.assertEqual(sent, [])
        deliver.assert_not_called()


class LongTaskTest(unittest.TestCase):
    def test_short_single_line_is_sent_as_is(self) -> None:
        self.assertEqual(cli_bridge.inline_or_spill("fix the typo"), "fix the typo")

    def test_long_or_multiline_task_is_spilled_to_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            text = "第一步：读代码\n第二步：改\n" + "x" * 5000
            pointer = cli_bridge.inline_or_spill(text, spill_dir=Path(tmp), label="job 1")
            self.assertNotIn("\n", pointer)
            cli_bridge.require_safe_text(pointer, allow_newline=False)
            files = list(Path(tmp).iterdir())
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_text(encoding="utf-8").rstrip("\n"), text)
            self.assertIn(str(files[0]), pointer)
            on_disk = hashlib.sha256(files[0].read_bytes()).hexdigest()[:16]
            self.assertIn(on_disk, pointer)            # receiver's `sha256sum` matches the pointer
            self.assertIn(on_disk, files[0].name)

    def test_text_file_source_no_longer_refuses_long_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "task.md"
            source.write_text("line\n" * 2000, encoding="utf-8")
            with mock.patch.object(cli_bridge, "TASK_SPILL_DIR", Path(tmp) / "spill"):
                injected = cli_bridge.read_text_source(None, str(source))
            self.assertIn("task-", injected)
            cli_bridge.require_safe_text(injected, allow_newline=False)


class CodexLongTurnTest(unittest.TestCase):
    def test_turn_start_far_behind_the_tail_window_is_still_busy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-x.jsonl"
            rows = [{"type": "session_meta", "payload": {"id": "t", "source": "cli"}},
                    {"type": "event_msg", "payload": {"type": "task_started"}}]
            big = {"type": "response_item", "payload": {"type": "function_call_output", "output": "y" * 50_000}}
            with path.open("w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row) + "\n")
                for _ in range(10):  # 500KB of tool output after the turn started
                    fh.write(json.dumps(big) + "\n")
            self.assertEqual(provider_state.rollout_snapshot(path, 200)["state"], "busy")

    def test_working_line_outranks_the_input_box_below_it(self) -> None:
        screen = "• Working (5m 02s • esc to interrupt)\n\n› Ask Codex to do anything\n\n  gpt-5.5 high · 80% left\n"
        self.assertEqual(provider_state.live_tail_state(screen)[0], "busy")

    def test_nested_claude_inside_codex_is_still_codex(self) -> None:
        cmdlines = {1: "bash ai-session-shell", 2: "node /usr/bin/codex resume x", 3: "codex", 4: "claude -p ok"}
        with mock.patch.object(provider_state, "process_cmdline", side_effect=lambda pids: cmdlines[pids[0]]):
            provider = provider_state.detect_provider(
                cli_bridge.Target(name="w", pane="s:1.0", expected_command="bash"), {"command": "bash"}, [1, 2, 3, 4])
        self.assertEqual(provider, "codex")


class TargetsHygieneTest(unittest.TestCase):
    def test_prune_removes_only_targets_whose_pane_is_gone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            targets_file = Path(tmp) / "cli-targets.json"
            with mock.patch.object(cli_bridge, "TARGETS_FILE", targets_file), \
                 mock.patch.object(cli_bridge, "TARGETS_LOCK_FILE", Path(tmp) / "lock"):
                cli_bridge.save_targets({
                    "gone": cli_bridge.Target(name="gone", pane="s:1.0", expected_command="claude", pane_id="%1"),
                    "live": cli_bridge.Target(name="live", pane="s:2.0", expected_command="claude", pane_id="%2"),
                    "restarted": cli_bridge.Target(name="restarted", pane="s:3.0", expected_command="claude", pane_id="%3"),
                })
                statuses = {"gone": "missing", "live": "ok", "restarted": "process-changed"}
                with mock.patch.object(cli_bridge, "target_status", side_effect=lambda t: statuses[t.name]):
                    cli_bridge.cmd_targets(mock.Mock(prune=True, yes=False))
                    self.assertEqual(set(cli_bridge.load_targets()), {"gone", "live", "restarted"})
                    cli_bridge.cmd_targets(mock.Mock(prune=True, yes=True))
                self.assertEqual(set(cli_bridge.load_targets()), {"live", "restarted"})

    def test_empty_display_message_for_a_vanished_pane_is_missing(self) -> None:
        # tmux exits 0 with empty fields for a pane id that no longer exists.
        with mock.patch.object(cli_bridge, "tmux", return_value=mock.Mock(returncode=0, stdout="\t\t\t\t\t\n", stderr="")):
            status = cli_bridge.target_status(cli_bridge.Target(name="w", pane="%28", expected_command="claude", pane_id="%28"))
        self.assertEqual(status, "missing")

    def test_identity_errors_say_how_to_recover(self) -> None:
        with mock.patch.object(cli_bridge, "pane_info", side_effect=SystemExit("target pane not found: %1")):
            with self.assertRaises(SystemExit) as ctx:
                cli_bridge.target_info(cli_bridge.Target(name="w", pane="s:1.0", expected_command="claude", pane_id="%1"))
        self.assertIn("secretary-bus register --name w --pane %1", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
