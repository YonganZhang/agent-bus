#!/usr/bin/env python3
"""Integration test for the top-level agent-bus CLI wrapper (alias: secretary-bus)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


CLI = Path(__file__).resolve().parents[1] / "bin" / "agent-bus"
SESSION = f"secretary-bus-cli-wrapper-test-{os.getpid()}"


def run_cmd(args: list[str], *, env: dict[str, str] | None = None, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, env=env, cwd=str(cwd) if cwd else None, check=check)


@unittest.skipIf(shutil.which("tmux") is None, "tmux is required")
class SecretaryBusCliTest(unittest.TestCase):
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

    def tearDown(self) -> None:
        run_cmd(["tmux", "kill-session", "-t", SESSION], check=False)
        self.tmp.cleanup()

    def bus_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return run_cmd([str(CLI), *args], env=self.env)

    def test_wrapper_exposes_codex_help(self) -> None:
        help_text = self.bus_cli("codex", "--help").stdout
        self.assertIn("Codex controller", help_text)
        self.assertIn("status", help_text)
        self.assertIn("wait", help_text)
        self.assertIn("watch", help_text)
        self.assertIn("interrupt", help_text)
        self.assertIn("dashboard", help_text)
        self.assertIn("--no-mcp", help_text)

    def test_retired_codexd_exits_2_with_replacement_hint(self) -> None:
        for alias in ("codexd", "codex-daemon"):
            result = run_cmd([str(CLI), alias, "worker-list"], env=self.env, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn("retired", result.stderr)
            self.assertIn("codex start", result.stderr)
            self.assertIn("--wait", result.stderr)
            self.assertIn("leader", result.stderr)

    def test_wrapper_accepts_help_alias(self) -> None:
        help_text = self.bus_cli("help").stdout
        self.assertIn("secretary-bus", help_text)
        self.assertNotIn("codexd", help_text)
        self.assertIn("--wait is required", help_text)
        self.assertIn("targets | register | send", help_text)
        self.assertIn("leader create", help_text)

    def test_wrapper_exposes_generic_leader_help(self) -> None:
        help_text = self.bus_cli("leader", "--help").stdout
        self.assertIn("provider-neutral leader sessions", help_text)
        self.assertIn("create", help_text)
        self.assertIn("assign", help_text)
        self.assertIn("tick", help_text)
        self.assertIn("watch", help_text)
        self.assertIn("doctor", help_text)

    def test_wrapper_exposes_provider_state_and_leader_daemon_help(self) -> None:
        provider_help = self.bus_cli("provider-state", "--help").stdout
        self.assertIn("provider", provider_help.lower())
        self.assertIn("--max-chars", provider_help)
        daemon_help = self.bus_cli("leaderd", "--help").stdout
        self.assertIn("start", daemon_help)
        self.assertIn("status", daemon_help)
        self.assertIn("stop", daemon_help)

    def test_wrapper_exposes_cards_organization_help(self) -> None:
        help_text = self.bus_cli("cards", "--help").stdout
        self.assertIn("AI Session Cards", help_text)
        self.assertIn("favorite", help_text)
        self.assertIn("unfavorite", help_text)
        self.assertIn("category", help_text)
        self.assertIn("uncategorize", help_text)

    def test_archived_agent_view_commands_are_rejected(self) -> None:
        result = run_cmd([str(CLI), "agents", "--help"], env=self.env, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("retired Agent View/workspace-sessions/governor model", result.stderr + result.stdout)

    def test_wrapper_dispatches_supervisor_flow(self) -> None:
        self.bus_cli("register", "--name", "worker", "--pane", f"{SESSION}:0.0", "--expected-command", "bash", "--shell")
        started = self.bus_cli(
            "start",
            "--target",
            "worker",
            "--repo",
            str(self.repo),
            "--task",
            "printf 'wrapper-change\\n' >> README.md && echo WRAPPER_DONE",
            "--yes",
        )
        job_id = started.stdout.split("job id=", 1)[1].splitlines()[0]
        time.sleep(0.4)
        self.bus_cli("collect", job_id)
        job_dir = self.bus / "secretary-jobs" / job_id
        self.assertIn("WRAPPER_DONE", (job_dir / "response-since-start.txt").read_text(encoding="utf-8"))
        self.assertIn("+wrapper-change", (job_dir / "git-diff-worktree-since-baseline.patch").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
