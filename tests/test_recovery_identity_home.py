#!/usr/bin/env python3
"""Regression tests for recovery identity fixes.

1. CODEX_HOME comes from the Codex process's own open root rollout, not the
   wrapper's environ; restore/relaunch/verify use and check the recorded home.
2. Recovery pins one baseline; snapshots never promote while recovery runs.
3. Snapshot / Cards / wrapper paths ignore the caller's CODEX_HOME.
4. Live identity (claude agents pid / open root rollout) beats argv; a
   disagreement is recorded as a conflict instead of silently choosing argv.
6. Codex panes get @ai_provider / @ai_codex_home; restart on an exited AI
   points to `recovery relaunch` instead of turning the window into bash.
"""

from __future__ import annotations

import fcntl
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SCRIPT = SCRIPTS / "secretary_recovery.py"
SPEC = importlib.util.spec_from_file_location("secretary_recovery_identity", SCRIPT)
assert SPEC and SPEC.loader
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)

ROOT_ID = "01a00000-0000-7000-8000-000000000101"
CHILD_ID = "01a00000-0000-7000-8000-000000000102"
OLD_ID = "01900000-0000-7000-8000-000000000001"
CLAUDE_OLD = "11111111-2222-4333-8444-555555555555"
CLAUDE_NEW = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"


def write_rollout(home: Path, session_id: str, *, parent: str = "", cwd: str = "/work") -> Path:
    path = home / "sessions" / "2020" / "01" / "01" / f"rollout-2020-01-01T00-00-00-{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {"id": session_id, "cwd": cwd}
    if parent:
        payload.update(
            source={"subagent": {"thread_spawn": {"parent_thread_id": parent}}},
            thread_source="subagent",
            parent_thread_id=parent,
        )
    else:
        payload["source"] = "cli"
    path.write_text(json.dumps({"type": "session_meta", "payload": payload}) + "\n", encoding="utf-8")
    return path


def codex_pane_tree():
    """wrapper(bash ai-session-shell env CODEX_HOME=... codex resume X) -> node -> codex."""
    children = {100: [101], 101: [102]}
    commands = {100: "bash", 101: "node", 102: "codex"}
    return children, commands


def fake_argv(table: dict[int, list[str]]):
    return lambda pid: table.get(pid, [])


class CodexHomeFromRolloutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.iso = self.base / ".codex-homes" / "proj-abc123"
        self.shared = self.base / ".codex"

    def resolve(self, argv_table, rollouts, *, environ_home="", records=None, stamped=""):
        children, commands = codex_pane_tree()
        with mock.patch.object(recovery, "read_proc_argv", side_effect=fake_argv(argv_table)), \
             mock.patch.object(recovery.provider_state, "rollout_paths_for_pids",
                               side_effect=lambda pids: list(rollouts) if 102 in pids else []), \
             mock.patch.object(recovery, "_codex_process_home", return_value=environ_home), \
             mock.patch.object(recovery, "_dashboard_resolved_history", return_value=("", "")):
            return recovery.resolve_provider(
                100, "%8", "/work", children, commands,
                claude_records=records, stamped_codex_home=stamped,
            )

    def wrapper_argv(self, session_id: str) -> dict[int, list[str]]:
        return {
            100: ["bash", "/h/.codex/scripts/ai-session-shell", "env", f"CODEX_HOME={self.iso}",
                  "codex", "resume", session_id],
            101: ["node", "/h/.npm-global/bin/codex", "resume", session_id],
            102: ["/vendor/codex", "resume", session_id],
        }

    def test_home_comes_from_the_open_root_rollout_not_the_wrapper_environ(self) -> None:
        """改前: codex_home = read_proc_env(wrapper) = "" -> 快照里隔离窗口的 home 几乎全为空,
        恢复在共享 ~/.codex 里 resume,接上迁移前的旧分叉。"""
        root = write_rollout(self.iso, ROOT_ID)
        child = write_rollout(self.iso, CHILD_ID, parent=ROOT_ID)
        provider = self.resolve(self.wrapper_argv(ROOT_ID), [root, child])
        self.assertEqual(provider["session_id"], ROOT_ID)
        self.assertEqual(provider["source"], "open-rollout-fd")
        self.assertEqual(provider["codex_home"], str(self.iso))
        self.assertEqual(provider["codex_home_source"], "open-rollout")
        self.assertEqual(provider["history_record_id"], root.stem)

    def test_a_lone_subagent_rollout_never_becomes_the_window_identity(self) -> None:
        child = write_rollout(self.iso, CHILD_ID, parent=ROOT_ID)
        provider = self.resolve(self.wrapper_argv(ROOT_ID), [child])
        # 子智能体的号绝不能顶上来;没有根 rollout 时退回 argv 兜底(同时给出 argv 里的 home)。
        self.assertEqual(provider["session_id"], ROOT_ID)
        self.assertEqual(provider["source"], "process-argv")
        self.assertEqual(provider["codex_home"], str(self.iso))
        self.assertEqual(provider["codex_home_source"], "argv-env")

    def test_home_rules(self) -> None:
        self.assertEqual(
            recovery._codex_home_from_rollout(str(write_rollout(self.iso, ROOT_ID))), str(self.iso)
        )
        self.assertEqual(recovery._codex_home_from_rollout("/x/other/rollout-a.jsonl"), "")
        self.assertEqual(
            recovery._codex_home_from_argv(["bash", "w", "env", "CODEX_HOME=/h/i", "codex"]), "/h/i"
        )
        self.assertEqual(recovery._codex_home_from_argv(["codex", "resume", ROOT_ID]), "")


class ResolveCodexHomeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.shared = base / ".codex"
        self.homes = base / ".codex-homes"
        self.iso = self.homes / "proj-abc123"
        self.shared.mkdir()
        self.iso.mkdir(parents=True)
        for patcher in (
            mock.patch.object(recovery, "DEFAULT_CODEX_HOME", self.shared),
            mock.patch.object(recovery, "CODEX_HOMES_ROOT", self.homes),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_recorded_home_must_really_hold_the_rollout(self) -> None:
        write_rollout(self.iso, ROOT_ID)
        self.assertEqual(recovery.resolve_codex_home(ROOT_ID, str(self.iso)), (str(self.iso), "recorded", ""))
        self.assertEqual(
            recovery.resolve_codex_home(ROOT_ID, str(self.shared))[2],
            "codex-rollout-missing-in-recorded-home",
        )

    def test_old_snapshot_without_home_refuses_a_copy_in_two_homes(self) -> None:
        """迁移遗留: 共享目录一份更短的旧副本 + 隔离目录一份新的 -> 说不清,拒绝,
        绝不退回共享目录(改前: codex_home 为空 -> 直接在共享目录 resume)。"""
        write_rollout(self.iso, ROOT_ID)
        write_rollout(self.shared, ROOT_ID)
        self.assertEqual(recovery.resolve_codex_home(ROOT_ID, "")[2], "codex-home-ambiguous:2")
        self.assertEqual(recovery.resolve_codex_home(OLD_ID, "")[2], "codex-rollout-not-found")
        write_rollout(self.iso, OLD_ID)
        self.assertEqual(recovery.resolve_codex_home(OLD_ID, ""), (str(self.iso), "located", ""))

    def test_plan_blocks_restore_and_relaunch_when_the_home_is_unprovable(self) -> None:
        write_rollout(self.iso, ROOT_ID)
        write_rollout(self.shared, ROOT_ID)
        cwd = str(self.iso)
        pane = {
            "window_index": 8, "window_name": "w8", "pane_index": 0, "pane_id": "%8",
            "cwd": cwd, "preference_key": f"secretary_web:8.0|{cwd}",
            "provider": recovery._provider_record("codex", ROOT_ID, "test"),
            "cards": {},
        }
        plan = recovery.build_recovery_plan(
            {"tmux_server": "1:1", "panes": [pane]}, {"tmux_server": "1:1", "panes": []},
            codex_home_resolver=recovery.resolve_codex_home,
        )
        self.assertEqual(plan["items"][0]["status"], "blocked")
        self.assertEqual(plan["items"][0]["reason"], "codex-home-ambiguous:2")

        # 快照记了 home -> restore 带着它拉起,launch argv 里是 env CODEX_HOME=<隔离目录>
        pane["provider"]["codex_home"] = str(self.iso)
        plan = recovery.build_recovery_plan(
            {"tmux_server": "1:1", "panes": [pane]}, {"tmux_server": "1:1", "panes": []},
            codex_home_resolver=recovery.resolve_codex_home,
        )
        item = plan["items"][0]
        self.assertEqual(item["status"], "restore")
        self.assertIn(f"CODEX_HOME={self.iso}", recovery._launch_argv(item))


class HomeMismatchVerifyTest(unittest.TestCase):
    def snapshot_pane(self, home: str) -> dict:
        provider = recovery._provider_record("codex", ROOT_ID, "open-rollout-fd", codex_home=home)
        return {
            "window_index": 8, "window_name": "w8", "pane_index": 0, "pane_id": "%8",
            "cwd": "/work", "preference_key": "secretary_web:8.0|/work",
            "provider": provider, "cards": {},
        }

    def test_same_session_in_another_home_fails_verify(self) -> None:
        """改前: verify 只比 provider + 会话号,在共享目录接上旧分叉也报通过。"""
        snapshot = {"tmux_server": "1:1", "panes": [self.snapshot_pane("/h/.codex-homes/proj")]}
        live = {"tmux_server": "1:1", "panes": [self.snapshot_pane("/h/.codex")]}
        plan = recovery.build_recovery_plan(snapshot, live)
        self.assertEqual(plan["items"][0]["status"], "home-mismatch")
        with mock.patch.object(recovery, "load_source", return_value=(snapshot, Path("snap"))), \
             mock.patch.object(recovery, "capture_state", return_value=live), \
             mock.patch.object(recovery, "resolve_codex_home", side_effect=lambda s, r="": (r, "recorded", "")), \
             mock.patch.object(recovery, "live_cards", return_value=([], "")), \
             redirect_stdout(io.StringIO()) as out:
            rc = recovery.verify_command(SimpleNamespace(session="secretary_web", snapshot="", summary=False))
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out.getvalue())["missing"][0]["status"], "home-mismatch")

        # 同一个 home -> already-live
        live = {"tmux_server": "1:1", "panes": [self.snapshot_pane("/h/.codex-homes/proj")]}
        self.assertEqual(recovery.build_recovery_plan(snapshot, live)["items"][0]["status"], "already-live")

    def test_relaunch_is_not_verified_by_a_process_in_the_wrong_home(self) -> None:
        item = {"provider": "codex", "resume_id": ROOT_ID, "codex_home": "/h/.codex-homes/proj",
                "target": "s:8.0", "target_pane_id": "%8", "target_pane_server": "1:1"}
        wrong = ("1:1", {"%8": ("codex", ROOT_ID, "/h/.codex")})
        with mock.patch.object(recovery, "_live_pane_identities", return_value=wrong):
            verified, pending, _ = recovery.drive_with_retries(
                [item], "s", lambda *a: None, pause_seconds=0, settle_seconds=0,
                retries=1, retry_backoff=0,
            )
        self.assertEqual((len(verified), len(pending)), (0, 1))
        right = ("1:1", {"%8": ("codex", ROOT_ID, "/h/.codex-homes/proj")})
        with mock.patch.object(recovery, "_live_pane_identities", return_value=right):
            verified, pending, _ = recovery.drive_with_retries(
                [item], "s", lambda *a: None, pause_seconds=0, settle_seconds=0,
                retries=1, retry_backoff=0,
            )
        self.assertEqual((len(verified), len(pending)), (1, 0))


class RecoveryLockAndPinnedBaselineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.snapdir = Path(self.tmp.name)
        patcher = mock.patch.object(recovery, "SNAPSHOT_DIR", self.snapdir)
        patcher.start()
        self.addCleanup(patcher.stop)
        # auto_command closes the boot placeholder through this script when it
        # exists; point it nowhere so the unit tests never run a real tmux command.
        env = mock.patch.dict(os.environ, {"SECRETARY_ENSURE_TMUX": str(self.snapdir / "no-such-ensure-script")})
        env.start()
        self.addCleanup(env.stop)

    def state(self, count: int) -> dict:
        return {
            "schema_version": 3, "captured_at": "now", "session": "secretary_web",
            "pane_count": count, "recoverable_pane_count": 0, "logical_sha256": f"sha{count}",
            "tmux_error": "", "cards": {"prefs": {}, "error": "", "manifest": {"summary": {}}},
            "panes": [],
        }

    def test_snapshot_while_recovery_holds_the_lock_only_writes_observed(self) -> None:
        """改前: 定时快照不看恢复锁,恢复到一半的现场被判 good,覆盖 latest-good 与当天 daily。"""
        good = self.state(57)
        recovery.atomic_write_json(self.snapdir / "latest-good.json", good)
        with recovery.auto_restore_lock_path().open("a+") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            with mock.patch.object(recovery, "capture_state", return_value=self.state(57)), \
                 redirect_stdout(io.StringIO()) as out:
                rc = recovery.snapshot_command(SimpleNamespace(session="secretary_web", force=False))
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "observed-only")
        self.assertEqual(recovery.load_json(self.snapdir / "latest-good.json"), good)
        self.assertEqual(list(self.snapdir.glob("daily-*.json")), [])
        observed = recovery.load_json(self.snapdir / "latest-observed.json")
        self.assertEqual(observed["health"]["reasons"], ["recovery-in-progress"])

        # 锁释放后照常升级为 good。
        with mock.patch.object(recovery, "capture_state", return_value=self.state(57)), \
             redirect_stdout(io.StringIO()):
            rc = recovery.snapshot_command(SimpleNamespace(session="secretary_web", force=False))
        self.assertEqual(rc, 0)
        self.assertEqual(len(list(self.snapdir.glob("daily-*.json"))), 1)

    def test_auto_runs_every_step_against_one_pinned_copy(self) -> None:
        """改前: 自动恢复的 relaunch / verify 不带 --snapshot,读到中途被换掉的基线。"""
        recovery.atomic_write_json(self.snapdir / "latest-good.json", self.state(57))
        seen: list[list[str]] = []

        def step(argv):
            seen.append(argv)
            return 0, {"ok": True}

        with mock.patch.object(recovery, "_run_step", side_effect=step), \
             redirect_stdout(io.StringIO()) as out:
            rc = recovery.auto_command(
                SimpleNamespace(session="secretary_web", snapshot="", yes=True, dry_run=False, summary=False)
            )
        self.assertEqual(rc, 0)
        pinned = json.loads(out.getvalue())["pinned"]
        self.assertTrue(Path(pinned).name.startswith("pinned-auto-"))
        self.assertEqual(recovery.load_json(Path(pinned)), self.state(57))
        self.assertEqual([argv[0] for argv in seen], ["restore", "relaunch", "reconcile-cards", "verify"])
        for argv in seen:
            self.assertEqual(argv[argv.index("--snapshot") + 1], pinned)
        # 恢复进行中不能再起第二个
        with recovery.auto_restore_lock_path().open("a+") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            with self.assertRaises(recovery.RecoveryError):
                recovery.auto_command(
                    SimpleNamespace(session="secretary_web", snapshot="", yes=True, dry_run=False, summary=False)
                )

    def test_auto_defaults_to_dry_run(self) -> None:
        recovery.atomic_write_json(self.snapdir / "latest-good.json", self.state(57))
        with mock.patch.object(recovery, "_run_step") as step, \
             mock.patch.object(recovery, "capture_state", return_value={"tmux_server": "1:1", "panes": []}), \
             redirect_stdout(io.StringIO()) as out:
            rc = recovery.auto_command(
                SimpleNamespace(session="secretary_web", snapshot="", yes=False, dry_run=False, summary=False)
            )
        self.assertEqual(rc, 0)
        self.assertEqual(step.call_count, 0)
        self.assertEqual(json.loads(out.getvalue())["mode"], "dry-run")
        self.assertEqual(list(self.snapdir.glob("pinned-*.json")), [])


