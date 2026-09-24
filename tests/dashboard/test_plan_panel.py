#!/usr/bin/env python3
"""Card plan panel + one-click archive: has_plan / task title on /api/panes,
/api/plan resolved only from the pane's live cwd, whitelisted plan actions
mapped onto the configured plan CLI (the fake CLI in fixtures/ on a temporary
project), input limits, CSRF refusal, the idle-only archive request and the
501 answers while the optional integration is not configured.

Run: python3 -m pytest test_plan_panel.py -q
"""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import shutil
import threading
import time
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import os

import server
from plan_stub import ARCHIVE_PROMPT_TEXT, enable_plan_integration, wait_plan_index, write_archive_prompt
from test_git_status import GitStatusTestBase, git, make_repo, wait_refreshed

# A minimal starter plan (synthetic placeholder text); the fake CLI parses and edits it.
MINIMAL_PLAN = Path(__file__).resolve().parent / "fixtures" / "plan_panel_task_plan.md"
PLAN_REL = Path("_wiki-methodology") / "_top" / "_task_plan.md"


def fake_pane(cwd: object, pane_id: str = "%901", kind: str = "Claude", ai_alive: bool = True) -> server.Pane:
    return server.Pane(
        pane_id=pane_id, target="secretary_web:9.0", session="secretary_web", window_index=9, pane_index=0,
        window_name="w", command="claude", cwd=str(cwd), title="", active=False, kind=kind, project="p",
        preview="", status="", pane_pid="4242", pane_start_time="777", ai_alive=ai_alive,
    )


def api_request(method: str, path: str, body: bytes | None = None,
                headers: dict[str, str] | None = None) -> tuple[int, dict[str, object]]:
    old_authorized = server.Handler.authorized
    old_log_message = server.Handler.log_message
    server.Handler.authorized = lambda _handler: True
    server.Handler.log_message = lambda _handler, _fmt, *_args: None
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=30)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        status, payload = response.status, json.loads(response.read().decode("utf-8"))
        conn.close()
        return status, payload
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        server.Handler.authorized = old_authorized
        server.Handler.log_message = old_log_message


def post_json(path: str, payload: object) -> tuple[int, dict[str, object]]:
    return api_request("POST", path, json.dumps(payload).encode("utf-8"),
                       {"Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"})


class PlanTestBase(GitStatusTestBase):
    def make_top_project(self, project: Path) -> Path:
        plan = project / PLAN_REL
        plan.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(MINIMAL_PLAN, plan)
        return plan

    def setUp(self) -> None:
        super().setUp()
        with server._GIT_LOCK:
            server._PLAN_TRACK_CACHE.clear()
            server._ARCHIVE_EVAL_PENDING.clear()
        server._PLAN_FILE_FOR_PROJECT.clear()
        enable_plan_integration(self, write_archive_prompt(self.base))
        # Archive runs live in prefs.json: never touch the real one.
        patcher = mock.patch.object(server, "PREFS", self.base / "prefs.json")
        patcher.start()
        self.addCleanup(patcher.stop)


class HasPlanAndTaskTitleTest(PlanTestBase):
    def summary(self, cwd: Path) -> dict[str, object]:
        root = server.git_root_for_cwd(str(cwd))
        server.pane_git_summary(str(cwd))
        wait_refreshed(root)
        wait_plan_index()
        return server.pane_git_summary(str(cwd))

    def test_plan_in_parent_project_found_from_subdirectory(self) -> None:
        repo = make_repo(self.base / "mono")
        project = repo / "projects" / "p"
        (project / "src" / "deep").mkdir(parents=True)
        plan = self.make_top_project(project)
        self.assertEqual(server.find_top_plan(str(project / "src" / "deep"), str(repo)), (str(project), str(plan)))
        self.assertIs(self.summary(project / "src" / "deep")["has_plan"], True)
        # The repo root itself is outside that project: no plan between it and the git root.
        self.assertIs(self.summary(repo)["has_plan"], False)

    def test_plan_at_git_root_and_no_plan(self) -> None:
        repo = make_repo(self.base / "repo")
        (repo / "a" / "b").mkdir(parents=True)
        self.assertIs(self.summary(repo / "a" / "b")["has_plan"], False)
        self.make_top_project(repo)
        self.assertIs(self.summary(repo / "a" / "b")["has_plan"], True)

    def test_plan_above_git_root_is_not_used(self) -> None:
        self.make_top_project(self.base / "outer")
        repo = make_repo(self.base / "outer" / "inner")
        self.assertIsNone(server.find_top_plan(str(repo), str(repo)))
        self.assertIs(self.summary(repo)["has_plan"], False)

    def test_task_title_gate_and_id_fallback(self) -> None:
        repo = make_repo(self.base / "repo")
        plan = repo / PLAN_REL
        plan.parent.mkdir(parents=True)
        plan.write_text(
            "# Plan\n\n## Active Work\n\n"
            "- [ ] P1.2 写卡片计划面板 · state=in_progress · gate=全量测试通过 · gate=截图\n"
            "- [ ] P1.3 later · state=pending\n",
            encoding="utf-8",
        )
        server.top_active_task_info(str(repo), str(repo))
        wait_plan_index()
        info = server.top_active_task_info(str(repo), str(repo))
        self.assertEqual(info, {"id": "P1.2", "title": "写卡片计划面板", "gate": "全量测试通过"})
        summary = self.summary(repo)
        self.assertEqual((summary["task"], summary["task_title"], summary["task_gate"]),
                         ("P1.2", "写卡片计划面板", "全量测试通过"))
        # task/<ID> branch not in the plan: ID only, the card falls back to showing it.
        info = server.top_active_task_info(str(repo), str(repo), "task/P9.1")
        self.assertEqual(info, {"id": "P9.1", "title": "", "gate": ""})
        git(repo, "checkout", "-q", "-b", "task/P9.1")
        with server._GIT_LOCK:
            server._GIT_STATUS_CACHE.clear()
        summary = self.summary(repo)
        self.assertEqual((summary["task"], summary["task_title"], summary["task_gate"]), ("P9.1", None, None))


