#!/usr/bin/env python3
"""Regression tests for Claude picker parsing (extract_choice_block).

Covers the cases raised in a Codex/Gemini review:
- a real ❯ picker is extracted with the highlighted row marked selected;
- a plain numbered list in prose is NOT eaten;
- a Markdown blockquote numbered list ("> 1. ...") is NOT mis-detected;
- long option labels that wrap in narrow panes are merged;
- a partially-scrolled picker (options starting at 2) is still detected;
- box borders around the picker are tolerated;
- the ⎿ Claude tool-result marker is parsed as a Tool result block.

Run: python3 test_choice_parse.py
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import subprocess
from types import SimpleNamespace

import server


class ExtractChoiceBlockTest(unittest.TestCase):
    def opts(self, text):
        _rem, blk = server.extract_choice_block(text)
        return blk

    def test_highlighted_picker(self):
        rem, blk = server.extract_choice_block("● 选哪个?\n\n❯ 1. A\n  2. B\n  3. C\n  Enter to select")
        self.assertIsNotNone(blk)
        self.assertEqual(rem, "")
        self.assertEqual(blk["question"], "● 选哪个?")
        self.assertEqual(len(blk["options"]), 3)
        self.assertTrue(blk["options"][0]["selected"])
        self.assertFalse(blk["options"][1]["selected"])

    def test_tmux_blank_screen_tail_does_not_hide_picker(self):
        blk = self.opts("● 选哪个?\n\n❯ 1. A\n  2. B\n  Enter to select\n" + ("\n" * 80))
        self.assertIsNotNone(blk)
        self.assertEqual([o["n"] for o in blk["options"]], [1, 2])

    def test_tmux_hard_wrapped_unindented_option_lines_are_merged(self):
        text = (
            "请选择本次处理方式：\n"
            "❯ 1. 先执行完整回归测试，再提交并推送\n"
            "；这个选项故意写得很长，用来验证选择框会根据内容自动增高。\n"
            "  2. 只做快速 smoke test，然后把风险写清楚\n"
            "写清楚；这个续行没有缩进但仍属于第二个选项。\n"
            "Enter to select\n"
            + ("\n" * 80)
        )
        blk = self.opts(text)
        self.assertIsNotNone(blk)
        self.assertIn("自动增高", blk["options"][0]["text"])
        self.assertIn("仍属于第二个选项", blk["options"][1]["text"])

    def test_prose_list_not_eaten(self):
        self.assertIsNone(self.opts("● 总结:\n1. 跑命令\n2. 提交"))

    def test_decision_checklist_not_rendered_as_choice(self):
        text = (
            "需要你决定(我下一步据此执行)\n"
            "1. 模块A 先做哪个方向? 方案甲(确认 worker 方向)还是 方案乙(worker 需要纠偏)?\n"
            "2. 模块B 的 /api/demo 别名:要我现在就在模块C 的代理里同时挂上吗?(只改我这边,低风险)\n"
            "3. 模块D 要我帮它 /compact 恢复吗?\n"
            "4. 模块A 的端口/鉴权/映射三项待定,等你定了方向再做。\n"
            "Enter to select"
        )
        rem, blk = server.extract_choice_block(text)
        self.assertIsNone(blk)
        self.assertEqual(rem, text)

    def test_list_of_questions_without_heading_not_rendered_as_choice(self):
        text = (
            "接下来几件事:\n"
            "❯ 1. 模块A 要不要先重构?\n"
            "  2. 模块B 的接口是否需要改名?\n"
            "  3. 模块C 的测试怎么补?\n"
            "Enter to select"
        )
        rem, blk = server.extract_choice_block(text)
        self.assertIsNone(blk)

    def test_highlighted_decision_checklist_not_rendered_as_choice(self):
        text = (
            "需要你决定(我下一步据此执行)\n"
            "❯ 1. 模块A 先做哪个方向? 方案甲还是方案乙?\n"
            "  2. 模块B 要我现在就在模块C 的代理里同时挂上吗?\n"
            "  3. 模块D 要我帮它 /compact 恢复吗?\n"
            "Enter to select"
        )
        self.assertIsNone(self.opts(text))

    def test_blockquote_not_eaten(self):
        self.assertIsNone(
            self.opts("Assistant said:\n> 1. First quoted item\n> 2. Second quoted item")
        )

    def test_permission_prompt_via_footer(self):
        blk = self.opts("● Proceed?\n  1. Yes\n  2. No\n  to select")
        self.assertIsNotNone(blk)
        self.assertEqual(len(blk["options"]), 2)

    def test_wrapped_option_merged(self):
        blk = self.opts(
            "❯ 1. Keep\n  2. A very long option that wraps\n     onto the second line\n  Enter to select"
        )
        self.assertIsNotNone(blk)
        self.assertEqual(len(blk["options"]), 2)
        self.assertEqual(blk["options"][1]["text"], "A very long option that wraps onto the second line")

    def test_scrolled_picker_starting_at_two(self):
        blk = self.opts("  ❯ 2. opt two\n    3. opt three\n    4. opt four\n  Enter to select")
        self.assertIsNotNone(blk)
        self.assertEqual([o["n"] for o in blk["options"]], [2, 3, 4])

    def test_box_border_tolerated(self):
        blk = self.opts("╭───────╮\n❯ 1. 是\n  2. 否\n╰───────╯\nEnter to select")
        self.assertIsNotNone(blk)
        self.assertEqual(len(blk["options"]), 2)

    def test_question_moves_into_choice_block(self):
        blocks = server.parse_blocks(
            "前面是解释，应该保留。\n\n请选择接下来怎么做？\n\n❯ 1. 继续\n  2. 停止\n  Enter to select",
            pane_kind="Claude",
        )
        self.assertEqual(blocks[-1]["role"], "choice")
        self.assertEqual(blocks[-1]["question"], "请选择接下来怎么做？")
        self.assertEqual(blocks[0]["text"], "前面是解释，应该保留。")

    def test_multiselect_picker_detected(self):
        blk = self.opts(
            "请选择要处理的模块：\n\n"
            "❯ ☐ 1. 示例功能甲\n"
            "  ☑ 2. 示例功能乙\n"
            "  ☐ 3. 示例功能丙\n"
            "Space to select · Enter to confirm"
        )
        self.assertIsNotNone(blk)
        self.assertTrue(blk["multiple"])
        self.assertEqual(blk["question"], "请选择要处理的模块：")
        self.assertEqual([o["n"] for o in blk["options"]], [1, 2, 3])
        self.assertFalse(blk["options"][0]["checked"])
        self.assertTrue(blk["options"][1]["checked"])
        self.assertTrue(blk["options"][0]["checkbox"])

    def test_custom_choice_detected(self):
        blk = self.opts(
            "你想怎么处理？\n\n"
            "❯ 1. 使用推荐方案\n"
            "  2. 自定义输入自己的意见\n"
            "Enter to select"
        )
        self.assertIsNotNone(blk)
        self.assertTrue(blk["allow_custom"])
        self.assertTrue(blk["options"][1]["custom"])
        self.assertFalse(blk["multiple"])


class ParseBlocksMarkerTest(unittest.TestCase):
    def test_claude_tool_result_marker(self):
        blocks = server.parse_blocks("● Bash(echo hi)\n  ⎿  hi\n  ⎿  done", pane_kind="Claude")
        self.assertIn("Tool result", [b["label"] for b in blocks])

    def test_claude_todo_summary_is_rendered_as_checklist(self):
        blocks = server.parse_blocks(
            "· Jitterbugging… (1m 19s · ↓ 2.2k tokens)\n"
            "  ⎿ \u00a0◻ 补齐示例图片素材: 第一组×3/第二组 …\n"
            "     ◻ 音频只有1个占位文件,需要替换…\n"
            "     ✔ 建 demo-env conda env + 依赖…\n"
            "     ✔ 装 diffusers + 下载示例模型 ch…\n"
            "      … +4 completed\n",
            pane_kind="Claude",
        )
        self.assertEqual(blocks[-1]["role"], "assistant")
        self.assertEqual(blocks[-1]["label"], "Todo")
        self.assertIn("- [ ] 补齐示例图片素材", blocks[-1]["text"])
        self.assertIn("- [x] 建 demo-env", blocks[-1]["text"])
        self.assertIn("- … +4 completed", blocks[-1]["text"])

    def test_claude_live_task_panel_keeps_all_states_in_one_todo_block(self):
        reconnecting = (
            "◦ Reconnecting... 1/5 (53s • esc to interrupt) · "
            "1 background terminal running · /ps to view · /stop to close"
        )
        text = (
            "10 tasks (3 done, 1 in progress, 6 open)\n"
            "◼ 固化 pipeline 和文档\n"
            "◻ 更新论文模板\n"
            "✔ 完成数据核验\n"
            "… +2 pending, 3 completed\n"
            f"{reconnecting}\n"
            "❯ 当前输入草稿"
        )
        blocks = server.parse_blocks(text, pane_kind="Claude")
        self.assertEqual([block["label"] for block in blocks], ["Todo"])
        todo = blocks[0]["text"]
        self.assertIn("**10 tasks", todo)
        self.assertIn("- [ ] **进行中：** 固化 pipeline", todo)
        self.assertIn("- [ ] 更新论文模板", todo)
        self.assertIn("- [x] 完成数据核验", todo)
        self.assertIn("+2 pending, 3 completed", todo)
        self.assertNotIn("Reconnecting", todo)
        self.assertEqual(server.infer_status(text, "claude"), "running")

    def test_in_progress_todo_beats_stale_error_prose(self):
        text = (
            "● 旧回复提到 error: 已经排查过\n"
            "10 tasks (3 done, 1 in progress, 6 open)\n"
            "◼ 正在执行当前任务\n"
            "◻ 后续任务\n"
        )
        self.assertEqual(server.infer_status(text, "claude"), "running")

    def test_spinner_glyph_does_not_change_blocks(self):
        # jitter 修: spinner 行每帧切换字形(• 实心 / ◦ 空心)。修前 `•` 那帧匹配
        # block marker 自成一块被丢弃, `◦` 那帧不匹配被并入上一块 → 上一块多一行 → 块高一行级
        # 横跳, 前端追底把视图钉底 → 整屏抖动。两种字形必须产出完全相同的 blocks。
        base = "● 先看了代码。\n  改完了 server.py。\n{glyph} Working (8m 12s · esc to interrupt)\n\n› Implement {{feature}}"
        dot = server.parse_blocks(base.format(glyph="•"), pane_kind="Codex")
        circ = server.parse_blocks(base.format(glyph="◦"), pane_kind="Codex")
        self.assertEqual([b["text"] for b in dot], [b["text"] for b in circ])
        # 易变状态行不得作为内容泄漏进任何块
        for b in dot:
            self.assertNotIn("Working (", b["text"])

    def test_pure_spinner_yields_no_blocks(self):
        # 纯 spinner 截屏(刚启动只有一行 Working): 任何字形都应是空 timeline, 不能把 spinner 当内容。
        for glyph in ("•", "◦", "●", "○"):
            self.assertEqual(
                server.parse_blocks(f"{glyph} Working (2s · esc to interrupt)\n", pane_kind="Codex"),
                [],
            )

    def test_codex_prose_soft_wrap_is_merged(self):
        blocks = server.parse_blocks(
            "• 目前状态是：\n\n"
            "  5. 当前写进配置的是一条占位信息；如果之前的真实配置值没有传进来，那这里不会\n"
            "  magically 有真实配置值，只会是占位记录。\n\n"
            "  换句话说：已经有一个占位记录，但还不是完整可用的配置。要变成完整配置，需要补上真实的配置值，或者\n"
            "  先把这台机器的备份恢复好，然后再合并进主配置。\n",
            pane_kind="Codex",
        )
        text = blocks[-1]["text"]
        self.assertIn("不会 magically 有真实配置值", text)
        self.assertIn("或者先把这台机器", text)
        self.assertNotIn("不会\n  magically", text)

    def test_codex_label_value_lines_are_preserved(self):
        blocks = server.parse_blocks(
            "• 目前状态是：\n\n"
            "  1. 我已经创建了独立的配置文件：\n"
            "     /srv/workspace/config/example-settings.toml\n\n"
            "  3. 我也在索引里登记了查找名：\n"
            "     DEMO_SETTING_NAME\n",
            pane_kind="Codex",
        )
        text = blocks[-1]["text"]
        self.assertIn("配置文件：\n     /srv/workspace", text)
        self.assertIn("查找名：\n     DEMO_SETTING_NAME", text)

    def test_codex_skill_warning_lines_are_filtered(self):
        blocks = server.parse_blocks(
            "• 已完成。\n\n"
            "⚠ Skipped loading 1 skill(s) due to invalid SKILL.md files.\n\n"
            "⚠ /srv/workspace/.codex/skills/demo-skill/SKILL.md: missing YAML frontmatter delimited by ---\n",
            pane_kind="Codex",
        )
        text = blocks[-1]["text"]
        self.assertNotIn("Skipped loading", text)
        self.assertNotIn("missing YAML", text)

    def test_codex_file_edit_diff_keeps_line_structure(self):
        blocks = server.parse_blocks(
            "• Added out/tasks/\n"
            "example_followup_task.md (+30\n"
            "-0)\n"
            "     1 +# Example Follow-up Task\n"
            "     2 +\n"
            "     3 +这是示例任务的第三行，内容很长会被终端在 e\n"
            "        xample 中间折开；续行没有前缀，也不能\n"
            "        被拆成新行。\n"
            "     4 +\n"
            "     5 +项目路径：`/srv/workspace/pr\n"
            "        ojects/example-project`\n",
            pane_kind="Codex",
        )
        block = blocks[-1]
        self.assertEqual(block["role"], "tool")
        self.assertEqual(block["label"], "File edit")
        self.assertIn("3 +这是示例任务的第三行，内容很长会被终端在 example 中间折开", block["text"])
        self.assertIn("\n4 +\n5 +项目路径：`/srv/workspace/projects/example-project`", block["text"])
        self.assertNotIn("被拆成新行。 4 + 5 +项目路径", block["text"])

    def test_picker_status_is_waiting(self):
        self.assertEqual(
            server.infer_status("● 选哪个?\n\n❯ 1. A\n  2. B\n  Enter to select", "claude"),
            "waiting",
        )
        self.assertEqual(
            server.infer_status("● 选哪个?\n\n❯ 1. A\n  2. B\n  Enter to select\n›", "claude"),
            "waiting",
        )
        self.assertEqual(
            server.infer_status("● 选哪个?\n\n❯ 1. A\n  2. B\n  Enter to select · Esc to cancel", "claude"),
            "waiting",
        )
        self.assertEqual(
            server.infer_status("● 选哪个?\n\n› 1. A\n  2. B", "claude"),
            "waiting",
        )

    def test_claude_thundering_status_is_running(self):
        self.assertEqual(
            server.infer_status("+ Thundering...(7m 17s · ↓21.4k tokens)", "claude"),
            "running",
        )
        self.assertEqual(
            server.infer_status("+ Thundering...(7m 17s · ↓21.4k tokens)\n›", "claude"),
            "running",
        )

    def test_claude_generic_spinner_status_is_running(self):
        self.assertEqual(
            server.infer_status("* Pouncing.. (19s · still thinking with xhigh effort)\n›", "claude"),
            "running",
        )

    def test_claude_compacting_status_is_running_and_chrome(self):
        text = (
            "+ Compacting conversation..(1m45s)\n"
            "0% until auto-compact\n"
            "❯"
        )
        self.assertEqual(server.infer_status(text, "claude"), "running")
        self.assertNotIn("auto-compact", server.filter_display_lines(text))
        blocks = server.parse_blocks(text, pane_kind="Claude")
        self.assertFalse(blocks, blocks)

    def test_workflow_footer_status_is_running(self):
        text = (
            "● Workflow 已经在跑了,跑完会通知我。\n\n"
            "✻ Waiting for 1 dynamic workflow to finish · 5 messages hidden (/focus to show)\n"
            "❯\n"
            "  ◯ example-followup-audit  独立核实上一轮修复是否覆盖了全部问题  "
            "5/6 agents done · 6m 26s · ↓ 452.4k tokens"
        )
        self.assertEqual(server.infer_status(text, "claude"), "running")
        blocks = server.parse_blocks(text, pane_kind="Claude")
        joined = "\n".join(block["text"] for block in blocks)
        self.assertIn("Workflow 已经在跑了", joined)
        self.assertNotIn("agents done", joined)
        self.assertNotIn("Waiting for 1 dynamic workflow", joined)

    def test_claude_monitor_footer_is_idle(self):
        text = (
            "## Summary\n"
            "- 总结: 已完成。\n\n"
            "✻ Sautéed for 47m 11s · 48 messages hidden\n"
            "  (/focus to show) · 1 monitor still\n"
            "  running\n"
            "   new task? /clear to save 389.2k tokens\n"
            "────────────────────── demo-project ──\n"
            "❯\n"
            "  ⏵⏵ auto mode on · ← for agents · 1\n"
            "                                    focus"
        )
        self.assertEqual(server.infer_status(text, "claude"), "idle")
        self.assertNotIn("running", server.pane_activity_signature(text).lower())

    def test_plain_confirm_text_is_not_waiting(self):
        self.assertEqual(
            server.infer_status("Please confirm the result in the README after review.\n›", "claude"),
            "idle",
        )

    def test_ai_output_change_keeps_pane_running(self):
        server.PANE_ACTIVITY_CACHE.clear()
        self.assertEqual(
            server.infer_pane_status("%test", "● 正在输出第一行\n›", "claude", "Claude"),
            "idle",
        )
        self.assertEqual(
            server.infer_pane_status("%test", "● 正在输出第一行\n第二行\n›", "claude", "Claude"),
            "running",
        )
        self.assertEqual(
            server.infer_pane_status("%test", "● 正在输出第一行\n第二行\n›", "claude", "Claude"),
            "running",
        )

    def test_claude_tool_summary_lines_are_filtered(self):
        text = "\n".join([
            "● Inspecting context",
            "Read 2 files, running 1 shell command",
            "Read 1 file, called 1 tool",
            "›",
        ])
        filtered = server.filter_display_lines(text)
        self.assertNotIn("Read 2 files", filtered)
        self.assertNotIn("called 1 tool", filtered)
        blocks = server.parse_blocks(text, pane_kind="Claude")
        self.assertFalse(any(block["label"] == "Tool" for block in blocks), blocks)

    def test_claude_tool_summary_filter_does_not_hide_normal_prose(self):
        self.assertFalse(server.is_claude_tool_summary_line("Running 5 test files in parallel"))
        text = "先看了代码。\nRunning 5 test files in parallel\n›"
        self.assertIn("Running 5 test files", server.pane_activity_signature(text))


class ChoiceKeySendTest(unittest.TestCase):
    def test_choice_keys_are_narrowly_allowed(self):
        original_pane_by_id = server.pane_by_id
        original_run_tmux = server.run_tmux
        sent = []
        try:
            server.pane_by_id = lambda pane_id: SimpleNamespace(pane_id=pane_id, target="0:0.0")

            def fake_run_tmux(args):
                sent.append(args)
                return subprocess.CompletedProcess(args, 0, "", "")

            server.run_tmux = fake_run_tmux
            server.send_key_to_pane("%1", "1")
            server.send_key_to_pane("%1", "C-m")
            with self.assertRaises(ValueError):
                server.send_key_to_pane("%1", "A")
        finally:
            server.pane_by_id = original_pane_by_id
            server.run_tmux = original_run_tmux

        self.assertEqual(sent, [["send-keys", "-t", "%1", "1"], ["send-keys", "-t", "%1", "C-m"]])


FIXTURES = __import__("pathlib").Path(__file__).resolve().parent / "fixtures"


class LiveChoiceNeedsNoInputBoxTest(unittest.TestCase):
    """A live picker takes over the input area; an input box on screen means no picker.

    Both fixtures are real Claude Code v2.1.280 captures. The first
    one shows the symptom: right after a new prompt, the previous answer's
    numbered list sits above the running footer (``esc to interrupt``) and used
    to be rendered as a clickable choice card.
    """

    def fixture(self, name):
        return (FIXTURES / name).read_text(encoding="utf-8")

    def test_numbered_answer_above_running_input_box_is_not_a_choice(self):
        text = self.fixture("claude_running_after_numbered_answer.txt")
        self.assertIsNone(server.live_choice_block(text))
        roles = [b["role"] for b in server.parse_blocks(text, pane_kind="Claude")]
        self.assertNotIn("choice", roles)

    def test_real_model_picker_is_still_a_choice(self):
        text = self.fixture("claude_model_picker.txt")
        blk = server.live_choice_block(text)
        self.assertIsNotNone(blk)
        self.assertEqual([o["n"] for o in blk["options"]], [1, 2, 3, 4, 5])
        self.assertTrue(blk["options"][0]["selected"])
        self.assertEqual(server.parse_blocks(text, pane_kind="Claude")[-1]["role"], "choice")



class UnnumberedDialogTest(unittest.TestCase):
    """没有编号的对话框（例如 "Make auto mode your default…?"）。

    Claude 自己报 waiting / dialog open，屏幕上却认不出选择题，卡片显示空闲甚至完成。
    """

    def fixture(self):
        return (FIXTURES / "claude_auto_mode_dialog.txt").read_text(encoding="utf-8")

    def test_dialog_rows_become_a_nav_choice(self):
        blk = server.live_nav_choice_block(self.fixture())
        self.assertTrue(blk["nav"])
        self.assertIn("Make auto mode your default permission mode?", blk["question"])
        self.assertEqual([(o["text"], o["selected"]) for o in blk["options"]],
                         [("Yes, set auto mode as my default permission mode", True), ("No, keep bypass permissions", False)])

    def test_never_while_an_input_box_is_drawn(self):
        screen = "● 回复\n  ❯ 这是引用的一行\n    另一行\n" + "\n".join(["─" * 40, "❯", "─" * 40, "  ⏵⏵ bypass permissions on"])
        self.assertIsNone(server.live_nav_choice_block(screen))

    def test_choose_moves_highlight_step_by_step_and_verifies(self):
        options = ["Yes, allow once", "Yes, and don't ask again", "No, and tell Claude what to do differently"]
        state = {"highlight": 0, "open": True, "sent": []}

        def render():
            if not state["open"]:
                return "● done\n" + "\n".join(["─" * 40, "❯", "─" * 40, "  ⏵⏵ bypass permissions on"])
            rows = [("   ❯ " if i == state["highlight"] else "     ") + text for i, text in enumerate(options)]
            return "\n".join(["─" * 60, " Do you want to proceed?", "", *rows, "", " Esc to cancel"])

        def fake_run_tmux(args):
            if args[0] in {"display-message", "list-clients"}:
                return subprocess.CompletedProcess(args, 0, "0\n", "")
            key = args[-1]
            state["sent"].append(key)
            if key == "Down":
                state["highlight"] = min(state["highlight"] + 1, len(options) - 1)
            elif key == "Up":
                state["highlight"] = max(state["highlight"] - 1, 0)
            elif key == "C-m":
                state["open"] = False
            return subprocess.CompletedProcess(args, 0, "", "")

        saved = (server.pane_by_id, server.capture, server.run_tmux, server.time.sleep)
        saved_lock_dir, lock_dir = server.dialogs.LOCK_DIR, tempfile.TemporaryDirectory()
        server.dialogs.LOCK_DIR = Path(lock_dir.name)
        server.pane_by_id = lambda pane_id: SimpleNamespace(pane_id=pane_id, target="0:0.0")
        server.capture = lambda *_a, **_k: render()
        server.run_tmux = fake_run_tmux
        server.time.sleep = lambda _s: None
        try:
            server.choose_nav_option("%1", "No, and tell Claude what to do differently")
            self.assertEqual(state["sent"], ["Down", "Down", "C-m"])
            state.update(highlight=0, open=False, sent=[])
            with self.assertRaises(ValueError):  # 对话框已经不在了：什么都不发
                server.choose_nav_option("%1", "Yes, allow once")
            self.assertEqual(state["sent"], [])
        finally:
            server.pane_by_id, server.capture, server.run_tmux, server.time.sleep = saved
            server.dialogs.LOCK_DIR = saved_lock_dir
            lock_dir.cleanup()

    def test_screen_only_timeline_moves_the_question_into_the_choice(self):
        import os
        saved = server.claude_agent_records
        server.claude_agent_records = lambda: [{"pid": os.getpid(), "status": "waiting", "waitingFor": "dialog open"}]
        try:
            pane = SimpleNamespace(ai_session_id="", pane_pid=str(os.getpid()))
            blocks = [{"role": "assistant", "label": "AI output",
                       "text": "上一段回复\nMake auto mode your default permission mode?\nAuto mode lets Claude handle"}]
            out = server._with_dialog_choice(blocks, pane, self.fixture())
            self.assertEqual(out[0]["text"], "上一段回复")
            self.assertEqual(out[-1]["role"], "choice")
        finally:
            server.claude_agent_records = saved

    def test_official_waiting_status_wins_over_screen(self):
        import os
        saved = server.claude_agent_records
        server.claude_agent_records = lambda: [{"pid": os.getpid(), "sessionId": "s", "status": "waiting", "waitingFor": "dialog open"}]
        try:
            status = server.infer_pane_status("%x", self.fixture(), "claude", "Claude", True, "", str(os.getpid()))
            self.assertEqual(status, "waiting")
        finally:
            server.claude_agent_records = saved



class CardsAutoApproveTest(unittest.TestCase):
    """Cards answers permission-type dialogs in any window once auto-approve is enabled (opt-in)."""

    def setUp(self):
        self.saved = (server.list_panes, server.capture, server.run_tmux, server.cli_bridge.auto_approve_enabled,
                      server.event_ledger.append_event, server.dialogs.LOCK_DIR)
        self.lock_dir = tempfile.TemporaryDirectory()
        server.dialogs.LOCK_DIR = Path(self.lock_dir.name)
        self.events = []
        self.sent = []
        self.in_mode = "0"
        server._AUTO_APPROVE_RECENT.clear()
        server._WEB_INPUT_AT.clear()
        server.event_ledger.append_event = lambda kind, **kw: self.events.append(kind)
        server.cli_bridge.auto_approve_enabled = lambda: True

    def tearDown(self):
        (server.list_panes, server.capture, server.run_tmux, server.cli_bridge.auto_approve_enabled,
         server.event_ledger.append_event, server.dialogs.LOCK_DIR) = self.saved
        self.lock_dir.cleanup()

    def run_with(self, screens):
        state = {"screen": screens[0]}
        pane = SimpleNamespace(pane_id="%5", target="s:5.0", kind="Claude", status="waiting")
        server.list_panes = lambda *a, **k: [pane]
        server.capture = lambda *a, **k: state["screen"]

        def fake_run_tmux(args):
            if args[0] == "display-message":
                return subprocess.CompletedProcess(args, 0, self.in_mode + "\n", "")
            if args[0] == "list-clients":
                return subprocess.CompletedProcess(args, 0, "", "")
            self.sent.append(args[-1])
            if args[-1] == "C-m":
                state["screen"] = "● ok\n" + "\n".join(["─" * 40, "❯", "─" * 40, "  ⏵⏵ bypass permissions on"])
            return subprocess.CompletedProcess(args, 0, "", "")

        server.run_tmux = fake_run_tmux
        return server.auto_approve_waiting_panes(now=1000.0)

    def test_mode_offer_is_answered_and_logged(self):
        screen = (FIXTURES / "claude_auto_mode_dialog.txt").read_text(encoding="utf-8")
        # Highlight already on the policy answer, so one verified Enter is enough.
        screen = screen.replace("   ❯ Yes, set auto mode", "     Yes, set auto mode").replace(
            "     No, keep bypass permissions", "   ❯ No, keep bypass permissions")
        answered = self.run_with([screen])
        self.assertEqual([a["kind"] for a in answered], ["mode-offer"])
        self.assertEqual(self.sent[-1], "C-m")
        self.assertIn("pane_prompt_auto_answered", self.events)

    def test_work_question_is_left_for_the_user(self):
        question = "\n".join(["─" * 60, " Which database should we use?", "", "   ❯ 1. PostgreSQL",
                              "     2. SQLite", "", " Enter to select"])
        self.assertEqual(self.run_with([question]), [])
        self.assertEqual(self.sent, [])

    def mode_offer_on_policy_answer(self):
        screen = (FIXTURES / "claude_auto_mode_dialog.txt").read_text(encoding="utf-8")
        return screen.replace("   ❯ Yes, set auto mode", "     Yes, set auto mode").replace(
            "     No, keep bypass permissions", "   ❯ No, keep bypass permissions")

    def test_pane_used_from_this_page_just_now_is_left_alone(self):
        server._WEB_INPUT_AT["%5"] = 995.0
        self.assertEqual(self.run_with([self.mode_offer_on_policy_answer()]), [])
        self.assertEqual(self.sent, [])

    def test_copy_mode_or_a_busy_pane_is_retried_later_without_keys(self):
        self.in_mode = "1"
        self.assertEqual(self.run_with([self.mode_offer_on_policy_answer()]), [])
        self.assertEqual((self.sent, self.events), ([], []))
        self.in_mode = "0"
        with server.dialogs.pane_lock("%5"):
            self.assertEqual(self.run_with([self.mode_offer_on_policy_answer()]), [])
        self.assertEqual(self.sent, [])
        self.assertEqual([a["kind"] for a in self.run_with([self.mode_offer_on_policy_answer()])], ["mode-offer"])

    def test_switch_off_means_nothing_is_sent(self):
        server.cli_bridge.auto_approve_enabled = lambda: False
        screen = (FIXTURES / "claude_auto_mode_dialog.txt").read_text(encoding="utf-8")
        self.assertEqual(self.run_with([screen]), [])
        self.assertEqual(self.sent, [])

    def test_cards_env_can_enable_or_veto_the_dashboard_pass(self):
        screen = (FIXTURES / "claude_auto_mode_dialog.txt").read_text(encoding="utf-8")
        screen = screen.replace("   ❯ Yes, set auto mode", "     Yes, set auto mode").replace(
            "     No, keep bypass permissions", "   ❯ No, keep bypass permissions")
        server.cli_bridge.auto_approve_enabled = lambda: False
        with mock.patch.dict(os.environ, {"CARDS_AUTO_APPROVE": "1"}):
            self.assertEqual([a["kind"] for a in self.run_with([screen])], ["mode-offer"])
        server._AUTO_APPROVE_RECENT.clear()
        self.sent.clear()
        server.cli_bridge.auto_approve_enabled = lambda: True
        with mock.patch.dict(os.environ, {"CARDS_AUTO_APPROVE": "0"}):
            self.assertEqual(self.run_with([screen]), [])
        self.assertEqual(self.sent, [])


class CardsAutoApproveLoopSwitchTest(unittest.TestCase):
    """The background loop is opt-in: it starts only when explicitly requested."""

    def requested(self, **env):
        clean = {k: v for k, v in os.environ.items() if k not in {"CARDS_AUTO_APPROVE", "AGENT_BUS_AUTO_APPROVE"}}
        clean.update(env)
        with mock.patch.dict(os.environ, clean, clear=True):
            return server.dashboard_auto_approve_requested()

    def test_default_is_off(self):
        self.assertFalse(self.requested())

    def test_either_switch_turns_it_on(self):
        self.assertTrue(self.requested(CARDS_AUTO_APPROVE="1"))
        self.assertTrue(self.requested(AGENT_BUS_AUTO_APPROVE="1"))

    def test_cards_zero_vetoes_the_bus_switch(self):
        self.assertFalse(self.requested(CARDS_AUTO_APPROVE="0", AGENT_BUS_AUTO_APPROVE="1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
