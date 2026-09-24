#!/usr/bin/env python3
"""Per-card Git status on /api/panes: repo detection, cached background
refresh, timeout degradation and TOP in-progress task lookup."""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import server
from plan_stub import enable_plan_integration, wait_plan_index


GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}

PLAN_TEMPLATE = """# Task Plan

## Active Work

- [x] P1.1 finished thing · state=done
- [ ] P1.2 current thing · state={state}
- [ ] P1.3 later thing · state=pending

## Notes
"""


def git(cwd: Path | str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=GIT_ENV, timeout=10,
    ).stdout


def make_repo(path: Path, *, commit: bool = True, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", branch)
    if commit:
        (path / "a.txt").write_text("a\n", encoding="utf-8")
        git(path, "add", "a.txt")
        git(path, "commit", "-q", "-m", "first commit " + "x" * 80)
    return path


def reset_git_caches() -> None:
    with server._GIT_LOCK:
        server._GIT_STATUS_CACHE.clear()
        server._GIT_STATUS_PENDING.clear()
        server._GIT_ROOT_CACHE.clear()
        server._TOP_PLAN_CACHE.clear()
        server._TOP_PLAN_PENDING.clear()


def wait_refreshed(root: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with server._GIT_LOCK:
            if root not in server._GIT_STATUS_PENDING and root in server._GIT_STATUS_CACHE:
                return
        time.sleep(0.02)
    raise AssertionError(f"git status for {root} never refreshed")


class GitStatusTestBase(unittest.TestCase):
    def setUp(self) -> None:
        reset_git_caches()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(reset_git_caches)
        self.base = Path(self.tmp.name).resolve()
        # compute_git_status inherits os.environ: keep the user's git config out.
        patcher = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)


class GitRootAndStatusTest(GitStatusTestBase):
    def test_non_repo_pane_gets_no_git_field(self) -> None:
        plain = self.base / "plain"
        plain.mkdir()
        self.assertIsNone(server.git_root_for_cwd(str(plain)))
        self.assertIsNone(server.pane_git_summary(str(plain)))
        self.assertIsNone(server.pane_git_summary(""))

    def test_root_found_from_subdirectory_and_worktree_file(self) -> None:
        repo = make_repo(self.base / "repo")
        sub = repo / "src" / "deep"
        sub.mkdir(parents=True)
        self.assertEqual(server.git_root_for_cwd(str(sub)), str(repo))
        wt = self.base / "wt"
        git(repo, "worktree", "add", "-q", "-b", "side", str(wt))
        self.assertTrue((wt / ".git").is_file())
        self.assertEqual(server.git_root_for_cwd(str(wt)), str(wt))

    def test_status_fields_without_remote(self) -> None:
        repo = make_repo(self.base / "repo")
        (repo / "a.txt").write_text("changed\n", encoding="utf-8")
        (repo / "new1.txt").write_text("n\n", encoding="utf-8")
        (repo / "new2.txt").write_text("n\n", encoding="utf-8")
        status = server.compute_git_status(str(repo))
        self.assertEqual(status["state"], "ok")
        self.assertEqual(status["branch"], "main")
        self.assertEqual(status["dirty"], 3)
        self.assertEqual(status["modified"], 1)
        self.assertEqual(status["untracked"], 2)
        self.assertIsNone(status["ahead"])
        self.assertIs(status["has_remote"], False)
        self.assertEqual(len(status["last_commit_subject"]), 60)
        self.assertTrue(status["last_commit_subject"].startswith("first commit"))
        self.assertLessEqual(abs(time.time() - status["last_commit_ts"]), 120)

    def test_ahead_counts_commits_over_upstream(self) -> None:
        remote = self.base / "remote.git"
        git(self.base, "init", "-q", "--bare", str(remote))
        repo = make_repo(self.base / "repo")
        git(repo, "remote", "add", "origin", str(remote))
        git(repo, "push", "-q", "-u", "origin", "main")
        (repo / "b.txt").write_text("b\n", encoding="utf-8")
        git(repo, "add", "b.txt")
        git(repo, "commit", "-q", "-m", "second")
        git(repo, "commit", "-q", "--allow-empty", "-m", "third")
        status = server.compute_git_status(str(repo))
        self.assertEqual(status["ahead"], 2)
        self.assertIs(status["has_remote"], True)
        self.assertEqual(status["dirty"], 0)
        # a remote without upstream tracking -> ahead unknown (null), remote still true
        git(repo, "checkout", "-q", "-b", "local-only")
        status = server.compute_git_status(str(repo))
        self.assertIsNone(status["ahead"])
        self.assertIs(status["has_remote"], True)

    def test_dirty_count_is_capped(self) -> None:
        repo = make_repo(self.base / "repo")
        for index in range(5):
            (repo / f"u{index}.txt").write_text("u\n", encoding="utf-8")
        with mock.patch.object(server, "GIT_DIRTY_CAP", 3):
            status = server.compute_git_status(str(repo))
        self.assertEqual((status["dirty"], status["modified"], status["untracked"]), (3, 0, 3))

    def test_modified_and_untracked_are_capped_separately(self) -> None:
        repo = make_repo(self.base / "repo")
        for index in range(4):
            (repo / f"t{index}.txt").write_text("t\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "tracked")
        for index in range(4):
            (repo / f"t{index}.txt").write_text("changed\n", encoding="utf-8")
        (repo / "u1.txt").write_text("u\n", encoding="utf-8")
        with mock.patch.object(server, "GIT_DIRTY_CAP", 3):
            status = server.compute_git_status(str(repo))
        # each counter capped on its own; dirty = min(4 + 1, cap)
        self.assertEqual((status["modified"], status["untracked"], status["dirty"]), (3, 1, 3))

    def test_modified_counts_staged_renamed_deleted_and_conflicted(self) -> None:
        repo = make_repo(self.base / "repo")
        for name in ("b.txt", "c.txt", "d.txt"):
            (repo / name).write_text(name + "\n" * 3, encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "more")
        (repo / "a.txt").write_text("staged\n", encoding="utf-8")
        git(repo, "add", "a.txt")                       # "1 M." staged change
        git(repo, "mv", "b.txt", "b2.txt")              # "2 R." rename
        git(repo, "rm", "-q", "c.txt")                  # "1 D." deletion
        (repo / "new.txt").write_text("n\n", encoding="utf-8")
        git(repo, "add", "new.txt")                     # "1 A." added = tracked
        (repo / "loose.txt").write_text("l\n", encoding="utf-8")  # "? " untracked
        status = server.compute_git_status(str(repo))
        self.assertEqual((status["modified"], status["untracked"], status["dirty"]), (4, 1, 5))

        conflict = make_repo(self.base / "conflict")
        git(conflict, "checkout", "-q", "-b", "side")
        (conflict / "a.txt").write_text("side\n", encoding="utf-8")
        git(conflict, "commit", "-q", "-am", "side")
        git(conflict, "checkout", "-q", "main")
        (conflict / "a.txt").write_text("main\n", encoding="utf-8")
        git(conflict, "commit", "-q", "-am", "main")
        subprocess.run(["git", "-C", str(conflict), "merge", "-q", "side"], capture_output=True, env=GIT_ENV, timeout=10)
        self.assertIn("u ", git(conflict, "status", "--porcelain=v2"))
        status = server.compute_git_status(str(conflict))
        self.assertEqual((status["modified"], status["untracked"], status["dirty"]), (1, 0, 1))

    def test_unborn_branch_and_detached_head(self) -> None:
        empty = make_repo(self.base / "empty", commit=False)
        status = server.compute_git_status(str(empty))
        self.assertEqual(status["state"], "ok")
        self.assertEqual(status["branch"], "main")
        self.assertIsNone(status["last_commit_ts"])
        repo = make_repo(self.base / "repo")
        sha = git(repo, "rev-parse", "HEAD").strip()
        git(repo, "checkout", "-q", "--detach")
        self.assertEqual(server.compute_git_status(str(repo))["branch"], "@" + sha[:7])

    def test_git_failure_becomes_unknown(self) -> None:
        not_repo = self.base / "fake"
        (not_repo / ".git").mkdir(parents=True)  # looks like a repo, git disagrees
        status = server.compute_git_status(str(not_repo))
        self.assertEqual(status["state"], "unknown")
        self.assertIn("git status", status["error"])


class GitTimeoutTest(GitStatusTestBase):
    def test_slow_git_times_out_and_unknown_is_cached(self) -> None:
        repo = make_repo(self.base / "repo")
        bindir = self.base / "bin"
        bindir.mkdir()
        fake = bindir / "git"
        fake.write_text("#!/bin/sh\nexec sleep 5\n", encoding="utf-8")
        fake.chmod(0o755)
        calls = []
        real_compute = server.compute_git_status

        def counting(root: str) -> dict:
            calls.append(root)
            return real_compute(root)

        with mock.patch.dict(os.environ, {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}), \
                mock.patch.object(server, "GIT_CMD_TIMEOUT", 0.3), \
                mock.patch.object(server, "compute_git_status", counting):
            started = time.monotonic()
            self.assertEqual(server.pane_git_summary(str(repo))["state"], "pending")
            self.assertLess(time.monotonic() - started, 0.2)
            wait_refreshed(str(repo))
            summary = server.pane_git_summary(str(repo))
            again = server.pane_git_summary(str(repo))
        self.assertEqual(summary["state"], "unknown")
        self.assertIn("timeout", summary["error"])
        self.assertIsNone(summary["dirty"])
        self.assertEqual(again["state"], "unknown")
        self.assertEqual(len(calls), 1, "unknown must be cached for the TTL, not retried every poll")


class GitCacheTest(GitStatusTestBase):
    def test_panes_in_same_repo_share_one_cached_computation(self) -> None:
        repo = make_repo(self.base / "repo")
        (repo / "sub").mkdir()
        calls = []
        real_compute = server.compute_git_status

        def counting(root: str) -> dict:
            calls.append(root)
            return real_compute(root)

        with mock.patch.object(server, "compute_git_status", counting):
            first = server.pane_git_summary(str(repo))
            server.pane_git_summary(str(repo / "sub"))
            self.assertEqual(first["state"], "pending")
            wait_refreshed(str(repo))
            a = server.pane_git_summary(str(repo))
            b = server.pane_git_summary(str(repo / "sub"))
        self.assertEqual(calls, [str(repo)])
        self.assertEqual(a["state"], "ok")
        self.assertEqual(a["branch"], "main")
        self.assertEqual(b["root"], str(repo))
        self.assertIsInstance(a["last_commit_age"], int)
        self.assertNotIn("last_commit_ts", a)

    def test_expired_entry_serves_stale_while_refreshing(self) -> None:
        repo = make_repo(self.base / "repo")
        server.pane_git_summary(str(repo))
        wait_refreshed(str(repo))
        with server._GIT_LOCK:
            ts, status = server._GIT_STATUS_CACHE[str(repo)]
            server._GIT_STATUS_CACHE[str(repo)] = (ts - server.GIT_STATUS_TTL - 1, status)
        (repo / "dirty.txt").write_text("d\n", encoding="utf-8")
        stale = server.pane_git_summary(str(repo))
        self.assertEqual(stale["dirty"], 0)
        wait_refreshed(str(repo))
        fresh = server.pane_git_summary(str(repo))
        self.assertEqual((fresh["dirty"], fresh["modified"], fresh["untracked"]), (1, 0, 1))

    def test_build_panes_response_does_not_block_on_git(self) -> None:
        repo = make_repo(self.base / "repo")
        pane = server.Pane(
            pane_id="%1", target="s:1.0", session="s", window_index=1, pane_index=0,
            window_name="w", command="claude", cwd=str(repo), title="", active=False, kind="Claude",
            project="", preview="", status="idle", ai_alive=False,
        )
        plain = server.Pane(**{**pane.__dict__, "pane_id": "%2", "cwd": str(self.base)})

        def slow(root: str) -> dict:
            time.sleep(1.5)
            return {"root": root, "state": "ok", "branch": "main", "dirty": 0}

        with mock.patch.object(server, "list_panes", return_value=[pane, plain]), \
                mock.patch.object(server, "latest_jobs_by_pane_cached", return_value={}), \
                mock.patch.object(server, "pane_running_subagents", return_value=[]), \
                mock.patch.object(server, "compute_git_status", slow):
            started = time.monotonic()
            panes = server.build_panes_response("s")
            elapsed = time.monotonic() - started
            wait_refreshed(str(repo))
        self.assertLess(elapsed, 0.5)
        self.assertEqual(panes[0]["git"]["state"], "pending")
        self.assertEqual(panes[0]["git"]["root"], str(repo))
        self.assertNotIn("git", panes[1])


class TopTaskTest(GitStatusTestBase):
    """The in-progress task comes from the configured plan CLI (``plan list``),
    refreshed on the git thread pool and cached by the plan's mtime."""

    def setUp(self) -> None:
        super().setUp()
        enable_plan_integration(self)

    def write_plan(self, root: Path, state: str = "in_progress") -> Path:
        plan = root / "_wiki-methodology" / "_top" / "_task_plan.md"
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text(PLAN_TEMPLATE.format(state=state), encoding="utf-8")
        return plan

    def active(self, cwd: Path, root: Path, branch: object = None) -> str | None:
        server.top_active_task(str(cwd), str(root), branch)  # first call schedules the plan list
        wait_plan_index()
        return server.top_active_task(str(cwd), str(root), branch)

    def test_in_progress_task_is_read_and_cached_by_mtime(self) -> None:
        repo = make_repo(self.base / "repo")
        plan = self.write_plan(repo)
        self.assertEqual(self.active(repo, repo), "P1.2")
        calls: list[list[str]] = []
        real = server.run_plan_cli
        with mock.patch.object(server, "run_plan_cli", side_effect=lambda args, *a, **k: calls.append(args) or real(args, *a, **k)):
            self.assertEqual(server.top_active_task(str(repo), str(repo)), "P1.2")
            self.assertEqual(calls, [], "unchanged plan must not be listed again")
            plan.write_text(PLAN_TEMPLATE.format(state="pending"), encoding="utf-8")
            os.utime(plan, ns=(time.time_ns(), time.time_ns() + 5_000_000_000))
            # A changed plan is re-listed in the background; the stale summary is served meanwhile.
            self.assertEqual(server.top_active_task(str(repo), str(repo)), "P1.2")
            wait_plan_index()
            self.assertIsNone(server.top_active_task(str(repo), str(repo)))
            self.assertEqual(calls, [["plan", "list", str(repo)]])

    def test_hot_path_never_runs_the_cli(self) -> None:
        repo = make_repo(self.base / "repo")
        self.write_plan(repo)
        started = threading.Event()
        release = threading.Event()

        def slow_cli(args, *a, **k):
            started.set()
            release.wait(10)
            return {"items": []}

        with mock.patch.object(server, "run_plan_cli", side_effect=slow_cli):
            t0 = time.monotonic()
            self.assertIsNone(server.top_active_task(str(repo), str(repo)))
            self.assertLess(time.monotonic() - t0, 0.5)
            self.assertTrue(started.wait(5))
            release.set()
            wait_plan_index()

    def test_plan_in_subproject_between_cwd_and_root(self) -> None:
        repo = make_repo(self.base / "mono")
        project = repo / "projects" / "p"
        (project / "src").mkdir(parents=True)
        self.write_plan(project)
        self.assertEqual(self.active(project / "src", repo), "P1.2")
        self.assertIsNone(self.active(repo, repo))

    def test_task_branch_wins(self) -> None:
        repo = make_repo(self.base / "repo")
        self.write_plan(repo)
        self.assertEqual(self.active(repo, repo, "task/P9.1"), "P9.1")
        self.assertEqual(self.active(repo, repo, "task/not-an-id"), "P1.2")

    def test_cli_failure_and_disabled_integration_give_null(self) -> None:
        repo = make_repo(self.base / "repo")
        self.write_plan(repo)
        with mock.patch.object(server, "run_plan_cli", side_effect=server.PlanApiError("bad plan", status=502)):
            self.assertIsNone(self.active(repo, repo))
        reset_git_caches()
        with mock.patch.object(server, "PLAN_CLI", None):
            self.assertIsNone(server.find_top_plan(str(repo), str(repo)))
            self.assertIsNone(server.top_active_task(str(repo), str(repo)))

    def test_summary_carries_task(self) -> None:
        repo = make_repo(self.base / "repo")
        self.write_plan(repo)
        server.pane_git_summary(str(repo))
        wait_refreshed(str(repo))
        wait_plan_index()
        summary = server.pane_git_summary(str(repo))
        self.assertEqual(summary["task"], "P1.2")
        self.assertEqual(summary["task_title"], "current thing")
        self.assertIsNone(summary["task_gate"])
        self.assertIs(summary["has_plan"], True)
        self.assertEqual(
            set(summary),
            {"root", "state", "branch", "dirty", "modified", "untracked", "ahead", "has_remote", "last_commit_age",
             "last_commit_subject", "task", "task_title", "task_gate", "has_plan"},
        )

    def test_without_plan_integration_the_git_row_still_works(self) -> None:
        repo = make_repo(self.base / "repo")
        self.write_plan(repo)
        with mock.patch.object(server, "PLAN_CLI", None):
            server.pane_git_summary(str(repo))
            wait_refreshed(str(repo))
            summary = server.pane_git_summary(str(repo))
        self.assertEqual(summary["state"], "ok")
        self.assertEqual((summary["task"], summary["has_plan"]), (None, False))


if __name__ == "__main__":
    unittest.main()
