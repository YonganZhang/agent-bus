#!/usr/bin/env python3
"""Integration tests for the direct tmux CLI bridge.

These tests use a temporary tmux session and never send input to real
Claude/Codex panes. There is no message-board relay, so only the direct register/send path is tested here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "scripts" / "cli_bridge.py"
SESSION = f"agent-bus-cli-bridge-test-{os.getpid()}"


def run_cmd(args: list[str], *, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, env=env, check=check)


@unittest.skipIf(shutil.which("tmux") is None, "tmux is required")
class CliBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.bus = Path(self.tmp.name) / "bus"
        self.env = os.environ.copy()
        self.env["AGENT_BUS_DIR"] = str(self.bus)
        self.direct_file = Path(self.tmp.name) / "direct.txt"
        self.dry_file = Path(self.tmp.name) / "dry.txt"
        self.bad_file = Path(self.tmp.name) / "bad.txt"
        self.newline_file = Path(self.tmp.name) / "newline.txt"
        run_cmd(["tmux", "kill-session", "-t", SESSION], check=False)
        run_cmd(["tmux", "new-session", "-d", "-s", SESSION, "-n", "bridge", "-c", self.tmp.name, "bash --noprofile --norc"])
        time.sleep(0.2)
        self.bridge("register", "--name", "test-bash", "--pane", f"{SESSION}:0.0", "--expected-command", "bash", "--shell")

    def tearDown(self) -> None:
        run_cmd(["tmux", "kill-session", "-t", SESSION], check=False)
        self.tmp.cleanup()

    def bridge(self, *args: str, check: bool = True, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = self.env.copy()
        if extra_env:
            env.update(extra_env)
        return run_cmd(["python3", str(BRIDGE), *args], env=env, check=check)

    def test_direct_send_requires_yes(self) -> None:
        self.bridge("send", "--target", "test-bash", "--text", f"echo BAD > {self.dry_file}", "--enter")
        self.assertFalse(self.dry_file.exists())

        self.bridge("send", "--target", "test-bash", "--text", f"echo DIRECT_OK > {self.direct_file}", "--enter", "--yes")
        time.sleep(0.2)
        self.assertEqual(self.direct_file.read_text().strip(), "DIRECT_OK")
        sent = self.bridge("send", "--target", "test-bash", "--text", "echo SECRET_VALUE", "--yes")
        self.assertIn("<redacted on send", sent.stdout)
        self.assertNotIn("SECRET_VALUE", sent.stdout)

    def test_enter_into_a_pane_whose_ai_exited_is_refused(self) -> None:
        # ai-session-shell keeps its pid and execs bash when the AI exits, so the
        # frozen identity still matches; the text must not become shell commands.
        self.bridge("register", "--name", "ai-gone", "--pane", f"{SESSION}:0.0", "--expected-command", "bash")
        result = self.bridge("send", "--target", "ai-gone", "--text", f"echo BAD > {self.bad_file}", "--enter", "--yes",
                             check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no Claude/Codex process runs", result.stderr)
        time.sleep(0.2)
        self.assertFalse(self.bad_file.exists())
        self.bridge("send", "--target", "ai-gone", "--text", f"echo SHELL_OK > {self.direct_file}", "--enter", "--yes",
                    "--allow-shell")
        time.sleep(0.2)
        self.assertEqual(self.direct_file.read_text().strip(), "SHELL_OK")

    def test_send_accepts_utf8_text_file_without_shell_quoting(self) -> None:
        prompt_file = Path(self.tmp.name) / "prompt.txt"
        output_file = Path(self.tmp.name) / "from-file.txt"
        prompt_file.write_text(f"printf FILE_OK > {output_file}", encoding="utf-8")
        self.bridge(
            "send",
            "--target",
            "test-bash",
            "--text-file",
            str(prompt_file),
            "--enter",
            "--yes",
        )
        time.sleep(0.2)
        self.assertEqual(output_file.read_text(encoding="utf-8"), "FILE_OK")

    def test_command_mismatch_and_newline_are_blocked(self) -> None:
        self.bridge("register", "--name", "bad-target", "--pane", f"{SESSION}:0.0", "--expected-command", "claude")
        mismatch = self.bridge(
            "send",
            "--target",
            "bad-target",
            "--text",
            f"echo BAD > {self.bad_file}",
            "--enter",
            "--yes",
            check=False,
        )
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertIn("target command mismatch", mismatch.stderr + mismatch.stdout)
        self.assertFalse(self.bad_file.exists())

        newline = self.bridge(
            "send",
            "--target",
            "test-bash",
            "--text",
            f"echo A\necho B > {self.newline_file}",
            "--enter",
            "--yes",
            check=False,
        )
        self.assertNotEqual(newline.returncode, 0)
        self.assertIn("newline input is blocked", newline.stderr + newline.stdout)
        self.assertFalse(self.newline_file.exists())

        carriage_return = self.bridge(
            "send",
            "--target",
            "test-bash",
            "--text",
            "echo SAFE\recho BAD",
            "--yes",
            check=False,
        )
        self.assertNotEqual(carriage_return.returncode, 0)
        self.assertIn("control character", carriage_return.stderr + carriage_return.stdout)

        escape = self.bridge(
            "send",
            "--target",
            "test-bash",
            "--text",
            "echo SAFE\x1b[200~",
            "--yes",
            check=False,
        )
        self.assertNotEqual(escape.returncode, 0)
        self.assertIn("control character", escape.stderr + escape.stdout)

    def test_concurrent_register_does_not_lose_entries(self) -> None:
        # Regression: register()'s
        # load-modify-save cycle had no file lock, so two racing `register`
        # calls could each load the file, add their own entry in memory, then
        # whichever wrote last would silently discard the other's
        # registration. 20 concurrent registrations against the same pane, all
        # different names, must all survive.
        names = [f"concurrent-target-{i}" for i in range(20)]
        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(
                pool.map(
                    lambda name: self.bridge(
                        "register", "--name", name, "--pane", f"{SESSION}:0.0", "--expected-command", "bash", "--shell", check=False
                    ),
                    names,
                )
            )
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)

        targets_file = self.bus / "cli-targets.json"
        stored = json.loads(targets_file.read_text(encoding="utf-8"))
        for name in names:
            self.assertIn(name, stored, f"{name} was silently lost to a concurrent-write race")

    def test_register_rejects_tmux_fallback_to_a_different_pane(self) -> None:
        """An invalid pane index must never silently bind to pane 0.

        tmux ``display-message -t session:window.99`` may resolve the window
        and report its active pane instead of failing. A registered target is
        an identity contract, so accepting that fallback could send a leader's
        instruction to the wrong AI.
        """
        result = self.bridge(
            "register",
            "--name",
            "must-not-fallback",
            "--pane",
            f"{SESSION}:0.99",
            "--expected-command",
            "bash",
            "--shell",
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("resolved to a different pane", result.stderr + result.stdout)
        stored = json.loads((self.bus / "cli-targets.json").read_text(encoding="utf-8"))
        self.assertNotIn("must-not-fallback", stored)

    def test_registered_foreground_process_restart_is_detected(self) -> None:
        run_cmd(["tmux", "send-keys", "-t", f"{SESSION}:0.0", "sleep 60", "Enter"])
        time.sleep(0.2)
        self.bridge(
            "register",
            "--name",
            "sleep-worker",
            "--pane",
            f"{SESSION}:0.0",
            "--expected-command",
            "sleep",
        )
        before = json.loads((self.bus / "cli-targets.json").read_text(encoding="utf-8"))["sleep-worker"]
        self.assertGreater(before["foreground_pid"], 0)

        run_cmd(["tmux", "send-keys", "-t", f"{SESSION}:0.0", "C-c"])
        run_cmd(["tmux", "send-keys", "-t", f"{SESSION}:0.0", "sleep 60", "Enter"])
        time.sleep(0.2)
        targets = self.bridge("targets")
        self.assertIn("process-changed", targets.stdout)

    def test_send_follows_stable_pane_id_after_window_coordinate_changes(self) -> None:
        moved_file = Path(self.tmp.name) / "moved.txt"
        run_cmd(["tmux", "move-window", "-s", f"{SESSION}:0", "-t", f"{SESSION}:4"])
        self.bridge(
            "send",
            "--target",
            "test-bash",
            "--text",
            f"echo STABLE_ID > {moved_file}",
            "--enter",
            "--yes",
        )
        time.sleep(0.2)
        self.assertEqual(moved_file.read_text(encoding="utf-8").strip(), "STABLE_ID")


if __name__ == "__main__":
    unittest.main()
