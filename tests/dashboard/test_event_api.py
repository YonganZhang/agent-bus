#!/usr/bin/env python3
"""Regression tests for card-dashboard event ledger integration."""

from __future__ import annotations

import hashlib
import http.client
import json
import threading
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import server
import event_ledger


def api_get(path: str) -> tuple[int, dict[str, object]]:
    """Exercise the real GET handler on an ephemeral unauthenticated test port."""
    old_authorized = server.Handler.authorized
    old_log_message = server.Handler.log_message
    server.Handler.authorized = lambda _handler: True
    server.Handler.log_message = lambda _handler, _fmt, *_args: None
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=2)
        conn.request("GET", path)
        response = conn.getresponse()
        status = response.status
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()
        return status, payload
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        server.Handler.authorized = old_authorized
        server.Handler.log_message = old_log_message


class CardEventIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_ledger = event_ledger.LEDGER
        self.old_events = event_ledger.EVENTS_FILE
        self.old_jobs = event_ledger.JOBS_DIR
        self.old_counter = event_ledger.COUNTER_FILE
        self.old_lock = event_ledger.LOCK_FILE
        self.old_offsets = event_ledger.OFFSETS_FILE
        event_ledger.LEDGER = Path(self.tmp.name) / "ledger"
        event_ledger.EVENTS_FILE = event_ledger.LEDGER / "events.jsonl"
        event_ledger.JOBS_DIR = event_ledger.LEDGER / "jobs"
        event_ledger.COUNTER_FILE = event_ledger.LEDGER / "next-event-id.txt"
        event_ledger.LOCK_FILE = event_ledger.LEDGER / ".lock"
        event_ledger.OFFSETS_FILE = event_ledger.LEDGER / "event-offsets.json"
        server.JOB_CACHE.clear()
        server.JOB_EVENT_RECORDS_CACHE.clear()

    def tearDown(self) -> None:
        event_ledger.LEDGER = self.old_ledger
        event_ledger.EVENTS_FILE = self.old_events
        event_ledger.JOBS_DIR = self.old_jobs
        event_ledger.COUNTER_FILE = self.old_counter
        event_ledger.LOCK_FILE = self.old_lock
        event_ledger.OFFSETS_FILE = self.old_offsets
        server.JOB_CACHE.clear()
        server.JOB_EVENT_RECORDS_CACHE.clear()
        server.PROVIDER_RUNTIME_CACHE.clear()
        self.tmp.cleanup()

    def test_tmux_capture_controls_are_bounded_and_support_an_isolated_socket(self) -> None:
        self.assertGreaterEqual(server._PANE_CAPTURE_WORKERS, 1)
        self.assertLessEqual(server._PANE_CAPTURE_WORKERS, 8)
        with mock.patch.object(server, "TMUX_SOCKET", "/tmp/card-test.sock"), \
             mock.patch.object(server, "TMUX_LABEL", ""), \
             mock.patch.object(server.subprocess, "run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            server.run_tmux(["list-panes"])
        self.assertEqual(run.call_args.args[0][:3], ["tmux", "-S", "/tmp/card-test.sock"])

    def test_capture_lock_has_a_deadline(self) -> None:
        with tempfile.TemporaryFile() as lock, \
             mock.patch.object(server, "PANE_CAPTURE_LOCK_TIMEOUT", 0), \
             mock.patch.object(server.fcntl, "flock", side_effect=BlockingIOError):
            self.assertFalse(server._acquire_capture_lock(lock))

    def test_pane_by_id_keeps_stamped_identity_and_marks_exited_agent(self) -> None:
        row = "\t".join([
            "%41", "secretary_web", "7", "0", "named", "bash", "/work/project",
            "title", "1", "4242", "session-41", "/work/project/session-41.jsonl",
        ]) + "\n"
        with mock.patch.object(
            server, "run_tmux",
            return_value=SimpleNamespace(returncode=0, stdout=row, stderr=""),
        ), mock.patch.object(server, "_foreground_child_command", return_value=""), \
             mock.patch.object(server, "_shell_wrapper_provider", return_value="claude"), \
             mock.patch.object(server, "_process_start_token", return_value="4242:9"):
            pane = server.pane_by_id("%41")
        self.assertIsNotNone(pane)
        self.assertEqual(pane.ai_session_id, "session-41")
        self.assertEqual(pane.ai_transcript, "/work/project/session-41.jsonl")
        self.assertFalse(pane.ai_alive)
        self.assertEqual(pane.kind, "Claude")

    def test_pane_by_id_preserves_empty_trailing_stamp_fields(self) -> None:
        row = "\t".join([
            "%42", "secretary_web", "8", "0", "fresh", "codex", "/work/project",
            "title", "0", "5252", "", "",
        ]) + "\n"
        with mock.patch.object(
            server, "run_tmux",
            return_value=SimpleNamespace(returncode=0, stdout=row, stderr=""),
        ), mock.patch.object(server, "_process_start_token", return_value="5252:1"):
            pane = server.pane_by_id("%42")
        self.assertIsNotNone(pane)
        self.assertEqual(pane.ai_session_id, "")
        self.assertEqual(pane.ai_transcript, "")
        self.assertTrue(pane.ai_alive)

    def test_capture_api_does_not_turn_a_dead_agent_prompt_green(self) -> None:
        pane = server.Pane(
            pane_id="%41", target="secretary_web:7.0", session="secretary_web",
            window_index=7, pane_index=0, window_name="named", command="codex",
            cwd="/work/project", title="", active=True, kind="Codex", project="project",
            preview="", status="", pane_pid="4242", ai_alive=False,
        )
        screen = "last answer\n› Ask Codex to do anything"
        with mock.patch.object(server, "pane_by_id", return_value=pane), \
             mock.patch.object(server, "capture", return_value=screen), \
             mock.patch.object(server, "blocks_for_pane_with_meta", return_value=([], {})), \
             mock.patch.object(server, "sync_pane_job_status", return_value={}):
            status, payload = api_get("/cards/api/capture?pane=%2541&compact=1")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "needs attention")

    def test_job_event_cache_applies_appends_without_rebuilding_full_ledger(self) -> None:
        event_ledger.upsert_job(
            "incremental-job",
            source="card-dashboard",
            pane="%1",
            target="test:1.0",
            status="sent",
        )
        _signature, records = server._job_records_from_events_cached()
        self.assertEqual(records["incremental-job"]["status"], "sent")

        event_ledger.upsert_job(
            "incremental-job",
            source="card-dashboard",
            pane="%1",
            target="test:1.0",
            status="running",
        )
        with mock.patch.object(
            server,
            "_rebuild_job_records_from_events",
            side_effect=AssertionError("append should not rescan the full ledger"),
        ):
            _signature, records = server._job_records_from_events_cached()

        self.assertEqual(records["incremental-job"]["status"], "running")

    def test_pane_status_updates_latest_active_job(self) -> None:
        event_ledger.upsert_job("cards-test", source="card-dashboard", pane="%1", target="test:1.0", status="sent")
        summary = server.sync_pane_job_status("%1", "test:1.0", "running")
        self.assertEqual(summary["status"], "running")
        self.assertEqual(event_ledger.get_job("cards-test")["status"], "running")

    def test_job_summary_exposes_safe_fields_only(self) -> None:
        job = {
            "id": "job-1",
            "status": "completed",
            "task": "full task should not be exposed",
            "task_preview": "short task",
            "response_started_at": "2026-08-05T22:00:00+08:00",
            "secret": "do-not-return",
        }
        summary = server.job_summary(job)
        self.assertEqual(summary["task_preview"], "short task")
        self.assertEqual(summary["response_started_at"], "2026-08-05T22:00:00+08:00")
        self.assertNotIn("task", summary)
        self.assertNotIn("secret", summary)

    def test_provider_runtime_observation_requires_exact_same_pane(self) -> None:
        old_snapshot = server.provider_state.snapshot_target
        server.PROVIDER_RUNTIME_CACHE.clear()
        job = {
            "id": "leader-child",
            "source": "secretary-bus-supervisor",
            "status": "running",
            "target": "worker-a",
            "pane": "%1",
        }
        server.provider_state.snapshot_target = lambda *_args, **_kwargs: {
            "runtime": {"pane_id": "%1"},
            "state": {"value": "busy", "confidence": "high", "source": "codex_rollout_fd"},
        }
        try:
            observed = server.provider_runtime_observation(job, "idle")
            self.assertEqual(observed["status"], "running")
            self.assertEqual(observed["source"], "codex_rollout_fd")
            server.PROVIDER_RUNTIME_CACHE.clear()
            server.provider_state.snapshot_target = lambda *_args, **_kwargs: {
                "runtime": {"pane_id": "%999"},
                "state": {"value": "busy", "confidence": "high", "source": "codex_rollout_fd"},
            }
            self.assertEqual(server.provider_runtime_observation(job, "idle"), {})
        finally:
            server.provider_state.snapshot_target = old_snapshot
            server.PROVIDER_RUNTIME_CACHE.clear()

    def test_events_api_exposes_global_head_without_changing_last_id(self) -> None:
        for number in range(3):
            event_ledger.append_event("test", source="test", message=f"event {number}")
        status, payload = api_get("/cards/api/events?after=0&limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["events"]), 1)
        self.assertEqual(payload["last_id"], 1)
        self.assertEqual(payload["head_id"], 3)

    def test_events_api_head_does_not_expose_reserved_unwritten_id(self) -> None:
        committed = event_ledger.append_event("committed", source="test")
        event_ledger.COUNTER_FILE.write_text(str(committed["id"] + 1), encoding="utf-8")
        status, payload = api_get("/cards/api/events?after=9999&limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(payload["events"], [])
        self.assertEqual(payload["last_id"], committed["id"])
        self.assertEqual(payload["head_id"], committed["id"])

    def _pane(self) -> server.Pane:
        return server.Pane(
            pane_id="%41",
            target="secretary_web:7.0",
            session="secretary_web",
            window_index=7,
            pane_index=0,
            window_name="test",
            command="codex",
            cwd="/work/test",
            title="test",
            active=True,
            kind="Codex",
            project="test",
            preview="",
            status="running",
            pane_pid="4242",
            pane_start_time="4242:99",
        )

    def test_close_pane_rejects_stale_identity_without_killing(self) -> None:
        pane = self._pane()
        old_pane_by_id = server.pane_by_id
        old_run_tmux = server.run_tmux
        calls: list[list[str]] = []
        server.pane_by_id = lambda _pane_id: pane
        server.run_tmux = lambda args: calls.append(args)
        try:
            with self.assertRaises(server.PaneIdentityConflict):
                server.close_pane(pane.pane_id, pane.pane_pid, "4242:old")
        finally:
            server.pane_by_id = old_pane_by_id
            server.run_tmux = old_run_tmux
        self.assertEqual(calls, [])

    def test_close_pane_kills_exact_pane_and_cancels_active_jobs(self) -> None:
        pane = self._pane()
        event_ledger.upsert_job(
            "cards-close-test",
            source="card-dashboard",
            pane=pane.pane_id,
            target=pane.target,
            status="running",
        )
        old_pane_by_id = server.pane_by_id
        old_run_tmux = server.run_tmux
        lookups = 0
        calls: list[list[str]] = []

        def fake_pane_by_id(_pane_id):
            nonlocal lookups
            lookups += 1
            return pane if lookups == 1 else None

        def fake_run_tmux(args):
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        server.pane_by_id = fake_pane_by_id
        server.run_tmux = fake_run_tmux
        try:
            result = server.close_pane(pane.pane_id, pane.pane_pid, pane.pane_start_time)
        finally:
            server.pane_by_id = old_pane_by_id
            server.run_tmux = old_run_tmux
        self.assertEqual(calls, [["kill-pane", "-t", pane.pane_id]])
        self.assertEqual(result["cancelled_jobs"], 1)
        self.assertEqual(event_ledger.get_job("cards-close-test")["status"], "cancelled")
        events, _last_id = event_ledger.read_events(after=0, limit=100)
        self.assertTrue(any(event["kind"] == "pane_closed" for event in events))

    def test_send_job_id_retries_return_one_durable_receipt_without_duplicate_tmux_input(self) -> None:
        pane = self._pane()
        old_pane_by_id = server.pane_by_id
        old_send_text = server.send_text_to_pane
        calls: list[tuple[str, str, bool]] = []
        server.pane_by_id = lambda _pane_id: pane

        def fake_send_text(pane_id, text, enter):
            calls.append((pane_id, text, enter))
            return pane

        server.send_text_to_pane = fake_send_text
        try:
            first = server.send_message_with_receipt(
                pane.pane_id,
                "retry-safe message",
                True,
                job_id="cards-web-dedupe-test",
                expected_pid=pane.pane_pid,
                expected_start_time=pane.pane_start_time,
            )
            retry = server.send_message_with_receipt(
                pane.pane_id,
                "retry-safe message",
                True,
                job_id="cards-web-dedupe-test",
                expected_pid=pane.pane_pid,
                expected_start_time=pane.pane_start_time,
            )
            with self.assertRaises(server.SendRequestConflict):
                server.send_message_with_receipt(
                    pane.pane_id,
                    "different message",
                    True,
                    job_id="cards-web-dedupe-test",
                    expected_pid=pane.pane_pid,
                    expected_start_time=pane.pane_start_time,
                )
        finally:
            server.pane_by_id = old_pane_by_id
            server.send_text_to_pane = old_send_text
        self.assertEqual(calls, [(pane.pane_id, "retry-safe message", True)])
        self.assertFalse(first["deduplicated"])
        self.assertTrue(retry["deduplicated"])
        self.assertEqual(retry["delivery"], "terminal")
        events, _last_id = event_ledger.read_events(after=0, limit=100)
        self.assertEqual(sum(event["kind"] == "tmux_send" for event in events), 1)

    def test_send_failure_is_audited_and_not_reported_as_delivered(self) -> None:
        pane = self._pane()
        old_pane_by_id = server.pane_by_id
        old_send_text = server.send_text_to_pane
        server.pane_by_id = lambda _pane_id: pane

        def fail_send(_pane_id, _text, _enter):
            raise RuntimeError("tmux failed")

        server.send_text_to_pane = fail_send
        try:
            with self.assertRaisesRegex(RuntimeError, "tmux failed"):
                server.send_message_with_receipt(
                    pane.pane_id,
                    "will fail",
                    True,
                    job_id="cards-web-failure-test",
                    expected_pid=pane.pane_pid,
                    expected_start_time=pane.pane_start_time,
                )
        finally:
            server.pane_by_id = old_pane_by_id
            server.send_text_to_pane = old_send_text
        self.assertEqual(event_ledger.get_job("cards-web-failure-test")["status"], "failed")
        events, _last_id = event_ledger.read_events(after=0, limit=100)
        self.assertTrue(any(event["kind"] == "tmux_send_failed" for event in events))

    def test_real_send_supersedes_every_older_active_cards_job(self) -> None:
        pane = self._pane()
        for job_id, status, created_at in (
            ("cards-old-running", "running", "2026-08-05T16:53:41+08:00"),
            ("cards-old-waiting", "waiting_user", "2026-08-05T19:38:03+08:00"),
        ):
            event_ledger.upsert_job(
                job_id,
                source="card-dashboard",
                status=status,
                pane=pane.pane_id,
                target=pane.target,
                task_preview="旧消息",
                created_at=created_at,
            )
        old_pane_by_id = server.pane_by_id
        old_send_text = server.send_text_to_pane
        server.pane_by_id = lambda _pane_id: pane
        server.send_text_to_pane = lambda _pane_id, _text, _enter: pane
        try:
            result = server.send_message_with_receipt(
                pane.pane_id,
                "新消息",
                True,
                job_id="cards-new-message",
                expected_pid=pane.pane_pid,
                expected_start_time=pane.pane_start_time,
            )
        finally:
            server.pane_by_id = old_pane_by_id
            server.send_text_to_pane = old_send_text

        self.assertEqual(result["job"]["status"], "sent")
        self.assertEqual(event_ledger.get_job("cards-old-running")["status"], "completed")
        self.assertEqual(event_ledger.get_job("cards-old-waiting")["status"], "completed")
        active = event_ledger.list_jobs(pane=pane.pane_id, include_terminal=False, limit=20)
        self.assertEqual([job["id"] for job in active], ["cards-new-message"])


class PrefsAtomicSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_prefs = server.PREFS
        server.PREFS = Path(self.tmp.name) / "prefs.json"

    def tearDown(self) -> None:
        server.PREFS = self.old_prefs
        self.tmp.cleanup()

    def test_generic_pref_merge_preserves_favorites(self) -> None:
        server.merge_prefs({"paneFavorites": {"pane-a": True}, "categories": ["全部"]})
        prefs = server.merge_prefs({
            "paneAliases": {"pane-a": "A"},
            "paneFavorites": {"stale-old-tab-pane": True},
        })
        self.assertEqual(prefs["paneFavorites"], {"pane-a": True})
        self.assertEqual(prefs["categories"], list(server.FIXED_CATEGORY_ORDER))

    def test_stale_whole_alias_map_cannot_undo_a_rename(self) -> None:
        server.merge_prefs({"paneAliases": {"pane-a": "旧名"}})
        with server.PREFS_LOCK:
            prefs = server._read_prefs_unlocked()
            prefs["paneAliases"] = {"pane-a": "新名"}  # renamed via /api/prefs/pane
            server._write_prefs_unlocked(prefs)
        prefs = server.merge_prefs({"paneAliases": {"pane-a": "旧名"}, "paneOrder": ["pane-a"]})
        self.assertEqual(prefs["paneAliases"], {"pane-a": "新名"})
        self.assertEqual(prefs["paneOrder"], ["pane-a"])

    def test_concurrent_devices_update_different_favorites_without_lost_write(self) -> None:
        barrier = threading.Barrier(2)

        def favorite(key):
            barrier.wait(timeout=2)
            server.update_favorite_pref(key, True)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(favorite, key) for key in ("phone-pane", "desktop-pane")]
            for future in futures:
                future.result(timeout=2)
        self.assertEqual(
            server.read_prefs()["paneFavorites"],
            {"phone-pane": True, "desktop-pane": True},
        )

    def test_category_mutation_updates_one_pane_without_replacing_others(self) -> None:
        server.merge_prefs({
            "categories": ["最近", "全部", "开发", "论文"],
            "paneCategories": {"pane-a": "开发"},
        })
        first = server.update_category_pref("pane-b", "论文")
        second = server.update_category_pref("pane-a", "")
        self.assertEqual(first["paneCategories"], {"pane-a": "开发", "pane-b": "论文"})
        self.assertEqual(second["paneCategories"], {"pane-b": "论文"})
        self.assertIn("开发", second["categories"], "fixed categories must survive becoming empty")

    def test_last_unassignment_auto_deletes_custom_category(self) -> None:
        server.merge_prefs({"categories": [*server.FIXED_CATEGORY_ORDER, "任务·临时"]})
        server.update_category_pref("pane-a", "任务·临时", create_category=True)
        prefs = server.update_category_pref("pane-a", "")
        self.assertNotIn("任务·临时", prefs["categories"])
        self.assertNotIn("pane-a", prefs["paneCategories"])

    def test_manual_delete_rejects_fixed_and_unassigns_custom_category(self) -> None:
        server.update_category_pref("pane-a", "任务·可删", create_category=True)
        server.update_category_pref("pane-b", "任务·可删")
        with self.assertRaisesRegex(ValueError, "fixed category"):
            server.delete_category_pref("开发")
        prefs, removed = server.delete_category_pref("任务·可删")
        self.assertEqual(removed, 2)
        self.assertNotIn("任务·可删", prefs["categories"])
        self.assertEqual(prefs["paneCategories"], {})

    def test_live_inventory_does_not_delete_temporarily_missing_categories(self) -> None:
        live = server.Pane(
            pane_id="%77", target="secretary_web:12.0", session="secretary_web",
            window_index=12, pane_index=0, window_name="paper", command="codex",
            cwd="/work/paper", title="paper", active=False, kind="Codex", project="paper",
            preview="", status="idle", pane_pid="777", pane_start_time="777:9",
        )
        server.update_category_pref(server.pane_preference_key(live), "任务·保留", create_category=True)
        missing_key = "secretary_web:99.0|/temporarily-missing"
        server.update_category_pref(missing_key, "任务·暂离线", create_category=True)
        with (
            mock.patch.object(server, "list_panes", return_value=[live]),
            mock.patch.object(server, "latest_jobs_by_pane_cached", return_value={}),
        ):
            payload = server.build_panes_response("secretary_web")
        prefs = server.read_prefs()
        self.assertEqual([item["pane_id"] for item in payload], ["%77"])
        self.assertIn("任务·保留", prefs["categories"])
        self.assertIn("任务·暂离线", prefs["categories"])
        self.assertEqual(prefs["paneCategories"][missing_key], "任务·暂离线")

    def test_category_mutation_requires_known_category_unless_create_is_explicit(self) -> None:
        server.merge_prefs({"categories": ["最近", "全部", "开发"]})
        with self.assertRaisesRegex(ValueError, "unknown category"):
            server.update_category_pref("pane-a", "任务·论文")
        prefs = server.update_category_pref("pane-a", "任务·论文", create_category=True)
        self.assertIn("任务·论文", prefs["categories"])
        self.assertEqual(prefs["paneCategories"]["pane-a"], "任务·论文")

    def test_identity_verified_pane_preference_mutates_favorite_category_and_alias_once(self) -> None:
        server.merge_prefs({"categories": ["最近", "全部", "开发", "论文"]})
        pane = server.Pane(
            pane_id="%77",
            target="secretary_web:12.0",
            session="secretary_web",
            window_index=12,
            pane_index=0,
            window_name="paper",
            command="codex",
            cwd="/work/paper",
            title="paper",
            active=False,
            kind="Codex",
            project="paper",
            preview="",
            status="idle",
            pane_pid="777",
            pane_start_time="777:9",
        )
        server.merge_prefs({"paneAliases": {pane.target: "旧别名"}})
        prefs, key = server.update_pane_preferences(
            pane,
            favorite=True,
            category="论文",
            alias="论文总控",
        )
        self.assertEqual(key, "secretary_web:12.0|/work/paper")
        self.assertTrue(prefs["paneFavorites"][key])
        self.assertEqual(prefs["paneCategories"][key], "论文")
        self.assertEqual(prefs["paneAliases"][key], "论文总控")
        self.assertNotIn(pane.pane_id, prefs["paneFavorites"])
        self.assertNotIn(pane.pane_id, prefs["paneAliases"])
        self.assertNotIn(pane.target, prefs["paneAliases"])
        prefs, _key = server.update_pane_preferences(pane, alias="")
        self.assertNotIn(key, prefs["paneAliases"])

    def test_identity_verified_alias_rejects_oversized_value(self) -> None:
        pane = server.Pane(
            pane_id="%77", target="secretary_web:77.0", session="secretary_web",
            window_index=77, pane_index=0, window_name="worker", command="codex", cwd="/work",
            title="", active=False, kind="Codex", project="", preview="", status="idle",
            pane_pid="777", pane_start_time="777:9",
        )
        with self.assertRaisesRegex(ValueError, "invalid alias"):
            server.update_pane_preferences(pane, alias="a" * 41)

    def test_group_preference_assigns_all_members_in_one_snapshot(self) -> None:
        panes = [
            server.Pane(
                pane_id=f"%{index}", target=f"secretary_web:{index}.0", session="secretary_web",
                window_index=index, pane_index=0, window_name=f"worker-{index}", command="codex",
                cwd=f"/work/{index}", title="", active=False, kind="Codex", project="",
                preview="", status="idle", pane_pid=str(700 + index), pane_start_time=f"{700 + index}:9",
            )
            for index in (21, 22, 23)
        ]
        prefs, keys = server.update_pane_group_preferences(panes, "任务·Cards", create_category=True)
        self.assertEqual(len(keys), 3)
        self.assertIn("任务·Cards", prefs["categories"])
        self.assertEqual({prefs["paneCategories"][key] for key in keys}, {"任务·Cards"})

    def test_group_preference_rejects_duplicates_without_writing(self) -> None:
        pane = server.Pane(
            pane_id="%77", target="secretary_web:77.0", session="secretary_web",
            window_index=77, pane_index=0, window_name="worker", command="codex", cwd="/work",
            title="", active=False, kind="Codex", project="", preview="", status="idle",
            pane_pid="777", pane_start_time="777:9",
        )
        with self.assertRaisesRegex(ValueError, "duplicate pane"):
            server.update_pane_group_preferences([pane, pane], "任务·重复", create_category=True)
        self.assertNotIn("任务·重复", server.read_prefs().get("categories", []))

    def test_generic_pref_merge_preserves_atomic_categories(self) -> None:
        server.merge_prefs({"categories": ["全部", "开发"], "paneCategories": {"pane-a": "开发"}})
        prefs = server.merge_prefs({"paneCategories": {"stale-pane": "开发"}, "paneAliases": {"pane-a": "A"}})
        self.assertEqual(prefs["paneCategories"], {"pane-a": "开发"})
        self.assertEqual(prefs["paneAliases"], {"pane-a": "A"})

    def test_stale_whole_category_list_cannot_hide_mapped_custom_category(self) -> None:
        server.update_category_pref("pane-a", "任务·保留", create_category=True)
        prefs = server.merge_prefs({"categories": ["最近", "全部", "开发"]})
        self.assertIn("任务·保留", prefs["categories"])
        self.assertEqual(prefs["paneCategories"]["pane-a"], "任务·保留")


class CardsAPIContractTest(unittest.TestCase):
    def test_no_preview_inventory_never_captures_every_pane(self) -> None:
        row = (
            "%41\tsecretary_web\t7\t0\tproject\tcodex\t/work/project\t"
            "title\t1\t4242\t\t\n"
        )
        with mock.patch.object(
            server,
            "run_tmux",
            return_value=SimpleNamespace(returncode=0, stdout=row, stderr=""),
        ), mock.patch.object(
            server,
            "capture",
            side_effect=AssertionError("include_preview=False must not capture panes"),
        ), mock.patch.object(server, "_process_start_token", return_value="4242:9"):
            panes = server.list_panes("secretary_web", include_preview=False)
        self.assertEqual(len(panes), 1)
        self.assertEqual(panes[0].preview, "")

    def test_active_pane_uses_one_display_message_and_api_returns_it(self) -> None:
        old_run_tmux = server.run_tmux
        calls: list[list[str]] = []

        def fake_run_tmux(args):
            calls.append(args)
            return SimpleNamespace(
                returncode=0,
                stdout="%41\tsecretary_web\t7\t0\tproject\tcodex\t/work/project\ttitle\t1\t1\n",
                stderr="",
            )

        server.run_tmux = fake_run_tmux
        try:
            pane = server.active_pane("secretary_web")
        finally:
            server.run_tmux = old_run_tmux
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:4], ["display-message", "-p", "-t", "secretary_web"])
        self.assertNotIn("list-panes", calls[0])
        self.assertNotIn("capture-pane", calls[0])
        self.assertEqual(pane["pane_id"], "%41")
        self.assertEqual(pane["pane"], "%41")
        self.assertEqual(pane["window_index"], 7)
        self.assertTrue(pane["active"])

        old_active_pane = server.active_pane
        server.active_pane = lambda _session=server.DEFAULT_SESSION: pane
        try:
            status, payload = api_get("/cards/api/active-pane")
        finally:
            server.active_pane = old_active_pane
        self.assertEqual(status, 200)
        self.assertEqual(payload["active_pane"]["pane"], "%41")
        self.assertEqual(payload["active_pane"]["window_index"], 7)

    def test_capture_raw_is_opt_in_and_hash_is_stable(self) -> None:
        text = "same raw terminal text <unsafe>"
        old_pane_by_id = server.pane_by_id
        old_capture = server.capture
        old_blocks = server.blocks_for_pane_with_meta
        old_infer_status = server.infer_status
        old_sync = server.sync_pane_job_status
        server.pane_by_id = lambda _pane: None
        server.capture = lambda _pane, history=900: text
        server.blocks_for_pane_with_meta = lambda _pane, _text: ([{"role": "assistant", "label": "AI output", "text": "ok"}], {})
        server.infer_status = lambda _text, _kind: "idle"
        server.sync_pane_job_status = lambda _pane, _target, _status: {}
        try:
            status, compact = api_get("/cards/api/capture?pane=%2541&compact=1")
            raw_status, with_raw = api_get("/cards/api/capture?pane=%2541&raw=1")
            legacy_status, legacy = api_get("/cards/api/capture?pane=%2541")
        finally:
            server.pane_by_id = old_pane_by_id
            server.capture = old_capture
            server.blocks_for_pane_with_meta = old_blocks
            server.infer_status = old_infer_status
            server.sync_pane_job_status = old_sync
        expected_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self.assertEqual((status, raw_status, legacy_status), (200, 200, 200))
        self.assertEqual(compact["raw_hash"], expected_hash)
        self.assertEqual(with_raw["raw_hash"], expected_hash)
        self.assertNotIn("raw", compact)
        self.assertNotIn("safe_html", compact)
        self.assertNotIn("safe_html", with_raw)
        self.assertEqual(with_raw["raw"], text)
        self.assertEqual(legacy["raw"], text, "pre-deployment tabs keep the legacy default contract")

    def test_history_before_fails_closed_for_shared_claude_transcript(self) -> None:
        pane = server.Pane(
            pane_id="%3",
            target="secretary_web:2.0",
            session="secretary_web",
            window_index=2,
            pane_index=0,
            window_name="claude-test",
            command="claude",
            cwd="/work/claude-test",
            title="claude-test",
            active=False,
            kind="Claude",
            project="claude-test",
            preview="",
            status="idle",
        )
        old_pane_by_id = server.pane_by_id
        old_transcript_path = server.transcript_path_for_pane_id
        old_shared = server._shared_live_transcript_panes
        old_history_before = server.history_before
        history_called = False

        def forbidden_history(*_args, **_kwargs):
            nonlocal history_called
            history_called = True
            raise AssertionError("ambiguous shared transcript must not be paginated")

        server.pane_by_id = lambda _pane_id: pane
        server.transcript_path_for_pane_id = lambda _pane_id: "/tmp/shared.jsonl"
        server._shared_live_transcript_panes = lambda _path, _pane_id: ["%2"]
        server.history_before = forbidden_history
        try:
            status, payload = api_get("/cards/api/history_before?pane=%253")
        finally:
            server.pane_by_id = old_pane_by_id
            server.transcript_path_for_pane_id = old_transcript_path
            server._shared_live_transcript_panes = old_shared
            server.history_before = old_history_before

        self.assertEqual(status, 200)
        self.assertEqual(payload["blocks"], [])
        self.assertFalse(payload["has_more"])
        self.assertEqual(payload["reason"], "shared-transcript")
        self.assertEqual(payload["shared_panes"], ["%2"])
        self.assertFalse(history_called)


class PanesResponseCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.old_build = server.build_panes_response
        with server._PANES_RESP_CONDITION:
            server._PANES_RESP_CACHE.clear()
            server._PANES_RESP_REFRESHING.clear()
            server._PANES_RESP_FAILURES.clear()

    def tearDown(self) -> None:
        server.build_panes_response = self.old_build
        with server._PANES_RESP_CONDITION:
            server._PANES_RESP_CACHE.clear()
            server._PANES_RESP_REFRESHING.clear()
            server._PANES_RESP_FAILURES.clear()
            server._PANES_RESP_CONDITION.notify_all()

    def test_cold_concurrent_requests_share_one_build(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        calls = 0
        calls_lock = threading.Lock()

        def build(_session):
            nonlocal calls
            with calls_lock:
                calls += 1
            entered.set()
            self.assertTrue(release.wait(timeout=2))
            return [{"version": 1}]

        server.build_panes_response = build
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(server._panes_response_cached, "test") for _ in range(6)]
            self.assertTrue(entered.wait(timeout=1))
            time.sleep(0.03)
            release.set()
            results = [future.result(timeout=2) for future in futures]
        self.assertEqual(calls, 1)
        self.assertEqual(results, [[{"version": 1}]] * 6)

    def test_expired_snapshot_returns_stale_and_starts_one_refresh(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        calls = 0
        calls_lock = threading.Lock()

        def build(_session):
            nonlocal calls
            with calls_lock:
                calls += 1
            entered.set()
            self.assertTrue(release.wait(timeout=2))
            return [{"version": 2}]

        server.build_panes_response = build
        with server._PANES_RESP_CONDITION:
            server._PANES_RESP_CACHE["test"] = (
                time.monotonic() - server._PANES_RESP_TTL - 1,
                server.now_iso(),
                [{"version": 1}],
            )
        started_at = time.monotonic()
        snapshot = server._panes_response_snapshot("test")
        elapsed = time.monotonic() - started_at
        self.assertEqual(snapshot.panes, [{"version": 1}])
        self.assertTrue(snapshot.stale)
        self.assertTrue(snapshot.refreshing)
        self.assertGreater(snapshot.snapshot_age_ms, 0)
        self.assertLess(elapsed, 0.2)
        self.assertTrue(entered.wait(timeout=1))
        for _ in range(5):
            self.assertEqual(server._panes_response_cached("test"), [{"version": 1}])
        self.assertEqual(calls, 1)
        release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with server._PANES_RESP_CONDITION:
                if "test" not in server._PANES_RESP_REFRESHING:
                    break
            time.sleep(0.01)
        self.assertEqual(server._panes_response_cached("test"), [{"version": 2}])
        time.sleep(0.03)
        self.assertEqual(calls, 1, "refreshes must remain request-triggered, not loop in background")

    def test_cold_concurrent_failure_is_shared_during_backoff(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        calls = 0
        calls_lock = threading.Lock()

        def build(_session):
            nonlocal calls
            with calls_lock:
                calls += 1
            entered.set()
            self.assertTrue(release.wait(timeout=2))
            raise RuntimeError("tmux unavailable")

        server.build_panes_response = build
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(server._panes_response_cached, "test") for _ in range(5)]
            self.assertTrue(entered.wait(timeout=1))
            time.sleep(0.03)
            release.set()
            for future in futures:
                with self.assertRaises(server.PanesResponseUnavailable):
                    future.result(timeout=2)
        self.assertEqual(calls, 1)

    def test_overage_refresh_failure_preserves_stale_snapshot_metadata(self) -> None:
        snapshot_at = "2026-07-10T12:00:00+00:00"
        with server._PANES_RESP_CONDITION:
            server._PANES_RESP_CACHE["test"] = (
                time.monotonic() - server._PANES_RESP_MAX_STALE - 1,
                snapshot_at,
                [{"version": 1}],
            )
        server.build_panes_response = lambda _session: (_ for _ in ()).throw(RuntimeError("tmux unavailable"))
        with self.assertRaises(server.PanesResponseUnavailable) as raised:
            server._panes_response_snapshot("test")
        self.assertEqual(raised.exception.snapshot_at, snapshot_at)
        self.assertGreaterEqual(
            raised.exception.snapshot_age_ms,
            int((server._PANES_RESP_MAX_STALE + 1) * 1000),
        )


if __name__ == "__main__":
    unittest.main()
