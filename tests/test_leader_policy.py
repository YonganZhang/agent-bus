#!/usr/bin/env python3
"""Leader deadlines and automatic answering of worker permission dialogs.

Every pane here belongs to a tmux session this test creates; worker dialogs
are drawn by a small fake program that records the keys it receives.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "scripts" / "cli_bridge.py"
LEADER = ROOT / "scripts" / "leader.py"
SESSION = f"agent-bus-leader-policy-test-{os.getpid()}"

FAKE_DIALOG = r'''
import os, sys, termios, time, tty

kind, out = sys.argv[1], sys.argv[2]
RULE = "─" * 60
if kind == "permission":
    header = ["● Bash(git push)", RULE, " Bash command", "", "   git push origin main", "", " Do you want to proceed?"]
    rows = ["Yes", "Yes, and don't ask again for git commands in /repo", "No, and tell Claude what to do differently"]
    footer = ["", " Esc to cancel · Tab to amend"]
else:
    header = [RULE, " Which database should the service use?", ""]
    rows = ["PostgreSQL", "SQLite"]
    footer = ["", " Enter to select · Esc to cancel"]
selected = 0


def draw():
    marks = [("   ❯" if i == selected else "    ") + f" {i + 1}. {text}" for i, text in enumerate(rows)]
    sys.stdout.write("\x1b[2J\x1b[H" + "\r\n".join(header + marks + footer))
    sys.stdout.flush()


fd = sys.stdin.fileno()
saved = termios.tcgetattr(fd)
tty.setraw(fd)
answer = None
try:
    draw()
    while answer is None:
        data = os.read(fd, 32)
        with open(out + ".keys", "ab") as handle:
            handle.write(data + b"|")
        if data in (b"\x1b[B", b"\x1bOB"):
            selected = min(selected + 1, len(rows) - 1)
        elif data in (b"\x1b[A", b"\x1bOA"):
            selected = max(selected - 1, 0)
        elif data in (b"\r", b"\n"):
            answer = rows[selected]
        draw()
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, saved)
with open(out, "w", encoding="utf-8") as handle:
    handle.write(answer)
sys.stdout.write("\x1b[2J\x1b[Hanswered\r\n")
sys.stdout.flush()
time.sleep(3600)
'''


def run_cmd(args: list[str], *, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, env=env, check=check)


@unittest.skipIf(shutil.which("tmux") is None, "tmux is required")
class LeaderPolicyTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bus = self.root / "bus"
        self.fake = self.root / "fake_dialog.py"
        self.fake.write_text(FAKE_DIALOG, encoding="utf-8")
        self.env = os.environ.copy()
        self.env["AGENT_BUS_DIR"] = str(self.bus)
        run_cmd(["tmux", "kill-session", "-t", SESSION], check=False)
        run_cmd(["tmux", "new-session", "-d", "-s", SESSION, "-n", "leader", "-c", str(self.root), "bash --noprofile --norc"])
        self.windows = 1

    def tearDown(self) -> None:
        run_cmd(["tmux", "kill-session", "-t", SESSION], check=False)
        self.tmp.cleanup()

    def add_worker(self, name: str, command: str, expected_command: str) -> None:
        index = self.windows
        self.windows += 1
        run_cmd(["tmux", "new-window", "-d", "-t", SESSION, "-n", name, "-c", str(self.root), command])
        time.sleep(0.6)
        self.register(name, f"{SESSION}:{index}.0", expected_command)

    def add_dialog_worker(self, name: str, kind: str) -> Path:
        out = self.root / f"{name}.answer"
        command = f"{shlex.quote(sys.executable)} {shlex.quote(str(self.fake))} {kind} {shlex.quote(str(out))}"
        self.add_worker(name, command, "")
        return out

    def register(self, name: str, pane: str, expected_command: str) -> None:
        # An empty expected_command records the live command (the fake dialog's interpreter).
        expected = ["--expected-command", expected_command] if expected_command else []
        run_cmd(["python3", str(BRIDGE), "register", "--name", name, "--pane", pane, *expected, "--shell"], env=self.env)

    def leader(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return run_cmd(["python3", str(LEADER), *args], env=self.env, check=check)

    def create(self, *workers: str, extra: tuple[str, ...] = ()) -> dict:
        self.register("leader", f"{SESSION}:0.0", "bash")
        argv = ["create", "--hidden", "--id", "demo", "--leader", "leader"]
        for worker in workers:
            argv += ["--worker", worker]
        argv += ["--objective", "policy test", *extra, "--json"]
        return json.loads(self.leader(*argv).stdout)

    def events(self, kind: str) -> list[dict]:
        path = self.bus / "event-ledger" / "events.jsonl"
        if not path.exists():
            return []
        return [event for event in map(json.loads, path.read_text(encoding="utf-8").splitlines()) if event["kind"] == kind]

    def keys_received(self, out: Path) -> bytes:
        path = Path(str(out) + ".keys")
        return path.read_bytes() if path.exists() else b""


class WorkerPromptAutoAnswerTest(LeaderPolicyTestBase):
    PERMISSIVE = "Yes, and don't ask again for git commands in /repo"

    def setUp(self) -> None:
        super().setUp()
        # Auto-approve is opt-in; these tests exercise the enabled policy.
        enabled = json.loads(self.leader("config", "--auto-approve-permissions", "true", "--json").stdout)
        self.assertTrue(enabled["auto_approve_permissions"])

    def test_tick_does_nothing_by_default(self) -> None:
        (self.bus / "config.json").unlink()
        out = self.add_dialog_worker("worker-a", "permission")
        self.create("worker-a")
        payload = self.tick()
        time.sleep(0.2)
        self.assertFalse(out.exists())
        self.assertEqual(self.keys_received(out), b"")
        self.assertFalse([c for c in payload["changes"] if str(c.get("kind", "")).startswith("worker_prompt_auto")])

    def tick(self) -> dict:
        return json.loads(self.leader("tick", "demo", "--force-probe", "--json", check=False).stdout)

    def test_tick_auto_answers_a_permission_dialog_and_records_it(self) -> None:
        # Before: tick never looked at a needs_input worker, so the permission
        # prompt waited for a human and nothing was recorded.
        out = self.add_dialog_worker("worker-a", "permission")
        self.create("worker-a")
        payload = self.tick()
        time.sleep(0.3)
        self.assertEqual(out.read_text(encoding="utf-8"), self.PERMISSIVE)
        self.assertIn("worker_prompt_auto_answered", [change.get("kind") for change in payload["changes"]])
        [event] = self.events("worker_prompt_auto_answered")
        self.assertEqual(event["target"], "worker-a")
        self.assertEqual(event["data"]["kind"], "permission")
        self.assertEqual(event["data"]["answer"], self.PERMISSIVE)
        self.assertEqual(event["data"]["question"], "Do you want to proceed?")
        # One Down to the permissive row, then Enter; verified between steps.
        self.assertEqual(self.keys_received(out), b"\x1b[B|\r|")

    def test_tick_leaves_a_question_about_the_work_to_the_user_once(self) -> None:
        out = self.add_dialog_worker("worker-a", "question")
        self.create("worker-a")
        first = self.tick()
        second = self.tick()
        time.sleep(0.2)
        self.assertFalse(out.exists())
        self.assertEqual(self.keys_received(out), b"")
        self.assertIn("worker_prompt_needs_user", [change.get("kind") for change in first["changes"]])
        self.assertNotIn("worker_prompt_needs_user", [change.get("kind") for change in second["changes"]])
        [event] = self.events("worker_prompt_needs_user")
        self.assertEqual(event["data"]["kind"], "question")
        self.assertEqual(event["data"]["options"], ["PostgreSQL", "SQLite"])
        self.assertEqual(self.events("worker_prompt_auto_answered"), [])

    def test_tick_does_not_answer_when_the_owner_turned_it_off(self) -> None:
        out = self.add_dialog_worker("worker-a", "permission")
        self.create("worker-a")
        config = json.loads(self.leader("config", "--auto-approve-permissions", "false", "--json").stdout)
        self.assertFalse(config["auto_approve_permissions"])
        payload = self.tick()
        time.sleep(0.2)
        self.assertFalse(out.exists())
        self.assertEqual(self.keys_received(out), b"")
        self.assertFalse([c for c in payload["changes"] if str(c.get("kind", "")).startswith("worker_prompt")])

    def test_approve_without_key_answers_by_policy_not_enter(self) -> None:
        # Before: approve pressed Enter, which confirmed the highlighted "Yes"
        # (row 1) instead of the owner's standing "don't ask again" answer.
        out = self.add_dialog_worker("worker-a", "permission")
        self.create("worker-a")
        approved = json.loads(self.leader("approve", "demo", "--worker", "worker-a", "--yes", "--json").stdout)
        time.sleep(0.3)
        self.assertTrue(approved["sent"])
        self.assertEqual(approved["answer"], self.PERMISSIVE)
        self.assertEqual(approved["dialog_kind"], "permission")
        self.assertEqual(out.read_text(encoding="utf-8"), self.PERMISSIVE)
        self.assertEqual(self.keys_received(out), b"\x1b[B|\r|")

    def test_approve_refuses_a_question_and_sends_nothing(self) -> None:
        out = self.add_dialog_worker("worker-a", "question")
        self.create("worker-a")
        refused = self.leader("approve", "demo", "--worker", "worker-a", "--yes", check=False)
        time.sleep(0.2)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("question about the work", refused.stderr)
        self.assertEqual(self.keys_received(out), b"")

    def test_approve_without_a_dialog_does_not_press_enter(self) -> None:
        # Before: approve sent a blind Enter, submitting whatever was typed.
        self.add_worker("worker-a", "bash --noprofile --norc", "bash")
        self.create("worker-a")
        marker = self.root / "blind-enter.txt"
        run_cmd(["tmux", "send-keys", "-t", f"{SESSION}:1.0", "-l", f"touch {shlex.quote(str(marker))}"])
        refused = self.leader("approve", "demo", "--worker", "worker-a", "--yes", check=False)
        time.sleep(0.3)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("no dialog", refused.stderr)
        self.assertFalse(marker.exists())
        raw = json.loads(
            self.leader("approve", "demo", "--worker", "worker-a", "--key", "Enter", "--yes", "--json").stdout
        )
        time.sleep(0.3)
        self.assertTrue(raw["sent"])
        self.assertTrue(marker.exists())


class LeaderDeadlineTest(LeaderPolicyTestBase):
    def test_hard_deadline_and_progress_stall_are_reported_once_without_acting(self) -> None:
        # Before: both deadlines were stored in monitor_policy and never read.
        self.add_worker("worker-a", "bash --noprofile --norc", "bash")
        self.create("worker-a", extra=("--hard-deadline", "2", "--progress-deadline", "1"))
        assigned = json.loads(
            self.leader(
                "assign", "demo", "--worker", "worker-a", "--repo", str(self.root), "--task", "echo working", "--yes", "--json"
            ).stdout
        )
        first = json.loads(self.leader("tick", "demo", "--json").stdout)
        self.assertNotIn("leader_progress_stalled", [c.get("kind") for c in first["changes"]])
        time.sleep(2.3)
        crossed = json.loads(self.leader("tick", "demo", "--json").stdout)
        kinds = [c.get("kind") for c in crossed["changes"]]
        self.assertIn("leader_progress_stalled", kinds)
        self.assertIn("leader_deadline_exceeded", kinds)
        again = json.loads(self.leader("tick", "demo", "--json").stdout)
        self.assertFalse(again["changed"])
        self.assertEqual(len(self.events("leader_deadline_exceeded")), 1)
        self.assertEqual(len(self.events("leader_progress_stalled")), 1)

        status = json.loads(self.leader("status", "demo", "--json").stdout)
        self.assertEqual(status["status"], "running")
        self.assertTrue(status["deadlines"]["hard_deadline_exceeded"])
        self.assertIn("worker-a", status["deadlines"]["stalled_workers"])
        doctor = json.loads(self.leader("doctor", "demo", "--json", check=False).stdout)
        self.assertTrue(doctor["deadlines"]["hard_deadline_exceeded"])
        job = json.loads((self.bus / "event-ledger" / "jobs" / f"{assigned['job_id']}.json").read_text(encoding="utf-8"))
        self.assertNotIn(job["status"], {"interrupted", "cancelled", "failed"})


if __name__ == "__main__":
    unittest.main()
