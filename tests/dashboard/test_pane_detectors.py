#!/usr/bin/env python3
"""每种 AI CLI 的屏幕结构识别，各自钉住。

上游改一次界面，坏的应该只有对应那一个类和这一组断言，而不是整个状态判定。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import pane_detectors

RULE = "─" * 96
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pane_status_cases.json"


class ClaudeScreenTest(unittest.TestCase):
    def test_input_box_is_found_from_the_bottom_pair_of_rules(self) -> None:
        split = pane_detectors.split_screen(["hello", "world", RULE, "❯", RULE, "  ⏵⏵ auto mode on"])
        self.assertTrue(split.has_input_region)
        self.assertEqual(split.provider, "claude")
        self.assertEqual(split.conversation, ["hello", "world"])
        self.assertIn("⏵⏵ auto mode on", split.input_lines[-1])

    def test_the_top_rule_may_carry_a_project_name(self) -> None:
        """真实的上边框常在右端挂着项目名，只认纯横线会整个漏掉输入框。"""
        lines = ["a", "b", "─" * 60 + " some-project ─", "❯", RULE, "  ⏵⏵ auto mode on"]
        split = pane_detectors.split_screen(lines)
        self.assertTrue(split.has_input_region)
        self.assertEqual(split.conversation, ["a", "b"])

    def test_a_far_away_rule_is_not_treated_as_the_top_border(self) -> None:
        """输入框很矮；远处的分隔线不能被当成上边框，否则会把半屏对话吞进输入区。"""
        lines = ["─" * 60 + " header ─"] + [f"line{i}" for i in range(20)] + [RULE, "❯"]
        split = pane_detectors.split_screen(lines)
        self.assertFalse(split.has_input_region)

    def test_subagent_panel_below_the_footer_stays_in_the_input_region(self) -> None:
        """子智能体面板挂在状态栏下面，行数随子智能体个数变；它不能把输入框"顶"出底部。

        形状取自 Claude Code 2.1.280 的真实屏幕。
        """
        panel = ["", "  ● main"] + [f"  ◯ fast-worker (+1)  Reading file{i}.md" for i in range(10)]
        lines = ["● 回复", "✻ Waiting for 10 background agents to finish", "", RULE, "❯", RULE,
                 "  ⏵⏵ bypass permissions on · 1 shell · /tasks to see subagents · ← for agents · ↓ to manage"] + panel
        split = pane_detectors.split_screen(lines)
        self.assertTrue(split.has_input_region)
        self.assertEqual(split.conversation[-1], "")
        self.assertEqual(pane_detectors.strip_agent_panel(lines), lines[:7])

    def test_a_top_level_assistant_bullet_is_not_panel_chrome(self) -> None:
        """AI 回复的 "● " 顶格写；死掉的输入框下面跟着的回复不能被当成面板放过。"""
        lines = ["x", RULE, "❯", RULE] + [f"● reply {i}" for i in range(8)]
        self.assertFalse(pane_detectors.split_screen(lines).has_input_region)

    def test_a_single_rule_is_not_enough(self) -> None:
        self.assertFalse(pane_detectors.split_screen(["a", RULE, "❯"]).has_input_region)


class CodexScreenTest(unittest.TestCase):
    def test_prompt_line_anchors_the_input_region(self) -> None:
        split = pane_detectors.split_screen(
            ["done", "› Ask Codex to do anything", "  gpt-5.6-luna medium · ~/x"]
        )
        self.assertTrue(split.has_input_region)
        self.assertEqual(split.provider, "codex")
        self.assertEqual(split.conversation, ["done"])

    def test_codex_wins_over_a_stray_rule_pair(self) -> None:
        """Codex 屏幕里也可能出现横线；文案锚点比几何特征更可信，必须先命中。"""
        split = pane_detectors.split_screen(
            [RULE, "x", RULE, "› Ask Codex to do anything", "  gpt-5.6-luna medium"]
        )
        self.assertEqual(split.provider, "codex")


class NoInputRegionTest(unittest.TestCase):
    def test_plain_shell_output_has_no_input_region(self) -> None:
        split = pane_detectors.split_screen(["$ ls", "a.txt", "b.txt", "$ "])
        self.assertFalse(split.has_input_region)
        self.assertEqual(split.provider, "")

    def test_empty_screen_is_handled(self) -> None:
        self.assertFalse(pane_detectors.split_screen([]).has_input_region)


class RealScreenCoverageTest(unittest.TestCase):
    def test_live_ai_screens_all_expose_an_input_region(self) -> None:
        """真实抓屏里，凡是判成 idle/running/waiting 的 AI 窗口都该找得到输入区——
        找不到就说明结构识别对某种界面失效了，那正是状态判定要开始瞎猜的前一步。"""
        cases = json.loads(FIXTURES.read_text(encoding="utf-8"))
        missing = [
            (c["window"], c["kind"])
            for c in cases
            if c["kind"] in {"Claude", "Codex"}
            # waiting 排除在外: 停在选择器上时输入框本来就被选项列表替掉了,
            # 那时"没有输入区"是正确描述,不是识别失败。
            and c["expected_status"] in {"idle", "running"}
            and not pane_detectors.split_screen(c["capture_tail"].splitlines()).has_input_region
        ]
        self.assertEqual(missing, [], f"这些真实窗口没识别出输入区: {missing}")


if __name__ == "__main__":
    unittest.main()


class AnchorMustBeAtBottomTest(unittest.TestCase):
    """输入区必须真的长在屏幕底部。

    锚点也会出现在滚动历史里——上一屏的输入框、或者有人往对话里贴了一段界面截图。
    把那种残影当成"此刻在等你打字"，会让一个已经死掉的会话永远显示 idle，
    needs attention 告警整个失效。
    """

    def test_a_scrolled_away_claude_input_box_is_not_the_live_one(self) -> None:
        lines = ["old", RULE, "❯", RULE] + [f"后来又输出了第{i}行" for i in range(30)]
        self.assertFalse(pane_detectors.split_screen(lines).has_input_region)

    def test_a_codex_prompt_quoted_in_the_conversation_is_not_the_live_one(self) -> None:
        lines = ["› Ask Codex to do anything"] + [f"line{i}" for i in range(30)]
        self.assertFalse(pane_detectors.split_screen(lines).has_input_region)

    def test_the_real_bottom_anchor_still_wins(self) -> None:
        lines = ["old", RULE, "❯", RULE, "更多输出", RULE, "❯", RULE, "  ⏵⏵ auto mode on"]
        split = pane_detectors.split_screen(lines)
        self.assertTrue(split.has_input_region)
        self.assertIn("更多输出", split.conversation)

    def test_every_real_screen_still_passes(self) -> None:
        """加了位置约束不能把真实窗口误伤掉。"""
        cases = json.loads(FIXTURES.read_text(encoding="utf-8"))
        missing = [
            c["window"] for c in cases
            if c["kind"] in {"Claude", "Codex"} and c["expected_status"] in {"idle", "running"}
            and not pane_detectors.split_screen(c["capture_tail"].splitlines()).has_input_region
        ]
        self.assertEqual(missing, [], f"位置约束误伤了这些真实窗口: {missing}")
