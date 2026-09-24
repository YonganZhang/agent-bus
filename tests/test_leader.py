#!/usr/bin/env python3
"""Integration tests for generic, provider-neutral leader sessions."""

from __future__ import annotations

import json
import hashlib
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "scripts" / "cli_bridge.py"
LEADER = ROOT / "scripts" / "leader.py"
SESSION = f"agent-bus-leader-test-{os.getpid()}"


def run_cmd(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        text=True,
        capture_output=True,
        env=env,
        cwd=str(cwd) if cwd else None,
        check=check,
    )


@unittest.skipIf(shutil.which("tmux") is None, "tmux is required")
class LeaderSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bus = self.root / "bus"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        run_cmd(["git", "init", "-q"], cwd=self.repo)
        run_cmd(["git", "config", "user.email", "leader@example.local"], cwd=self.repo)
        run_cmd(["git", "config", "user.name", "Leader Test"], cwd=self.repo)
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        run_cmd(["git", "add", "README.md"], cwd=self.repo)
        run_cmd(["git", "commit", "-q", "-m", "base"], cwd=self.repo)

        self.env = os.environ.copy()
        self.env["AGENT_BUS_DIR"] = str(self.bus)
        run_cmd(["tmux", "kill-session", "-t", SESSION], check=False)
        run_cmd(
            ["tmux", "new-session", "-d", "-s", SESSION, "-n", "leader", "-c", str(self.repo), "bash --noprofile --norc"]
        )
        run_cmd(["tmux", "new-window", "-d", "-t", SESSION, "-n", "worker-a", "-c", str(self.repo), "bash --noprofile --norc"])
        run_cmd(["tmux", "new-window", "-d", "-t", SESSION, "-n", "worker-b", "-c", str(self.repo), "bash --noprofile --norc"])
        run_cmd(["tmux", "new-window", "-d", "-t", SESSION, "-n", "outsider", "-c", str(self.repo), "bash --noprofile --norc"])
        time.sleep(0.2)
        self.register("leader", f"{SESSION}:0.0")
        self.register("worker-a", f"{SESSION}:1.0")
        self.register("worker-b", f"{SESSION}:2.0")
        self.register("outsider", f"{SESSION}:3.0")

    def tearDown(self) -> None:
        run_cmd(["tmux", "kill-session", "-t", SESSION], check=False)
        self.tmp.cleanup()

    def register(self, name: str, pane: str, expected_command: str = "bash") -> None:
        run_cmd(
            [
                "python3",
                str(BRIDGE),
                "register",
                "--name",
                name,
                "--pane",
                pane,
                "--expected-command",
                expected_command,
                "--shell",  # test runtimes are fake AIs (bash, python)
            ],
            env=self.env,
        )

    def leader(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        argv = list(args)
        if argv and argv[0] == "create" and "--hidden" not in argv and "--cards-category" not in argv:
            argv.insert(1, "--hidden")
        return run_cmd(["python3", str(LEADER), *argv], env=self.env, check=check)

    def create(self) -> dict:
        result = self.leader(
            "create",
            "--id",
            "demo",
            "--leader",
            "leader",
            "--worker",
            "worker-a",
            "--worker",
            "worker-b",
            "--objective",
            "Coordinate two arbitrary AI workers",
            "--json",
        )
        return json.loads(result.stdout)

    def create_single(self, leader_id: str = "solo", worker: str = "worker-a") -> dict:
        return json.loads(
            self.leader(
                "create",
                "--id",
                leader_id,
                "--leader",
                "leader",
                "--worker",
                worker,
                "--objective",
                "Coordinate one worker through verification",
                "--json",
            ).stdout
        )

    def test_discover_enumerates_every_pane_in_the_current_users_tmux_server(self) -> None:
        discovered = json.loads(self.leader("discover", "--json").stdout)
        own = [pane for pane in discovered["panes"] if pane["session"] == SESSION]
        self.assertEqual(len(own), 4)
        self.assertEqual({pane["window_name"] for pane in own}, {"leader", "worker-a", "worker-b", "outsider"})
        self.assertEqual({pane["registered_names"][0] for pane in own}, {"leader", "worker-a", "worker-b", "outsider"})
        self.assertEqual(len({pane["pane_id"] for pane in own}), 4)
        self.assertTrue(all(pane["runtime_fingerprint"] for pane in own))

    def test_trusted_owner_assign_spills_multiline_task_and_dry_run_overrides(self) -> None:
        # A multi-line task is no longer pasted line by line: it is written to
        # a task file inside the job directory and one pointer line is typed.
        self.leader("config", "--trusted-owner", "true")
        self.create()
        output = self.root / "trusted-assign.txt"
        quoted = shlex.quote(str(output))
        task = f"printf 'one\\n' > {quoted}\nprintf 'two\\n' >> {quoted}"
        assigned = json.loads(
            self.leader(
                "assign",
                "demo",
                "--worker",
                "worker-a",
                "--repo",
                str(self.repo),
                "--task",
                task,
                "--json",
            ).stdout
        )
        time.sleep(0.2)
        self.assertTrue(assigned["sent"])
        job_dir = self.bus / "secretary-jobs" / assigned["job_id"]
        spilled = list(job_dir.glob("task-*.md"))
        self.assertEqual(len(spilled), 1)
        self.assertEqual(spilled[0].read_text(encoding="utf-8").rstrip("\n"), task)
        job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
        self.assertNotIn("\n", job["delivered_text"])
        self.assertIn(str(spilled[0]), job["delivered_text"])
        self.assertFalse(output.exists())

        dry_output = self.root / "trusted-dry-run.txt"
        dry = json.loads(
            self.leader(
                "assign",
                "demo",
                "--worker",
                "worker-b",
                "--repo",
                str(self.repo),
                "--task",
                f"touch {shlex.quote(str(dry_output))}",
                "--dry-run",
                "--json",
            ).stdout
        )
        time.sleep(0.1)
        self.assertFalse(dry["sent"])
        self.assertFalse(dry_output.exists())

    def test_assign_dry_run_does_not_consume_attempt_or_block_real_assign(self) -> None:
        self.leader("config", "--trusted-owner", "true")
        self.create()
        dry = json.loads(
            self.leader(
                "assign",
                "demo",
                "--worker",
                "worker-a",
                "--repo",
                str(self.repo),
                "--task",
                "echo preview-only",
                "--dry-run",
                "--json",
            ).stdout
        )
        self.assertFalse(dry["sent"])
        self.assertEqual(dry["job_id"], "")
        after_dry = json.loads(self.leader("status", "demo", "--json").stdout)
        self.assertEqual(after_dry["workers"]["worker-a"]["job_ids"], [])
        self.assertEqual(after_dry["workers"]["worker-a"]["status"], "unassigned")

        output = self.root / "after-dry-run.txt"
        real = json.loads(
            self.leader(
                "assign",
                "demo",
                "--worker",
                "worker-a",
                "--repo",
                str(self.repo),
                "--task",
                f"touch {shlex.quote(str(output))}",
                "--json",
            ).stdout
        )
        time.sleep(0.1)
        self.assertTrue(real["sent"])
        self.assertTrue(real["job_id"].startswith("lead-demo-worker-a-"))
        self.assertTrue(output.exists())

    def test_keys_and_approve_are_trusted_by_default_and_action_keys_remain_idempotent(self) -> None:
        self.leader("config", "--trusted-owner", "true")
        self.create()
        keyed = self.root / "keyed.txt"
        run_cmd(
            ["tmux", "send-keys", "-t", f"{SESSION}:1.0", "-l", f"touch {shlex.quote(str(keyed))}"],
        )
        first = json.loads(
            self.leader(
                "keys",
                "demo",
                "--worker",
                "worker-a",
                "--key",
                "Enter",
                "--action-key",
                "key-enter-once",
                "--json",
            ).stdout
        )
        time.sleep(0.1)
        self.assertTrue(first["sent"])
        self.assertFalse(first["duplicate"])
        self.assertTrue(keyed.exists())

        approved = self.root / "approved.txt"
        run_cmd(
            ["tmux", "send-keys", "-t", f"{SESSION}:1.0", "-l", f"touch {shlex.quote(str(approved))}"],
        )
        # Raw keys stay available with --key; without it approve answers a
        # dialog by policy (tests/test_leader_policy.py).
        approval = json.loads(
            self.leader(
                "approve",
                "demo",
                "--worker",
                "worker-a",
                "--key",
                "Enter",
                "--action-key",
                "approve-once",
                "--json",
            ).stdout
        )
        time.sleep(0.1)
        self.assertTrue(approval["sent"])
        self.assertTrue(approved.exists())

        duplicate = json.loads(
            self.leader(
                "keys",
                "demo",
                "--worker",
                "worker-a",
                "--key",
                "Enter",
                "--action-key",
                "key-enter-once",
                "--json",
            ).stdout
        )
        self.assertTrue(duplicate["duplicate"])

    def test_interrupt_sends_ctrl_c_and_terminalizes_the_current_job(self) -> None:
        self.leader("config", "--trusted-owner", "true")
        self.create()
        assigned = json.loads(
            self.leader(
                "assign",
                "demo",
                "--worker",
                "worker-a",
                "--repo",
                str(self.repo),
                "--task",
                "echo assigned",
                "--json",
            ).stdout
        )
        time.sleep(0.1)
        interrupted = json.loads(
            self.leader(
                "interrupt",
                "demo",
                "--worker",
                "worker-a",
                "--action-key",
                "interrupt-attempt-1",
                "--json",
            ).stdout
        )
        self.assertTrue(interrupted["sent"])
        self.assertTrue(interrupted["verified"])
        job = json.loads(
            (self.bus / "event-ledger" / "jobs" / f"{assigned['job_id']}.json").read_text(encoding="utf-8")
        )
        self.assertEqual(job["status"], "interrupted")
        state = json.loads(self.leader("status", "demo", "--json").stdout)
        self.assertEqual(state["workers"]["worker-a"]["status"], "interrupted")
        rejected = self.leader(
            "steer",
            "demo",
            "--worker",
            "worker-a",
            "--text",
            "must not reopen",
            check=False,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("terminal job cannot continue", rejected.stderr + rejected.stdout)

    def test_interrupt_that_does_not_stop_runtime_keeps_job_active(self) -> None:
        self.leader("config", "--trusted-owner", "true")
        code = "import signal,time;signal.signal(signal.SIGINT,signal.SIG_IGN);time.sleep(30)"
        run_cmd(["tmux", "send-keys", "-t", f"{SESSION}:1.0", "-l", f"exec python3 -c {shlex.quote(code)}"])
        run_cmd(["tmux", "send-keys", "-t", f"{SESSION}:1.0", "Enter"])
        time.sleep(0.2)
        self.register("worker-a", f"{SESSION}:1.0", expected_command="python3")
        self.create()
        assigned = json.loads(
            self.leader(
                "assign",
                "demo",
                "--worker",
                "worker-a",
                "--repo",
                str(self.repo),
                "--task",
                "input ignored by sleeping runtime",
                "--json",
            ).stdout
        )
        result = self.leader(
            "interrupt",
            "demo",
            "--worker",
            "worker-a",
            "--wait",
            "0.25",
            "--interval",
            "0.05",
            "--action-key",
            "ignored-interrupt",
            "--json",
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["sent"])
        self.assertFalse(payload["verified"])
        job = json.loads(
            (self.bus / "event-ledger" / "jobs" / f"{assigned['job_id']}.json").read_text(encoding="utf-8")
        )
        self.assertNotIn(job["status"], {"interrupted", "completed", "failed", "cancelled"})
        state = json.loads(self.leader("status", "demo", "--json").stdout)
        self.assertNotEqual(state["workers"]["worker-a"]["status"], "interrupted")
        self.assertEqual(state["actions"]["ignored-interrupt"]["status"], "sent_unverified")

    def test_restart_rotates_the_frozen_runtime_and_prevents_the_old_job_from_continuing(self) -> None:
        self.leader("config", "--trusted-owner", "true")
        before = self.create()["workers"]["worker-a"]
        assigned = json.loads(
            self.leader(
                "assign",
                "demo",
                "--worker",
                "worker-a",
                "--repo",
                str(self.repo),
                "--task",
                "echo old-attempt",
                "--json",
            ).stdout
        )
        time.sleep(0.1)
        restarted = json.loads(
            self.leader(
                "restart",
                "demo",
                "--worker",
                "worker-a",
                "--command",
                "bash --noprofile --norc",
                "--action-key",
                "restart-worker-a-once",
                "--json",
            ).stdout
        )
        self.assertTrue(restarted["sent"])
        after = json.loads(self.leader("status", "demo", "--json").stdout)["workers"]["worker-a"]
        self.assertEqual(after["pane_id"], before["pane_id"])
        self.assertNotEqual(after["pane_pid"], before["pane_pid"])
        self.assertNotEqual(after["runtime_fingerprint"], before["runtime_fingerprint"])
        self.assertEqual(after["status"], "restarted")
        self.assertTrue(json.loads(self.leader("doctor", "demo", "--json").stdout)["healthy"])

        old_continue = run_cmd(
            [
                "python3",
                str(ROOT / "scripts" / "supervisor.py"),
                "continue",
                assigned["job_id"],
                "--text",
                "must never reach replacement runtime",
                "--yes",
            ],
            env=self.env,
            check=False,
        )
        self.assertNotEqual(old_continue.returncode, 0)
        self.assertIn("terminal job cannot continue", old_continue.stderr + old_continue.stdout)

        duplicate = json.loads(
            self.leader(
                "restart",
                "demo",
                "--worker",
                "worker-a",
                "--command",
                "bash --noprofile --norc",
                "--action-key",
                "restart-worker-a-once",
                "--json",
            ).stdout
        )
        self.assertTrue(duplicate["duplicate"])
        unchanged = json.loads(self.leader("status", "demo", "--json").stdout)["workers"]["worker-a"]
        self.assertEqual(unchanged["pane_pid"], after["pane_pid"])

        successor_out = self.root / "successor.txt"
        successor = json.loads(
            self.leader(
                "assign",
                "demo",
                "--worker",
                "worker-a",
                "--repo",
                str(self.repo),
                "--task",
                f"touch {shlex.quote(str(successor_out))}",
                "--json",
            ).stdout
        )
        time.sleep(0.1)
        self.assertTrue(successor["sent"])
        self.assertTrue(successor_out.exists())

    def test_restart_reconciles_prepared_action_after_respawn_before_finalize(self) -> None:
        self.leader("config", "--trusted-owner", "true")
        self.create()
        assigned = json.loads(
            self.leader(
                "assign",
                "demo",
                "--worker",
                "worker-a",
                "--repo",
                str(self.repo),
                "--task",
                "echo old-runtime",
                "--json",
            ).stdout
        )
        state_path = self.bus / "leader-sessions" / "demo.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        worker = state["workers"]["worker-a"]
        command = "bash --noprofile --norc"
        payload_hash = hashlib.sha256(
            f"{command}\0bash\0{self.repo.resolve()}".encode("utf-8")
        ).hexdigest()[:16]
        action = {
            "key": "restart-crash-window",
            "kind": "restart",
            "worker": "worker-a",
            "pane_id": worker["pane_id"],
            "runtime_fingerprint": worker["runtime_fingerprint"],
            "payload_hash": payload_hash,
            "status": "prepared",
            "at": "simulated-crash",
        }
        state.setdefault("actions", {})[action["key"]] = action
        state["last_action"] = action
        state_path.write_text(json.dumps(state), encoding="utf-8")

        run_cmd(
            [
                "tmux",
                "respawn-pane",
                "-k",
                "-t",
                worker["pane_id"],
                "-c",
                str(self.repo),
                command,
            ]
        )
        time.sleep(0.2)
        respawned_pid = int(
            run_cmd(["tmux", "display-message", "-p", "-t", worker["pane_id"], "#{pane_pid}"])
            .stdout.strip()
        )
        reconciled = json.loads(
            self.leader(
                "restart",
                "demo",
                "--worker",
                "worker-a",
                "--command",
                command,
                "--action-key",
                action["key"],
                "--json",
            ).stdout
        )
        self.assertTrue(reconciled["sent"])
        self.assertTrue(reconciled["reconciled"])
        final = json.loads(self.leader("status", "demo", "--json").stdout)
        self.assertEqual(final["workers"]["worker-a"]["pane_pid"], respawned_pid)
        self.assertEqual(final["actions"][action["key"]]["status"], "sent")
        job = json.loads(
            (self.bus / "event-ledger" / "jobs" / f"{assigned['job_id']}.json").read_text(encoding="utf-8")
        )
        self.assertEqual(job["status"], "interrupted")

    def test_kill_retires_the_worker_releases_its_claim_and_terminalizes_its_job(self) -> None:
        self.leader("config", "--trusted-owner", "true")
        worker_pane_id = self.create()["workers"]["worker-b"]["pane_id"]
        assigned = json.loads(
            self.leader(
                "assign",
                "demo",
                "--worker",
                "worker-b",
                "--repo",
                str(self.repo),
                "--task",
                "echo before-kill",
                "--json",
            ).stdout
        )
        time.sleep(0.1)
        killed = json.loads(
            self.leader(
                "kill",
                "demo",
                "--worker",
                "worker-b",
                "--action-key",
                "kill-worker-b-once",
                "--json",
            ).stdout
        )
        self.assertTrue(killed["sent"])
        resolved = run_cmd(
            ["tmux", "display-message", "-p", "-t", worker_pane_id, "#{{pane_id}}"],
            check=False,
        )
        self.assertNotEqual(resolved.stdout.strip(), worker_pane_id)
        job = json.loads(
            (self.bus / "event-ledger" / "jobs" / f"{assigned['job_id']}.json").read_text(encoding="utf-8")
        )
        self.assertEqual(job["status"], "interrupted")
        state = json.loads(self.leader("status", "demo", "--json").stdout)
        self.assertTrue(state["workers"]["worker-b"]["retired"])
        self.assertEqual(state["workers"]["worker-b"]["status"], "killed")
        claims = json.loads((self.bus / "leader-sessions" / "claims.json").read_text(encoding="utf-8"))
        self.assertNotIn(state["workers"]["worker-b"]["pane_id"], claims)
        self.assertTrue(json.loads(self.leader("doctor", "demo", "--json").stdout)["healthy"])

        duplicate = json.loads(
            self.leader(
                "kill",
                "demo",
                "--worker",
                "worker-b",
                "--action-key",
                "kill-worker-b-once",
                "--json",
            ).stdout
        )
        self.assertTrue(duplicate["duplicate"])
        rejected = self.leader(
            "assign",
            "demo",
            "--worker",
            "worker-b",
            "--repo",
            str(self.repo),
            "--task",
            "must not target killed pane",
            check=False,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("retired", rejected.stderr + rejected.stdout)

    def test_create_is_scoped_to_registered_members_and_has_stable_identity(self) -> None:
        state = self.create()
        self.assertEqual(state["id"], "demo")
        self.assertEqual(state["leader"], "leader")
        self.assertEqual(set(state["workers"]), {"worker-a", "worker-b"})
        self.assertNotIn("outsider", state["workers"])
        for worker in state["workers"].values():
            self.assertTrue(worker["pane"].startswith(f"{SESSION}:"))
            self.assertTrue(worker["pane_id"].startswith("%"))
            self.assertGreater(worker["pane_pid"], 0)

        status = json.loads(self.leader("status", "demo", "--json").stdout)
        self.assertEqual(status["objective"], "Coordinate two arbitrary AI workers")
        self.assertEqual(status["status"], "running")

    def test_assign_records_a_supervisor_job_and_tick_filters_events(self) -> None:
        self.create()
        assigned = json.loads(
            self.leader(
                "assign",
                "demo",
                "--worker",
                "worker-a",
                "--repo",
                str(self.repo),
                "--task",
                "Review the repository and report",
                "--json",
            ).stdout
        )
        self.assertEqual(assigned["worker"], "worker-a")
        self.assertTrue(assigned["job_id"].startswith("lead-demo-worker-a-"))

        tick = json.loads(self.leader("tick", "demo", "--json").stdout)
        self.assertEqual(tick["leader_id"], "demo")
        self.assertTrue(tick["changed"])
        self.assertTrue(tick["events"])
        self.assertEqual({event["target"] for event in tick["events"]}, {"worker-a"})
        self.assertNotIn("Review the repository and report", json.dumps(tick))

    def test_watch_times_out_on_unrelated_activity(self) -> None:
        self.create()
        # Create a dry-run job outside this leader's declared membership.
        run_cmd(
            [
                "python3",
                str(ROOT / "scripts" / "supervisor.py"),
                "start",
                "--target",
                "outsider",
                "--repo",
                str(self.repo),
                "--task",
                "Unrelated work",
            ],
            env=self.env,
        )
        watched = json.loads(
            self.leader(
                "watch",
                "demo",
                "--timeout",
                "0.25",
                "--interval",
                "0.05",
                "--probe-interval",
                "60",
                "--json",
            ).stdout
        )
        self.assertFalse(watched["changed"])
        self.assertEqual(watched["reason"], "timeout")
        self.assertEqual(watched["events"], [])

    def test_doctor_detects_process_identity_change(self) -> None:
        self.create()
        run_cmd(["tmux", "respawn-pane", "-k", "-t", f"{SESSION}:1.0", "sleep 60"])
        diagnosed = json.loads(self.leader("doctor", "demo", "--json", check=False).stdout)
        self.assertFalse(diagnosed["healthy"])
        worker = next(item for item in diagnosed["members"] if item["name"] == "worker-a")
        self.assertIn(worker["status"], {"command_mismatch", "process_changed"})

    def test_active_worker_claim_rejects_a_second_leader(self) -> None:
        self.create()
        conflict = self.leader(
            "create",
            "--id",
            "competing",
            "--leader",
            "outsider",
            "--worker",
            "worker-a",
            "--objective",
            "Competing leader must fail closed",
            check=False,
        )
        self.assertNotEqual(conflict.returncode, 0)
        self.assertIn("already claimed by leader demo", conflict.stderr + conflict.stdout)

        taken_over = json.loads(
            self.leader(
                "create",
                "--id",
                "competing",
                "--leader",
                "outsider",
                "--worker",
                "worker-a",
                "--objective",
                "Explicitly take over",
                "--takeover",
                "--json",
            ).stdout
        )
        self.assertEqual(taken_over["id"], "competing")
        old = json.loads(self.leader("status", "demo", "--json").stdout)
        self.assertEqual(old["status"], "superseded")

    def test_concurrent_controls_and_takeover_leave_one_owner_without_corrupt_state(self) -> None:
        self.leader("config", "--trusted-owner", "true")
        self.create()
        commands = [
            [
                "python3",
                str(LEADER),
                "keys",
                "demo",
                "--worker",
                "worker-a",
                "--key",
                "Enter",
                "--action-key",
                f"race-key-{index}",
                "--json",
            ]
            for index in range(8)
        ]
        takeover = [
            "python3",
            str(LEADER),
            "create",
            "--hidden",
            "--id",
            "race-winner",
            "--leader",
            "outsider",
            "--worker",
            "worker-a",
            "--objective",
            "win concurrent takeover",
            "--takeover",
            "--json",
        ]
        processes = [
            subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
            for command in [*commands[:4], takeover, *commands[4:]]
        ]
        results = [process.communicate(timeout=10) + (process.returncode,) for process in processes]
        self.assertEqual(results[4][2], 0, results[4][1])
        old = json.loads(self.leader("status", "demo", "--json").stdout)
        winner = json.loads(self.leader("status", "race-winner", "--json").stdout)
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(winner["status"], "running")
        claims = json.loads((self.bus / "leader-sessions" / "claims.json").read_text(encoding="utf-8"))
        pane_id = winner["workers"]["worker-a"]["pane_id"]
        self.assertEqual(claims[pane_id]["leader_id"], "race-winner")
        rejected = self.leader(
            "keys",
            "demo",
            "--worker",
            "worker-a",
            "--key",
            "Enter",
            "--action-key",
            "after-takeover",
            check=False,
        )
        self.assertNotEqual(rejected.returncode, 0)

    def test_worker_completion_requires_leader_verification_evidence(self) -> None:
        self.create()
        assigned = json.loads(
            self.leader(
                "assign",
                "demo",
                "--worker",
                "worker-a",
                "--repo",
                str(self.repo),
                "--task",
                "Review the repository and report",
                "--json",
            ).stdout
        )
        assigned_b = json.loads(
            self.leader(
                "assign",
                "demo",
                "--worker",
                "worker-b",
                "--repo",
                str(self.repo),
                "--task",
                "Run an independent review",
                "--json",
            ).stdout
        )
        for job_id in (assigned["job_id"], assigned_b["job_id"]):
            ledger_job = self.bus / "event-ledger" / "jobs" / f"{job_id}.json"
            job = json.loads(ledger_job.read_text(encoding="utf-8"))
            job["status"] = "completed"
            ledger_job.write_text(json.dumps(job), encoding="utf-8")

        tick = json.loads(self.leader("tick", "demo", "--json").stdout)
        self.assertEqual(tick["phase"], "verifying")
        state = json.loads(self.leader("status", "demo", "--json").stdout)
        self.assertEqual(state["status"], "running")

        unverified = self.leader("close", "demo", "--status", "completed", check=False)
        self.assertNotEqual(unverified.returncode, 0)
        self.assertIn("completion requires at least one --evidence", unverified.stderr + unverified.stdout)
        # Before: terminal worker claims plus any evidence text closed the
        # leader as completed without a single `leader verify`.
        not_accepted = self.leader(
            "close", "demo", "--status", "completed", "--evidence", "python -m unittest: passed", check=False
        )
        self.assertNotEqual(not_accepted.returncode, 0)
        message = not_accepted.stderr + not_accepted.stdout
        self.assertIn("secretary-bus leader verify demo --worker worker-a --evidence", message)
        self.assertIn("secretary-bus leader verify demo --worker worker-b --evidence", message)
        self.assertEqual(json.loads(self.leader("status", "demo", "--json").stdout)["status"], "running")
        self.leader("verify", "demo", "--worker", "worker-a", "--evidence", "diff reviewed")
        still = self.leader(
            "close", "demo", "--status", "completed", "--evidence", "python -m unittest: passed", check=False
        )
        self.assertNotEqual(still.returncode, 0)
        self.assertNotIn("--worker worker-a", still.stderr + still.stdout)
        self.assertIn("--worker worker-b", still.stderr + still.stdout)
        self.leader("verify", "demo", "--worker", "worker-b", "--evidence", "review read")
        closed = json.loads(
            self.leader(
                "close",
                "demo",
                "--status",
                "completed",
                "--evidence",
                "python -m unittest: passed",
                "--json",
            ).stdout
        )
        self.assertEqual(closed["status"], "completed")

    def test_verify_requires_evidence_and_completes_latest_job_without_interrupt(self) -> None:
        self.leader("config", "--trusted-owner", "true")
        self.create_single()
        assigned = json.loads(
            self.leader(
                "assign",
                "solo",
                "--worker",
                "worker-a",
                "--repo",
                str(self.repo),
                "--task",
                "echo implementation-finished",
                "--json",
            ).stdout
        )
        time.sleep(0.1)
        self.leader("collect", "solo", "--worker", "worker-a")
        rejected = self.leader(
            "verify",
            "solo",
            "--worker",
            "worker-a",
            "--action-key",
            "verify-worker-a",
            check=False,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("evidence", rejected.stderr + rejected.stdout)

        verified = json.loads(
            self.leader(
                "verify",
                "solo",
                "--worker",
                "worker-a",
                "--evidence",
                "response and repository independently inspected",
                "--action-key",
                "verify-worker-a",
                "--json",
            ).stdout
        )
        self.assertTrue(verified["verified"])
        self.assertFalse(verified["duplicate"])
        state = json.loads(self.leader("status", "solo", "--json").stdout)
        self.assertEqual(state["workers"]["worker-a"]["status"], "completed")
        self.assertEqual(state["phase"], "verifying")
        job = json.loads(
            (self.bus / "event-ledger" / "jobs" / f"{assigned['job_id']}.json").read_text(encoding="utf-8")
        )
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["verified_by_leader"], "solo")
        pane_check = run_cmd(
            [
                "tmux",
                "display-message",
                "-p",
                "-t",
                state["workers"]["worker-a"]["pane_id"],
                "#{pane_id}",
            ],
            check=False,
        )
        self.assertEqual(pane_check.returncode, 0)

        duplicate = json.loads(
            self.leader(
                "verify",
                "solo",
                "--worker",
                "worker-a",
                "--evidence",
                "response and repository independently inspected",
                "--action-key",
                "verify-worker-a",
                "--json",
            ).stdout
        )
        self.assertTrue(duplicate["duplicate"])

    def test_tick_skips_dead_runtime_identity_after_latest_job_is_terminal(self) -> None:
        self.leader("config", "--trusted-owner", "true")
        state = self.create_single()
        self.leader(
            "assign",
            "solo",
            "--worker",
            "worker-a",
            "--repo",
            str(self.repo),
            "--task",
            "echo ready-to-interrupt",
            "--json",
        )
        time.sleep(0.1)
        interrupted = json.loads(
            self.leader(
                "interrupt",
                "solo",
                "--worker",
                "worker-a",
                "--wait",
                "0.5",
                "--action-key",
                "terminal-before-pane-exit",
                "--json",
            ).stdout
        )
        self.assertTrue(interrupted["verified"])
        run_cmd(["tmux", "kill-pane", "-t", state["workers"]["worker-a"]["pane_id"]])
        ticked = self.leader("tick", "solo", "--json", check=False)
        self.assertEqual(ticked.returncode, 0, ticked.stderr + ticked.stdout)
        payload = json.loads(ticked.stdout)
        self.assertEqual(payload["issues"], [])
        self.assertEqual(payload["workers"]["worker-a"], "interrupted")
        self.assertEqual(payload["phase"], "verifying")

    def test_leader_help_exposes_verify_command(self) -> None:
        help_text = run_cmd(["python3", str(LEADER), "--help"], env=self.env).stdout
        self.assertIn("verify", help_text)

    def test_completed_close_rejects_active_workers_even_with_evidence(self) -> None:
        self.create()
        result = self.leader(
            "close",
            "demo",
            "--status",
            "completed",
            "--evidence",
            "A check passed while workers were still active",
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("workers have not all reached terminal claims", result.stderr + result.stdout)

    def test_tick_reacquires_its_own_expired_uncontested_claim(self) -> None:
        self.create()
        claims_path = self.bus / "leader-sessions" / "claims.json"
        claims = json.loads(claims_path.read_text(encoding="utf-8"))
        for claim in claims.values():
            claim["lease_until"] = 0
        claims_path.write_text(json.dumps(claims), encoding="utf-8")

        tick = json.loads(self.leader("tick", "demo", "--json").stdout)
        self.assertFalse(tick["issues"])
        doctor = json.loads(self.leader("doctor", "demo", "--json").stdout)
        self.assertTrue(doctor["healthy"])

    def test_tick_keeps_a_paused_leaders_expired_claim_and_persists_its_own_renewal(self) -> None:
        # 改前: 任一 leader tick 都把所有过期 claim 写回删掉，暂停中的 leader 醒来即 claim_lost。
        self.create()
        sessions = self.bus / "leader-sessions"
        claims_path = sessions / "claims.json"
        (sessions / "paused.json").write_text(json.dumps({"id": "paused", "status": "running"}), encoding="utf-8")
        (sessions / "gone.json").write_text(json.dumps({"id": "gone", "status": "closed"}), encoding="utf-8")
        claims = json.loads(claims_path.read_text(encoding="utf-8"))
        for claim in claims.values():
            claim["lease_until"] = time.time() + 5  # due for renewal
        claims["%paused"] = {"leader_id": "paused", "worker": "w", "runtime_fingerprint": "x", "lease_until": 1}
        claims["%gone"] = {"leader_id": "gone", "worker": "w", "runtime_fingerprint": "x", "lease_until": 1}
        claims_path.write_text(json.dumps(claims), encoding="utf-8")

        tick = json.loads(self.leader("tick", "demo", "--json").stdout)
        self.assertFalse(tick["issues"])
        after = json.loads(claims_path.read_text(encoding="utf-8"))
        self.assertIn("%paused", after)
        self.assertNotIn("%gone", after)
        demo = [claim for claim in after.values() if claim["leader_id"] == "demo"]
        self.assertEqual(len(demo), 2)
        self.assertTrue(all(claim["lease_until"] > time.time() + 60 for claim in demo))

    def test_runtime_remap_aborts_before_creating_an_attempt(self) -> None:
        self.create()
        run_cmd(
            [
                "python3",
                str(BRIDGE),
                "register",
                "--name",
                "worker-a",
                "--pane",
                f"{SESSION}:1.0",
                "--expected-command",
                "claude",
            ],
            env=self.env,
        )
        result = self.leader(
            "assign",
            "demo",
            "--worker",
            "worker-a",
            "--repo",
            str(self.repo),
            "--task",
            "must fail before send",
            "--yes",
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        state = json.loads(self.leader("status", "demo", "--json").stdout)
        self.assertEqual(state["workers"]["worker-a"]["job_ids"], [])
        self.assertIn("target command mismatch", result.stderr + result.stdout)

    def test_steer_action_key_is_idempotent(self) -> None:
        self.create()
        assigned = json.loads(
            self.leader(
                "assign",
                "demo",
                "--worker",
                "worker-a",
                "--repo",
                str(self.repo),
                "--task",
                "echo START",
                "--yes",
                "--json",
            ).stdout
        )
        self.assertTrue(assigned["sent"])
        out = self.root / "steer-count.txt"
        command = f"printf 'once\\n' >> {out}"
        first = json.loads(
            self.leader(
                "steer",
                "demo",
                "--worker",
                "worker-a",
                "--text",
                command,
                "--action-key",
                "demo-steer-1",
                "--yes",
                "--json",
            ).stdout
        )
        self.leader(
            "keys",
            "demo",
            "--worker",
            "worker-a",
            "--key",
            "C-l",
            "--action-key",
            "intervening-clear",
            "--yes",
            "--json",
        )
        second = json.loads(
            self.leader(
                "steer",
                "demo",
                "--worker",
                "worker-a",
                "--text",
                command,
                "--action-key",
                "demo-steer-1",
                "--yes",
                "--json",
            ).stdout
        )
        time.sleep(0.2)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(out.read_text(encoding="utf-8").splitlines(), ["once"])

    def test_create_persists_recovery_contract(self) -> None:
        state = json.loads(
            self.leader(
                "create",
                "--id",
                "contract",
                "--leader",
                "leader",
                "--worker",
                "worker-a",
                "--objective",
                "Deliver X",
                "--acceptance",
                "tests pass",
                "--authority",
                "non-destructive verification",
                "--scope",
                "worker-a=backend only",
                "--write-owner",
                "worker-a=src/backend",
                "--progress-deadline",
                "600",
                "--hard-deadline",
                "1800",
                "--max-attempts",
                "2",
                "--json",
            ).stdout
        )
        self.assertEqual(state["task_map"], {"worker-a": "backend only"})
        self.assertEqual(state["write_ownership"], {"worker-a": "src/backend"})
        self.assertEqual(state["authority"], ["non-destructive verification"])
        self.assertEqual(state["monitor_policy"]["max_attempts_per_worker"], 2)
        self.assertEqual(state["display_mode"], "hidden")
        self.assertEqual(state["cards_category"], "")

    def test_tick_resets_a_cursor_ahead_of_rebuilt_ledger(self) -> None:
        self.create()
        state_path = self.bus / "leader-sessions" / "demo.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["event_cursor"] = 9999
        state_path.write_text(json.dumps(state), encoding="utf-8")
        self.leader("tick", "demo", "--json")
        updated = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertLess(updated["event_cursor"], 9999)

    def test_expired_old_leader_cannot_resurrect_after_new_leader_closes(self) -> None:
        self.create()
        claims_path = self.bus / "leader-sessions" / "claims.json"
        claims = json.loads(claims_path.read_text(encoding="utf-8"))
        for claim in claims.values():
            claim["lease_until"] = 0
        claims_path.write_text(json.dumps(claims), encoding="utf-8")
        self.leader(
            "create",
            "--id",
            "replacement",
            "--leader",
            "outsider",
            "--worker",
            "worker-a",
            "--objective",
            "Temporary replacement",
        )
        self.leader("close", "replacement", "--status", "cancelled")
        stale_tick = json.loads(self.leader("tick", "demo", "--json", check=False).stdout)
        self.assertTrue(any(issue.startswith("claim_lost:") for issue in stale_tick["issues"]))


    # --- audit regressions -------------------------------------

    def ledger(self, code: str) -> str:
        script = f"import sys; sys.path.insert(0, {str(ROOT / 'scripts')!r}); import event_ledger as L\n{code}"
        return run_cmd(["python3", "-c", script], env=self.env).stdout

    def ledger_job(self, job_id: str) -> dict:
        return json.loads((self.bus / "event-ledger" / "jobs" / f"{job_id}.json").read_text(encoding="utf-8"))

    def ledger_events(self, job_id: str) -> list[dict]:
        lines = (self.bus / "event-ledger" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        return [event for event in map(json.loads, lines) if event.get("job_id") == job_id]

    def assign_single(self, leader_id: str = "solo", task: str = "echo attempt") -> str:
        return json.loads(
            self.leader(
                "assign", leader_id, "--worker", "worker-a", "--repo", str(self.repo), "--task", task, "--json"
            ).stdout
        )["job_id"]

    def test_control_events_do_not_revive_a_failed_job(self) -> None:
        # Before: keys/approve appended status=running for the failed job and
        # Cards replay brought it back to life.
        self.leader("config", "--trusted-owner", "true")
        self.create_single()
        job_id = self.assign_single()
        self.ledger(f"L.upsert_job({job_id!r}, status='failed', message='worker crashed')")
        self.leader("keys", "solo", "--worker", "worker-a", "--key", "Escape", "--json")
        self.leader("approve", "solo", "--worker", "worker-a", "--key", "Enter", "--json")
        self.assertEqual(self.ledger_job(job_id)["status"], "failed")
        replayed = ""
        for event in self.ledger_events(job_id):
            if event.get("status"):
                replayed = event["status"]
            if event["kind"] == "leader_control":
                self.assertEqual(event["status"], "")
        self.assertEqual(replayed, "failed")

    def test_close_cancels_the_leaders_still_active_attempts(self) -> None:
        # Before: close only terminalized leader:<id>; the child job stayed
        # active forever with no owner.
        self.create_single()
        job_id = self.assign_single(task="sleep 30")
        closed = json.loads(self.leader("close", "solo", "--status", "cancelled", "--json").stdout)
        self.assertEqual(closed["cancelled_jobs"], [job_id])
        job = self.ledger_job(job_id)
        self.assertEqual(job["status"], "cancelled")
        self.assertIn("closed as cancelled", job["message"])

    def test_takeover_cancels_the_superseded_leaders_active_attempts(self) -> None:
        self.create_single()
        job_id = self.assign_single(task="sleep 30")
        self.leader(
            "create", "--id", "successor", "--leader", "outsider", "--worker", "worker-a",
            "--objective", "take over", "--takeover", "--json",
        )
        self.assertEqual(json.loads(self.leader("status", "solo", "--json").stdout)["status"], "superseded")
        job = self.ledger_job(job_id)
        self.assertEqual(job["status"], "cancelled")
        self.assertIn("superseded by successor", job["message"])

    def test_abandon_is_the_way_out_when_the_worker_pane_is_gone(self) -> None:
        # Before: after the pane vanished every control said "run doctor" and
        # nothing could terminalize the attempt.
        state = self.create_single()
        job_id = self.assign_single()
        time.sleep(0.2)
        alive = self.leader("abandon", "solo", "--worker", "worker-a", "--reason", "test", check=False)
        self.assertNotEqual(alive.returncode, 0)
        self.assertIn("still alive", alive.stderr + alive.stdout)
        self.assertNotEqual(self.ledger_job(job_id)["status"], "interrupted")

        run_cmd(["tmux", "kill-pane", "-t", state["workers"]["worker-a"]["pane_id"]])
        refused = self.leader("verify", "solo", "--worker", "worker-a", "--evidence", "x", check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("secretary-bus leader abandon solo --worker worker-a", refused.stderr + refused.stdout)

        abandoned = json.loads(
            self.leader("abandon", "solo", "--worker", "worker-a", "--reason", "pane closed by user", "--json").stdout
        )
        self.assertEqual(abandoned["terminalized_jobs"], [job_id])
        self.assertIn("no longer exists", abandoned["evidence"])
        job = self.ledger_job(job_id)
        self.assertEqual(job["status"], "interrupted")
        self.assertIn("pane closed by user", job["message"])
        after = json.loads(self.leader("status", "solo", "--json").stdout)
        self.assertTrue(after["workers"]["worker-a"]["retired"])
        claims = json.loads((self.bus / "leader-sessions" / "claims.json").read_text(encoding="utf-8"))
        self.assertNotIn(state["workers"]["worker-a"]["pane_id"], claims)
        ticked = json.loads(self.leader("tick", "solo", "--json").stdout)
        self.assertEqual(ticked["issues"], [])
        again = json.loads(self.leader("abandon", "solo", "--worker", "worker-a", "--reason", "again", "--json").stdout)
        self.assertTrue(again["duplicate"])

    def test_completed_close_accepts_an_abandoned_worker_but_not_an_unverified_one(self) -> None:
        state = self.create()
        job_a = self.assign_single("demo", task="echo a")
        job_b = json.loads(
            self.leader(
                "assign", "demo", "--worker", "worker-b", "--repo", str(self.repo), "--task", "echo b", "--json"
            ).stdout
        )["job_id"]
        self.ledger(f"L.upsert_job({job_b!r}, status='failed', message='crashed')")
        run_cmd(["tmux", "kill-pane", "-t", state["workers"]["worker-a"]["pane_id"]])
        self.leader("abandon", "demo", "--worker", "worker-a", "--reason", "pane closed by user")
        self.assertEqual(json.loads(self.leader("tick", "demo", "--json").stdout)["phase"], "verifying")
        refused = self.leader("close", "demo", "--status", "completed", "--evidence", "checked", check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn(f"worker-b: attempt {job_b} status=failed not verified", refused.stderr)
        self.assertNotIn("worker-a:", refused.stderr)
        self.assertEqual(self.ledger_job(job_a)["status"], "interrupted")
        # Non-completed closes are not gated by verification.
        self.assertEqual(
            json.loads(self.leader("close", "demo", "--status", "failed", "--json").stdout)["status"], "failed"
        )

    def test_a_killed_worker_can_be_accounted_for_with_a_reason(self) -> None:
        # Before: a worker this leader killed could be neither verified nor
        # abandoned, so the leader could never close as completed.
        self.leader("config", "--trusted-owner", "true")
        self.create_single()
        self.leader("assign", "solo", "--worker", "worker-a", "--repo", str(self.repo), "--task", "echo a", "--json")
        time.sleep(0.1)
        self.leader("kill", "solo", "--worker", "worker-a", "--action-key", "kill-a", "--json")
        self.leader("tick", "solo", "--json", check=False)
        refused = self.leader("close", "solo", "--status", "completed", "--evidence", "checked", check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("worker-a", refused.stderr)
        out = json.loads(self.leader("abandon", "solo", "--worker", "worker-a", "--reason", "duplicate work", "--json").stdout)
        self.assertTrue(out.get("after_kill"))
        closed = self.leader("close", "solo", "--status", "completed", "--evidence", "checked", "--json", check=False)
        self.assertEqual(closed.returncode, 0, closed.stderr)

    def test_a_persisting_identity_issue_is_not_a_new_change(self) -> None:
        # Before: every tick reported changed=true, so `leader watch` returned
        # immediately and the leader's monitor loop spun.
        state = self.create_single()
        run_cmd(["tmux", "kill-pane", "-t", state["workers"]["worker-a"]["pane_id"]])
        first = json.loads(self.leader("tick", "solo", "--json", check=False).stdout)
        self.assertTrue(first["changed"])
        self.assertTrue(first["issues"])
        second = json.loads(self.leader("tick", "solo", "--json", check=False).stdout)
        self.assertFalse(second["changed"])
        self.assertEqual(second["issues"], first["issues"])
        started = time.time()
        watched = self.leader("watch", "solo", "--timeout", "1", "--interval", "0.2", "--json", check=False)
        self.assertGreaterEqual(time.time() - started, 0.9)
        self.assertEqual(json.loads(watched.stdout)["reason"], "timeout")
        self.assertEqual(watched.returncode, 1)

    def test_idle_tick_writes_neither_leader_state_nor_claims(self) -> None:
        # Before: each tick bumped the revision and rewrote claims.json.
        self.create_single()
        self.leader("tick", "solo", "--json")
        state_path = self.bus / "leader-sessions" / "solo.json"
        claims_path = self.bus / "leader-sessions" / "claims.json"
        revision = json.loads(state_path.read_text(encoding="utf-8"))["revision"]
        claims_mtime = claims_path.stat().st_mtime_ns
        for _ in range(3):
            self.assertFalse(json.loads(self.leader("tick", "solo", "--json").stdout)["changed"])
        self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["revision"], revision)
        self.assertEqual(claims_path.stat().st_mtime_ns, claims_mtime)

    def test_rejected_task_text_does_not_consume_an_attempt(self) -> None:
        # Before: the ledger job was created before the text check, the failed
        # attempt was kept and counted against --max-attempts.
        self.leader(
            "create", "--id", "once", "--leader", "leader", "--worker", "worker-a",
            "--objective", "single attempt budget", "--max-attempts", "1", "--json",
        )
        bad = self.leader(
            "assign", "once", "--worker", "worker-a", "--repo", str(self.repo), "--task", "bad \x1b[31m", check=False
        )
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("control character", bad.stderr + bad.stdout)
        state = json.loads(self.leader("status", "once", "--json").stdout)
        self.assertEqual(state["workers"]["worker-a"]["job_ids"], [])
        self.assertTrue(self.assign_single("once").startswith("lead-once-worker-a-"))

    def test_assign_accepts_a_task_file(self) -> None:
        self.create_single()
        task_file = self.root / "task.md"
        task_file.write_text("line one\nline two\n", encoding="utf-8")
        assigned = json.loads(
            self.leader(
                "assign", "solo", "--worker", "worker-a", "--repo", str(self.repo),
                "--task-file", str(task_file), "--json",
            ).stdout
        )
        job_dir = self.bus / "secretary-jobs" / assigned["job_id"]
        self.assertEqual(
            next(job_dir.glob("task-*.md")).read_text(encoding="utf-8"), "line one\nline two\n"
        )


if __name__ == "__main__":
    unittest.main()
