#!/usr/bin/env python3
"""Card Git line: run the real cardGitHtml/formatAgeShort from index.html in node."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import unittest


INDEX = (Path(__file__).resolve().parents[2] / "dashboard" / "index.html")


def snippet(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    return source[start:source.index(end_marker, start)]


class FrontendCardGitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        source = INDEX.read_text(encoding="utf-8")
        cls.source = source
        cls.prelude = "\n".join([
            snippet(source, "    const escapeHtml = (value) =>", "\n\n"),
            snippet(source, "    function formatAgeShort(", "\n    // 卡片底部 Git 小字行"),
            snippet(source, "    function cardGitHtml(", "\n    async function loadPanes("),
            snippet(source, "    const ARCHIVE_BADGE_SECONDS", "\n    // 详情标题栏右侧"),
        ])

    def render(self, git: object, **pane: object) -> str:
        payload = json.dumps({"git": git, **pane}, ensure_ascii=False)
        script = self.prelude + f"\nprocess.stdout.write(cardGitHtml({payload}));"
        result = subprocess.run(["node", "-e", script], check=False, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return result.stdout

    def test_no_git_field_renders_nothing(self) -> None:
        self.assertEqual(self.render(None), "")

    def test_full_line(self) -> None:
        html = self.render({
            "state": "ok", "branch": "main", "dirty": 3, "ahead": 2, "has_remote": True,
            "last_commit_age": 7200, "last_commit_subject": "fix <b> & \"x\"", "task": "P1.2", "root": "/r",
        })
        self.assertIn('<span class="cg-branch">main</span>', html)
        self.assertIn('<span class="cg-dirty">未提交 3</span>', html)
        self.assertIn('<span class="cg-ahead cg-warn">未推送 2</span>', html)
        self.assertIn('<span class="cg-age">上次归档 2小时前</span>', html)
        self.assertIn('<span class="cg-task" title="任务 P1.2">▶ P1.2</span>', html)
        self.assertIn('title="最近提交：fix &lt;b&gt; &amp; &quot;x&quot;"', html)

    def test_legacy_highlight_and_caps(self) -> None:
        html = self.render({"state": "ok", "branch": "dev", "dirty": 999, "ahead": None, "has_remote": False,
                            "last_commit_age": 30})
        self.assertIn('<span class="cg-dirty cg-alert">未提交 999+</span>', html)
        self.assertIn('<span class="cg-remote cg-warn">无远端</span>', html)
        self.assertIn("上次归档 刚刚", html)
        self.assertIn('class="cg-dirty cg-alert">未提交 50<', self.render({"state": "ok", "dirty": 50}))
        self.assertIn('class="cg-dirty">未提交 49<', self.render({"state": "ok", "dirty": 49}))

    def pending(self, modified: int, untracked: int, age: object) -> str:
        return self.render({"state": "ok", "branch": "main", "modified": modified, "untracked": untracked,
                            "dirty": min(modified + untracked, 999), "last_commit_age": age})

    def test_pending_count_label_title_and_split(self) -> None:
        html = self.pending(19, 999, 3 * 3600)
        self.assertIn(
            '<span class="cg-pending cg-alert" title="新文件 999（未跟踪且未忽略）· 改动 19（已追踪、未提交）">'
            '待归档 999+<span class="cg-split">新 999+ · 改 19</span></span>',
            html,
        )
        self.assertIn('<span class="cg-age">上次归档 3小时前</span>', html)
        self.assertNotIn(">未提交 ", html)  # the tooltip may say 未提交; the legacy counter must be gone
        self.assertNotIn("未跟踪 ", html)
        self.assertIn(">待归档 7<span class=\"cg-split\">新 3 · 改 4</span>", self.pending(4, 3, 60))

    def test_pending_color_thresholds(self) -> None:
        hour, day = 3600, 86400
        cases = [
            # (modified, untracked, age, expected class suffix)
            (3, 4, 2 * hour, ""),               # few, archived recently -> neutral
            (0, 49, 23 * hour, ""),
            (0, 50, hour, " cg-amber"),          # >= 50 -> amber
            (1, 0, 25 * hour, " cg-amber"),      # > 24h since last archive -> amber
            (5, 0, None, " cg-amber"),           # never archived (no commit) -> amber
            (120, 79, hour, " cg-amber"),        # 199 -> still amber
            (100, 100, hour, " cg-alert"),       # >= 200 -> red
            (1, 0, 8 * day, " cg-alert"),        # > 7 days -> red
        ]
        for modified, untracked, age, cls in cases:
            with self.subTest(modified=modified, untracked=untracked, age=age):
                self.assertIn(f'<span class="cg-pending{cls}" title=', self.pending(modified, untracked, age))

    def test_zero_pending_hides_counter_even_if_old(self) -> None:
        html = self.pending(0, 0, 30 * 86400)
        self.assertNotIn("待归档", html)
        self.assertNotIn("cg-alert", html)
        self.assertIn("上次归档 1个月前", html)
        self.assertNotIn("上次归档", self.pending(0, 3, None))  # no commit yet -> no relative time

    def test_split_hidden_on_narrow_screens(self) -> None:
        self.assertRegex(self.source, r"@media \(max-width: 760px\) \{\s*\.card-git \.cg-split \{\s*display: none;")
        self.assertIn(".card-git .cg-amber {", self.source)

    def test_legacy_dirty_only_falls_back_to_total(self) -> None:
        html = self.render({"state": "ok", "branch": "main", "dirty": 12})
        self.assertIn('<span class="cg-dirty">未提交 12</span>', html)
        self.assertNotIn("待归档", html)

    def test_task_title_label_truncation_and_tooltip(self) -> None:
        title = "PMA团队破局：5号确认提升=加深纹理与10000步 <x>"
        html = self.render({"state": "ok", "task": "P3.9.PMA14", "task_title": title, "task_gate": "先跑 A & B"})
        self.assertIn('title="任务 P3.9.PMA14：PMA团队破局：5号确认提升=加深纹理与10000步 &lt;x&gt;；验收：先跑 A &amp; B"', html)
        # Label is cut at 32 display units (CJK = 2) and ends with an ellipsis.
        label = html.split("▶ ", 1)[1].split("</span>", 1)[0]
        self.assertTrue(label.endswith("…"), label)
        width = sum(2 if ord(ch) >= 0x1100 else 1 for ch in label[:-1])
        self.assertLessEqual(width, 32)
        self.assertGreaterEqual(width, 31)
        short = self.render({"state": "ok", "task": "P1.2", "task_title": "写测试", "task_gate": None})
        self.assertIn('<span class="cg-task" title="任务 P1.2：写测试">▶ 写测试</span>', short)
        fallback = self.render({"state": "ok", "task": "P1.2", "task_title": None})
        self.assertIn('<span class="cg-task" title="任务 P1.2">▶ P1.2</span>', fallback)

    def test_clean_pushed_repo_shows_no_counters(self) -> None:
        html = self.render({"state": "ok", "branch": "main", "dirty": 0, "ahead": 0, "has_remote": True,
                            "last_commit_age": 86400 * 3, "task": None})
        self.assertNotIn("未提交", html)
        self.assertNotIn("待归档", html)
        self.assertNotIn("未推送", html)
        self.assertNotIn("无远端", html)
        self.assertIn("上次归档 3天前", html)

    def test_pending_and_unknown_states(self) -> None:
        self.assertIn("Git 读取中", self.render({"state": "pending", "root": "/r"}))
        html = self.render({"state": "unknown", "error": "timeout after 2s: git status"})
        self.assertIn("Git 状态未知", html)
        self.assertIn('title="timeout after 2s: git status"', html)

    def test_card_template_and_signature_use_git_line(self) -> None:
        render_cards = snippet(self.source, "    function renderCards() {", "\n    // 时间线横向滑动动画")
        self.assertIn("cardGitHtml(p),", render_cards)
        self.assertIn("${cardGitHtml(pane)}", render_cards)
        # 计划/归档按钮挪到了详情标题栏: 卡片模板里不再有它们, ▶ 任务标题负责打开计划。
        self.assertNotIn("card-tool card-plan", render_cards)
        self.assertNotIn("archive", render_cards)
        self.assertNotIn(">计划</button>", render_cards)
        self.assertNotIn(">归档</button>", render_cards)
        self.assertIn('cards.querySelectorAll("[data-card-plan]")', render_cards)

    def test_task_title_opens_plan_only_when_the_project_has_one(self) -> None:
        git = {"state": "ok", "task": "P1.2", "task_title": "写测试", "has_plan": True}
        html = self.render(git, pane_id="%7")
        self.assertIn('<button type="button" class="cg-task cg-task-link" data-card-plan="%7" '
                      'title="任务 P1.2：写测试（点开看计划）">▶ 写测试</button>', html)
        html = self.render({**git, "has_plan": False}, pane_id="%7")
        self.assertIn('<span class="cg-task" title="任务 P1.2：写测试">▶ 写测试</span>', html)
        self.assertNotIn("data-card-plan", html)

    def test_archive_badge_running_done_and_expired(self) -> None:
        git = {"state": "ok", "branch": "main"}
        running = self.render(git, pane_id="%7", archive={"state": "running", "sent_at": 1, "age_s": 5})
        self.assertIn('<span class="cg-archive-run"', running)
        self.assertIn(">归档中</span>", running)
        self.assertLess(running.index("归档中"), running.index("cg-branch"))
        done = {"state": "done", "sent_at": 1, "age_s": 120, "base_pending": 50,
                "result": {"new_commits": 4, "pending": 3, "plan_changed": True}}
        html = self.render(git, pane_id="%7", archive=done)
        self.assertIn('class="cg-archive-done" title="归档完成：待归档 50→3，新增 4 个提交，计划已更新">归档✓</span>', html)
        self.assertNotIn("归档", self.render(git, pane_id="%7", archive={**done, "age_s": 3601}))  # 1 小时后消失
        failed = self.render(git, pane_id="%7", archive={**done, "result": {"error": "git status unknown"}})
        self.assertIn('class="cg-archive-done is-error" title="归档结果读取失败：git status unknown">归档?</span>', failed)



if __name__ == "__main__":
    unittest.main()
