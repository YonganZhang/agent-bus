#!/usr/bin/env python3
"""Plan panel v2 (the plan file itself, planning-with-files style) and the
detail-header plan/archive tools: run the real index.html helpers in node.

The task list fed to the tree is what the plan CLI's ``plan list`` returns for
the same fixture text (the fake CLI in fixtures/), the way /api/plan's
``tasks`` is.

Run: python3 -m pytest test_frontend_plan.py -q
"""

from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path

import server
from plan_stub import plan_list_items

INDEX = (Path(__file__).resolve().parents[2] / "dashboard" / "index.html")

PLAN = """# Demo · Task Plan

🧭 当前 [P3.9] task=[P3.9.B2] 重建主线
> Done Criteria: 全部验收 **通过**

## Current Coordinate

- Goal: 做完 demo
- Current Task: P3.9.B2
- Next Task: P3.9.B1
- Latest Learning: 先核对
- Blocker: 无

旧交接只作历史。

## Active Work

- [x] P3.9.A1 第一件
- [x] P3.9.A2 第二件
- [x] P3.9.A3 第三件
- [ ] P3.9.B1 父任务 · gate=全量测试 · dep=P3.9.A3 · ev=_outputs/x.json
  - [x] P3.9.B1.1 子任务一
  - [ ] P3.9.B1.2 子任务二 · state=blocked · reason=等数据
- [ ] P3.9.B2 进行中任务 · state=in_progress
- [x] P3.9.B3 取消的 · state=cancelled · reason=合并
- [ ] P3.9.C.1 父缺失的子任务
- [ ] 没有 ID 的坏行
- [ ] P2.1 旧阶段一
- [x] P2.2 旧阶段二
- [ ] P4.1 无名阶段一
- [ ] P4.2 无名阶段二
说明文字

## Phase 进展

| Phase | 内容 | 状态 |
|---|---|---|
| **P2** | 早期探索 | 完成 |

## 决策表

| 时间 | 决策 | 理由 |
|---|---|---|
| 09-24 | 用大纲树 | 像 planning-with-files |

## 错误表

- 无

## 接力协议

1. 先读 stamp
"""


