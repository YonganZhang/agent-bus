#!/usr/bin/env python3
"""Tests for the event-driven leader wake-up daemon."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import cli_bridge  # noqa: E402
import leader_daemon  # noqa: E402


class LeaderDaemonTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.daemons = Path(self.tmp.name) / "leader-daemons"
        self.daemons_patch = mock.patch.object(leader_daemon, "DAEMONS", self.daemons)
        self.daemons_patch.start()
        self.load_state_patch = mock.patch.object(
            leader_daemon.leader,
            "load_state",
            return_value={
                "id": "demo",
                "leader": "leader-window",
                "status": "running",
                "phase": "monitor",
                "event_cursor": 0,
                "workers": {},
            },
        )
        self.load_state_patch.start()

    def tearDown(self) -> None:
        self.load_state_patch.stop()
        self.daemons_patch.stop()
        self.tmp.cleanup()

    @staticmethod
    def changed_payload(event_id: int, *, excerpt: str = "") -> dict[str, object]:
        changes: list[dict[str, object]] = []
        events: list[dict[str, object]] = []
        if excerpt:
            changes.append({"worker": "worker", "kind": "pane_changed", "excerpt": excerpt})
        else:
            events.append(
                {"id": event_id, "kind": "job_status_changed", "job_id": "child", "status": "running"}
            )
        return {
            "leader_id": "demo",
            "changed": True,
            "reason": "event_or_state_change",
            "status": "running",
            "phase": "monitor",
            "cursor": event_id,
            "events": events,
            "changes": changes,
            "issues": [],
            "workers": {"worker": "running"},
        }

    @staticmethod
    def unchanged_payload(cursor: int) -> dict[str, object]:
        return {
            "leader_id": "demo",
            "changed": False,
            "reason": "no_change",
            "status": "running",
            "phase": "monitor",
            "cursor": cursor,
            "events": [],
            "changes": [],
            "issues": [],
            "workers": {"worker": "running"},
        }

    def state_writes(self, run) -> list[dict]:
        """Run ``run()`` and return every JSON payload written to this daemon's state file."""
        writes: list[dict] = []
        real = leader_daemon.leader.write_json_atomic

        def spy(path, payload):
            if Path(path).name == "state.json":
                writes.append(json.loads(json.dumps(payload)))
            return real(path, payload)

        with mock.patch.object(leader_daemon.leader, "write_json_atomic", side_effect=spy):
            run()
        return writes

    def test_idle_cycles_do_not_rewrite_the_state_file(self) -> None:
        # Before: every idle cycle wrote state.json at least three times
        # (in-flight marker, marker clear, end of cycle).
        unchanged = self.unchanged_payload(1)
        with mock.patch.object(leader_daemon.leader, "tick_once", return_value=unchanged):
            writes = self.state_writes(
                lambda: leader_daemon.run_loop("demo", interval=0, dry_run=True, max_cycles=6)
            )
        self.assertLessEqual(len(writes), 3, [w.get("status") for w in writes])
        self.assertEqual(writes[-1]["status"], "stopped")

    def test_inflight_marker_is_written_before_a_reportable_tick_commits(self) -> None:
        changed = self.changed_payload(9)

        def tick(_leader_id, *, probe_interval, force_probe, before_commit=None):
            before_commit(7)
            return changed

        with mock.patch.object(leader_daemon.leader, "tick_once", side_effect=tick), mock.patch.object(
            leader_daemon, "provider_gate", return_value={"value": "busy", "source": "test"}
        ):
            writes = self.state_writes(
                lambda: leader_daemon.run_loop("demo", interval=0, dry_run=True, max_cycles=1)
            )
        markers = [index for index, w in enumerate(writes) if (w.get("tick_inflight") or {}).get("cursor") == 7]
        queued = [index for index, w in enumerate(writes) if w.get("pending_notification")]
        self.assertTrue(markers and queued)
        self.assertLess(markers[0], queued[0])
        self.assertFalse(writes[-1].get("tick_inflight"))

    def test_loop_deduplicates_notifications_and_exits_on_terminal_leader(self) -> None:
        first = {
            "leader_id": "demo",
            "changed": True,
            "reason": "event_or_state_change",
            "status": "running",
            "phase": "monitor",
            "cursor": 8,
            "events": [{"id": 8, "kind": "job_status_changed", "job_id": "child", "status": "running"}],
            "changes": [],
            "issues": [],
            "workers": {"worker": "running"},
        }
        terminal = {
            "leader_id": "demo",
            "changed": True,
            "reason": "leader_terminal",
            "status": "completed",
            "phase": "closed",
            "events": [],
            "workers": {},
        }
        with mock.patch.object(
            leader_daemon.leader, "tick_once", side_effect=[first, dict(first), terminal]
        ), mock.patch.object(
            leader_daemon, "provider_gate", return_value={"value": "idle", "source": "test"}
        ), mock.patch.object(
            leader_daemon, "notify_leader", return_value={"sent": False, "dry_run": True}
        ) as notify:
            result = leader_daemon.run_loop(
                "demo", interval=0, probe_interval=60, dry_run=True, max_cycles=5
            )

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["reason"], "leader_terminal")
        # A terminal leader is a stop condition, not a final wake-up event.
        self.assertEqual(notify.call_count, 1)
        state = json.loads((self.daemons / "demo" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["notifications"], 0)
        self.assertEqual(state["status"], "stopped")

    def test_dry_run_keeps_pending_outbox_until_live_delivery_ack(self) -> None:
        changed = self.changed_payload(1)
        unchanged = self.unchanged_payload(1)
        with mock.patch.object(leader_daemon.leader, "tick_once", return_value=changed), mock.patch.object(
            leader_daemon, "provider_gate", create=True, return_value={"value": "idle"}
        ), mock.patch.object(
            leader_daemon, "notify_leader", return_value={"sent": False, "dry_run": True}
        ) as preview:
            first = leader_daemon.run_loop("demo", interval=0, dry_run=True, max_cycles=1)

        self.assertEqual(first["reason"], "max_cycles")
        self.assertEqual(first["notifications"], 0)
        self.assertTrue(first["pending_notification"])
        self.assertEqual(preview.call_count, 1)

        with mock.patch.object(leader_daemon.leader, "tick_once", return_value=unchanged), mock.patch.object(
            leader_daemon, "provider_gate", create=True, return_value={"value": "idle"}
        ), mock.patch.object(
            leader_daemon, "notify_leader", return_value={"sent": True, "dry_run": False}
        ) as send:
            second = leader_daemon.run_loop("demo", interval=0, dry_run=False, max_cycles=1)

        self.assertEqual(send.call_count, 1)
        delivery_id = second["pending_notification"]["delivery_id"]
        self.assertEqual(second["notifications"], 1)
        self.assertTrue(second["pending_notification"]["awaiting_receipt"])
        self.assertTrue(second["last_notification"]["transport_acked"])
        self.assertFalse(second["last_notification"]["processed_ack"])

        leader_daemon.record_receipt("demo", delivery_id)
        with mock.patch.object(leader_daemon.leader, "tick_once", return_value=unchanged):
            acked = leader_daemon.run_loop("demo", interval=0, dry_run=False, max_cycles=1)
        self.assertFalse(acked.get("pending_notification"))
        self.assertTrue(acked["last_notification"]["processed_ack"])

    def test_notification_failure_keeps_outbox_and_retries_without_losing_event(self) -> None:
        changed = self.changed_payload(2)
        unchanged = self.unchanged_payload(2)
        with mock.patch.object(leader_daemon.leader, "tick_once", return_value=changed), mock.patch.object(
            leader_daemon, "provider_gate", create=True, return_value={"value": "idle"}
        ), mock.patch.object(leader_daemon, "notify_leader", side_effect=RuntimeError("tmux unavailable")):
            first = leader_daemon.run_loop(
                "demo", interval=0, dry_run=False, max_cycles=1, delivery_retry_seconds=0
            )

        self.assertEqual(first["status"], "stopped")
        self.assertEqual(first["reason"], "max_cycles")
        self.assertIn("tmux unavailable", first["pending_notification"]["last_error"])
        self.assertEqual(first["notifications"], 0)

        with mock.patch.object(leader_daemon.leader, "tick_once", return_value=unchanged), mock.patch.object(
            leader_daemon, "provider_gate", create=True, return_value={"value": "idle"}
        ), mock.patch.object(
            leader_daemon, "notify_leader", return_value={"sent": True, "dry_run": False}
        ) as send:
            second = leader_daemon.run_loop(
                "demo", interval=0, dry_run=False, max_cycles=1, delivery_retry_seconds=0
            )

        self.assertEqual(send.call_count, 1)
        self.assertTrue(second.get("pending_notification"))
        self.assertEqual(second["notifications"], 1)

    def test_busy_leader_batches_pending_changes_and_sends_once_when_idle(self) -> None:
        first = self.changed_payload(3)
        second = self.changed_payload(4)
        idle = self.unchanged_payload(4)
        gates = [
            {"value": "busy", "source": "claude_agents"},
            {"value": "needs_input", "source": "tmux_live_tail"},
            {"value": "idle", "source": "claude_agents"},
        ]
        with mock.patch.object(
            leader_daemon.leader, "tick_once", side_effect=[first, second, idle]
        ), mock.patch.object(
            leader_daemon, "provider_gate", create=True, side_effect=gates
        ), mock.patch.object(
            leader_daemon,
            "notify_leader",
            return_value={"sent": True, "dry_run": False},
        ) as notify:
            result = leader_daemon.run_loop(
                "demo", interval=0, dry_run=False, max_cycles=3, provider_poll_seconds=0
            )

        self.assertEqual(notify.call_count, 1)
        delivered = notify.call_args.args[1]
        self.assertEqual([event["id"] for event in delivered["events"]], [3, 4])
        self.assertTrue(delivered["delivery_id"].startswith("leaderd-demo-"))
        self.assertTrue(result.get("pending_notification"))
        self.assertEqual(result["notifications"], 1)
        self.assertEqual(result["last_provider_gate"]["value"], "idle")

    def test_unknown_provider_gate_keeps_pending_instead_of_injecting(self) -> None:
        changed = self.changed_payload(9)
        with mock.patch.object(leader_daemon.leader, "tick_once", return_value=changed), mock.patch.object(
            leader_daemon,
            "provider_gate",
            return_value={"value": "unknown", "confidence": "low", "source": "tmux_live_tail"},
        ), mock.patch.object(leader_daemon, "notify_leader") as notify:
            result = leader_daemon.run_loop("demo", interval=0, dry_run=False, max_cycles=1)

        notify.assert_not_called()
        self.assertTrue(result.get("pending_notification"))
        self.assertEqual(result["last_provider_gate"]["value"], "unknown")

    def test_low_confidence_idle_gate_is_not_safe_enough_to_inject(self) -> None:
        changed = self.changed_payload(12)
        with mock.patch.object(leader_daemon.leader, "tick_once", return_value=changed), mock.patch.object(
            leader_daemon,
            "provider_gate",
            return_value={"value": "idle", "confidence": "low", "source": "tmux_live_tail"},
        ), mock.patch.object(leader_daemon, "notify_leader") as notify:
            result = leader_daemon.run_loop("demo", interval=0, dry_run=False, max_cycles=1)

        notify.assert_not_called()
        self.assertTrue(result.get("pending_notification"))

    # ---- 负责人输入框里有没发出去的草稿: 不能把通知拼进去一起提交 ----

    RULE = "─" * 60
    CLAUDE_TOP = "──────── demo-project ─"

    def claude_screen(self, *box_rows: str) -> str:
        return "\n".join(["● 上一轮的回复", "", self.CLAUDE_TOP, *box_rows, self.RULE,
                          "  ⏵⏵ bypass permissions on (shift+tab to cycle)"]) + "\n"

    def test_composer_draft_reads_only_real_unsent_text(self) -> None:
        codex_footer = "  gpt-5.5 high · 80% left · ~/projects/demo"
        cases = {
            "claude empty": (self.claude_screen("❯ "), ""),
            "claude placeholder": (self.claude_screen('❯ Try "fix the lint errors"'), ""),
            "claude draft": (self.claude_screen("❯ 帮我看一下 worker-3 为什么"), "帮我看一下 worker-3 为什么"),
            "claude multi-line draft": (self.claude_screen("❯ first line", "  second line"), "first line\nsecond line"),
            "codex empty": ("• 完成\n\n› Ask Codex to do anything\n\n" + codex_footer + "\n", ""),
            "codex draft": ("• 完成\n\n› 先别合并,等我\n\n" + codex_footer + "\n", "先别合并,等我"),
            "codex dialog option": ("Allow command?\n\n› 1. Yes\n  2. No\n\nPress enter to confirm\n", ""),
        }
        for name, (screen, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(leader_daemon.composer_draft(screen), expected)

    def test_draft_in_leader_input_defers_the_wake_up_until_it_is_gone(self) -> None:
        changed = self.changed_payload(21)
        unchanged = self.unchanged_payload(21)
        gates = [
            {"value": "idle", "confidence": "high", "source": "claude_agents", "draft_chars": 14},
            {"value": "idle", "confidence": "high", "source": "claude_agents", "draft_chars": 0},
        ]
        with mock.patch.object(leader_daemon.leader, "tick_once", side_effect=[changed, unchanged]), \
             mock.patch.object(leader_daemon, "provider_gate", side_effect=gates), \
             mock.patch.object(leader_daemon, "notify_leader", return_value={"sent": True, "dry_run": False}) as notify:
            deferred = leader_daemon.run_loop(
                "demo", interval=0, dry_run=False, max_cycles=1, provider_poll_seconds=0
            )
            notify.assert_not_called()
            pending = deferred["pending_notification"]
            self.assertIn("unsent draft (14 chars)", pending["deferred_reason"])
            self.assertIn("next_gate_at", pending)
            self.assertNotIn("draft", json.dumps(deferred["last_provider_gate"]).replace("draft_chars", ""))

            sent = leader_daemon.run_loop(
                "demo", interval=0, dry_run=False, max_cycles=1, provider_poll_seconds=0
            )
        self.assertEqual(notify.call_count, 1)
        self.assertNotIn("deferred_reason", sent["pending_notification"])
        log = (self.daemons / "demo" / "daemon.log").read_text(encoding="utf-8")
        self.assertEqual(log.count('"notification_deferred"'), 1)

    def test_provider_gate_reports_draft_length_from_the_leader_pane(self) -> None:
        snapshot = {
            "provider": "claude",
            "state": {"value": "idle", "confidence": "high", "source": "claude_agents"},
            "session": {"exact": True},
            "runtime": {"pane_id": "%9"},
        }
        with mock.patch.object(leader_daemon.provider_state, "snapshot_target", return_value=snapshot), \
             mock.patch.object(leader_daemon.provider_state, "capture_pane",
                               return_value=self.claude_screen("❯ half a message")) as capture:
            gate = leader_daemon.provider_gate("demo")
        capture.assert_called_once_with("%9")
        self.assertEqual(gate["value"], "idle")
        self.assertEqual(gate["draft_chars"], len("half a message"))

    def test_repeated_inflight_delivery_id_is_not_queued_again_while_waiting_for_receipt(self) -> None:
        changed = self.changed_payload(10)
        with mock.patch.object(
            leader_daemon.leader, "tick_once", side_effect=[changed, dict(changed)]
        ), mock.patch.object(
            leader_daemon, "provider_gate", return_value={"value": "idle", "source": "test"}
        ), mock.patch.object(
            leader_daemon, "notify_leader", return_value={"sent": True, "dry_run": False}
        ) as notify:
            result = leader_daemon.run_loop("demo", interval=0, dry_run=False, max_cycles=2)

        self.assertEqual(notify.call_count, 1)
        self.assertTrue(result["pending_notification"]["awaiting_receipt"])
        self.assertFalse(result.get("deferred_notification"))

    def test_recover_inflight_tick_reconstructs_durable_event_before_next_tick(self) -> None:
        state = {
            "leader_id": "demo",
            "tick_inflight": {"cursor": 10, "at": "before-crash"},
            "pending_notification": {},
        }
        contract = {
            "id": "demo",
            "status": "running",
            "phase": "monitor",
            "event_cursor": 11,
            "workers": {"worker": {"job_ids": ["child"], "status": "running"}},
        }
        event = {"id": 11, "kind": "job_status_changed", "job_id": "child", "status": "completed"}
        with mock.patch.object(leader_daemon.leader, "load_state", return_value=contract), mock.patch.object(
            leader_daemon.leader.event_ledger, "read_events", return_value=([event], 11)
        ):
            recovered = leader_daemon.recover_inflight_tick("demo", state)

        self.assertTrue(recovered)
        self.assertFalse(state.get("tick_inflight"))
        pending = state["pending_notification"]
        self.assertEqual(pending["payload"]["events"][0]["id"], 11)
        self.assertTrue(pending["delivery_id"].startswith("leaderd-demo-"))

    def test_recover_inflight_tick_merges_new_event_behind_awaiting_delivery(self) -> None:
        first = self.changed_payload(10)
        state: dict[str, object] = {
            "leader_id": "demo",
            "tick_inflight": {"cursor": 10, "at": "before-crash"},
            "pending_notification": {},
            "deferred_notification": {},
        }
        pending = leader_daemon.queue_notification(state, first)
        pending["awaiting_receipt"] = True
        contract = {
            "id": "demo",
            "status": "running",
            "phase": "monitor",
            "event_cursor": 11,
            "workers": {"worker": {"job_ids": ["child"], "status": "completed"}},
        }
        event = {"id": 11, "kind": "job_status_changed", "job_id": "child", "status": "completed"}
        with mock.patch.object(leader_daemon.leader, "load_state", return_value=contract), mock.patch.object(
            leader_daemon.leader.event_ledger, "read_events", return_value=([event], 11)
        ):
            recovered = leader_daemon.recover_inflight_tick("demo", state)

        self.assertTrue(recovered)
        self.assertFalse(state.get("tick_inflight"))
        self.assertEqual(state["pending_notification"]["payload"]["events"][0]["id"], 10)
        self.assertEqual(state["deferred_notification"]["payload"]["events"][0]["id"], 11)

    def test_receipt_state_is_durable_before_receipt_file_cleanup(self) -> None:
        state: dict[str, object] = {
            "leader_id": "demo",
            "pending_notification": {},
            "deferred_notification": {},
            "last_notification": {},
        }
        pending = leader_daemon.queue_notification(state, self.changed_payload(13))
        pending["awaiting_receipt"] = True
        leader_daemon.write_state("demo", state)
        leader_daemon.record_receipt("demo", str(pending["delivery_id"]))

        with mock.patch.object(Path, "unlink", side_effect=RuntimeError("crash after state commit")):
            with self.assertRaisesRegex(RuntimeError, "crash after state commit"):
                leader_daemon.consume_receipt("demo", state)

        durable = json.loads(leader_daemon.daemon_state_path("demo").read_text(encoding="utf-8"))
        self.assertFalse(durable.get("pending_notification"))
        self.assertTrue(durable["last_notification"]["processed_ack"])

    def test_queue_merge_preserves_provider_gate_cooldown(self) -> None:
        state: dict[str, object] = {"leader_id": "demo"}
        first = leader_daemon.queue_notification(state, self.changed_payload(20))
        first["next_gate_at"] = 1234.5

        merged = leader_daemon.queue_notification(state, self.changed_payload(21))

        self.assertEqual(merged["next_gate_at"], 1234.5)
        self.assertEqual([event["id"] for event in merged["payload"]["events"]], [20, 21])

    def test_pure_spinner_redraws_do_not_wake_leader(self) -> None:
        first = self.changed_payload(5, excerpt="⠋ Working… 1.0s · esc to interrupt")
        second = self.changed_payload(6, excerpt="⠙ Working… 1.1s · esc to interrupt")
        with mock.patch.object(
            leader_daemon.leader, "tick_once", side_effect=[first, second]
        ), mock.patch.object(
            leader_daemon, "provider_gate", create=True, return_value={"value": "idle"}
        ), mock.patch.object(leader_daemon, "notify_leader") as notify:
            result = leader_daemon.run_loop("demo", interval=0, dry_run=False, max_cycles=2)

        notify.assert_not_called()
        self.assertEqual(result["filtered_redraws"], 2)
        self.assertFalse(result.get("pending_notification"))

    def test_same_semantic_pane_signal_can_wake_again_after_cooldown(self) -> None:
        payload = self.changed_payload(11, excerpt="tests still failing in module A")
        state: dict[str, object] = {}
        with mock.patch.object(leader_daemon.time, "time", side_effect=[100.0, 101.0, 200.0]):
            first = leader_daemon.notification_candidate(state, payload)
            within_cooldown = leader_daemon.notification_candidate(state, payload)
            after_cooldown = leader_daemon.notification_candidate(state, payload)

        self.assertIsNotNone(first)
        self.assertIsNone(within_cooldown)
        self.assertIsNotNone(after_cooldown)

    def test_semantic_pane_changes_respect_redraw_cooldown(self) -> None:
        first = self.changed_payload(7, excerpt="phase A: indexing complete")
        second = self.changed_payload(8, excerpt="phase B: tests running")
        with mock.patch.object(
            leader_daemon.leader, "tick_once", side_effect=[first, second]
        ), mock.patch.object(
            leader_daemon, "provider_gate", create=True, return_value={"value": "idle"}
        ), mock.patch.object(
            leader_daemon,
            "notify_leader",
            return_value={"sent": True, "dry_run": False},
        ) as notify:
            result = leader_daemon.run_loop(
                "demo", interval=0, dry_run=False, max_cycles=2, redraw_cooldown=60
            )

        self.assertEqual(notify.call_count, 1)
        self.assertTrue(result.get("pending_notification"))
        self.assertEqual(result["deferred_notification"]["payload"]["changes"][-1]["excerpt"], "phase B: tests running")

    def test_rotate_log_keeps_one_bounded_backup(self) -> None:
        log = leader_daemon.daemon_log_path("demo")
        log.parent.mkdir(parents=True)
        log.write_text("x" * 32, encoding="utf-8")

        rotated = leader_daemon.rotate_log("demo", max_bytes=16, backups=1)

        self.assertTrue(rotated)
        self.assertFalse(log.exists())
        self.assertEqual(log.with_name("daemon.log.1").read_text(encoding="utf-8"), "x" * 32)

    def test_runtime_log_append_rotates_while_daemon_is_running(self) -> None:
        for index in range(4):
            leader_daemon.append_log("demo", {"event": "cycle", "index": index}, max_bytes=48, backups=1)

        log = leader_daemon.daemon_log_path("demo")
        self.assertTrue(log.exists())
        self.assertTrue(log.with_name("daemon.log.1").exists())
        self.assertLessEqual(log.stat().st_size, 96)

    def test_notify_uses_frozen_exact_pane_and_dry_run_never_sends(self) -> None:
        target = cli_bridge.Target(
            name="leader-window",
            pane="s:0.0",
            pane_id="%9",
            pane_pid=100,
            pane_start_time="10",
            foreground_pid=101,
            foreground_start_time="11",
            expected_command="claude",
        )
        leader_state = {
            "id": "demo",
            "leader": "leader-window",
            "leader_runtime": {"pane_id": "%9", "runtime_fingerprint": "fingerprint"},
        }
        payload = {
            "leader_id": "demo",
            "changed": True,
            "reason": "event_or_state_change",
            "events": [{"id": 1, "kind": "done"}],
            "changes": [],
            "issues": [],
            "workers": {"worker": "completed"},
        }
        with mock.patch.object(leader_daemon.leader, "load_state", return_value=leader_state), mock.patch.object(
            leader_daemon.leader, "target_or_die", return_value=target
        ), mock.patch.object(
            leader_daemon.leader,
            "runtime_snapshot",
            return_value={"pane_id": "%9", "runtime_fingerprint": "fingerprint"},
        ), mock.patch.object(leader_daemon.cli_bridge, "send_to_target") as send:
            dry = leader_daemon.notify_leader("demo", payload, dry_run=True)
            live = leader_daemon.notify_leader("demo", payload, dry_run=False)

        self.assertFalse(dry["sent"])
        self.assertTrue(live["sent"])
        self.assertEqual(send.call_count, 1)
        args, kwargs = send.call_args
        self.assertEqual(args[0].pane_id, "%9")
        self.assertIn("LEADER_EVENT", args[1])
        self.assertLessEqual(len(args[1]), 3800)
        self.assertTrue(kwargs["yes"])

    def test_status_rejects_reused_pid_with_different_start_token(self) -> None:
        path = self.daemons / "demo" / "state.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"leader_id": "demo", "status": "running", "pid": 123, "pid_start_time": "old"}),
            encoding="utf-8",
        )
        with mock.patch.object(leader_daemon, "pid_alive", return_value=True), mock.patch.object(
            leader_daemon, "process_start_time", return_value="new"
        ):
            status = leader_daemon.daemon_status("demo")

        self.assertFalse(status["running"])
        self.assertEqual(status["status"], "stale")
        self.assertEqual(status["reason"], "pid_reused")

    def test_parser_exposes_start_run_status_and_stop(self) -> None:
        parser = leader_daemon.build_parser()
        for command in ("start", "run", "status", "stop"):
            parsed = parser.parse_args([command, "demo"])
            self.assertEqual(parsed.cmd, command)
        parsed = parser.parse_args(["ack", "demo", "leaderd-demo-deadbeef"])
        self.assertEqual(parsed.cmd, "ack")


@unittest.skipIf(shutil.which("tmux") is None, "tmux is required")
class LeaderDaemonLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.bus = Path(self.tmp.name) / "bus"
        self.env = os.environ.copy()
        self.env["AGENT_BUS_DIR"] = str(self.bus)
        self.session = f"agent-bus-leaderd-test-{os.getpid()}"
        subprocess.run(["tmux", "kill-session", "-t", self.session], capture_output=True, check=False)
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", self.session, "-n", "leader", "bash --noprofile --norc"],
            check=True,
        )
        subprocess.run(
            ["tmux", "new-window", "-d", "-t", self.session, "-n", "worker", "bash --noprofile --norc"],
            check=True,
        )
        time.sleep(0.15)
        for name, pane in (("leader", f"{self.session}:0.0"), ("worker", f"{self.session}:1.0")):
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "cli_bridge.py"),
                    "register",
                    "--name",
                    name,
                    "--pane",
                    pane,
                    "--expected-command",
                    "bash",
                    "--shell",
                ],
                env=self.env,
                text=True,
                capture_output=True,
                check=True,
            )
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "leader.py"),
                "create",
                "--hidden",
                "--id",
                "lifecycle",
                "--leader",
                "leader",
                "--worker",
                "worker",
                "--objective",
                "exercise daemon lifecycle",
            ],
            env=self.env,
            text=True,
            capture_output=True,
            check=True,
        )

    def tearDown(self) -> None:
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "leader_daemon.py"), "stop", "lifecycle", "--stop-timeout", "1"],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        subprocess.run(["tmux", "kill-session", "-t", self.session], capture_output=True, check=False)
        self.tmp.cleanup()

    def test_background_start_status_and_stop_use_temp_bus(self) -> None:
        started = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "leader_daemon.py"),
                "start",
                "lifecycle",
                "--dry-run",
                "--interval",
                "0.05",
                "--probe-interval",
                "60",
            ],
            env=self.env,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn("leader daemon id=lifecycle", started.stdout)
        status = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "leader_daemon.py"), "status", "lifecycle", "--json"],
            env=self.env,
            text=True,
            capture_output=True,
            check=True,
        )
        payload = json.loads(status.stdout)
        self.assertTrue(payload["running"])
        self.assertTrue(payload["pid_start_time"])
        self.assertTrue(str(payload["state_file"]).startswith(str(self.bus)))

        stopped = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "leader_daemon.py"), "stop", "lifecycle"],
            env=self.env,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn("leader daemon stopped", stopped.stdout)
        final = json.loads(
            subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "leader_daemon.py"), "status", "lifecycle", "--json"],
                env=self.env,
                text=True,
                capture_output=True,
                check=False,
            ).stdout
        )
        self.assertFalse(final["running"])
        self.assertEqual(final["reason"], "stop_requested")


if __name__ == "__main__":
    unittest.main()
