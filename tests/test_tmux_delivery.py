#!/usr/bin/env python3
"""Unit tests for the shared bracketed-paste tmux delivery primitive."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import tmux_delivery  # noqa: E402


def completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class TmuxDeliveryTest(unittest.TestCase):
    def test_paste_finishes_before_delayed_submit(self) -> None:
        calls: list[tuple[str, object]] = []

        def command(args):
            calls.append(("command", list(args)))
            return completed("0\n" if args[0] == "display-message" else "")

        def input_command(args, text):
            calls.append(("input", (list(args), text)))
            return completed()

        tmux_delivery.paste_and_submit(
            "%7",
            "长中文任务",
            True,
            submit_delay=0.18,
            run_command=command,
            run_input=input_command,
            sleep=lambda seconds: calls.append(("sleep", seconds)),
        )
        command_args = [value for kind, value in calls if kind == "command"]
        self.assertEqual(command_args[0], ["display-message", "-p", "-t", "%7", "#{pane_in_mode}"])
        self.assertEqual(command_args[1][0], "paste-buffer")
        self.assertIn("-p", command_args[1])
        self.assertEqual(command_args[-1], ["send-keys", "-t", "%7", "C-m"])
        paste_index = next(index for index, item in enumerate(calls) if item[0] == "command" and item[1][0] == "paste-buffer")
        sleep_index = next(index for index, item in enumerate(calls) if item[0] == "sleep")
        submit_index = next(index for index, item in enumerate(calls) if item == ("command", ["send-keys", "-t", "%7", "C-m"]))
        self.assertLess(paste_index, sleep_index)
        self.assertLess(sleep_index, submit_index)

    def test_copy_mode_is_cancelled_before_paste(self) -> None:
        commands: list[list[str]] = []

        def command(args):
            commands.append(list(args))
            return completed("1\n" if args[0] == "display-message" else "")

        tmux_delivery.paste_and_submit(
            "%8",
            "text",
            False,
            run_command=command,
            run_input=lambda _args, _text: completed(),
        )
        self.assertEqual(commands[1], ["send-keys", "-t", "%8", "-X", "cancel"])
        self.assertEqual(commands[2][0], "paste-buffer")


if __name__ == "__main__":
    unittest.main()
