#!/usr/bin/env python3
"""Integration tests for the Secretary Bus supervisor."""

from __future__ import annotations

import os
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "scripts" / "cli_bridge.py"
SUPERVISOR = ROOT / "scripts" / "supervisor.py"
SESSION = f"secretary-bus-supervisor-test-{os.getpid()}"


def run_cmd(args: list[str], *, env: dict[str, str] | None = None, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, env=env, cwd=str(cwd) if cwd else None, check=check)


@unittest.skipIf(shutil.which("tmux") is None, "tmux is required")
class SupervisorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bus = self.root / "bus"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        run_cmd(["git", "init", "-q"], cwd=self.repo)
        run_cmd(["git", "config", "user.email", "secretary-bus@example.local"], cwd=self.repo)
        run_cmd(["git", "config", "user.name", "Secretary Bus"], cwd=self.repo)
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        run_cmd(["git", "add", "README.md"], cwd=self.repo)
        run_cmd(["git", "commit", "-q", "-m", "base"], cwd=self.repo)
        self.env = os.environ.copy()
        self.env["AGENT_BUS_DIR"] = str(self.bus)
        run_cmd(["tmux", "kill-session", "-t", SESSION], check=False)
        run_cmd(["tmux", "new-session", "-d", "-s", SESSION, "-n", "worker", "-c", str(self.repo), "bash --noprofile --norc"])
        time.sleep(0.2)
        self.bridge("register", "--name", "worker", "--pane", f"{SESSION}:0.0", "--expected-command", "bash", "--shell")

    def tearDown(self) -> None:
        run_cmd(["tmux", "kill-session", "-t", SESSION], check=False)
        self.tmp.cleanup()

    def bridge(self, *args: str) -> subprocess.CompletedProcess[str]:
        return run_cmd(["python3", str(BRIDGE), *args], env=self.env)

    def supervisor(self, *args: str) -> subprocess.CompletedProcess[str]:
        return run_cmd(["python3", str(SUPERVISOR), *args], env=self.env)

    def test_dispatch_collects_reply_and_git_diff(self) -> None:
        task = "printf 'worker-change\\n' >> README.md && echo WORKER_DONE"
        started = self.supervisor("start", "--target", "worker", "--repo", str(self.repo), "--task", task, "--yes")
        job_id = started.stdout.split("job id=", 1)[1].splitlines()[0]
        time.sleep(0.4)
        collected = self.supervisor("collect", job_id)
        self.assertIn("report=", collected.stdout)

        job_dir = self.bus / "secretary-jobs" / job_id
        response = (job_dir / "response-since-start.txt").read_text(encoding="utf-8")
        self.assertIn("WORKER_DONE", response)
        ledger_job = json.loads((self.bus / "event-ledger" / "jobs" / f"{job_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger_job["status"], "waiting_user")
        events = (self.bus / "event-ledger" / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn('"kind":"tmux_send"', events)
        self.assertIn('"kind":"job_collected"', events)
        diff = (job_dir / "git-diff-worktree-since-baseline.patch").read_text(encoding="utf-8")
        self.assertIn("+worker-change", diff)
        report = (job_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("Secretary Bus Job Report", report)
        self.assertIn("git-diff-worktree-since-baseline.patch", report)

    def test_collect_includes_untracked_file_diff(self) -> None:
        task = "printf 'new-file\\n' > generated.txt && echo GENERATED_DONE"
        started = self.supervisor("start", "--target", "worker", "--repo", str(self.repo), "--task", task, "--yes")
        job_id = started.stdout.split("job id=", 1)[1].splitlines()[0]
        time.sleep(0.4)
        self.supervisor("collect", job_id)

        job_dir = self.bus / "secretary-jobs" / job_id
        untracked = (job_dir / "git-untracked-files.txt").read_text(encoding="utf-8")
        self.assertIn("generated.txt", untracked)
        untracked_diff = (job_dir / "git-diff-untracked.patch").read_text(encoding="utf-8")
        self.assertIn("+new-file", untracked_diff)
        worktree_diff = (job_dir / "git-diff-worktree-since-baseline.patch").read_text(encoding="utf-8")
        self.assertIn("+new-file", worktree_diff)

    def test_collect_parses_completion_marker(self) -> None:
        task = "echo COMPLETION_STATUS: COMPLETE"
        started = self.supervisor("start", "--target", "worker", "--repo", str(self.repo), "--task", task, "--yes")
        job_id = started.stdout.split("job id=", 1)[1].splitlines()[0]
        time.sleep(0.3)
        self.supervisor("collect", job_id)
        ledger_job = json.loads((self.bus / "event-ledger" / "jobs" / f"{job_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger_job["status"], "completed")

    def test_continue_records_followup(self) -> None:
        started = self.supervisor("start", "--target", "worker", "--repo", str(self.repo), "--task", "echo FIRST", "--yes")
        job_id = started.stdout.split("job id=", 1)[1].splitlines()[0]
        self.supervisor("continue", job_id, "--text", "echo SECOND", "--yes")
        time.sleep(0.3)
        self.supervisor("collect", job_id)
        response = (self.bus / "secretary-jobs" / job_id / "response-since-start.txt").read_text(encoding="utf-8")
        self.assertIn("FIRST", response)
        self.assertIn("SECOND", response)

    def test_dry_run_continue_preserves_waiting_user_status(self) -> None:
        started = self.supervisor("start", "--target", "worker", "--repo", str(self.repo), "--task", "echo FIRST", "--yes")
        job_id = started.stdout.split("job id=", 1)[1].splitlines()[0]
        time.sleep(0.2)
        self.supervisor("collect", job_id)
        self.supervisor("continue", job_id, "--text", "echo NOT_SENT")
        ledger_job = json.loads((self.bus / "event-ledger" / "jobs" / f"{job_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger_job["status"], "waiting_user")

    def test_markerless_recollect_does_not_downgrade_terminal_status(self) -> None:
        started = self.supervisor(
            "start",
            "--target",
            "worker",
            "--repo",
            str(self.repo),
            "--task",
            "echo COMPLETION_STATUS: COMPLETE",
            "--yes",
        )
        job_id = started.stdout.split("job id=", 1)[1].splitlines()[0]
        time.sleep(0.2)
        self.supervisor("collect", job_id)
        self.supervisor("collect", job_id, "--history", "1")
        ledger_job = json.loads((self.bus / "event-ledger" / "jobs" / f"{job_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger_job["status"], "completed")

    def test_collect_rejects_target_remap_after_job_start(self) -> None:
        started = self.supervisor("start", "--target", "worker", "--repo", str(self.repo), "--task", "echo FIRST", "--yes")
        job_id = started.stdout.split("job id=", 1)[1].splitlines()[0]
        other = run_cmd([
            "tmux",
            "new-window",
            "-d",
            "-P",
            "-F",
            "#{session_name}:#{window_index}.#{pane_index}",
            "-t",
            SESSION,
            "-n",
            "other",
            "-c",
            str(self.repo),
            "bash --noprofile --norc",
        ]).stdout.strip()
        time.sleep(0.2)
        self.bridge("register", "--name", "worker", "--pane", other, "--expected-command", "bash", "--shell")
        result = run_cmd(["python3", str(SUPERVISOR), "collect", job_id], env=self.env, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("target mapping changed", result.stderr + result.stdout)

    def test_send_failure_is_recorded_as_failed_not_sent(self) -> None:
        self.bridge(
            "register",
            "--name",
            "wrong-command",
            "--pane",
            f"{SESSION}:0.0",
            "--expected-command",
            "claude",
        )
        result = run_cmd(
            [
                "python3",
                str(SUPERVISOR),
                "start",
                "--id",
                "send-failure",
                "--target",
                "wrong-command",
                "--repo",
                str(self.repo),
                "--task",
                "must not be sent",
                "--yes",
            ],
            env=self.env,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        ledger_job = json.loads(
            (self.bus / "event-ledger" / "jobs" / "send-failure.json").read_text(encoding="utf-8")
        )
        self.assertEqual(ledger_job["status"], "failed")

    def test_continue_rejects_same_pane_after_runtime_respawn(self) -> None:
        started = self.supervisor("start", "--target", "worker", "--repo", str(self.repo), "--task", "echo FIRST", "--yes")
        job_id = started.stdout.split("job id=", 1)[1].splitlines()[0]
        run_cmd(["tmux", "respawn-pane", "-k", "-t", f"{SESSION}:0.0", "bash --noprofile --norc"])
        time.sleep(0.2)
        self.bridge("register", "--name", "worker", "--pane", f"{SESSION}:0.0", "--expected-command", "bash", "--shell")
        result = run_cmd(
            ["python3", str(SUPERVISOR), "continue", job_id, "--text", "echo WRONG", "--yes"],
            env=self.env,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("target runtime changed since job start", result.stderr + result.stdout)

    def test_continue_rejects_terminal_attempt(self) -> None:
        started = self.supervisor(
            "start",
            "--target",
            "worker",
            "--repo",
            str(self.repo),
            "--task",
            "echo COMPLETION_STATUS: COMPLETE",
            "--yes",
        )
        job_id = started.stdout.split("job id=", 1)[1].splitlines()[0]
        time.sleep(0.2)
        self.supervisor("collect", job_id)
        result = run_cmd(
            ["python3", str(SUPERVISOR), "continue", job_id, "--text", "echo REOPEN", "--yes"],
            env=self.env,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("terminal job cannot continue", result.stderr + result.stdout)
        ledger_job = json.loads((self.bus / "event-ledger" / "jobs" / f"{job_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger_job["status"], "completed")

    def test_job_records_initiator_and_dirty_baseline(self) -> None:
        (self.repo / "README.md").write_text("dirty-before\n", encoding="utf-8")
        started = self.supervisor("start", "--target", "worker", "--repo", str(self.repo), "--task", "echo DIRTY", "--yes")
        self.assertIn("warning: repo had uncommitted changes", started.stdout)
        job_id = started.stdout.split("job id=", 1)[1].splitlines()[0]
        job = (self.bus / "secretary-jobs" / job_id / "job.json").read_text(encoding="utf-8")
        self.assertIn('"dirty": true', job)

    def test_prune_removes_old_jobs_only_with_yes(self) -> None:
        job_dir = self.bus / "secretary-jobs" / "old-job"
        job_dir.mkdir(parents=True)
        (job_dir / "job.json").write_text('{"id":"old-job","target":"worker"}\n', encoding="utf-8")
        old = time.time() - 10 * 86400
        os.utime(job_dir, (old, old))
        dry = self.supervisor("prune", "--days", "1")
        self.assertIn("dry-run only", dry.stdout)
        self.assertTrue(job_dir.exists())
        self.supervisor("prune", "--days", "1", "--yes")
        self.assertFalse(job_dir.exists())


if __name__ == "__main__":
    unittest.main()
