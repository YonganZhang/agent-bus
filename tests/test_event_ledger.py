#!/usr/bin/env python3
"""Unit tests for the shared Secretary Bus event ledger."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import event_ledger  # noqa: E402


class EventLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_ledger = event_ledger.LEDGER
        self.old_events = event_ledger.EVENTS_FILE
        self.old_jobs = event_ledger.JOBS_DIR
        self.old_counter = event_ledger.COUNTER_FILE
        self.old_offsets = event_ledger.OFFSETS_FILE
        self.old_lock = event_ledger.LOCK_FILE
        event_ledger.LEDGER = Path(self.tmp.name) / "ledger"
        event_ledger.EVENTS_FILE = event_ledger.LEDGER / "events.jsonl"
        event_ledger.JOBS_DIR = event_ledger.LEDGER / "jobs"
        event_ledger.COUNTER_FILE = event_ledger.LEDGER / "next-event-id.txt"
        event_ledger.OFFSETS_FILE = event_ledger.LEDGER / "event-offsets.json"
        event_ledger.LOCK_FILE = event_ledger.LEDGER / ".lock"

    def tearDown(self) -> None:
        event_ledger.LEDGER = self.old_ledger
        event_ledger.EVENTS_FILE = self.old_events
        event_ledger.JOBS_DIR = self.old_jobs
        event_ledger.COUNTER_FILE = self.old_counter
        event_ledger.OFFSETS_FILE = self.old_offsets
        event_ledger.LOCK_FILE = self.old_lock
        self.tmp.cleanup()

    def test_append_after_torn_line_keeps_the_next_event(self) -> None:
        """A writer that died mid-line leaves a fragment without a newline;
        the next event must land on its own line instead of being glued on."""
        event_ledger.append_event("first", message="ok")
        with event_ledger.EVENTS_FILE.open("ab") as fh:
            fh.write(b'{"id":2,"kind":"torn","mess')
        event = event_ledger.append_event("after_crash", message="must survive")
        events, last_id = event_ledger.read_events(after=0, limit=10)
        self.assertEqual([e["kind"] for e in events], ["first", "after_crash"])
        self.assertEqual(last_id, event["id"])
        self.assertTrue(event_ledger.EVENTS_FILE.read_bytes().endswith(b"\n"))
        self.assertEqual(event_ledger.current_committed_event_id(), event["id"])
        # And a reader resuming after the first event still finds it.
        self.assertEqual([e["kind"] for e in event_ledger.read_events(after=1)[0]], ["after_crash"])

    def test_upsert_job_writes_job_and_event_under_one_lock(self) -> None:
        """Appending the event after releasing the lock lets a concurrent
        upsert slip in between, so events.jsonl order can contradict the job
        files.  Every job write and its event must share one lock hold."""
        real_locked = event_ledger.locked
        holds: list[int] = []
        state = {"held": 0}
        seen: list[tuple[str, int]] = []

        @contextlib.contextmanager
        def counting_locked():
            with real_locked():
                holds.append(len(holds) + 1)
                state["held"] = holds[-1]
                try:
                    yield
                finally:
                    state["held"] = 0

        real_write = event_ledger.write_json_atomic
        real_append = event_ledger._append_event_locked

        def write(path, data):
            if path.parent == event_ledger.JOBS_DIR:
                seen.append(("job", state["held"]))
            return real_write(path, data)

        def append(kind, **kw):
            seen.append((kind, state["held"]))
            return real_append(kind, **kw)

        with mock.patch.object(event_ledger, "locked", counting_locked), \
             mock.patch.object(event_ledger, "write_json_atomic", write), \
             mock.patch.object(event_ledger, "_append_event_locked", append):
            event_ledger.upsert_job("job-l", status="running", pane="%1")
            event_ledger.upsert_job("job-l", status="completed")
            event_ledger.upsert_job("job-l", status="running")  # refused reopen
        self.assertEqual(
            seen,
            [("job", 1), ("job_created", 1), ("job", 2), ("job_status_changed", 2), ("job_reopen_rejected", 3)],
        )
        self.assertEqual(len(holds), 3, "each upsert must take the ledger lock exactly once")

    def test_watch_until_terminal_wakes_on_idle_observation(self) -> None:
        """Cards records pane_idle_observed (not completed) for leader/supervisor
        jobs; a waiter wakes on an idle that happened after its starting point,
        but an old idle from an earlier turn must not return it immediately."""
        import argparse
        import contextlib
        import io

        def watch(after: float, timeout: float) -> tuple[float, str]:
            args = argparse.Namespace(after=after, timeout=timeout, limit=50, pane="", job_id="job-w",
                                      until_terminal=True, interval=0.05)
            stderr = io.StringIO()
            started = event_ledger.time.time()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                event_ledger.cmd_watch(args)
            return event_ledger.time.time() - started, stderr.getvalue()

        event_ledger.upsert_job("job-w", pane="%1", target="s:1.0", status="running", source="secretary-bus-supervisor")
        dispatched_at = event_ledger.current_committed_event_id()
        event_ledger.append_event("pane_idle_observed", job_id="job-w", pane="%1", status="running")

        elapsed, hint = watch(after=dispatched_at, timeout=2)  # idle after the caller's point: wake
        self.assertLess(elapsed, 1.5)
        self.assertIn("not proof of completion", hint)

        elapsed, hint = watch(after=0, timeout=0.4)  # stale idle from before this watch: keep waiting
        self.assertGreaterEqual(elapsed, 0.35)
        self.assertNotIn("not proof of completion", hint)
        self.assertEqual(event_ledger.get_job("job-w")["status"], "running")

    def test_upsert_job_records_status_events(self) -> None:
        event_ledger.upsert_job("job-1", pane="%1", target="secretary:1.0", status="sent", task_preview="Do work")
        event_ledger.upsert_job("job-1", pane="%1", target="secretary:1.0", status="running")
        job = event_ledger.get_job("job-1")
        self.assertIsNotNone(job)
        self.assertEqual(job["status"], "running")
        events, last_id = event_ledger.read_events(after=0)
        self.assertGreaterEqual(last_id, 2)
        self.assertEqual([event["kind"] for event in events[:2]], ["job_created", "job_status_changed"])

    def test_completion_marker_updates_latest_active_pane_job(self) -> None:
        event_ledger.upsert_job("job-1", pane="%1", target="secretary:1.0", status="running")
        updated = event_ledger.mark_completion_for_pane(
            "%1",
            "all done\nCOMPLETION_STATUS: BLOCKED\n",
            source="test",
            target="secretary:1.0",
        )
        self.assertIsNotNone(updated)
        self.assertEqual(updated["status"], "blocked")
        events, _last_id = event_ledger.read_events(after=0)
        self.assertIn("completion_marker", [event["kind"] for event in events])

    def test_list_jobs_can_filter_active_by_pane(self) -> None:
        event_ledger.upsert_job("job-1", pane="%1", status="running")
        event_ledger.upsert_job("job-2", pane="%1", status="completed")
        event_ledger.upsert_job("job-3", pane="%2", status="sent")
        jobs = event_ledger.list_jobs(pane="%1", include_terminal=False)
        self.assertEqual([job["id"] for job in jobs], ["job-1"])

    def test_event_offsets_and_counter_recover_after_counter_loss(self) -> None:
        first = event_ledger.append_event("first", job_id="job-1")
        second = event_ledger.append_event("second", job_id="job-1")
        events, last_id = event_ledger.read_events(after=first["id"])
        self.assertEqual([event["kind"] for event in events], ["second"])
        self.assertEqual(last_id, second["id"])
        event_ledger.COUNTER_FILE.unlink()
        third = event_ledger.append_event("third", job_id="job-1")
        self.assertGreater(third["id"], second["id"])

    def test_read_events_limit_and_reset_cursor(self) -> None:
        first = event_ledger.append_event("first")
        second = event_ledger.append_event("second")
        third = event_ledger.append_event("third")
        events, last_id = event_ledger.read_events(after=0, limit=2)
        self.assertEqual([event["id"] for event in events], [first["id"], second["id"]])
        self.assertEqual(last_id, second["id"])
        events, last_id = event_ledger.read_events(after=second["id"], limit=2)
        self.assertEqual([event["id"] for event in events], [third["id"]])
        self.assertEqual(last_id, third["id"])
        events, last_id = event_ledger.read_events(after=9999, limit=2)
        self.assertEqual(events, [])
        self.assertEqual(last_id, third["id"])

    def test_committed_head_does_not_expose_reserved_unwritten_id(self) -> None:
        committed = event_ledger.append_event("committed")
        # Reproduce the small append_event window after id allocation but
        # before the JSONL line and offset index have been committed.
        event_ledger.COUNTER_FILE.write_text(str(committed["id"] + 1), encoding="utf-8")
        self.assertEqual(event_ledger.current_last_event_id(), committed["id"] + 1)
        self.assertEqual(event_ledger.current_committed_event_id(), committed["id"])
        events, last_id = event_ledger.read_events(after=9999, limit=2)
        self.assertEqual(events, [])
        self.assertEqual(last_id, committed["id"])

    def test_prune_removes_old_jobs_and_events_only_with_yes(self) -> None:
        event_ledger.upsert_job("old", pane="%1", status="completed", updated_at="2000-01-01T00:00:00+00:00")
        event_ledger.append_event("old_event", job_id="old")
        dry = event_ledger.prune(30, yes=False)
        self.assertGreaterEqual(dry["jobs"], 1)
        self.assertIsNotNone(event_ledger.get_job("old"))
        removed = event_ledger.prune(30, yes=True)
        self.assertGreaterEqual(removed["jobs"], 1)
        self.assertIsNone(event_ledger.get_job("old"))

    def test_latest_jobs_by_pane_scans_once(self) -> None:
        event_ledger.upsert_job("job-1", pane="%1", status="sent", updated_at="2026-01-01T00:00:00+00:00")
        event_ledger.upsert_job("job-2", pane="%1", status="running", updated_at="2026-01-02T00:00:00+00:00")
        event_ledger.upsert_job("job-3", pane="%2", status="blocked", updated_at="2026-01-03T00:00:00+00:00")
        latest = event_ledger.latest_jobs_by_pane()
        self.assertEqual(latest["%1"]["id"], "job-2")
        self.assertEqual(latest["%2"]["id"], "job-3")


    # --- audit regressions -------------------------------------

    def job_events(self, job_id: str) -> list[dict]:
        events, _ = event_ledger.read_events(after=0, limit=1000, job_id=job_id)
        return events

    def test_terminal_job_is_not_reopened_by_a_later_active_status(self) -> None:
        # Before: upsert_job applied failed -> running unconditionally, which
        # is how leader control events revived 10 failed jobs.
        event_ledger.upsert_job("job-1", pane="%1", status="running")
        event_ledger.upsert_job("job-1", status="failed", message="boom")
        returned = event_ledger.upsert_job("job-1", status="running", message="approve sent")
        self.assertEqual(returned["status"], "failed")
        self.assertEqual(event_ledger.get_job("job-1")["status"], "failed")
        self.assertEqual(event_ledger.get_job("job-1")["message"], "boom")
        kinds = [event["kind"] for event in self.job_events("job-1")]
        self.assertEqual(kinds[-1], "job_reopen_rejected")
        self.assertNotIn("running", [e["status"] for e in self.job_events("job-1")[kinds.index("job_status_changed") + 1:]])

    def test_explicit_reopen_is_still_possible(self) -> None:
        event_ledger.upsert_job("job-1", status="completed", completed_at="2026-01-01T00:00:00+00:00")
        reopened = event_ledger.upsert_job("job-1", reopen=True, status="running", message="rework")
        self.assertEqual(reopened["status"], "running")
        self.assertNotIn("completed_at", reopened)

    def test_terminal_to_terminal_update_is_allowed(self) -> None:
        event_ledger.upsert_job("job-1", status="waiting_user")
        event_ledger.upsert_job("job-1", status="interrupted")
        self.assertEqual(event_ledger.upsert_job("job-1", status="completed")["status"], "completed")

    def test_active_status_on_an_event_for_a_terminal_job_does_not_revive_it_on_replay(self) -> None:
        # Cards rebuilds job state by replaying event status in order.
        event_ledger.upsert_job("job-1", pane="%1", status="running")
        event_ledger.upsert_job("job-1", status="failed")
        control = event_ledger.append_event("leader_control", job_id="job-1", status="running", message="keys sent")
        self.assertEqual(control["status"], "")
        self.assertEqual(control["data"]["ignored_status"], "running")
        self.assertEqual(control["data"]["job_status"], "failed")
        replayed = ""
        for event in self.job_events("job-1"):
            if event.get("status"):
                replayed = event["status"]
        self.assertEqual(replayed, "failed")

    def test_completion_marker_echoed_in_the_prompt_is_not_a_worker_claim(self) -> None:
        # Before: the first regex match was the prompt's own instruction, so a
        # still-working worker was recorded as completed.
        prompt = "Fix the parser, run tests, then end with COMPLETION_STATUS: COMPLETE on its own line."
        capture = (
            "> Fix the parser, run tests, then end with COMPLETION_STATUS:\n"
            "  COMPLETE on its own line.\n\n"
            "* Working… (12s · esc to interrupt)\n"
        )
        self.assertEqual(event_ledger.completion_status_from_text(capture, prompts=[prompt]), "")
        finished = capture + "Tests pass.\nCOMPLETION_STATUS: BLOCKED\n"
        self.assertEqual(event_ledger.completion_status_from_text(finished, prompts=[prompt]), "blocked")

    def test_last_completion_marker_wins(self) -> None:
        text = "COMPLETION_STATUS: BLOCKED\nretried\nCOMPLETION_STATUS: COMPLETE\n"
        self.assertEqual(event_ledger.completion_status_from_text(text), "completed")

    def test_marker_count_guard_when_the_prompt_echo_scrolled_away(self) -> None:
        prompt = "please do it and print COMPLETION_STATUS: COMPLETE when done"
        only_quoted = "...COMPLETION_STATUS: COMPLETE when done\nWorking\n"
        self.assertEqual(event_ledger.completion_status_from_text(only_quoted, prompts=[prompt]), "")
        with_claim = only_quoted + "done\nCOMPLETION_STATUS: FAILED\n"
        self.assertEqual(event_ledger.completion_status_from_text(with_claim, prompts=[prompt]), "failed")

    def test_offsets_index_is_a_short_compat_tail_and_reads_use_binary_search(self) -> None:
        # Before: every append rewrote an index of up to 10000 offsets.
        ids = [event_ledger.append_event(f"e{index}", job_id=f"job-{index % 7}")["id"] for index in range(300)]
        offsets = json.loads(event_ledger.OFFSETS_FILE.read_text(encoding="utf-8"))
        self.assertLessEqual(len(offsets["offsets"]), event_ledger.MAX_EVENT_OFFSETS)
        self.assertEqual(offsets["last_id"], ids[-1])
        event_ledger.OFFSETS_FILE.unlink()
        with mock.patch.object(event_ledger, "SEARCH_LINEAR_BYTES", 64):
            for after in (0, 1, 57, 150, 298, 299):
                events, last_id = event_ledger.read_events(after=ids[after - 1] if after else 0, limit=3)
                expected = ids[after : after + 3]
                self.assertEqual([event["id"] for event in events], expected, after)
                self.assertEqual(last_id, expected[-1] if expected else ids[-1])
            events, _ = event_ledger.read_events(after=ids[100], limit=5, job_id="job-3")
            self.assertTrue(all(event["job_id"] == "job-3" and event["id"] > ids[100] for event in events))
        self.assertEqual(event_ledger.current_committed_event_id(), ids[-1])

    def test_committed_head_ignores_an_in_progress_trailing_line(self) -> None:
        committed = event_ledger.append_event("committed")
        with event_ledger.EVENTS_FILE.open("a", encoding="utf-8") as fh:
            fh.write('{"id": %d, "kind": "half' % (committed["id"] + 1))
        self.assertEqual(event_ledger.current_committed_event_id(), committed["id"])

    def test_prune_moves_old_records_to_legacy_instead_of_deleting(self) -> None:
        # Before: prune unlinked job files and dropped event lines.
        event_ledger.upsert_job("old", pane="%1", status="completed", updated_at="2000-01-01T00:00:00+00:00")
        event_ledger.upsert_job("new", pane="%2", status="running")
        event_ledger.append_event("old_event", job_id="old")
        dry = event_ledger.prune(30, yes=False)
        legacy = Path(dry["legacy_dir"])
        self.assertEqual(legacy.parent, event_ledger.LEDGER.parent / "_legacy" / "event-ledger-pruned")
        self.assertFalse(legacy.exists())
        moved = event_ledger.prune(30, yes=True)
        self.assertEqual(moved["jobs"], 1)
        self.assertIsNone(event_ledger.get_job("old"))
        self.assertIsNotNone(event_ledger.get_job("new"))
        self.assertEqual(json.loads((legacy / "jobs" / "old.json").read_text(encoding="utf-8"))["id"], "old")
        segments = list(legacy.glob("events-pruned-*.jsonl"))
        self.assertEqual(len(segments), 1)
        self.assertIn('"old_event"', segments[0].read_text(encoding="utf-8"))
        self.assertIn("恢复", (legacy / "README.md").read_text(encoding="utf-8"))
        remaining = event_ledger.EVENTS_FILE.read_text(encoding="utf-8")
        self.assertNotIn('"job_id":"old"', remaining)


def iso_ago(now: float, seconds: float) -> str:
    return datetime.fromtimestamp(now - seconds).astimezone().isoformat(timespec="seconds")


class ReapTest(unittest.TestCase):
    setUp = EventLedgerTest.setUp
    tearDown = EventLedgerTest.tearDown
    job_events = EventLedgerTest.job_events
    NOW = 1_800_000_000.0

    def put_job(self, job_id: str, *, status: str, pane: str, updated_ago: float, created_ago: float | None = None, **extra) -> None:
        created = created_ago if created_ago is not None else updated_ago
        event_ledger.upsert_job(
            job_id,
            status=status,
            pane=pane,
            source="test",
            created_at=iso_ago(self.NOW, created),
            updated_at=iso_ago(self.NOW, updated_ago),
            **extra,
        )

    def snapshot(self, panes: dict[str, int], *, server_age: float = 30 * 86400) -> dict:
        return {
            "server_start": self.NOW - server_age,
            "panes": {pane: {"pane_pid": pid, "pane_start_time": f"start-{pid}"} for pane, pid in panes.items()},
        }

    def run_reap(self, snapshots, **kwargs) -> dict:
        queue = list(snapshots)

        def query():
            item = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(item, Exception):
                raise item
            return item

        return event_ledger.reap(now=self.NOW, tmux_query=query, confirm_after=0, sleep=lambda _s: None, **kwargs)

    def rows(self, result: dict) -> dict[str, dict]:
        return {row["job_id"]: row for row in result["candidates"]}

    def test_thresholds_and_death_proofs(self) -> None:
        live = {"%1": 101, "%2": 102, "%9": 109}
        self.put_job("recent-live", status="running", pane="%9", updated_ago=600)
        self.put_job("young-running", status="running", pane="%50", updated_ago=2 * 3600)
        self.put_job("old-sent", status="sent", pane="%51", updated_ago=2 * 3600)
        self.put_job("before-server", status="running", pane="%1", updated_ago=40 * 86400, created_ago=40 * 86400)
        self.put_job("gone", status="waiting_user", pane="%52", updated_ago=3 * 86400)
        self.put_job("reused", status="running", pane="%2", updated_ago=3 * 86400, pane_pid=77, pane_start_time="start-77")
        self.put_job("same", status="running", pane="%1", updated_ago=3 * 86400, pane_pid=101, pane_start_time="start-101")
        self.put_job("done", status="completed", pane="%53", updated_ago=9 * 86400)
        self.put_job("stale", status="stale", pane="%54", updated_ago=9 * 86400)

        dry = self.run_reap([self.snapshot(live)])
        rows = self.rows(dry)
        self.assertEqual(dry["aborted"], "")
        self.assertEqual(set(rows), {"old-sent", "before-server", "gone", "reused", "same"})
        self.assertEqual(rows["before-server"]["decision"], "reap")
        self.assertIn("tmux server started", rows["before-server"]["basis"])
        self.assertEqual(rows["gone"]["decision"], "reap")
        self.assertIn("absent from two queries", rows["gone"]["basis"])
        self.assertEqual(rows["reused"]["decision"], "reap")
        self.assertIn("reused", rows["reused"]["basis"])
        self.assertEqual(rows["same"]["decision"], "warn")
        for key in ("job_id", "source", "status", "age_hours", "basis"):
            self.assertIn(key, rows["gone"])
        # dry-run writes nothing
        self.assertEqual(event_ledger.get_job("gone")["status"], "waiting_user")

        written = self.run_reap([self.snapshot(live)], yes=True)
        self.assertEqual(self.rows(written)["gone"]["decision"], "reaped")
        for job_id in ("old-sent", "before-server", "gone", "reused"):
            job = event_ledger.get_job(job_id)
            self.assertEqual(job["status"], "interrupted", job_id)
            self.assertTrue(job["reap_basis"])
        self.assertEqual(event_ledger.get_job("same")["status"], "running")
        self.assertIn("job_reap_warning", [event["kind"] for event in self.job_events("same")])
        self.assertEqual(event_ledger.get_job("young-running")["status"], "running")
        again = self.run_reap([self.snapshot(live)], yes=True)
        self.assertEqual(set(self.rows(again)), {"same"})

    def test_pane_that_reappears_between_queries_is_not_reaped(self) -> None:
        self.put_job("recent", status="running", pane="%9", updated_ago=60)
        self.put_job("flappy", status="running", pane="%60", updated_ago=3 * 86400)
        first = self.snapshot({"%9": 9})
        second = self.snapshot({"%9": 9, "%60": 60})
        result = self.run_reap([first, second], yes=True)
        self.assertEqual(self.rows(result)["flappy"]["decision"], "skip")
        self.assertEqual(event_ledger.get_job("flappy")["status"], "running")

    def test_running_leader_with_live_claim_protects_its_jobs(self) -> None:
        sessions = event_ledger.LEDGER.parent / "leader-sessions"
        sessions.mkdir(parents=True)
        (sessions / "claims.json").write_text(
            json.dumps({"%70": {"leader_id": "lead", "lease_until": self.NOW + 600}}), encoding="utf-8"
        )
        (sessions / "lead.json").write_text(
            json.dumps({"id": "lead", "status": "running", "workers": {"w": {"pane_id": "%70", "job_ids": ["owned"]}}}),
            encoding="utf-8",
        )
        self.put_job("recent", status="running", pane="%9", updated_ago=60)
        self.put_job("owned", status="running", pane="%70", updated_ago=3 * 86400)
        result = self.run_reap([self.snapshot({"%9": 9})], yes=True)
        self.assertEqual(self.rows(result)["owned"]["decision"], "protected")
        self.assertEqual(event_ledger.get_job("owned")["status"], "running")

    def test_batch_aborts_without_trustworthy_tmux_evidence(self) -> None:
        self.put_job("recent", status="running", pane="%9", updated_ago=60)
        self.put_job("gone", status="running", pane="%60", updated_ago=3 * 86400)
        cases = [
            [event_ledger.ReapAbort("tmux query failed: no server")],
            [{"server_start": self.NOW - 86400, "panes": {}}],
            [self.snapshot({"%1": 1})],  # the recently active pane %9 is invisible: wrong socket
            [self.snapshot({"%9": 9}), self.snapshot({"%9": 9}, server_age=10)],  # server changed
        ]
        for snapshots in cases:
            result = self.run_reap(snapshots, yes=True)
            self.assertTrue(result["aborted"], snapshots)
            self.assertEqual(event_ledger.get_job("gone")["status"], "running")
            self.assertNotIn("job_reaped", [event["kind"] for event in self.job_events("gone")])

    def test_reap_cli_dry_run_on_an_empty_ledger(self) -> None:
        env = os.environ.copy()
        env["AGENT_BUS_DIR"] = str(event_ledger.LEDGER.parent)
        env.pop("AGENT_EVENT_LEDGER_DIR", None)
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "event_ledger.py"), "reap", "--confirm-after", "0"],
            env=env, capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dry-run only", result.stdout)


if __name__ == "__main__":
    unittest.main()