class SuggestTaskIdTest(PlanTestBase):
    def test_bumps_last_direct_child_of_phase(self) -> None:
        ids = ["P3.9.E1", "P3.9.PMA13", "P3.9.PMA14", "P3.9.PMA14.1", "P2.1"]
        self.assertEqual(server.suggest_task_id(ids, "P3.9"), "P3.9.PMA15")
        self.assertEqual(server.suggest_task_id(["P0.1.E1", "P0.1.E2"], "P0.1"), "P0.1.E3")
        self.assertEqual(server.suggest_task_id(["P0.1.E1", "P0.1.E2", "P0.1.E3", "P0.1.E2.1"], "P0.1"),
                         "P0.1.E4")
        self.assertEqual(server.suggest_task_id([], "P2.3"), "P2.3.T1")
        self.assertEqual(server.suggest_task_id([], ""), "P1.T1")
        self.assertEqual(server.suggest_task_id(["P1.T1"], ""), "P1.T2")


class ActionMappingTest(PlanTestBase):
    def cmds(self, **payload: object) -> list[list[str]]:
        return server.plan_action_commands("/proj", payload)

    def test_each_action_maps_to_cli_argument_lists(self) -> None:
        self.assertEqual(
            self.cmds(action="add", id="P1.5", title="-dash title", gate="跑测试", depends_on="P1.2, P1.3"),
            [["plan", "add", "--id=P1.5", "--title=-dash title", "--gate=跑测试", "--depends-on=P1.2,P1.3", "--", "/proj"]],
        )
        self.assertEqual(self.cmds(action="add", id="P1.5", title="t"), [["plan", "add", "--id=P1.5", "--title=t", "--", "/proj"]])
        self.assertEqual(
            self.cmds(action="edit", id="P1.2", title="新标题", gate="g", depends_on="P1.1"),
            [["plan", "edit", "--title=新标题", "--add-gate=g", "--add-dep=P1.1", "--", "/proj", "P1.2"]],
        )
        self.assertEqual(
            self.cmds(action="note", text="-第一行\n第二行", kind="learn", id="P1.2"),
            [["plan", "note", "--id=P1.2", "--kind=learn", "--", "/proj", "-第一行\n第二行"]],
        )
        self.assertEqual(self.cmds(action="note", text="x"), [["plan", "note", "--kind=progress", "--", "/proj", "x"]])
        self.assertEqual(self.cmds(action="start", id="P1.2"), [["plan", "start", "--", "/proj", "P1.2"]])
        self.assertEqual(self.cmds(action="block", id="P1.2", reason="等数据"),
                         [["plan", "block", "--reason=等数据", "--", "/proj", "P1.2"]])
        self.assertEqual(self.cmds(action="cancel", id="P1.2", reason="不做了"),
                         [["plan", "cancel", "--reason=不做了", "--", "/proj", "P1.2"]])
        self.assertEqual(
            self.cmds(action="reopen", id="P1.2", reason="还要改"),
            [["plan", "reopen", "--", "/proj", "P1.2"],
             ["plan", "note", "--id=P1.2", "--kind=progress", "--", "/proj", "重开：还要改"]],
        )

    def test_add_without_id_generates_one_from_the_plan(self) -> None:
        plan = self.make_top_project(self.base / "p")
        cmds = server.plan_action_commands(str(self.base / "p"), {"action": "add", "title": "t"}, str(plan))
        self.assertEqual(cmds[0][2], "--id=P0.1.E2")

    def assert_rejected(self, fragment: str, **payload: object) -> None:
        with self.assertRaises(server.PlanApiError) as ctx:
            server.plan_action_commands("/proj", payload)
        self.assertIn(fragment, str(ctx.exception))
        self.assertEqual(ctx.exception.status, 400)

    def test_illegal_actions_and_fields_are_rejected(self) -> None:
        self.assert_rejected("不支持的操作", action="complete", id="P1.2")
        self.assert_rejected("不支持的操作", action="write_raw", text="x")
        self.assert_rejected("不支持的操作")
        self.assert_rejected("标题太长（最多 200 字）", action="add", id="P1.5", title="字" * 201)
        self.cmds(action="add", id="P1.5", title="字" * 200)  # exactly at the limit is fine
        self.assert_rejected("原因太长", action="block", id="P1.2", reason="x" * 301)
        self.assert_rejected("验收太长", action="add", id="P1.5", title="t", gate="g" * 301)
        self.assert_rejected("内容太长", action="note", text="n" * 301)
        self.assert_rejected("控制字符 U+0007", action="add", id="P1.5", title="bell\x07")
        self.assert_rejected("控制字符 U+0009", action="note", text="tab\there")
        self.assert_rejected("控制字符 U+007F", action="note", text="del\x7f")
        self.assert_rejected("原因只能写一行", action="block", id="P1.2", reason="l1\nl2")
        self.assert_rejected("标题只能写一行", action="add", id="P1.5", title="l1\nl2")
        self.assert_rejected("分隔符", action="add", id="P1.5", title="t · state=done")
        self.assert_rejected("分隔符", action="cancel", id="P1.2", reason="r · state=done")
        self.assert_rejected("标题必填", action="add", id="P1.5", title="  ")
        self.assert_rejected("原因必填", action="block", id="P1.2")
        self.assert_rejected("原因必填", action="reopen", id="P1.2")
        self.assert_rejected("内容必填", action="note")
        self.assert_rejected("任务 ID 格式不对", action="start", id="../../etc")
        self.assert_rejected("任务 ID 格式不对", action="start", id="--id=P1.2")
        self.assert_rejected("任务 ID 格式不对", action="start")
        self.assert_rejected("前置任务 ID 格式不对", action="add", id="P1.5", title="t", depends_on="P1.2,rm -rf")
        self.assert_rejected("笔记类型", action="note", text="x", kind="secret")
        self.assert_rejected("至少要填", action="edit", id="P1.2")
        self.assert_rejected("必须是文本", action="add", id="P1.5", title=["a"])

    def test_cli_failures_become_chinese_errors(self) -> None:
        self.assertIsNone(server.plan_cli_failure({"verdict": "ok", "item": {}}))
        self.assertIsNone(server.plan_cli_failure({"verdict": "warn", "errors": ["size"]}))
        conflict = server.plan_cli_failure(
            {"verdict": "block", "errors": ["task plan changed concurrently; reread and retry"]})
        self.assertEqual((conflict.status, str(conflict)), (409, "计划刚被别人改过，已刷新，请重试"))
        dup = server.plan_cli_failure({"verdict": "block", "errors": ["duplicate task id: P1.2"]})
        self.assertEqual((dup.status, str(dup)), (400, "任务 ID 已存在：P1.2"))
        other = server.plan_cli_failure({"error": "task plan not found: /x"})
        self.assertEqual(str(other), "计划命令被拒绝：task plan not found: /x")


