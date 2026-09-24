#!/usr/bin/env python3
"""Provider-aware delivery contract tests for Secretary Bus."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cli_bridge  # noqa: E402
import supervisor  # noqa: E402


TARGET = cli_bridge.Target(name="worker", pane="session:1.0", expected_command="node")
INFO = {
    "pane": "session:1.0",
    "pane_id": "%9",
    "pane_pid": 90,
    "pane_start_time": "1",
    "foreground_pid": 91,
    "foreground_start_time": "2",
    "command": "node",
    "cwd": "/work",
    "title": "codex",
}


def snapshot(state: str, assistant: str = "") -> dict[str, object]:
    return {
        "provider": "codex",
        "state": {"value": state},
        "last_assistant": assistant,
        "last_output": state,
    }


class CliBridgeDeliveryTest(unittest.TestCase):
    def test_idle_ai_dispatch_is_verified_after_one_bracketed_paste(self) -> None:
        with (
            mock.patch.object(cli_bridge, "target_info", return_value=INFO),
            mock.patch.object(cli_bridge, "provider_snapshot", side_effect=[snapshot("idle"), snapshot("busy")]),
            mock.patch.object(cli_bridge.tmux_delivery, "paste_and_submit") as deliver,
        ):
            result = cli_bridge.send_to_target(TARGET, "do work", True, True, False)
        deliver.assert_called_once()
        self.assertTrue(result["verified"])
        self.assertEqual(result["delivery"], "accepted")

    def test_busy_ai_rejects_new_dispatch_before_paste(self) -> None:
        with (
            mock.patch.object(cli_bridge, "target_info", return_value=INFO),
            mock.patch.object(cli_bridge, "provider_snapshot", return_value=snapshot("busy")),
            mock.patch.object(cli_bridge.tmux_delivery, "paste_and_submit") as deliver,
        ):
            with self.assertRaisesRegex(SystemExit, "refusing a new dispatch"):
                cli_bridge.send_to_target(TARGET, "new task", True, True, False)
        deliver.assert_not_called()

    def test_busy_followup_is_explicitly_reported_as_unverified_queue_or_steer(self) -> None:
        with (
            mock.patch.object(cli_bridge, "target_info", return_value=INFO),
            mock.patch.object(cli_bridge, "provider_snapshot", return_value=snapshot("busy")),
            mock.patch.object(cli_bridge.tmux_delivery, "paste_and_submit") as deliver,
        ):
            result = cli_bridge.send_to_target(
                TARGET,
                "correction",
                True,
                True,
                False,
                delivery_mode="followup",
            )
        deliver.assert_called_once()
        self.assertFalse(result["verified"])
        self.assertEqual(result["delivery"], "queued-or-steered")

    def test_one_extra_submit_requires_live_composer_evidence(self) -> None:
        tmux_result = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with (
            mock.patch.object(cli_bridge, "target_info", return_value=INFO),
            mock.patch.object(cli_bridge, "provider_snapshot", return_value=snapshot("idle")),
            mock.patch.object(cli_bridge.tmux_delivery, "paste_and_submit"),
            mock.patch.object(cli_bridge, "wait_for_provider_acceptance", side_effect=[None, snapshot("busy")]),
            mock.patch.object(cli_bridge, "composer_contains_prompt", return_value=True),
            mock.patch.object(cli_bridge, "tmux", return_value=tmux_result) as tmux,
        ):
            result = cli_bridge.send_to_target(TARGET, "task remains in composer", True, True, False)
        tmux.assert_called_once_with("send-keys", "-t", "%9", "C-m")
        self.assertEqual(result["submit_retry"], 1)

    def test_connectivity_error_after_submit_fails_loud(self) -> None:
        disconnected = snapshot("idle")
        disconnected["last_output"] = "error sending request to backend-api/codex/responses"
        with (
            mock.patch.object(cli_bridge, "target_info", return_value=INFO),
            mock.patch.object(cli_bridge, "provider_snapshot", side_effect=[snapshot("idle"), disconnected]),
            mock.patch.object(cli_bridge.tmux_delivery, "paste_and_submit"),
        ):
            with self.assertRaisesRegex(SystemExit, "provider connectivity error"):
                cli_bridge.send_to_target(TARGET, "task", True, True, False)

    def test_unconfirmed_submit_is_pending_and_does_not_blindly_press_enter(self) -> None:
        with (
            mock.patch.object(cli_bridge, "target_info", return_value=INFO),
            mock.patch.object(cli_bridge, "provider_snapshot", return_value=snapshot("idle")),
            mock.patch.object(cli_bridge.tmux_delivery, "paste_and_submit"),
            mock.patch.object(cli_bridge, "wait_for_provider_acceptance", return_value=None),
            mock.patch.object(cli_bridge, "composer_contains_prompt", return_value=False),
            mock.patch.object(cli_bridge, "tmux") as tmux,
        ):
            result = cli_bridge.send_to_target(TARGET, "task", True, True, False)
        tmux.assert_not_called()
        self.assertTrue(result["sent"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["delivery"], "submitted-pending-confirmation")
        self.assertEqual(result["state"], "pending_confirmation")

    def test_cmd_send_returns_retry_later_for_pending_confirmation(self) -> None:
        args = type("Args", (), {"target": "worker", "text": "task", "text_file": None, "enter": True,
                                   "yes": True, "allow_newline": False, "show_text": False, "queue": False})()
        with (
            mock.patch.object(cli_bridge, "load_targets", return_value={"worker": TARGET}),
            mock.patch.object(cli_bridge, "read_text_source", return_value="task"),
            mock.patch.object(
                cli_bridge,
                "send_to_target",
                return_value={"delivery": "submitted-pending-confirmation"},
            ),
        ):
            self.assertEqual(cli_bridge.cmd_send(args), 75)


class SupervisorDeliveryStatusTest(unittest.TestCase):
    def test_verified_busy_dispatch_is_running(self) -> None:
        self.assertEqual(
            supervisor.dispatch_status_from_delivery(
                {"verified": True, "delivery": "accepted", "provider": "codex", "state": "busy"}
            ),
            "running",
        )

    def test_verified_short_turn_is_completed(self) -> None:
        self.assertEqual(
            supervisor.dispatch_status_from_delivery(
                {"verified": True, "delivery": "accepted", "provider": "claude", "state": "idle"}
            ),
            "completed",
        )

    def test_terminal_transport_without_provider_ack_stays_sent(self) -> None:
        self.assertEqual(
            supervisor.dispatch_status_from_delivery(
                {"verified": False, "delivery": "terminal", "provider": "unknown"}
            ),
            "sent",
        )

    def test_unconfirmed_ai_dispatch_stays_sent_for_event_first_observation(self) -> None:
        self.assertEqual(
            supervisor.dispatch_status_from_delivery(
                {
                    "verified": False,
                    "delivery": "submitted-pending-confirmation",
                    "provider": "codex",
                    "state": "pending_confirmation",
                }
            ),
            "sent",
        )


if __name__ == "__main__":
    unittest.main()
