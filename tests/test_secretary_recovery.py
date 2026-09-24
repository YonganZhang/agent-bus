#!/usr/bin/env python3
"""Regression tests for identity-safe tmux/Cards recovery."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "secretary_recovery.py"
SPEC = importlib.util.spec_from_file_location("secretary_recovery", SCRIPT)
assert SPEC and SPEC.loader
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


def pane(
    window: int,
    *,
    cwd: str,
    kind: str = "codex",
    resume_id: str = "01900000-0000-7000-8000-000000000001",
    category: str = "",
    history_record_id: str = "",
) -> dict:
    if not history_record_id and kind == "claude":
        history_record_id = resume_id
    return {
        "window_index": window,
        "window_name": f"window-{window}",
        "pane_index": 0,
        "pane_id": f"%{window}",
        "pane_pid": 1000 + window,
        "cwd": cwd,
        "command": kind,
        "dead": False,
        "preference_key": f"secretary_web:{window}.0|{cwd}",
        "provider": {
            "kind": kind,
            "session_id": resume_id,
            "resume_id": resume_id,
            "history_record_id": history_record_id,
            "history_kind": (
                "codex-rollout" if kind == "codex" else "claude-transcript"
            ),
            "recoverable": bool(resume_id),
            "source": "test",
            "codex_home": "",
        },
        "cards": {
            "category": category,
            "favorite": False,
            "alias": "",
            "order_rank": window,
        },
    }


def live_observation(item: dict, pane_id: str = "%77", server: str = "1:1"):
    item["target_pane_id"] = pane_id
    item["target_pane_server"] = server
    return server, {pane_id: (item["provider"], item["resume_id"])}


EMPTY_OBSERVATION = ("1:1", {})


class SecretaryRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        # 命令级测试不能去真实的 ~/.codex / ~/.codex-homes 里找 rollout:
        # 这里让 CODEX_HOME 解析原样沿用快照记录;解析本身在 CodexHomeRecoveryTest 里单测。
        patcher = mock.patch.object(
            recovery, "resolve_codex_home",
            side_effect=lambda session_id, recorded="": (recorded, "recorded", ""),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_provider_resume_ids_are_extracted_without_copying_arbitrary_args(self) -> None:
        codex = recovery._provider_from_text(
            'bash -lc "codex resume 01900000-0000-7000-8000-000000000001; exec bash"'
        )
        claude = recovery._provider_from_text(
            "claude --resume 11111111-2222-4333-8444-555555555555"
        )
        pinned = recovery._provider_from_text(
            "ai-session-shell claude --session-id 11111111-2222-4333-8444-555555555555"
        )
        self.assertEqual(codex, ("codex", "01900000-0000-7000-8000-000000000001"))
        self.assertEqual(claude, ("claude", "11111111-2222-4333-8444-555555555555"))
        # 新窗口用 --session-id 出生就带号,快照必须认这一种写法。
        self.assertEqual(pinned, ("claude", "11111111-2222-4333-8444-555555555555"))
        self.assertEqual(recovery._provider_from_text("bash -lc echo codex"), ("", ""))
        self.assertEqual(
            recovery._session_id_from_path(
                "/sessions/2026/07/20/rollout-2026-07-20T00-00-00-01900000-0000-7000-8000-000000000001.jsonl"
            ),
            "01900000-0000-7000-8000-000000000001",
        )
        self.assertEqual(
            recovery._history_identity_from_path(
                "/sessions/2026/07/20/"
                "rollout-2026-07-20T00-00-00-01900000-0000-7000-8000-000000000001.jsonl"
            ),
            (
                "01900000-0000-7000-8000-000000000001",
                "rollout-2026-07-20T00-00-00-01900000-0000-7000-8000-000000000001",
            ),
        )

    def test_cards_manifest_records_names_memberships_and_both_history_ids(self) -> None:
        codex = pane(
            7,
            cwd="/work/codex",
            category="常用入口",
            history_record_id=(
                "rollout-2026-07-20T00-00-00-"
                "01900000-0000-7000-8000-000000000001"
            ),
        )
        codex["window_name"] = "入口原名"
        codex["cards"].update({"favorite": True, "alias": "入口收藏名"})
        claude = pane(
            8,
            cwd="/work/claude",
            kind="claude",
            resume_id="11111111-2222-4333-8444-555555555555",
            category="示例比赛",
        )
        manifest = recovery.build_cards_manifest(
            "secretary_web",
            [codex, claude],
            {"categories": ["最近", "全部", "示例比赛", "常用入口"]},
        )

        self.assertEqual(
            manifest["summary"],
            {
                "window_count": 2,
                "favorite_count": 1,
                "categorized_count": 2,
                "uncategorized_count": 0,
                "session_id_count": 2,
                "history_record_id_count": 2,
            },
        )
        first = manifest["windows"][0]
        self.assertEqual(first["window_name"], "入口原名")
        self.assertEqual(first["display_name"], "入口收藏名")
        self.assertEqual(
            first["session_id"],
            "01900000-0000-7000-8000-000000000001",
        )
        self.assertTrue(first["history_record_id"].startswith("rollout-"))
        categories = {item["name"]: item for item in manifest["categories"]}
        self.assertEqual(
            categories["常用入口"]["windows"][0]["display_name"],
            "入口收藏名",
        )
        self.assertEqual(manifest["favorites"][0]["target"], "secretary_web:7.0")

    def test_schema_v3_manifest_must_match_canonical_panes_and_prefs(self) -> None:
        wanted = pane(
            7,
            cwd="/work/codex",
            category="常用入口",
            history_record_id="rollout-test",
        )
        prefs = {"categories": ["最近", "全部", "常用入口"]}
        manifest = recovery.build_cards_manifest("secretary_web", [wanted], prefs)
        manifest_sha = recovery.hashlib.sha256(
            recovery.json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        snapshot = {
            "schema_version": 3,
            "session": "secretary_web",
            "panes": [wanted],
            "cards": {
                "prefs": prefs,
                "manifest": manifest,
                "manifest_sha256": manifest_sha,
            },
        }
        self.assertEqual(recovery.cards_manifest_errors(snapshot), [])
        snapshot["cards"]["manifest"]["favorites"].append({"target": "fake"})
        self.assertEqual(
            recovery.cards_manifest_errors(snapshot),
            ["cards-manifest-out-of-sync"],
        )

    def test_large_inventory_drop_is_degraded_and_cannot_replace_last_good(self) -> None:
        current = {"pane_count": 1, "tmux_error": "", "cards": {"error": ""}}
        baseline = {"pane_count": 54}
        self.assertIn("sudden-pane-drop:54->1", recovery.degradation_reasons(current, baseline))

    def test_degraded_snapshot_writes_observation_without_overwriting_last_good(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_dir = Path(tmp)
            last_good = {
                "schema_version": 2,
                "pane_count": 54,
                "logical_sha256": "good",
                "panes": [],
            }
            recovery.atomic_write_json(snapshot_dir / "latest-good.json", last_good)
            degraded = {
                "schema_version": 2,
                "captured_at": "now",
                "session": "secretary_web",
                "pane_count": 1,
                "recoverable_pane_count": 0,
                "logical_sha256": "bad",
                "tmux_error": "",
                "cards": {"prefs": {}, "error": ""},
                "panes": [],
            }
            with (
                mock.patch.object(recovery, "SNAPSHOT_DIR", snapshot_dir),
                mock.patch.object(recovery, "capture_state", return_value=degraded),
                redirect_stdout(io.StringIO()),
            ):
                status = recovery.snapshot_command(
                    SimpleNamespace(session="secretary_web", force=False)
                )
            self.assertEqual(status, 3)
            self.assertEqual(recovery.load_json(snapshot_dir / "latest-good.json"), last_good)
            self.assertEqual(
                recovery.load_json(snapshot_dir / "latest-observed.json")["pane_count"],
                1,
            )

    def test_recovery_plan_never_overwrites_an_occupied_window_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = str(Path(tmp))
            wanted = pane(0, cwd=cwd, category="示例比赛")
            placeholder = pane(
                0,
                cwd=cwd,
                resume_id="01900000-0000-7000-8000-000000000999",
            )
            plan = recovery.build_recovery_plan(
                {"captured_at": "before", "tmux_server": "1:1", "panes": [wanted]},
                {"tmux_server": "1:1", "panes": [placeholder]},
            )
        item = plan["items"][0]
        self.assertEqual(item["status"], "restore")
        self.assertEqual(item["remapped_from"], 0)
        self.assertEqual(item["target_window"], 1)

    def test_exact_provider_identity_is_idempotently_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = str(Path(tmp))
            wanted = pane(7, cwd=cwd)
            plan = recovery.build_recovery_plan(
                {"captured_at": "before", "tmux_server": "1:1", "panes": [wanted]},
                {"tmux_server": "1:1", "panes": [wanted]},
            )
        self.assertEqual(plan["counts"], {"already-live": 1})
        self.assertEqual(plan["items"][0]["status"], "already-live")

    def test_missing_project_directory_is_blocked_before_tmux_mutation(self) -> None:
        wanted = pane(7, cwd="/definitely/missing/recovery-project")
        plan = recovery.build_recovery_plan(
            {"captured_at": "before", "tmux_server": "1:1", "panes": [wanted]},
            {"tmux_server": "1:1", "panes": []},
        )
        self.assertEqual(plan["items"][0]["status"], "blocked")
        self.assertEqual(plan["items"][0]["reason"], "cwd-missing")

    def test_cards_verification_finds_category_and_favorite_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wanted = pane(7, cwd=str(Path(tmp)), category="示例比赛")
            wanted["cards"].update({"favorite": True, "alias": "比赛总控"})
            plan = recovery.build_recovery_plan(
                {"captured_at": "before", "tmux_server": "1:1", "panes": [wanted]},
                {"tmux_server": "1:1", "panes": [wanted]},
            )
        mismatches = recovery.cards_mismatches(
            plan,
            [
                {
                    "target": "secretary_web:7.0",
                    "category": "",
                    "favorite": False,
                    "alias": "",
                }
            ],
        )
        self.assertEqual(
            {(item["field"], item["expected"]) for item in mismatches},
            {
                ("category", "示例比赛"),
                ("favorite", True),
                ("alias", "比赛总控"),
            },
        )

    # --- 恢复链路加固: 掉号、空窗、锁竞争重试 ---

    def test_sticky_keeps_session_id_after_the_ai_process_exits(self) -> None:
        """AI 退出后 provider 变空,快照必须沿用上一版的号,否则下轮只能按 cwd 猜。"""
        with tempfile.TemporaryDirectory() as tmp:
            cwd = str(Path(tmp))
            before = pane(7, cwd=cwd)
            after = pane(7, cwd=cwd, resume_id="")
            after["provider"].update({"kind": "", "recoverable": False, "source": "shell-only"})
            applied = recovery.apply_sticky_providers([after], {"captured_at": "2026-08-29T00:00:00+08:00", "tmux_server": "1:1", "panes": [before]}, server_id="1:1", now=recovery._parse_iso_ts("2026-08-29T00:01:00+08:00"))
        self.assertEqual(applied, 1)
        self.assertEqual(after["provider"]["session_id"], before["provider"]["session_id"])
        self.assertTrue(after["provider"]["sticky"])
        self.assertTrue(after["provider"]["source"].startswith("sticky:"))
        self.assertTrue(after["provider"]["recoverable"])

    def test_sticky_refuses_on_cwd_change_and_on_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = str(Path(tmp))
            before = pane(7, cwd=cwd)
            moved = pane(7, cwd=cwd, resume_id="")
            moved["cwd"] = cwd + "/elsewhere"
            moved["provider"].update({"kind": "", "recoverable": False})
            self.assertEqual(
                recovery.apply_sticky_providers([moved], {"captured_at": "2026-08-29T00:00:00+08:00", "tmux_server": "1:1", "panes": [before]}, server_id="1:1", now=recovery._parse_iso_ts("2026-08-29T00:01:00+08:00")),
                0,
            )

            expired = pane(7, cwd=cwd, resume_id="")
            expired["provider"].update({"kind": "", "recoverable": False})
            stale_baseline = {"captured_at": "2026-08-01T00:00:00+08:00", "tmux_server": "1:1", "panes": [before]}
            self.assertEqual(
                recovery.apply_sticky_providers(
                    [expired],
                    stale_baseline,
                    now=recovery._parse_iso_ts("2026-08-29T00:00:00+08:00"),
                    server_id="1:1",
                ),
                0,
            )

    def test_dead_ai_in_a_live_window_is_stale_shell_not_a_second_window(self) -> None:
        """窗口还在、AI 死了时若判成 restore,就会凭空多开一个重复窗口。"""
        with tempfile.TemporaryDirectory() as tmp:
            cwd = str(Path(tmp))
            wanted = pane(7, cwd=cwd)
            shell_only = pane(7, cwd=cwd, resume_id="")
            shell_only["command"] = "bash"
            shell_only["provider"].update({"kind": "", "recoverable": False, "source": "shell-only"})
            plan = recovery.build_recovery_plan(
                {"captured_at": "before", "tmux_server": "1:1", "panes": [wanted]},
                {"tmux_server": "1:1", "panes": [shell_only]},
            )
        item = plan["items"][0]
        self.assertEqual(item["status"], "stale-shell")
        self.assertEqual(item["target_window"], 7)
        self.assertIn("codex", item["relaunch_command"])
        self.assertIn(wanted["provider"]["session_id"], item["relaunch_command"])

    def test_saved_pane_identity_is_recoverable_but_not_live(self) -> None:
        """A pane stamp survives process exit; it must route to relaunch, not already-live."""
        with tempfile.TemporaryDirectory() as tmp:
            cwd = str(Path(tmp))
            wanted = pane(7, cwd=cwd)
            stamped = json.loads(json.dumps(wanted))
            stamped["command"] = "bash"
            stamped["provider"]["source"] = "tmux-pane-option"
            self.assertFalse(recovery._provider_is_live(stamped["provider"]))
            plan = recovery.build_recovery_plan(
                {"captured_at": "before", "tmux_server": "1:1", "panes": [wanted]},
                {"tmux_server": "1:1", "panes": [stamped]},
            )
        self.assertEqual(plan["items"][0]["status"], "stale-shell")

    def test_retry_reuses_the_same_window_and_reports_what_never_came_up(self) -> None:
        """首轮 sqlite 抢锁失败的会话要在原窗口重来,而不是每次重试多一个窗口。"""
        with tempfile.TemporaryDirectory() as tmp:
            cwd = str(Path(tmp))
            item = dict(
                recovery.build_recovery_plan(
                    {"captured_at": "before", "tmux_server": "1:1", "panes": [pane(7, cwd=cwd)]},
                    {"tmux_server": "1:1", "panes": []},
                )["items"][0]
            )
        attempts: list[int] = []

        def launch(_item: dict, _session: str, attempt: int) -> None:
            attempts.append(attempt)

        # 前两轮观测不到身份,第三轮才起来。
        observations = [EMPTY_OBSERVATION, EMPTY_OBSERVATION, live_observation(item)]
        with mock.patch.object(recovery, "_live_pane_identities", side_effect=observations):
            verified, pending, failures = recovery.drive_with_retries(
                [item],
                "secretary_web",
                launch,
                pause_seconds=0,
                settle_seconds=0,
                retries=3,
                retry_backoff=0,
            )
        self.assertEqual(attempts, [1, 2, 3])
        self.assertEqual(len(verified), 1)
        self.assertEqual(pending, [])
        self.assertEqual(failures, [])

        with mock.patch.object(recovery, "_live_pane_identities", return_value=EMPTY_OBSERVATION):
            verified, pending, failures = recovery.drive_with_retries(
                [item],
                "secretary_web",
                launch,
                pause_seconds=0,
                settle_seconds=0,
                retries=2,
                retry_backoff=0,
            )
        self.assertEqual(verified, [])
        self.assertEqual(len(pending), 1)
        self.assertEqual(failures, [])

    def test_verification_requires_identity_to_survive_second_observation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            item = recovery.build_recovery_plan(
                {"captured_at": "before", "tmux_server": "1:1", "panes": [pane(7, cwd=str(Path(tmp)))]},
                {"tmux_server": "1:1", "panes": []},
            )["items"][0]
        first_observation = live_observation(item)
        with mock.patch.object(
            recovery, "_live_pane_identities",
            side_effect=[first_observation, EMPTY_OBSERVATION],
        ), \
             mock.patch.object(recovery.time, "sleep"):
            verified, pending, failures = recovery.drive_with_retries(
                [item], "secretary_web", lambda *_: None,
                pause_seconds=0, settle_seconds=0, retries=1, retry_backoff=0,
                stability_seconds=3,
            )
        self.assertEqual(verified, [])
        self.assertEqual(pending, [item])
        self.assertEqual(failures, [])

    def test_one_live_process_cannot_verify_two_items_with_the_same_session_id(self) -> None:
        """重复 resume id 合法存在，但每个活进程实例只能验收一个恢复项。"""
        with tempfile.TemporaryDirectory() as tmp:
            first = pane(7, cwd=str(Path(tmp)))
            second = pane(8, cwd=str(Path(tmp)))
            second["provider"] = dict(first["provider"])
            items = recovery.build_recovery_plan(
                {"captured_at": "before", "tmux_server": "1:1", "panes": [first, second]},
                {"tmux_server": "1:1", "panes": []},
            )["items"]
        live = live_observation(items[0], "%77")
        items[1]["target_pane_id"] = "%78"
        items[1]["target_pane_server"] = "1:1"
        with mock.patch.object(recovery, "_live_pane_identities", return_value=live):
            verified, pending, failures = recovery.drive_with_retries(
                items, "secretary_web", lambda *_: None,
                pause_seconds=0, settle_seconds=0, retries=1, retry_backoff=0,
            )
        self.assertEqual(len(verified), 1)
        self.assertEqual(len(pending), 1)
        self.assertEqual(failures, [])

    def test_another_panes_same_session_cannot_verify_a_failed_relaunch(self) -> None:
        """已有 pane A 的同 session 不能替 pane B 的失败 relaunch 验收。"""
        with tempfile.TemporaryDirectory() as tmp:
            wanted = pane(8, cwd=str(Path(tmp)))
            shell = pane(8, cwd=str(Path(tmp)), resume_id="")
            shell["command"] = "bash"
            shell["provider"].update({"kind": "", "recoverable": False})
            item = recovery.build_recovery_plan(
                {"captured_at": "before", "tmux_server": "1:1", "panes": [wanted]},
                {"tmux_server": "1:1", "panes": [shell]},
            )["items"][0]
        identity = (item["provider"], item["resume_id"])
        other_pane_only = ("1:1", {"%7": identity})
        with mock.patch.object(
            recovery, "_live_pane_identities", return_value=other_pane_only,
        ):
            verified, pending, failures = recovery.drive_with_retries(
                [item], "secretary_web", lambda *_: None,
                pause_seconds=0, settle_seconds=0, retries=1, retry_backoff=0,
            )
        self.assertEqual(verified, [])
        self.assertEqual(pending, [item])
        self.assertEqual(failures, [])


    def test_idle_shell_check_refuses_anything_that_is_not_an_empty_shell(self) -> None:
        """respawn 会 -k 掉现有进程,判据必须挡住启动中、有后台任务、pane 被换掉三种情况。"""

        def display(command: str, pane_id: str = "%7"):
            return SimpleNamespace(stdout=f"{command}\t4242\t{pane_id}\n", stderr="", returncode=0)

        empty_tree = ({}, {})

        # wrapper 已在前台但 AI 还没 exec 起来 -> 绝不能当成空闲 shell。
        with mock.patch.object(recovery, "run", return_value=display("ai-session-shell")), \
             mock.patch.object(recovery, "process_tree", return_value=empty_tree), \
             mock.patch.object(recovery, "_all_descendants", return_value=[]):
            self.assertFalse(recovery._pane_is_idle_shell("secretary_web:7", "%7"))

        # 前台是 bash,但子树里挂着 codex。
        with mock.patch.object(recovery, "run", return_value=display("bash")), \
             mock.patch.object(recovery, "process_tree", return_value=({4242: [99]}, {99: "codex"})), \
             mock.patch.object(recovery, "_all_descendants", return_value=[99]):
            self.assertFalse(recovery._pane_is_idle_shell("secretary_web:7", "%7"))

        # 前台是 bash,子树里是用户自己跑的后台任务 —— 同样不能杀。
        with mock.patch.object(recovery, "run", return_value=display("bash")), \
             mock.patch.object(recovery, "process_tree", return_value=({4242: [99]}, {99: "python3"})), \
             mock.patch.object(recovery, "_all_descendants", return_value=[99]):
            self.assertFalse(recovery._pane_is_idle_shell("secretary_web:7", "%7"))

        # 窗口号还在,但 pane 已经被换成别的了。
        with mock.patch.object(recovery, "run", return_value=display("bash", "%99")), \
             mock.patch.object(recovery, "process_tree", return_value=empty_tree), \
             mock.patch.object(recovery, "_all_descendants", return_value=[]):
            self.assertFalse(recovery._pane_is_idle_shell("secretary_web:7", "%7"))

        # 真的只剩一个空 bash。
        with mock.patch.object(recovery, "run", return_value=display("bash")), \
             mock.patch.object(recovery, "process_tree", return_value=empty_tree), \
             mock.patch.object(recovery, "_all_descendants", return_value=[]):
            self.assertTrue(recovery._pane_is_idle_shell("secretary_web:7", "%7"))

    def test_recycled_pane_id_does_not_inherit_or_expose_a_destructive_relaunch(self) -> None:
        """窗口号和 cwd 都一样但 pane 换了人时,既不能继承旧号,也不能给出就地 relaunch 入口。"""
        with tempfile.TemporaryDirectory() as tmp:
            cwd = str(Path(tmp))
            before = pane(7, cwd=cwd)
            recycled = pane(7, cwd=cwd, resume_id="")
            recycled["pane_id"] = "%99"
            recycled["command"] = "bash"
            recycled["provider"].update({"kind": "", "recoverable": False, "source": "shell-only"})

            self.assertEqual(
                recovery.apply_sticky_providers([recycled], {"captured_at": "2026-08-29T00:00:00+08:00", "tmux_server": "1:1", "panes": [before]}, server_id="1:1", now=recovery._parse_iso_ts("2026-08-29T00:01:00+08:00")),
                0,
            )
            plan = recovery.build_recovery_plan(
                {"captured_at": "before", "tmux_server": "1:1", "panes": [before]},
                {"tmux_server": "1:1", "panes": [recycled]},
            )
        item = plan["items"][0]
        # 快照知道该恢复哪条,但这个 pane 已经不是当初那个 —— 必须是会让 verify 变红的
        # unresolved-pane,而不是绿色的 already-live-key。
        self.assertEqual(item["status"], "unresolved-pane")
        self.assertNotIn("relaunch_command", item)

    def test_plan_targets_follow_the_requested_session_not_the_module_default(self) -> None:
        """安全检查和 respawn 必须指向同一个 session,否则会检查 A 却杀掉 B。"""
        with tempfile.TemporaryDirectory() as tmp:
            plan = recovery.build_recovery_plan(
                {"captured_at": "before", "tmux_server": "1:1", "panes": [pane(7, cwd=str(Path(tmp)))]},
                {"tmux_server": "1:1", "panes": []},
                "other_session",
            )
        self.assertTrue(plan["items"][0]["target"].startswith("other_session:"))

    def test_launch_errors_keep_their_retry_budget(self) -> None:
        """瞬时 tmux/OS 错误必须还能重试,否则 --retries 名不副实。"""
        with tempfile.TemporaryDirectory() as tmp:
            item = dict(
                recovery.build_recovery_plan(
                    {"captured_at": "before", "tmux_server": "1:1", "panes": [pane(7, cwd=str(Path(tmp)))]},
                    {"tmux_server": "1:1", "panes": []},
                )["items"][0]
            )
        attempts: list[int] = []

        def launch(_item: dict, _session: str, attempt: int) -> None:
            attempts.append(attempt)
            if attempt < 3:
                raise recovery.RecoveryError("pane no longer an idle shell: x")

        with mock.patch.object(
            recovery, "_live_pane_identities", return_value=live_observation(item)
        ):
            verified, pending, failures = recovery.drive_with_retries(
                [item], "secretary_web", launch,
                pause_seconds=0, settle_seconds=0, retries=3, retry_backoff=0,
            )
        self.assertEqual(attempts, [1, 2, 3])
        self.assertEqual(len(verified), 1)
        self.assertEqual((pending, failures), ([], []))

        # 一直失败时,最后一次的错误必须留在 failures 里,不能静默成功。
        def always_fail(_item: dict, _session: str, attempt: int) -> None:
            raise recovery.RecoveryError("pane no longer an idle shell: x")

        with mock.patch.object(recovery, "_live_pane_identities", return_value=EMPTY_OBSERVATION):
            verified, pending, failures = recovery.drive_with_retries(
                [item], "secretary_web", always_fail,
                pause_seconds=0, settle_seconds=0, retries=2, retry_backoff=0,
            )
        self.assertEqual(verified, [])
        self.assertEqual(len(pending), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["attempt"], "2")

    def test_shell_argv_leftovers_are_not_a_live_session(self) -> None:
        """`bash -lc 'claude --session-id X; exec bash'` 的 argv 在 AI 退出后依然留着;
        照抄它会把空窗口报成活会话,relaunch 也就永远救不了这个窗口。"""
        leftover = [
            "bash",
            "-lc",
            "claude --session-id 11111111-2222-4333-8444-555555555555; exec bash",
        ]
        with mock.patch.object(recovery, "_all_descendants", return_value=[1]), \
             mock.patch.object(recovery, "read_proc_argv", return_value=leftover), \
             mock.patch.object(recovery, "_claude_map_history", return_value=("", "")), \
             mock.patch.object(recovery, "_dashboard_resolved_history", return_value=("", "")):
            provider = recovery.resolve_provider(1, "%1", "/tmp", {}, {1: "bash"})
        self.assertEqual(provider["session_id"], "")
        self.assertFalse(provider["recoverable"])

        # 真正的 claude 进程(comm 就是 claude)仍然必须被认出来。
        real = ["claude", "--session-id", "11111111-2222-4333-8444-555555555555"]
        with mock.patch.object(recovery, "_all_descendants", return_value=[1]), \
             mock.patch.object(recovery, "read_proc_argv", return_value=real), \
             mock.patch.object(recovery, "_claude_map_history", return_value=("", "")), \
             mock.patch.object(recovery, "_dashboard_resolved_history", return_value=("", "")):
            provider = recovery.resolve_provider(1, "%1", "/tmp", {}, {1: "claude"})
        self.assertEqual(provider["kind"], "claude")
        self.assertEqual(provider["session_id"], "11111111-2222-4333-8444-555555555555")

    def test_pane_ids_are_only_trusted_inside_one_tmux_server_lifetime(self) -> None:
        """server 重启后 pane id 会从 %0 重新发号,跨 server 继承等于给新窗口贴旧会话。"""
        with tempfile.TemporaryDirectory() as tmp:
            cwd = str(Path(tmp))
            before = pane(7, cwd=cwd)
            after = pane(7, cwd=cwd, resume_id="")
            after["provider"].update({"kind": "", "recoverable": False, "source": "shell-only"})
            baseline = {"captured_at": "2026-08-29T00:00:00+08:00", "tmux_server": "111:9", "panes": [before]}

            self.assertEqual(
                recovery.apply_sticky_providers([after], baseline, server_id="222:9"), 0
            )
            self.assertEqual(recovery.apply_sticky_providers([after], baseline, server_id=""), 0)
            self.assertEqual(recovery.apply_sticky_providers([after], baseline, server_id="111:9", now=recovery._parse_iso_ts("2026-08-29T00:01:00+08:00")), 1)

            # server 换过时,连"窗口还在"都不能拿 pane id 证明 -> 必须是会变红的状态。
            shell_only = pane(7, cwd=cwd, resume_id="")
            shell_only["provider"].update({"kind": "", "recoverable": False})
            plan = recovery.build_recovery_plan(
                {"captured_at": "before", "tmux_server": "111:9", "panes": [before]},
                {"tmux_server": "222:9", "panes": [shell_only]},
            )
        self.assertEqual(plan["items"][0]["status"], "unresolved-pane")

    def test_idle_check_sees_the_whole_subtree_not_just_five_levels(self) -> None:
        """descendants() 的深度截断会漏看更深的后台任务,而漏看一次就会被 -k 杀掉。"""
        chain = {4242: [10], 10: [11], 11: [12], 12: [13], 13: [14], 14: [15]}
        commands = {n: "bash" for n in (4242, 10, 11, 12, 13, 14)}
        commands[15] = "python3"   # 第 6 层才出现的用户任务
        with mock.patch.object(
            recovery,
            "run",
            return_value=SimpleNamespace(stdout="bash\t4242\t%7\n", stderr="", returncode=0),
        ), mock.patch.object(recovery, "process_tree", return_value=(chain, commands)):
            self.assertFalse(recovery._pane_is_idle_shell("secretary_web:7", "%7"))
            self.assertEqual(len(recovery._all_descendants(4242, chain)), 7)

    def test_only_a_process_that_looks_like_the_provider_can_supply_an_identity(self) -> None:
        """任何进程的参数里抄一句启动命令都不能顶替真身份 —— 那会把空窗口压成 already-live。"""
        borrowed = ["python3", "-c", "run('claude --session-id 11111111-2222-4333-8444-555555555555')"]
        with mock.patch.object(recovery, "descendants", return_value=[1]), \
             mock.patch.object(recovery, "read_proc_argv", return_value=borrowed), \
             mock.patch.object(recovery, "_claude_map_history", return_value=("", "")), \
             mock.patch.object(recovery, "_dashboard_resolved_history", return_value=("", "")):
            provider = recovery.resolve_provider(1, "%1", "/tmp", {}, {1: "python3"})
        self.assertEqual(provider["session_id"], "")

    def test_restore_retry_follows_pane_ownership_not_the_window_number(self) -> None:
        """窗口号存在不等于那个窗口是本次建的; respawn 带 -k,认错主人就会杀掉别人的窗口。"""
        with tempfile.TemporaryDirectory() as tmp:
            cwd = str(Path(tmp))
            snapshot = {"captured_at": "before", "tmux_server": "1:1", "panes": [pane(7, cwd=cwd)]}
            live = {"tmux_server": "1:1", "panes": []}
            identity = ("codex", "01900000-0000-7000-8000-000000000001")
            base_args = dict(
                session="secretary_web", snapshot="", yes=True, all=True, limit=None,
                skip_cards=True, pause_seconds=0, settle_seconds=0, retry_backoff=0,
            )

            # 首轮 new-window 在建成之前就失败 -> 没有 pane 所有权 -> 次轮必须再 new-window。
            created: list[dict] = []

            def spawn(item, session):
                if not created:
                    created.append(item)
                    raise OSError("tmux busy")
                item["target_pane_id"] = "%77"
                item["target_pane_server"] = "1:1"

            with mock.patch.object(recovery, "load_source", return_value=(snapshot, Path("snap"))), \
                 mock.patch.object(recovery, "capture_state", return_value=live), \
                 mock.patch.object(recovery, "tmux_server_id", return_value="1:1"), \
                 mock.patch.object(recovery, "_pane_exists", return_value=True), \
                 mock.patch.object(recovery, "_window_exists", return_value=False), \
                 mock.patch.object(recovery, "spawn_item", side_effect=spawn) as spawned, \
                 mock.patch.object(recovery, "respawn_item") as respawned, \
                 mock.patch.object(
                     recovery, "_live_pane_identities",
                     return_value=("1:1", {"%77": identity}),
                 ), \
                 redirect_stdout(io.StringIO()):
                rc = recovery.restore_command(SimpleNamespace(retries=2, **base_args))
            self.assertEqual(rc, 0)
            self.assertEqual((spawned.call_count, respawned.call_count), (2, 0))

            # 本次建过的 pane 还在 -> 就地重来。
            with mock.patch.object(recovery, "load_source", return_value=(snapshot, Path("snap"))), \
                 mock.patch.object(recovery, "capture_state", return_value=live), \
                 mock.patch.object(recovery, "tmux_server_id", return_value="1:1"), \
                 mock.patch.object(recovery, "_pane_exists", return_value=True), \
                 mock.patch.object(recovery, "_window_exists", return_value=False), \
                 mock.patch.object(recovery, "spawn_item",
                                   side_effect=lambda item, session: item.update(
                                       {"target_pane_id": "%77", "target_pane_server": "1:1"})), \
                 mock.patch.object(recovery, "respawn_item") as respawned, \
                 mock.patch.object(
                     recovery, "_live_pane_identities",
                     side_effect=[EMPTY_OBSERVATION, ("1:1", {"%77": identity})],
                 ), \
                 redirect_stdout(io.StringIO()):
                rc = recovery.restore_command(SimpleNamespace(retries=2, **base_args))
            self.assertEqual(rc, 0)
            self.assertEqual(respawned.call_count, 1)

            # 窗口号被别的 pane 占着 -> 撞号判死,绝不 respawn。
            with mock.patch.object(recovery, "load_source", return_value=(snapshot, Path("snap"))), \
                 mock.patch.object(recovery, "capture_state", return_value=live), \
                 mock.patch.object(recovery, "tmux_server_id", return_value="1:1"), \
                 mock.patch.object(recovery, "_pane_exists", return_value=False), \
                 mock.patch.object(recovery, "_window_exists", return_value=True), \
                 mock.patch.object(recovery, "spawn_item") as spawned, \
                 mock.patch.object(recovery, "respawn_item") as respawned, \
                 mock.patch.object(recovery, "_live_pane_identities", return_value=EMPTY_OBSERVATION), \
                 redirect_stdout(io.StringIO()) as out:
                rc = recovery.restore_command(SimpleNamespace(retries=1, **base_args))
            self.assertEqual(rc, 2)
            self.assertEqual((spawned.call_count, respawned.call_count), (0, 0))
            self.assertIn("held by another pane", out.getvalue())

    def test_losing_session_ids_without_losing_panes_is_still_degraded(self) -> None:
        """窗口数不掉、号却成片消失时若照样拍快照,还能精确恢复的号就被永久抹掉了。"""
        with tempfile.TemporaryDirectory() as tmp:
            cwd = str(Path(tmp))
            before = [pane(i, cwd=cwd, resume_id=f"01900000-0000-7000-8000-0000000007{i:02d}")
                      for i in range(8)]
            after = []
            for item in before[:6]:
                blank = json.loads(json.dumps(item))
                blank["provider"].update({"kind": "", "session_id": "", "resume_id": "",
                                          "recoverable": False, "source": "shell-only"})
                after.append(blank)
            after.extend(json.loads(json.dumps(item)) for item in before[6:])
            reasons = recovery.degradation_reasons(
                {"pane_count": len(after), "panes": after},
                {"pane_count": len(before), "panes": before},
            )
        self.assertTrue(any(r.startswith("recoverable-identity-loss") for r in reasons), reasons)

    def test_ten_of_fifty_seven_identity_loss_cannot_replace_last_good(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = str(Path(tmp))
            before = [
                pane(i, cwd=cwd, resume_id=f"01900000-0000-7000-8000-{i:012x}")
                for i in range(57)
            ]
            after = json.loads(json.dumps(before))
            for item in after[:10]:
                item["provider"].update({
                    "kind": "", "session_id": "", "resume_id": "",
                    "recoverable": False, "source": "shell-only",
                })
            reasons = recovery.degradation_reasons(
                {"pane_count": 57, "panes": after},
                {"pane_count": 57, "panes": before},
            )
        self.assertIn("recoverable-identity-loss:57->47", reasons)

    def test_verify_fails_when_a_recoverable_window_is_only_a_stale_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = str(Path(tmp))
            wanted = pane(7, cwd=cwd)
            shell = json.loads(json.dumps(wanted))
            shell["command"] = "bash"
            shell["provider"]["source"] = "tmux-pane-option"
            snapshot = {
                "schema_version": 2, "captured_at": "before", "tmux_server": "1:1",
                "panes": [wanted], "cards": {},
            }
            live = {"tmux_server": "1:1", "panes": [shell]}
            with mock.patch.object(recovery, "load_source", return_value=(snapshot, Path("snap"))), \
                 mock.patch.object(recovery, "capture_state", return_value=live), \
                 mock.patch.object(recovery, "live_cards", return_value=([], "")), \
                 redirect_stdout(io.StringIO()) as out:
                rc = recovery.verify_command(SimpleNamespace(session="secretary_web", snapshot=""))
        payload = json.loads(out.getvalue())
        self.assertEqual(rc, 2)
        self.assertFalse(payload["ok"])
        self.assertEqual(len(payload["stale_shells"]), 1)

    def test_only_the_real_executable_position_can_claim_a_provider_identity(self) -> None:
        """`python3 claude --session-id X` 这种参数位上的 claude 不能顶替真身份。"""
        borrowed = ["python3", "claude", "--session-id", "11111111-2222-4333-8444-555555555555"]
        with mock.patch.object(recovery, "descendants", return_value=[1]), \
             mock.patch.object(recovery, "read_proc_argv", return_value=borrowed), \
             mock.patch.object(recovery, "_claude_map_history", return_value=("", "")), \
             mock.patch.object(recovery, "_dashboard_resolved_history", return_value=("", "")):
            provider = recovery.resolve_provider(1, "%1", "/tmp", {}, {1: "python3"})
        self.assertEqual(provider["session_id"], "")

        # 真 wrapper(`bash /path/ai-session-shell codex resume <id>`)仍然必须被认出来。
        wrapper = ["/bin/bash", "/home/u/.codex/scripts/ai-session-shell", "codex", "resume",
                   "01900000-0000-7000-8000-000000000001"]
        with mock.patch.object(recovery, "descendants", return_value=[1]), \
             mock.patch.object(recovery, "read_proc_argv", return_value=wrapper), \
             mock.patch.object(recovery, "codex_live_identity", return_value={}), \
             mock.patch.object(recovery, "_dashboard_resolved_history", return_value=("", "")):
            provider = recovery.resolve_provider(1, "%1", "/tmp", {}, {1: "bash"})
        self.assertEqual(provider["kind"], "codex")
        self.assertEqual(provider["session_id"], "01900000-0000-7000-8000-000000000001")

    def test_pane_ownership_expires_with_the_tmux_server(self) -> None:
        """server 重启后新 pane 也从 %0 开始。旧所有权既不能触发 respawn,
        也不能让这个目标混进 spawned 去改写别人的 Cards 分类。"""
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = {"captured_at": "before", "tmux_server": "1:1",
                        "panes": [pane(7, cwd=str(Path(tmp)))]}
            live = {"tmux_server": "1:1", "panes": []}
            args = SimpleNamespace(
                session="secretary_web", snapshot="", yes=True, all=True, limit=None,
                skip_cards=False, pause_seconds=0, settle_seconds=0, retries=2, retry_backoff=0,
            )

            # 第一轮在旧 server 上建成了 pane 再失败;第二轮 server 已经换人。
            # 轮1 还在旧 server 上(pane 就是那时建的),轮2 起 server 已经换人。
            servers = iter(["1:1"])

            def spawn(item, session):
                item["target_pane_id"] = "%0"
                item["target_pane_server"] = "1:1"
                raise OSError("tmux busy")

            with mock.patch.object(recovery, "load_source", return_value=(snapshot, Path("snap"))), \
                 mock.patch.object(recovery, "capture_state", return_value=live), \
                 mock.patch.object(recovery, "tmux_server_id", side_effect=lambda: next(servers, "2:2")), \
                 mock.patch.object(recovery, "_pane_exists", return_value=True), \
                 mock.patch.object(recovery, "_window_exists", side_effect=[False, True]), \
                 mock.patch.object(recovery, "spawn_item", side_effect=spawn) as spawned_calls, \
                 mock.patch.object(recovery, "respawn_item") as respawned, \
                 mock.patch.object(recovery, "_live_pane_identities", return_value=EMPTY_OBSERVATION), \
                 mock.patch.object(recovery, "restore_cards", return_value=[]) as cards, \
                 redirect_stdout(io.StringIO()) as out:
                rc = recovery.restore_command(args)
        payload = json.loads(out.getvalue())
        self.assertEqual(rc, 2)
        self.assertEqual(spawned_calls.call_count, 1)   # 首轮真的建过,才谈得上"旧所有权"
        self.assertEqual(respawned.call_count, 0)
        self.assertIn("held by another pane", out.getvalue())
        # 旧 server 上建过、如今已被别人占的目标,不能算本轮起的窗口。
        self.assertEqual(cards.call_count, 0)
        self.assertEqual(payload["spawned"], 0)
        self.assertEqual(payload["targets"], [])

    def test_relaunch_refuses_when_the_server_identity_cannot_be_proven(self) -> None:
        """读不出 server 身份时不能 fail open —— 那正是 pane id 不可比的情况。"""
        with tempfile.TemporaryDirectory() as tmp:
            cwd = str(Path(tmp))
            wanted = pane(7, cwd=cwd)
            shell_only = pane(7, cwd=cwd, resume_id="")
            shell_only["command"] = "bash"
            shell_only["provider"].update({"kind": "", "recoverable": False})
            snapshot = {"captured_at": "before", "tmux_server": "1:1", "panes": [wanted]}
            live = {"tmux_server": "1:1", "panes": [shell_only]}
            args = SimpleNamespace(
                session="secretary_web", snapshot="", yes=True, all=True, limit=None,
                pause_seconds=0, settle_seconds=0, retries=1, retry_backoff=0,
            )
            with mock.patch.object(recovery, "load_source", return_value=(snapshot, Path("snap"))), \
                 mock.patch.object(recovery, "capture_state", return_value=live), \
                 mock.patch.object(recovery, "tmux_server_id", return_value=""), \
                 mock.patch.object(recovery, "_pane_is_idle_shell", return_value=True), \
                 mock.patch.object(recovery, "respawn_item") as respawned, \
                 mock.patch.object(recovery, "_live_pane_identities", return_value=EMPTY_OBSERVATION), \
                 redirect_stdout(io.StringIO()) as out:
                rc = recovery.relaunch_command(args)
        self.assertEqual(rc, 2)
        self.assertEqual(respawned.call_count, 0)
        self.assertIn("unprovable", out.getvalue())

    def test_identity_loss_threshold_matches_the_documented_quarter(self) -> None:
        """基数 5 丢 2 已经是 40%,必须判 degraded,否则会覆盖掉还能精确恢复的号。"""
        with tempfile.TemporaryDirectory() as tmp:
            cwd = str(Path(tmp))
            before = [pane(i, cwd=cwd, resume_id=f"01900000-0000-7000-8000-0000000007{i:02d}")
                      for i in range(5)]
            after = json.loads(json.dumps(before))
            for item in after[:2]:
                item["provider"].update({"kind": "", "session_id": "", "resume_id": "",
                                         "recoverable": False, "source": "shell-only"})
            reasons = recovery.degradation_reasons(
                {"pane_count": 5, "panes": after}, {"pane_count": 5, "panes": before}
            )
        self.assertTrue(any(r.startswith("recoverable-identity-loss") for r in reasons), reasons)

    def test_pane_option_identity_survives_the_process_and_beats_guessing(self) -> None:
        """进程死了 pane option 还在 —— 这正是"窗口空着但知道该恢复哪条"要的东西。

        优先级也要对: 活进程的实时身份不能被残留标记覆盖(用户可能中途换了会话)。
        """
        sid = "11111111-2222-4333-8444-555555555555"
        rows = "\t".join([
            "secretary_web", "7", "win", "0", "%7", "4242", "/tmp", "bash", "0",
            "claude", sid,
        ])

        def fake_run(cmd, **kw):
            if cmd[:2] == ["tmux", "list-panes"]:
                return SimpleNamespace(stdout=rows + "\n", stderr="", returncode=0)
            return SimpleNamespace(stdout="", stderr="", returncode=0)

        # AI 进程已经退出 -> 推断不出身份 -> 采用 pane 上钉着的那个
        with mock.patch.object(recovery, "run", side_effect=fake_run), \
             mock.patch.object(recovery, "read_cards_prefs", return_value=({}, "")), \
             mock.patch.object(recovery, "process_tree", return_value=({}, {})), \
             mock.patch.object(recovery, "resolve_provider",
                               return_value=recovery._provider_record("", "", "shell-only")), \
             mock.patch.object(recovery, "tmux_server_id", return_value="1:1"):
            state = recovery.capture_state("secretary_web")
        provider = state["panes"][0]["provider"]
        self.assertEqual((provider["kind"], provider["session_id"]), ("claude", sid))
        self.assertEqual(provider["source"], "tmux-pane-option")

        # 活进程能确认 provider kind、但会话号因多条开放记录无法唯一解析时，可与同 kind
        # 的 pane 标记合并；这时标记不再是孤证，必须算 live。
        with mock.patch.object(recovery, "run", side_effect=fake_run), \
             mock.patch.object(recovery, "read_cards_prefs", return_value=({}, "")), \
             mock.patch.object(recovery, "process_tree", return_value=({}, {})), \
             mock.patch.object(
                 recovery, "resolve_provider",
                 return_value=recovery._provider_record(
                     "claude", "", "provider-without-stable-session"
                 ),
             ), \
             mock.patch.object(recovery, "tmux_server_id", return_value="1:1"):
            state = recovery.capture_state("secretary_web")
        provider = state["panes"][0]["provider"]
        self.assertEqual(provider["session_id"], sid)
        self.assertEqual(provider["source"], "live-process+tmux-pane-option")
        self.assertTrue(recovery._provider_is_live(provider))

        # 不同 provider 的活进程说明标记已经陈旧，不能用旧标记顶掉现场。
        with mock.patch.object(recovery, "run", side_effect=fake_run), \
             mock.patch.object(recovery, "read_cards_prefs", return_value=({}, "")), \
             mock.patch.object(recovery, "process_tree", return_value=({}, {})), \
             mock.patch.object(
                 recovery, "resolve_provider",
                 return_value=recovery._provider_record(
                     "codex", "", "provider-without-stable-session"
                 ),
             ), \
             mock.patch.object(recovery, "tmux_server_id", return_value="1:1"):
            state = recovery.capture_state("secretary_web")
        self.assertEqual(state["panes"][0]["provider"]["kind"], "codex")
        self.assertEqual(state["panes"][0]["provider"]["session_id"], "")

        # 进程活着且能实时解析出身份(claude agents 按 pid) -> 以实时身份为准,不被残留标记顶掉
        live = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
        with mock.patch.object(recovery, "run", side_effect=fake_run), \
             mock.patch.object(recovery, "read_cards_prefs", return_value=({}, "")), \
             mock.patch.object(recovery, "process_tree", return_value=({}, {})), \
             mock.patch.object(recovery, "resolve_provider",
                               return_value=recovery._provider_record("claude", live, "claude-agents-pid")), \
             mock.patch.object(recovery, "tmux_server_id", return_value="1:1"):
            state = recovery.capture_state("secretary_web")
        self.assertEqual(state["panes"][0]["provider"]["session_id"], live)

        # 只剩启动参数兜底时,argv 可能早已过期(/clear、/resume 之后 SessionStart hook
        # 会更新标记,argv 不会变): 标记胜出,并且冲突必须留痕,不能静默选 argv。
        with mock.patch.object(recovery, "run", side_effect=fake_run), \
             mock.patch.object(recovery, "read_cards_prefs", return_value=({}, "")), \
             mock.patch.object(recovery, "process_tree", return_value=({}, {})), \
             mock.patch.object(recovery, "resolve_provider",
                               return_value=recovery._provider_record("claude", live, "process-argv")), \
             mock.patch.object(recovery, "tmux_server_id", return_value="1:1"):
            state = recovery.capture_state("secretary_web")
        provider = state["panes"][0]["provider"]
        self.assertEqual(provider["session_id"], sid)
        self.assertEqual(provider["identity_conflict"]["argv"], live)

    def test_a_malformed_pane_option_is_ignored(self) -> None:
        """标记是外部写进来的,不能因为它长得不对就把垃圾当成会话号。"""
        for kind, sid in (("claude", "not-a-uuid"), ("bogus", "11111111-2222-4333-8444-555555555555"), ("", "")):
            rows = "\t".join(["secretary_web", "7", "win", "0", "%7", "4242", "/tmp", "bash", "0", kind, sid])

            def fake_run(cmd, _rows=rows, **kw):
                if cmd[:2] == ["tmux", "list-panes"]:
                    return SimpleNamespace(stdout=_rows + "\n", stderr="", returncode=0)
                return SimpleNamespace(stdout="", stderr="", returncode=0)

            with self.subTest(kind=kind, sid=sid), \
                 mock.patch.object(recovery, "run", side_effect=fake_run), \
                 mock.patch.object(recovery, "read_cards_prefs", return_value=({}, "")), \
                 mock.patch.object(recovery, "process_tree", return_value=({}, {})), \
                 mock.patch.object(recovery, "resolve_provider",
                                   return_value=recovery._provider_record("", "", "shell-only")), \
                 mock.patch.object(recovery, "tmux_server_id", return_value="1:1"):
                state = recovery.capture_state("secretary_web")
            self.assertEqual(state["panes"][0]["provider"]["session_id"], "")

    def test_two_panes_sharing_one_session_id_do_not_collapse_into_one(self) -> None:
        """一条会话被 --resume 开了两次时，两个窗口各占一个位置。

        用单值表存身份会让后来的覆盖先来的：两个窗口被当成一个，真正掉了的那个永远
        不会被恢复，而 verify 还会报告一切正常——坏状态就这样被写进下次开机要用的快照。
        """
        with tempfile.TemporaryDirectory() as tmp:
            cwd = str(Path(tmp))
            a = pane(7, cwd=cwd)
            b = pane(8, cwd=cwd)
            b["provider"] = dict(a["provider"])          # 同一个 session id
            b["pane_id"] = "%8"
            # 现场只剩一个 pane 还活着
            live_one = pane(7, cwd=cwd)
            plan = recovery.build_recovery_plan(
                {"captured_at": "before", "tmux_server": "1:1", "panes": [a, b]},
                {"tmux_server": "1:1", "panes": [live_one]},
            )
        statuses = sorted(item["status"] for item in plan["items"])
        self.assertEqual(statuses, ["already-live", "restore"],
                         f"两个共用会话号的窗口必须各自结算,实际: {statuses}")


@unittest.skipUnless(shutil.which("tmux"), "tmux required")
class RespawnItemSplitWindowTest(unittest.TestCase):
    """就地 respawn 只能动被检查过的那个 pane: 分屏里的其他 pane 必须原样保留。

    用独立 socket 的 tmux server,绝不碰默认 server 上的真实窗口。
    """

    def setUp(self) -> None:
        self.socket = f"ab-respawn-test-{os.getpid()}"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.socket_path: Path | None = None
        self.addCleanup(self._kill_server)

    def _kill_server(self) -> None:
        # 顺序不能反: 先删 socket 文件再 kill-server 会连不上 server,留下孤儿进程。
        subprocess.run(["tmux", "-L", self.socket, "kill-server"], capture_output=True, check=False)
        if self.socket_path is not None:
            self.socket_path.unlink(missing_ok=True)

    def tmux(self, *args: str) -> str:
        return subprocess.run(
            ["tmux", "-L", self.socket, *args],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout.strip()

    def isolated_run(self, args, *, timeout=15, check=True):
        assert args[0] == "tmux", args
        return subprocess.run(
            ["tmux", "-L", self.socket, *args[1:]],
            capture_output=True, text=True, timeout=timeout, check=check,
        )

    def test_respawn_keeps_sibling_pane_in_split_window(self) -> None:
        self.tmux("-f", "/dev/null", "new-session", "-d", "-s", "t", "-x", "200", "-y", "50", "sleep 600")
        # kill-server 不会删掉 socket 文件,交给 _kill_server 在 kill 之后清掉。
        self.socket_path = Path(self.tmux("display-message", "-p", "#{socket_path}"))
        target = self.tmux("display-message", "-p", "-t", "t:0", "#{pane_id}")
        sibling = self.tmux("split-window", "-d", "-P", "-F", "#{pane_id}", "-t", target, "sleep 601")
        sibling_pid = self.tmux("display-message", "-p", "-t", sibling, "#{pane_pid}")
        target_pid = self.tmux("display-message", "-p", "-t", target, "#{pane_pid}")
        item = {
            "target_pane_id": target,
            "target_window": 0,
            "cwd": self.tmp.name,
            "window_name": "relaunched",
            "provider": "claude",
            "resume_id": "abc",
        }
        with mock.patch.object(recovery, "run", self.isolated_run), \
             mock.patch.object(recovery, "_launch_argv", return_value=["sleep", "602"]):
            recovery.respawn_item(item, "t")
        panes = dict(
            line.split("\t", 1)
            for line in self.tmux("list-panes", "-t", "t:0", "-F", "#{pane_id}\t#{pane_pid}").splitlines()
        )
        self.assertEqual(set(panes), {target, sibling}, "分屏里的另一个 pane 被 respawn 销毁了")
        self.assertEqual(panes[sibling], sibling_pid, "兄弟 pane 的进程被替换了")
        self.assertNotEqual(panes[target], target_pid, "目标 pane 没有被重新拉起")
        self.assertEqual(self.tmux("display-message", "-p", "-t", target, "#{window_name}"), "relaunched")


if __name__ == "__main__":
    unittest.main()