def snippet(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    return source[start:source.index(end_marker, start)]


class PlanFrontendBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        source = INDEX.read_text(encoding="utf-8")
        cls.source = source
        cls.prelude = "\n".join([
            "const state = { panes: [], archiveDismissed: {} };",
            "const localStorage = { getItem: () => null, setItem: () => {} };",
            snippet(source, "    const escapeHtml = (value) =>", "\n    function kindClass(kind)"),
            snippet(source, "    function formatAgeShort(", "\n    // 卡片底部 Git 小字行"),
            snippet(source, "    const PLAN_REFRESH_MS", "\n    function renderPlanSub("),
            snippet(source, "    const ARCHIVE_BADGE_SECONDS", "\n    function renderDetailPlanTools("),
        ])
        cls.tasks = plan_list_items(PLAN)

    def js(self, body: str) -> object:
        script = self.prelude + "\nconst __out = (() => {\n" + body + "\n})();\nprocess.stdout.write(JSON.stringify(__out));"
        result = subprocess.run(["node", "-e", script], check=False, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def plan_html(self, extra: str = "", notes: object = None) -> str:
        data = {"raw": PLAN, "tasks": self.tasks, "show": {"next": {"id": "P3.9.B1", "title": "父任务"}, "blocker": "无"},
                "notes_by_task": notes or {}}
        return str(self.js(f"planState.data = {json.dumps(data, ensure_ascii=False)};\n{extra}\nreturn planDocumentHtml(planState.data);"))

    def tree(self, extra: str = "") -> list[dict[str, object]]:
        """Flattened tree: [{depth, kind, id, outline, state, agg, name, text}]"""
        return self.js(f"""
{extra}
const doc = parsePlanDocument({json.dumps(PLAN, ensure_ascii=False)});
const section = doc.sections.find((s) => s.heading === "Active Work");
const roots = buildPlanTaskTree(section.lines, {json.dumps(self.tasks, ensure_ascii=False)}, planPhaseNames(doc));
const out = [];
const walk = (level, depth) => level.forEach((n) => {{
  out.push({{ depth, kind: n.kind, id: n.id || "", outline: n.outline || "", state: n.kind === "raw" ? "" : planNodeState(n),
             agg: n.agg || null, name: n.name || "", text: n.text || "" }});
  if (n.children) walk(n.children, depth + 1);
}});
walk(roots, 0);
return out;""")  # type: ignore[return-value]


class PlanTreeTest(PlanFrontendBase):
    def test_plan_cli_sees_the_fixture_tasks(self) -> None:
        ids = [task["id"] for task in self.tasks]
        self.assertEqual(len(ids), 13)
        self.assertNotIn("没有", " ".join(ids))
        self.assertEqual(next(t for t in self.tasks if t["id"] == "P3.9.B1.2")["state"], "blocked")

    def test_nesting_virtual_phases_outline_numbers_and_raw_lines(self) -> None:
        rows = self.tree()
        shape = [(r["depth"], r["kind"], r["id"] or r["text"], r["outline"]) for r in rows]
        self.assertEqual(shape, [
            (0, "phase", "P3.9", "1"),
            (1, "task", "P3.9.A1", "1.1"),
            (1, "task", "P3.9.A2", "1.2"),
            (1, "task", "P3.9.A3", "1.3"),
            (1, "task", "P3.9.B1", "1.4"),
            (2, "task", "P3.9.B1.1", "1.4.1"),  # child hangs under its parent
            (2, "task", "P3.9.B1.2", "1.4.2"),
            (1, "task", "P3.9.B2", "1.5"),
            (1, "task", "P3.9.B3", "1.6"),
            (1, "task", "P3.9.C.1", "1.7"),     # parent P3.9.C missing -> flat among its would-be siblings
            (1, "raw", "- [ ] 没有 ID 的坏行", ""),  # unparsed line kept verbatim, no number
            (0, "phase", "P2", "2"),
            (1, "task", "P2.1", "2.1"),
            (1, "task", "P2.2", "2.2"),
            (0, "phase", "P4", "3"),
            (1, "task", "P4.1", "3.1"),
            (1, "task", "P4.2", "3.2"),
            (1, "raw", "说明文字", ""),
        ])
        names = {r["id"]: r["name"] for r in rows if r["kind"] == "phase"}
        self.assertEqual(names, {"P3.9": "重建主线", "P2": "早期探索", "P4": ""})  # stamp, Phase 进展 table, none

    def test_phase_state_and_counts_are_aggregated(self) -> None:
        rows = {r["id"]: r for r in self.tree() if r["kind"] == "phase"}
        self.assertEqual(rows["P3.9"]["state"], "in_progress")
        # 8 counted tasks under P3.9 (the cancelled one is not), 4 done.
        self.assertEqual((rows["P3.9"]["agg"]["done"], rows["P3.9"]["agg"]["total"]), (4, 8))
        self.assertEqual((rows["P2"]["state"], rows["P2"]["agg"]["done"], rows["P2"]["agg"]["total"]), ("pending", 1, 2))
        self.assertEqual(rows["P4"]["state"], "pending")
        cases = {
            "all_done": ("P5.1 a · state=done", "P5.2 b · state=cancelled", "done"),
            "blocked": ("P5.1 a · state=blocked · reason=r", "P5.2 b", "blocked"),
            "running_beats_blocked": ("P5.1 a · state=blocked · reason=r", "P5.2 b · state=in_progress", "in_progress"),
            "pending": ("P5.1 a · state=done", "P5.2 b", "pending"),
        }
        for label, (first, second, expected) in cases.items():
            with self.subTest(label):
                lines = [f"- [ ] {first}", f"- [ ] {second}"]
                tasks = plan_list_items("# t\n\n## Active Work\n\n" + "\n".join(lines) + "\n")
                got = self.js(f"""
const roots = buildPlanTaskTree({json.dumps(lines, ensure_ascii=False)}, {json.dumps(tasks, ensure_ascii=False)});
return [roots.length, roots[0].kind, planNodeState(roots[0])];""")
                self.assertEqual(got, [1, "phase", expected])


class PlanTabRenderTest(PlanFrontendBase):
    def test_summary_bar_progress_current_next_and_hidden_empty_blocker(self) -> None:
        html = self.plan_html()
        # 13 tasks, 1 cancelled -> 12 counted, 5 done (A1-A3, B1.1, P2.2).
        counted = [t for t in self.tasks if t["state"] != "cancelled"]
        done = sum(t["state"] == "done" for t in counted)
        self.assertIn(f"完成 {done}/{len(counted)} · 阻塞 1", html)
        self.assertIn(f'style="width:{round(done / len(counted) * 100)}%"', html)
        self.assertIn('data-plan-jump="P3.9.B2"', html)
        self.assertIn("▶ 进行中任务", html)
        self.assertIn('<span class="plan-summary-label">下一步</span>', html)
        self.assertNotIn("is-blocker", html)  # Blocker "无" -> no blocker line
        html = str(self.js(f"""return planSummaryHtml({{ tasks: [], show: {{ blocker: "等 GPU 空出来" }} }});"""))
        self.assertIn('<div class="plan-summary-line is-blocker"><span class="plan-summary-label">阻塞</span><span class="plan-summary-value">等 GPU 空出来</span>', html)

    def test_head_done_criteria_and_coordinate_cards(self) -> None:
        html = self.plan_html()
        self.assertIn('<h3 class="plan-doc-title">Demo · Task Plan</h3>', html)
        self.assertIn('<div class="plan-stamp">🧭 当前 [P3.9] task=[P3.9.B2] 重建主线</div>', html)
        self.assertIn('<div class="plan-done-criteria"><b>Done Criteria · 完成标准</b>全部验收 <strong>通过</strong></div>', html)
        for label in ("目标", "当前任务", "下一步", "最新心得", "卡点"):
            self.assertIn(f'<div class="plan-k">{label}</div>', html)
        # Task IDs in the coordinate are shown with their titles; the ID stays as small grey text.
        self.assertIn('进行中任务 <span class="plan-row-id">P3.9.B2</span>', html)
        self.assertIn("旧交接只作历史。", html)  # non key-value line of the section is kept

    def test_state_icons_highlight_reason_and_cancelled(self) -> None:
        html = self.plan_html()
        self.assertIn('class="plan-node is-task st-in_progress" data-plan-node="P3.9.B2"', html)
        self.assertIn('<span class="plan-icon" aria-label="进行中">▶</span>', html)
        self.assertIn('class="plan-node is-task st-cancelled" data-plan-node="P3.9.B3"', html)
        self.assertIn('<span class="plan-icon" aria-label="取消">✕</span>', html)
        self.assertIn('<span class="plan-icon" aria-label="待办">☐</span>', html)
        self.assertIn('<span class="plan-row-title">进行中任务</span><span class="plan-row-id">P3.9.B2</span>', html)
        # The blocked child sits inside the collapsed B1 branch; expand it to see the reason line.
        expanded = self.plan_html('planState.expanded.set("P3.9.B1", true);')
        self.assertIn('<span class="plan-icon" aria-label="阻塞">⛔</span>', expanded)
        self.assertIn('<span class="plan-row-reason">原因：等数据</span>', expanded)
        self.assertIn('<span class="plan-icon" aria-label="完成">✅</span>', expanded)
        # Metadata is folded until the row is opened; labels are 依赖/验收/证据.
        self.assertNotIn("<dt>验收</dt>", html)
        opened = self.plan_html('planState.openTasks.add("P3.9.B1");')
        for label, value in (("依赖", "P3.9.A3"), ("验收", "全量测试"), ("证据", "_outputs/x.json")):
            self.assertRegex(opened, rf"<dt>{label}</dt><dd>[^<]*{re.escape(value)}")
        self.assertIn('data-plan-act="start" data-plan-task="P3.9.B1"', opened)

    def test_default_expansion_follows_the_in_progress_path(self) -> None:
        html = self.plan_html()
        self.assertIn('data-plan-toggle="P3.9" aria-expanded="true"', html)
        self.assertIn('data-plan-toggle="P2" aria-expanded="false"', html)
        self.assertIn('data-plan-toggle="P4" aria-expanded="false"', html)
        self.assertIn('data-plan-toggle="P3.9.B1" aria-expanded="false"', html)
        self.assertNotIn('data-plan-node="P2.1"', html)
        self.assertNotIn('data-plan-node="P3.9.B1.1"', html)
        self.assertIn('<span class="plan-outline">1</span>', html)
        self.assertIn('<span class="plan-row-title">重建主线</span><span class="plan-row-id">阶段 P3.9</span><span class="plan-row-count"', html)
        self.assertIn('<span class="plan-row-title">阶段 P4</span><span class="plan-row-count"', html)
        self.assertIn('- [ ] 没有 ID 的坏行', html)  # raw line inside the expanded phase
        all_open = self.plan_html("planState.showAll = true;")
        for node in ("P2.1", "P3.9.B1.1", "P3.9.A1"):
            self.assertIn(f'data-plan-node="{node}"', all_open)
        self.assertIn("说明文字", all_open)

    def test_completed_runs_fold_until_expanded(self) -> None:
        html = self.plan_html()
        self.assertIn('data-plan-run="P3.9.A1">✅ 已完成 3 项 · 1.1–1.3（展开）</button>', html)
        self.assertNotIn('data-plan-node="P3.9.A2"', html)
        self.assertIn('data-plan-node="P3.9.B3"', html)  # a single closed row is not folded
        opened = self.plan_html('planState.openRuns.add("P3.9.A1");')
        self.assertIn("收起已完成 3 项（1.1–1.3）", opened)
        self.assertIn('data-plan-node="P3.9.A2"', opened)

    def test_other_sections_render_as_markdown_with_default_open_state(self) -> None:
        html = self.plan_html()
        sections = re.findall(r'<details class="plan-doc-section" data-plan-section="([^"]+)"( open)?>', html)
        self.assertEqual(sections, [
            ("Current Coordinate", " open"), ("Active Work", " open"), ("Phase 进展", " open"),
            ("决策表", " open"), ("错误表", " open"), ("接力协议", ""),
        ])
        decisions = html[html.index('data-plan-section="决策表"'):html.index('data-plan-section="错误表"')]
        self.assertIn('<div class="markdown plan-md"><div class="md-table-wrap"><table>', decisions)
        self.assertIn("<td>用大纲树</td>", decisions)
        self.assertIn("<ol><li>先读 stamp</li></ol>", html)
        user_closed = self.plan_html('planState.sections["决策表"] = false;')
        self.assertIn('data-plan-section="决策表">', user_closed)

    def test_notes_badges_and_list(self) -> None:
        notes = {"P3.9.B2": [
            {"date": "2026-09-23", "time": "10:00", "kind": "fail", "text": "方案 A 没过"},
            {"date": "2026-09-24", "time": "09:00", "kind": "learn", "text": "先核新增项"},
            {"date": "2026-09-24", "time": "09:10", "kind": "progress", "text": "跑完一轮"},
            {"date": "2026-09-24", "time": "09:20", "kind": "progress", "text": "跑完两轮"},
        ]}
        html = self.plan_html(notes=notes)
        self.assertIn('data-plan-notes="P3.9.B2" aria-expanded="false" title="最近 4 条笔记（点开看）">'
                      '<span class="k-fail">✗ 1</span><span class="k-learn">💡 1</span><span class="k-progress">· 2</span></button>', html)
        self.assertNotIn("方案 A 没过", html)
        opened = self.plan_html('planState.openNotes.add("P3.9.B2");', notes=notes)
        self.assertIn('<div class="plan-note k-fail"><span class="plan-note-head">✗ 失败 · 09-23 10:00</span> 方案 A 没过</div>', opened)
        self.assertLess(opened.index("跑完两轮"), opened.index("方案 A 没过"))  # newest first


class DetailToolsTest(PlanFrontendBase):
    def tools(self, pane: dict[str, object]) -> str:
        return str(self.js(f"return detailPlanToolsHtml({json.dumps(pane, ensure_ascii=False)});"))

    def test_git_brief_plan_and_archive_buttons(self) -> None:
        pane = {"pane_id": "%9", "kind": "Claude", "ai_alive": True,
                "git": {"state": "ok", "modified": 20, "untracked": 40, "ahead": 2, "last_commit_age": 3600, "has_plan": True}}
        html = self.tools(pane)
        self.assertIn('<span class="dpt-pending cg-amber" title="新文件 40 · 改动 20">待归档 60</span>', html)
        self.assertIn('<span class="dpt-ahead cg-warn">未推送 2</span>', html)
        self.assertIn('<button type="button" class="dpt-plan" data-detail-plan="%9"', html)
        self.assertRegex(html, r'<button type="button" class="dpt-archive" data-detail-archive="%9" title="让窗口里的 AI')
        clean = self.tools({**pane, "git": {"state": "ok", "modified": 0, "untracked": 0, "ahead": 0}})
        self.assertNotIn("待归档", clean)
        self.assertNotIn("未推送", clean)
        # no plan -> the 计划 button stays, greyed, with the reason as its title
        self.assertIn('class="dpt-plan is-disabled" aria-disabled="true" data-detail-plan="%9" title="这个项目还没有计划', clean)

    def test_archive_disabled_with_reason_without_ai(self) -> None:
        git = {"state": "ok", "has_plan": True}
        shell = self.tools({"pane_id": "%9", "kind": "Shell", "git": git})
        self.assertIn('class="dpt-archive is-disabled" aria-disabled="true" data-detail-archive="%9" title="窗口里没有 AI 进程（Claude/Codex），不能归档"', shell)
        dead = self.tools({"pane_id": "%9", "kind": "Codex", "ai_alive": False, "git": git})
        self.assertIn('aria-disabled="true" data-detail-archive="%9" title="窗口里的 AI 进程已退出，不能归档"', dead)

    def test_archive_running_and_result_line(self) -> None:
        pane = {"pane_id": "%9", "kind": "Claude", "git": {"state": "ok", "has_plan": True}}
        running = self.tools({**pane, "archive": {"state": "running", "sent_at": 100.5}})
        self.assertIn(">归档中…</span>", running)
        self.assertIn('aria-disabled="true" data-detail-archive="%9" title="归档进行中，等结果出来再发"', running)
        done = {"state": "done", "sent_at": 100.5, "age_s": 30, "base_pending": 50,
                "result": {"new_commits": 4, "pending": 3, "plan_changed": True}}
        html = self.tools({**pane, "archive": done})
        self.assertIn('data-archive-dismiss="%9" title="归档完成：待归档 50→3，新增 4 个提交，计划已更新（点一下关闭）">'
                      "归档完成：待归档 50→3，新增 4 个提交，计划已更新 ×</button>", html)
        empty = self.tools({**pane, "archive": {**done, "result": {"new_commits": 0, "pending": 50, "plan_changed": False}}})
        self.assertIn("dpt-archive-result is-empty", empty)
        self.assertIn("归档结束但没有新提交，去窗口看 AI 的回复", empty)
        dismissed = self.js(f"""state.archiveDismissed = {{ "%9@100.5": Date.now() }};
return detailPlanToolsHtml({json.dumps({**pane, "archive": done}, ensure_ascii=False)});""")
        self.assertNotIn("归档完成", str(dismissed))

    def test_markup_placement_and_narrow_screen_menu(self) -> None:
        head = snippet(self.source, '<header class="detail-head" id="detailHead">', "</header>")
        self.assertIn('<div class="detail-plan-tools" id="detailPlanTools"></div>', head)
        menu = snippet(head, '<div class="settings-body">', '<div class="settings-actions">')
        self.assertIn('<div class="settings-plan-actions" id="settingsPlanActions"></div>', menu)
        self.assertRegex(self.source, r"@media \(max-width: 640px\) \{\s*\.detail-plan-tools \{\s*display: none;\s*\}\s*"
                                      r"\.settings-plan-actions \{\s*display: flex;")
        render = snippet(self.source, "    function renderDetailPlanTools()", "\n    function handleDetailPlanToolsClick")
        self.assertIn('el("detailPlanTools").innerHTML = html;', render)
        self.assertIn('el("settingsPlanActions").innerHTML = html;', render)
    # 手机端的“计划”按钮现在在 ⌁ 展开的 ESC 那一组里，见 test_terminal_jump.FrontendTest。

if __name__ == "__main__":
    unittest.main()