class PlanApiTest(PlanTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = make_repo(self.base / "mono")
        self.project = self.repo / "projects" / "p"
        (self.project / "src").mkdir(parents=True)
        self.plan = self.make_top_project(self.project)
        self.pane = fake_pane(self.project / "src")
        patcher = mock.patch.object(server, "pane_by_id", side_effect=lambda pid: self.pane if pid == "%901" else None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_plan_is_resolved_only_from_pane_cwd(self) -> None:
        elsewhere = self.base / "elsewhere"
        self.make_top_project(elsewhere)
        calls: list[list[str]] = []
        real = server.run_plan_cli
        with mock.patch.object(server, "run_plan_cli", side_effect=lambda args, *a, **k: calls.append(args) or real(args, *a, **k)):
            status, data = api_request(
                "GET", f"/api/plan?pane=%25901&path={elsewhere}&project={elsewhere}&plan=/etc/passwd")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["project"], "p")
        self.assertEqual(data["plan_rel"], str(PLAN_REL))
        self.assertEqual(data["raw"], self.plan.read_text(encoding="utf-8"))
        self.assertEqual(data["show"]["current"], None)
        self.assertEqual([t["id"] for t in data["tasks"]], ["P0.1.E1"])
        self.assertEqual(data["suggested_id"], "P0.1.E2")
        self.assertEqual(calls, [["plan", "show", str(self.project)], ["plan", "list", str(self.project)]])
        self.assertRegex(data["sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(data["mtime_ns"].isdigit())
        # Unchanged plan file: no CLI run, no plan body.
        calls.clear()
        with mock.patch.object(server, "run_plan_cli", side_effect=AssertionError("CLI must not run")):
            status, same = api_request("GET", f"/api/plan?pane=%25901&if_mtime={data['mtime_ns']}")
        self.assertEqual((status, same["unchanged"]), (200, True))
        self.assertNotIn("raw", same)

    def test_notes_by_task_and_notes_aware_unchanged_polls(self) -> None:
        logs = self.plan.parent / "_logs"
        logs.mkdir()
        today = datetime.now().strftime("%Y-%m-%d")
        log = logs / f"{today}-plan.md"
        log.write_text(f"# {today} log\n\n- 09:00 note:fail P0.1.E1 方案 A 没过 · git=main@abc(+1 dirty)\n", encoding="utf-8")
        status, data = api_request("GET", "/api/plan?pane=%25901")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["notes_by_task"], {"P0.1.E1": [{"date": today, "time": "09:00", "kind": "fail", "text": "方案 A 没过"}]})
        self.assertIs(data["notes_truncated"], False)
        query = f"/api/plan?pane=%25901&if_mtime={data['mtime_ns']}&if_notes={data['notes_mtime_ns']}"
        with mock.patch.object(server, "run_plan_cli", side_effect=AssertionError("CLI must not run")):
            self.assertIs(api_request("GET", query)[1]["unchanged"], True)
        # A progress note only appends to the log: the plan file is unchanged, the poll is not.
        with log.open("a", encoding="utf-8") as handle:
            handle.write("- 09:30 note:progress P0.1.E1 第二轮 · git=main@abc\n")
        stat = log.stat()
        os.utime(log, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        status, fresh = api_request("GET", query)
        self.assertEqual((status, fresh["unchanged"]), (200, False))
        self.assertEqual([n["text"] for n in fresh["notes_by_task"]["P0.1.E1"]], ["方案 A 没过", "第二轮"])

    def test_bad_missing_and_planless_panes(self) -> None:
        self.assertEqual(api_request("GET", "/api/plan?pane=secretary_web:1")[0], 400)
        self.assertEqual(api_request("GET", "/api/plan")[0], 400)
        status, data = api_request("GET", "/api/plan?pane=%25902")
        self.assertEqual((status, data["error"]), (404, "窗口不存在或已关闭"))
        self.pane = fake_pane(self.repo)  # repo root: outside the project that has the plan
        status, data = api_request("GET", "/api/plan?pane=%25901")
        self.assertEqual((status, data["error"]), (404, "这个窗口所在的项目没有任务计划"))
        self.pane = fake_pane(self.base)  # not a git repo at all
        self.assertEqual(api_request("GET", "/api/plan?pane=%25901")[0], 404)

    def test_track_passes_structure_and_caches_per_repo(self) -> None:
        fake = {"verdict": "warn", "counts": {"commit": 2, "ignore": 1, "review": 3, "tracked_should_ignore": 0},
                "next_hint": "2 份待提交", "groups": {"commit": []}, "structure": {"root_loose": {"count": 1, "sample": ["x"]}}}
        with mock.patch.object(server, "run_plan_cli", return_value=fake) as cli:
            status, first = api_request("GET", "/api/plan/track?pane=%25901")
            status2, second = api_request("GET", "/api/plan/track?pane=%25901")
        self.assertEqual((status, status2), (200, 200))
        cli.assert_called_once_with(["track", str(self.project)], timeout=60.0)
        self.assertEqual(first["counts"], fake["counts"])
        self.assertEqual(first["structure"], fake["structure"])
        self.assertNotIn("groups", first)
        self.assertEqual((first["cached"], second["cached"]), (False, True))
        with server._GIT_LOCK:
            server._PLAN_TRACK_CACHE.clear()
        with mock.patch.object(server, "run_plan_cli", return_value={"verdict": "ok", "counts": {}}):
            self.assertNotIn("structure", api_request("GET", "/api/plan/track?pane=%25901")[1])

    def test_real_cli_actions_on_temporary_project(self) -> None:
        def act(**payload: object) -> tuple[int, dict[str, object]]:
            return post_json("/api/plan/action", {"pane": "%901", **payload})

        status, data = act(action="add", title="-写面板", gate="测试通过")  # no id -> generated
        self.assertEqual(status, 200, data)
        self.assertEqual(data["item_id"], "P0.1.E2")
        self.assertEqual(act(action="edit", id="P0.1.E2", title="写计划面板", depends_on="P0.1.E1")[0], 200)
        self.assertEqual(act(action="start", id="P0.1.E1")[0], 200)
        self.assertEqual(act(action="note", text="-进展一\n进展二", kind="progress")[0], 200)
        self.assertEqual(act(action="block", id="P0.1.E2", reason="等 E1")[0], 200)
        self.assertEqual(act(action="reopen", id="P0.1.E2", reason="E1 有进展")[0], 200)
        self.assertEqual(act(action="cancel", id="P0.1.E2", reason="合并到 E1")[0], 200)
        text = self.plan.read_text(encoding="utf-8")
        self.assertIn("- [ ] P0.1.E1 明确首个可验证任务 · state=in_progress", text)
        self.assertIn("- [x] P0.1.E2 写计划面板 · state=cancelled · dep=P0.1.E1 · gate=测试通过 · reason=合并到 E1", text)
        logs = "".join(p.read_text(encoding="utf-8") for p in (self.plan.parent / "_logs").glob("*-plan.md"))
        self.assertIn("进展一 进展二", logs)
        self.assertIn("重开：E1 有进展", logs)

        status, data = act(action="add", id="P0.1.E1", title="dup")
        self.assertEqual((status, data["error"]), (400, "任务 ID 已存在：P0.1.E1"))
        status, data = act(action="reopen", id="P0.1.E1", reason="x")
        self.assertEqual(status, 400)
        self.assertIn("只有已完成、已取消或阻塞的任务能重开", data["error"])
        before = self.plan.read_text(encoding="utf-8")
        for bad in ({"action": "complete", "id": "P0.1.E1"}, {"action": "add", "id": "P0.1.E9", "title": "x" * 201},
                    {"action": "block", "id": "P0.1.E1", "reason": "a\x1bb"}):
            status, data = act(**bad)
            self.assertEqual(status, 400, bad)
        self.assertEqual(self.plan.read_text(encoding="utf-8"), before)

    def test_conflict_is_409_and_logged_without_text(self) -> None:
        err = io.StringIO()
        conflict = {"verdict": "block", "errors": ["task plan changed concurrently; reread and retry"]}
        with mock.patch.object(server, "run_plan_cli", return_value=conflict), contextlib.redirect_stderr(err):
            with self.assertRaises(server.PlanApiError) as ctx:
                server.plan_action_for_pane({"pane": "%901", "action": "block", "id": "P0.1.E1", "reason": "机密原因"})
        self.assertEqual((ctx.exception.status, str(ctx.exception)), (409, "计划刚被别人改过，已刷新，请重试"))
        self.assertIn("plan action pane=%901 action=block project=p result=conflict", err.getvalue())
        self.assertNotIn("机密原因", err.getvalue())


class WriteEndpointCsrfTest(PlanTestBase):
    def test_plan_action_and_archive_refuse_forged_requests(self) -> None:
        body = b'{"pane":"%1","action":"start","id":"P1.2"}'
        with mock.patch.object(server, "plan_action_for_pane") as action, \
                mock.patch.object(server, "request_archive") as archive:
            for path in ("/api/plan/action", "/api/archive-request"):
                status, payload = api_request("POST", path, body, {"Content-Type": "text/plain"})
                self.assertEqual((status, payload["error"]), (403, "write requests must be application/json"))
                status, _ = api_request("POST", path, body, {"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"})
                self.assertEqual(status, 403)
                status, _ = api_request("POST", path, body, {"Content-Type": "application/x-www-form-urlencoded"})
                self.assertEqual(status, 403)
        action.assert_not_called()
        archive.assert_not_called()

    def test_plan_reads_are_get_only(self) -> None:
        self.assertEqual(post_json("/api/plan", {"pane": "%1"})[0], 404)


class ArchiveRequestTest(PlanTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.pane = fake_pane(self.base)
        self.status = "idle"
        for name, value in (
            ("pane_by_id", lambda pid: self.pane if pid == "%901" else None),
            ("capture", lambda pid, history=240, **_k: "screen"),
            ("infer_pane_status", lambda *a, **k: self.status),
        ):
            patcher = mock.patch.object(server, name, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(server, "send_message_with_receipt",
                                    return_value={"ok": True, "pane": "%901", "job_id": "cards-x"})
        self.send = patcher.start()
        self.addCleanup(patcher.stop)

    def archive(self, **extra: object) -> tuple[int, dict[str, object]]:
        return post_json("/api/archive-request", {"pane": "%901", **extra})

    def test_idle_ai_pane_gets_the_configured_archive_prompt(self) -> None:
        status, data = self.archive(pane_pid="4242", pane_start_time="777")
        self.assertEqual(status, 200, data)
        self.assertIs(data["archive"], True)
        self.send.assert_called_once_with("%901", ARCHIVE_PROMPT_TEXT, True, expected_pid="4242", expected_start_time="777")
        # The baseline is stored for the result check.  This pane's cwd is not a repo: that is
        # recorded as a "no Git yet" baseline (the archive run may create the repo), not an error.
        self.assertEqual(data["archive_run"]["state"], "running")
        stored = server.archive_runs_snapshot()["%901"]
        self.assertEqual(stored["state"], "running")
        self.assertNotIn("base_error", stored)
        self.assertEqual(stored["base"], {"head": "", "pending": None, "ahead": None, "no_git": True})

    def test_busy_or_ai_less_panes_are_refused(self) -> None:
        for state in ("running", "waiting", "needs attention", "quota_limited"):
            self.status = state
            status, data = self.archive()
            self.assertEqual((status, data["error"]), (409, "窗口正在工作，等它空闲再归档"), state)
        self.status = "idle"
        self.pane = fake_pane(self.base, kind="Shell")
        self.assertEqual(self.archive(), (409, {"error": "这个窗口里没有运行中的 AI，不能归档"}))
        self.pane = fake_pane(self.base, ai_alive=False)
        self.assertEqual(self.archive()[0], 409)
        self.assertEqual(post_json("/api/archive-request", {"pane": "%902"})[0], 404)
        self.assertEqual(post_json("/api/archive-request", {"pane": "secretary_web:1"})[0], 400)
        self.pane = fake_pane(self.base)
        self.assertEqual(self.archive(pane_pid="1", pane_start_time="2")[0], 409)  # pane instance changed
        self.send.assert_not_called()
        self.assertEqual(server.archive_runs_snapshot(), {})


class PlanNotesTest(PlanTestBase):
    TODAY = datetime(2026, 9, 24, 12, 0)

    def setUp(self) -> None:
        super().setUp()
        self.plan = self.make_top_project(self.base / "p")
        self.logs = self.plan.parent / "_logs"
        self.logs.mkdir()

    def write_log(self, day: str, *lines: str) -> Path:
        path = self.logs / f"{day}-plan.md"
        path.write_text(f"# {day} log\n\n" + "".join(line + "\n" for line in lines), encoding="utf-8")
        return path

    def notes(self) -> tuple[dict[str, list[dict[str, str]]], bool, str]:
        return server.plan_notes_by_task(str(self.plan), self.TODAY)

    def test_parses_share_top_note_lines_only(self) -> None:
        self.write_log(
            "2026-09-24",
            "- 09:00 note:fail P1.2 方案 A 没过 · git=main@abc(+1 dirty)",
            "- 09:05 note:learn P1.2 先核新增项活跃度 · git=main@abc",
            "- 09:10 note:progress plan 整体进展 · git=main@abc",   # plan-level note: not a task
            "- 09:20 start P1.2 · git=main@abc",                     # not a note
            "note:fail P1.2 缺前缀的行",                              # malformed
            "- 09:30 note:progress P1.3 " + "字" * 500 + " · git=x",
        )
        (self.logs / "2026-09-24-journal-archive.md").write_text("- 10:00 note:fail P1.2 不是日志文件\n", encoding="utf-8")
        notes, truncated, token = self.notes()
        self.assertEqual(notes["P1.2"], [
            {"date": "2026-09-24", "time": "09:00", "kind": "fail", "text": "方案 A 没过"},
            {"date": "2026-09-24", "time": "09:05", "kind": "learn", "text": "先核新增项活跃度"},
        ])
        self.assertEqual(sorted(notes), ["P1.2", "P1.3"])
        self.assertEqual(len(notes["P1.3"][0]["text"]), server.PLAN_NOTE_TEXT_LIMIT)
        self.assertIs(truncated, False)
        self.assertTrue(token.isdigit() and int(token) > 0)
        self.assertEqual(token, server.plan_notes_token(str(self.plan), self.TODAY))

    def test_latest_five_per_task_across_days_and_60_day_window(self) -> None:
        self.write_log("2026-07-01", "- 08:00 note:fail P1.2 太旧了 · git=x")  # > 60 days before TODAY
        self.write_log("2026-09-23", *[f"- 1{i}:00 note:progress P1.2 第 {i} 条 · git=x" for i in range(6)])
        self.write_log("2026-09-24", "- 08:00 note:learn P1.2 今天 · git=x")
        notes, _, _ = self.notes()
        self.assertEqual([n["text"] for n in notes["P1.2"]], ["第 2 条", "第 3 条", "第 4 条", "第 5 条", "今天"])
        self.assertNotIn("太旧了", json.dumps(notes, ensure_ascii=False))

    def test_byte_cap_keeps_the_newest_logs(self) -> None:
        newest = self.write_log("2026-09-24", "- 08:00 note:fail P1.2 最新 · git=x")
        self.write_log("2026-09-23", *[f"- 09:{i:02d} note:progress P1.3 旧 {i} · git=x" for i in range(50)])
        with mock.patch.object(server, "PLAN_NOTES_MAX_BYTES", newest.stat().st_size + 60):
            notes, truncated, _ = self.notes()
        self.assertIs(truncated, True)
        self.assertEqual(notes["P1.2"][0]["text"], "最新")
        # Only the tail of the older log fits: its newest notes survive, the first ones do not.
        texts = [n["text"] for n in notes.get("P1.3", [])]
        self.assertLessEqual(len(texts), 1)
        self.assertTrue(all(text == "旧 49" for text in texts), texts)

    def test_no_log_dir_and_plain_top_dir(self) -> None:
        shutil.rmtree(self.logs)
        self.assertEqual(self.notes(), ({}, False, "0"))
        plain = self.base / "q" / "top" / "_task_plan.md"
        (plain.parent / "logs").mkdir(parents=True)
        plain.write_text("# p\n", encoding="utf-8")
        (plain.parent / "logs" / "2026-09-24-plan.md").write_text("- 08:00 note:learn P2.1 x · git=y\n", encoding="utf-8")
        self.assertEqual(list(server.plan_notes_by_task(str(plain), self.TODAY)[0]), ["P2.1"])


class ArchiveRunTest(PlanTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = make_repo(self.base / "mono")
        self.project = self.repo / "projects" / "p"
        self.plan = self.make_top_project(self.project)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "plan")
        for name in ("n1.txt", "n2.txt", "n3.txt"):
            (self.project / name).write_text(name, encoding="utf-8")
        self.pane = fake_pane(self.project)

    def head(self) -> str:
        return git(self.repo, "rev-parse", "HEAD").strip()

    def test_baseline_then_result_with_new_commits_and_plan_change(self) -> None:
        run = server.archive_baseline(self.pane)
        self.assertEqual(run["state"], "running")
        self.assertEqual(run["base"], {"head": self.head(), "pending": 3, "ahead": None})
        self.assertEqual((run["pane_pid"], run["pane_start_time"]), ("4242", "777"))
        self.assertRegex(run["plan"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(server.save_archive_run("%901", run))
        # What the AI does while archiving: two commits, one file left, the plan updated.
        git(self.repo, "add", "projects/p/n1.txt")
        git(self.repo, "commit", "-q", "-m", "a1")
        with self.plan.open("a", encoding="utf-8") as handle:
            handle.write("\n- 归档记录\n")
        git(self.repo, "add", "projects/p/n2.txt", str(self.plan))
        git(self.repo, "commit", "-q", "-m", "a2")
        done = server.evaluate_archive_run("%901", run)
        self.assertEqual(done["state"], "done")
        self.assertEqual(done["result"], {"new_commits": 2, "pending": 1, "ahead": None, "head": self.head(), "plan_changed": True})
        stored = server.archive_runs_snapshot()["%901"]
        self.assertEqual(stored["state"], "done")
        view = server.archive_run_view(stored, time.time())
        self.assertEqual(view["base_pending"], 3)
        self.assertEqual(view["result"], {"new_commits": 2, "pending": 1, "ahead": None, "plan_changed": True})
        self.assertNotIn("root", view)
        # The card's git numbers are refreshed together with the result.
        with server._GIT_LOCK:
            cached = server._GIT_STATUS_CACHE[str(self.repo)][1]
        self.assertEqual((cached["modified"], cached["untracked"]), (0, 1))

    def test_result_without_commits_and_without_git(self) -> None:
        run = server.archive_baseline(self.pane)
        server.save_archive_run("%901", run)
        done = server.evaluate_archive_run("%901", run)
        self.assertEqual(done["result"]["new_commits"], 0)
        self.assertEqual(done["result"]["pending"], 3)
        self.assertIs(done["result"]["plan_changed"], False)
        # No repository and none created by the archive: an honest "no Git" result, not an error.
        outside = server.archive_baseline(fake_pane(self.base))
        self.assertNotIn("base_error", outside)
        self.assertEqual(server.evaluate_archive_run("%902", outside)["result"],
                         {"new_commits": 0, "pending": None, "ahead": None, "head": "", "no_git": True,
                          "plan_changed": False})

    def test_result_is_not_written_over_a_newer_request(self) -> None:
        old = server.archive_baseline(self.pane)
        server.save_archive_run("%901", old)
        newer = {**old, "sent_at": old["sent_at"] + 1}
        server.save_archive_run("%901", newer)
        server.evaluate_archive_run("%901", old)
        self.assertEqual(server.archive_runs_snapshot()["%901"]["state"], "running")

    def wait_done(self, timeout: float = 10.0) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = server.archive_runs_snapshot().get("%901") or {}
            if run.get("state") == "done":
                return run
            time.sleep(0.02)
        raise AssertionError("archive result never computed")

    def test_tick_waits_for_idle_and_30_seconds(self) -> None:
        run = server.archive_baseline(self.pane)
        server.save_archive_run("%901", run)
        sent = float(run["sent_at"])
        runs = server.archive_runs_snapshot()
        view = server.archive_run_tick(self.pane, "idle", runs, sent + 10)  # idle, but too soon
        self.assertEqual(view["state"], "running")
        server.archive_run_tick(self.pane, "running", runs, sent + 60)       # late, but busy
        self.assertEqual(server.archive_runs_snapshot()["%901"]["state"], "running")
        with server._GIT_LOCK:
            self.assertFalse(server._ARCHIVE_EVAL_PENDING)
        server.archive_run_tick(self.pane, "idle", runs, sent + 31)
        self.assertEqual(self.wait_done()["result"]["new_commits"], 0)
        with server._GIT_LOCK:
            self.assertFalse(server._ARCHIVE_EVAL_PENDING)
        # A different pane instance behind the same id does not inherit the run.
        other = fake_pane(self.project)
        other.pane_pid = "9999"
        self.assertIsNone(server.archive_run_tick(other, "idle", server.archive_runs_snapshot(), sent + 40))

    def test_runs_expire_after_24_hours(self) -> None:
        run = server.archive_baseline(self.pane)
        stale = {**run, "sent_at": time.time() - server.ARCHIVE_RUN_TTL - 1}
        server.save_archive_run("%901", stale)
        self.assertEqual(server.archive_runs_snapshot(), {})
        server.save_archive_run("%902", run)
        stored = json.loads(server.PREFS.read_text(encoding="utf-8"))[server.ARCHIVE_RUNS_KEY]
        self.assertEqual(list(stored), ["%902"])  # the expired one was pruned on write

    def test_client_prefs_cannot_overwrite_archive_runs(self) -> None:
        run = server.archive_baseline(self.pane)
        server.save_archive_run("%901", run)
        server.merge_prefs({server.ARCHIVE_RUNS_KEY: {}, "categories": ["最近"]})
        self.assertIn("%901", server.archive_runs_snapshot())

    def test_panes_response_carries_the_archive_view(self) -> None:
        run = server.archive_baseline(self.pane)
        server.save_archive_run("%901", run)
        other = fake_pane(self.project, pane_id="%902")
        with mock.patch.object(server, "list_panes", return_value=[self.pane, other]), \
                mock.patch.object(server, "latest_jobs_by_pane_cached", return_value={}), \
                mock.patch.object(server, "pane_running_subagents", return_value=0):
            panes = server.build_panes_response("secretary_web")
        by_id = {pane["pane_id"]: pane for pane in panes}
        self.assertEqual(by_id["%901"]["archive"]["state"], "running")
        self.assertEqual(by_id["%901"]["archive"]["base_pending"], 3)
        self.assertNotIn("archive", by_id["%902"])


class IntegrationNotConfiguredTest(GitStatusTestBase):
    """Without CARDS_TOP_CLI / CARDS_ARCHIVE_PROMPT the plan and archive features
    are off: greyed-out reasons on /api/panes, 501 from the endpoints, no CLI run."""

    def setUp(self) -> None:
        super().setUp()
        for name in ("PLAN_CLI", "ARCHIVE_PROMPT"):
            patcher = mock.patch.object(server, name, None)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(server, "PREFS", self.base / "prefs.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = make_repo(self.base / "repo")
        plan = self.repo / PLAN_REL
        plan.parent.mkdir(parents=True)
        shutil.copyfile(MINIMAL_PLAN, plan)
        self.pane = fake_pane(self.repo)
        for name, value in (("pane_by_id", lambda pid: self.pane if pid == "%901" else None),
                            ("run_plan_cli", AssertionError("no CLI is configured")),
                            ("send_message_with_receipt", AssertionError("nothing may be sent"))):
            patcher = mock.patch.object(server, name, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_panel_fields_explain_the_missing_integration(self) -> None:
        fields = server.pane_panel_fields(self.pane, server.pane_git_summary(str(self.repo)), None)
        self.assertEqual(fields["plan_available"], False)
        self.assertEqual(fields["plan_reason"], server.PLAN_NOT_CONFIGURED)
        self.assertEqual(fields["archive_blocker"], server.ARCHIVE_NOT_CONFIGURED)
        self.assertIn("CARDS_TOP_CLI", server.PLAN_NOT_CONFIGURED)
        self.assertIn("CARDS_ARCHIVE_PROMPT", server.ARCHIVE_NOT_CONFIGURED)

    def test_endpoints_answer_501(self) -> None:
        for path in ("/api/plan?pane=%25901", "/api/plan/track?pane=%25901"):
            status, data = api_request("GET", path)
            self.assertEqual((status, data["error"]), (501, server.PLAN_NOT_CONFIGURED), path)
        status, data = post_json("/api/plan/action", {"pane": "%901", "action": "start", "id": "P0.1.E1"})
        self.assertEqual((status, data["error"]), (501, server.PLAN_NOT_CONFIGURED))
        status, data = post_json("/api/archive-request", {"pane": "%901"})
        self.assertEqual((status, data["error"]), (501, server.ARCHIVE_NOT_CONFIGURED))

    def test_misconfigured_paths_fail_loudly(self) -> None:
        with mock.patch.object(server, "PLAN_CLI", self.base / "missing-cli.py"):
            status, data = api_request("GET", "/api/plan?pane=%25901")
        self.assertEqual(status, 500)
        self.assertIn("CARDS_TOP_CLI", data["error"])
        empty = self.base / "empty-prompt.md"
        empty.write_text("\n", encoding="utf-8")
        with mock.patch.object(server, "ARCHIVE_PROMPT", empty), \
                mock.patch.object(server, "capture", return_value="screen"), \
                mock.patch.object(server, "infer_pane_status", return_value="idle"):
            status, data = post_json("/api/archive-request", {"pane": "%901"})
        self.assertEqual(status, 500)
        self.assertIn("CARDS_ARCHIVE_PROMPT", data["error"])


if __name__ == "__main__":
    import unittest

    unittest.main(verbosity=2)


class TrackEndpointCrossSiteTest(PlanTestBase):
    def test_track_refuses_cross_site_trigger_but_serves_same_origin(self) -> None:
        with mock.patch.object(server, "plan_track_for_pane", return_value={"ok": True}) as track:
            status, payload = api_request("GET", "/api/plan/track?pane=%251", None, {"Sec-Fetch-Site": "cross-site"})
            self.assertEqual((status, payload["error"]), (403, "cross-site request refused"))
            track.assert_not_called()
            status, payload = api_request("GET", "/api/plan/track?pane=%251", None, {"Sec-Fetch-Site": "same-origin"})
            self.assertEqual((status, payload), (200, {"ok": True}))
            track.assert_called_once_with("%1")