class FixedBusDirectoryTest(unittest.TestCase):
    def test_paths_ignore_the_callers_codex_home(self) -> None:
        """改前: 在隔离 Codex 窗口里跑 recovery,快照/Cards 偏好全指向隔离目录
        (隔离目录下生成 degraded 快照,原因 cards-prefs-unreadable)。"""
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, HOME=tmp, CODEX_HOME=f"{tmp}/.codex-homes/iso")
            for key in ("AGENT_BUS_DEFAULT_CODEX_HOME", "AGENT_BUS_DIR", "AGENT_BUS_SNAPSHOT_DIR",
                        "AGENT_BUS_DASHBOARD_STATE_DIR", "AGENT_BUS_SESSION_SHELL"):
                env.pop(key, None)
            code = (
                "import importlib.util,sys;"
                f"spec=importlib.util.spec_from_file_location('r',{str(SCRIPT)!r});"
                "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
                "print(m.SNAPSHOT_DIR);print(m.PREFS_PATH);print(m.AI_SESSION_SHELL);print(m.SECRETARY_BUS)"
            )
            out = subprocess.run(["python3", "-c", code], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(out.returncode, 0, out.stderr)
            snapshot_dir, prefs, shell, bus = out.stdout.split()
            for line in (snapshot_dir, prefs):
                self.assertTrue(line.startswith(f"{tmp}/.codex/"), line)
            # The launch wrapper and CLI ship with the repository.
            repo = str(SCRIPTS.parent)
            for line in (shell, bus):
                self.assertTrue(line.startswith(repo + "/"), line)
        text = (SCRIPTS / "agent_window.sh").read_text(encoding="utf-8")
        self.assertNotIn("${CODEX_HOME:-", text)


class LiveIdentityBeatsArgvTest(unittest.TestCase):
    def test_claude_agents_pid_beats_a_stale_resume_argv(self) -> None:
        """改前: argv 优先 -> /clear、/resume 之后快照和 restart 回到切换前的对话。"""
        children, commands = {200: [201]}, {200: "bash", 201: "claude"}
        argv = {201: ["claude", "--session-id", CLAUDE_OLD]}
        records = [{"pid": 201, "sessionId": CLAUDE_NEW}, {"pid": 999, "sessionId": CLAUDE_OLD}]
        with mock.patch.object(recovery, "read_proc_argv", side_effect=fake_argv(argv)):
            provider = recovery.resolve_provider(200, "%9", "/w", children, commands,
                                                 claude_records=lambda: records)
        self.assertEqual(provider["session_id"], CLAUDE_NEW)
        self.assertEqual(provider["source"], "claude-agents-pid")
        self.assertEqual(provider["identity_conflict"], {"argv": CLAUDE_OLD, "live": CLAUDE_NEW, "resolution": "live"})

    def test_codex_open_rollout_beats_a_stale_resume_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "iso"
            new = write_rollout(home, ROOT_ID)
            children, commands = codex_pane_tree()
            argv = {101: ["node", "/x/bin/codex", "resume", OLD_ID], 102: ["/v/codex", "resume", OLD_ID]}
            with mock.patch.object(recovery, "read_proc_argv", side_effect=fake_argv(argv)), \
                 mock.patch.object(recovery.provider_state, "rollout_paths_for_pids", return_value=[new]), \
                 mock.patch.object(recovery, "_codex_process_home", return_value=""):
                provider = recovery.resolve_provider(100, "%8", "/w", children, commands)
        self.assertEqual(provider["session_id"], ROOT_ID)
        self.assertEqual(provider["identity_conflict"]["argv"], OLD_ID)

    def test_two_open_roots_and_neither_is_argv_is_a_conflict_not_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "iso"
            a = write_rollout(home, ROOT_ID)
            b = write_rollout(home, CHILD_ID)  # 另一条根会话(不是子智能体)
            children, commands = codex_pane_tree()
            argv = {101: ["node", "/x/bin/codex", "resume", OLD_ID]}
            with mock.patch.object(recovery, "read_proc_argv", side_effect=fake_argv(argv)), \
                 mock.patch.object(recovery.provider_state, "rollout_paths_for_pids", return_value=[a, b]), \
                 mock.patch.object(recovery, "_codex_process_home", return_value=str(home)), \
                 mock.patch.object(recovery, "_dashboard_resolved_history", return_value=("", "")):
                provider = recovery.resolve_provider(100, "%8", "/w", children, commands)
        self.assertEqual(provider["session_id"], "")
        self.assertEqual(provider["identity_conflict"]["resolution"], "unresolved")
        self.assertEqual(provider["identity_conflict"]["argv"], OLD_ID)


class CodexPaneStampTest(unittest.TestCase):
    ROW = "\t".join(["secretary_web", "8", "w8", "0", "%8", "4242", "/w", "node", "0", "", "", ""])

    def capture(self, provider, *, stamp: bool):
        calls: list[list[str]] = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd[:2] == ["tmux", "list-panes"]:
                return SimpleNamespace(stdout=self.ROW + "\n", stderr="", returncode=0)
            return SimpleNamespace(stdout="", stderr="", returncode=0)

        with mock.patch.object(recovery, "run", side_effect=fake_run), \
             mock.patch.object(recovery, "read_cards_prefs", return_value=({}, "")), \
             mock.patch.object(recovery, "process_tree", return_value=({}, {})), \
             mock.patch.object(recovery, "resolve_provider", return_value=provider), \
             mock.patch.object(recovery, "tmux_server_id", return_value="1:1"):
            recovery.capture_state("secretary_web", stamp_pane_options=stamp)
        return [cmd for cmd in calls if cmd[:2] == ["tmux", "set-option"]]

    def test_snapshot_stamps_provider_and_home_from_the_open_rollout(self) -> None:
        """改前: Codex pane 都没有 @ai_provider,AI 一退出 restart 就把窗口变成 bash。"""
        provider = recovery._provider_record("codex", ROOT_ID, "open-rollout-fd", codex_home="/h/iso")
        stamped = self.capture(provider, stamp=True)
        options = {cmd[-2]: cmd[-1] for cmd in stamped}
        self.assertEqual(options, {"@ai_provider": "codex", "@ai_session_id": ROOT_ID, "@ai_codex_home": "/h/iso"})
        # plan / verify 等只读路径不写 tmux
        self.assertEqual(self.capture(provider, stamp=False), [])
        # 不是精确的打开 rollout(只有 argv)就不打标记
        argv_only = recovery._provider_record("codex", ROOT_ID, "process-argv", codex_home="/h/iso")
        self.assertEqual(self.capture(argv_only, stamp=True), [])

    def test_exited_codex_keeps_its_home_through_the_pane_option(self) -> None:
        row = "\t".join(["secretary_web", "8", "w8", "0", "%8", "4242", "/w", "bash", "0",
                         "codex", ROOT_ID, "/h/iso"])

        def fake_run(cmd, **kw):
            if cmd[:2] == ["tmux", "list-panes"]:
                return SimpleNamespace(stdout=row + "\n", stderr="", returncode=0)
            return SimpleNamespace(stdout="", stderr="", returncode=0)

        with mock.patch.object(recovery, "run", side_effect=fake_run), \
             mock.patch.object(recovery, "read_cards_prefs", return_value=({}, "")), \
             mock.patch.object(recovery, "process_tree", return_value=({}, {})), \
             mock.patch.object(recovery, "resolve_provider",
                               return_value=recovery._provider_record("", "", "shell-only")), \
             mock.patch.object(recovery, "tmux_server_id", return_value="1:1"):
            state = recovery.capture_state("secretary_web")
        provider = state["panes"][0]["provider"]
        self.assertEqual((provider["kind"], provider["session_id"], provider["codex_home"]),
                         ("codex", ROOT_ID, "/h/iso"))
        self.assertEqual(provider["source"], "tmux-pane-option")


class StampedHomePairingTest(unittest.TestCase):
    """A pane-option session id is paired with the home recorded next to it."""

    def live(self, home: str):
        return recovery._provider_record("codex", "", "live-process", codex_home=home)

    def test_stamp_with_a_different_home_than_the_live_process_is_not_confirmed(self) -> None:
        with mock.patch.object(recovery, "resolve_provider", return_value=self.live("/h/.codex-homes/other")):
            provider = recovery._pane_provider(1, "%1", "/w", {}, {}, stamped_kind="codex",
                                               stamped_session=ROOT_ID, stamped_home="/h/.codex-homes/p")
        self.assertNotEqual(provider.get("session_id"), ROOT_ID)

    def test_matching_stamp_keeps_its_recorded_home(self) -> None:
        with mock.patch.object(recovery, "resolve_provider", return_value=self.live("")):
            provider = recovery._pane_provider(1, "%1", "/w", {}, {}, stamped_kind="codex",
                                               stamped_session=ROOT_ID, stamped_home="/h/.codex-homes/p")
        self.assertEqual((provider["session_id"], provider["codex_home"]), (ROOT_ID, "/h/.codex-homes/p"))


class TargetAndSummaryTest(unittest.TestCase):
    def plan(self) -> dict:
        def item(window, status, name, pane_id):
            return {"source_window": window, "target_window": window, "status": status,
                    "window_name": name, "target_pane_id": pane_id, "source_pane_id": pane_id,
                    "provider": "codex", "session_id": ROOT_ID, "codex_home": "/h/.codex-homes/p",
                    "cards": {"alias": f"alias-{window}"}}
        return {"counts": {"stale-shell": 1, "already-live": 1}, "snapshot_panes": 2, "live_panes": 2,
                "items": [item(8, "stale-shell", "w8", "%8"), item(9, "already-live", "w9", "%9")]}

    def test_target_selects_exactly_one_item(self) -> None:
        plan = self.plan()
        for selector in ("8", "%8", "w8", "alias-8"):
            self.assertEqual(recovery.select_target_item(plan, selector, "stale-shell")[0]["window_name"], "w8")
        with self.assertRaisesRegex(recovery.RecoveryError, "already-live"):
            recovery.select_target_item(plan, "9", "stale-shell")
        with self.assertRaisesRegex(recovery.RecoveryError, "no plan item"):
            recovery.select_target_item(plan, "77", "stale-shell")

    def test_summary_is_one_line_per_window(self) -> None:
        text = recovery.plan_summary_text(self.plan())
        lines = text.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("win8", lines[1])
        self.assertIn("stale-shell", lines[1])
        self.assertIn("home=p", lines[1])


@unittest.skipUnless(shutil.which("tmux"), "tmux required")
class AgentWindowRestartTest(unittest.TestCase):
    """真 tmux(独立 socket 目录),验证 restart 在 AI 已退出/有分屏时不动窗口。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = dict(os.environ, TMUX_TMPDIR=self.tmp.name, AGENT_BUS_CARDS_CHECKPOINT="0")
        self.env.pop("TMUX", None)
        self.tmux("new-session", "-d", "-s", "t", "-n", "w0", "-c", self.tmp.name, "bash --norc --noprofile")
        self.addCleanup(lambda: self.tmux("kill-server", check=False))

    def tmux(self, *args, check=True):
        return subprocess.run(["tmux", *args], env=self.env, capture_output=True, text=True, timeout=10, check=check)

    def restart(self, *args):
        return subprocess.run(
            ["bash", str(SCRIPTS / "agent_window.sh"), "restart", *args, "--session", "t"],
            env=self.env, capture_output=True, text=True, timeout=60,
        )

    def window_ids(self) -> str:
        return self.tmux("list-windows", "-t", "t", "-F", "#{window_id}").stdout

    def test_exited_codex_points_to_relaunch_instead_of_a_cross_model_error(self) -> None:
        """改前: 没有 @ai_provider 的 Codex 窗口 AI 退出后 `restart 0 codex` 报"跨模型",
        `restart 0` 则把窗口换成普通 bash。"""
        before = self.window_ids()
        result = self.restart("0", "codex")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("recovery relaunch --target 0", result.stderr)
        self.assertNotIn("跨模型", result.stderr)
        # 有标记时 `restart 0` 同样拒绝并提示 relaunch,不会变成 bash
        self.tmux("set-option", "-p", "-t", "t:0", "@ai_provider", "codex")
        self.tmux("set-option", "-p", "-t", "t:0", "@ai_session_id", ROOT_ID)
        self.tmux("set-option", "-p", "-t", "t:0", "@ai_codex_home", "/h/.codex-homes/p")
        result = self.restart("0")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("recovery relaunch --target 0", result.stderr)
        self.assertEqual(self.window_ids(), before)

    def test_restart_refuses_a_window_with_splits(self) -> None:
        self.tmux("split-window", "-d", "-t", "t:0", "bash --norc --noprofile")
        before = self.window_ids()
        result = self.restart("0", "--fresh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("分屏", result.stderr)
        self.assertEqual(self.window_ids(), before)
        self.assertEqual(self.tmux("list-panes", "-t", "t:0").stdout.count("\n"), 2)


if __name__ == "__main__":
    unittest.main()
