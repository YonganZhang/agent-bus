#!/usr/bin/env python3
"""Regression tests for JSONL transcript reading / parsing.

- a single huge last line (e.g. a big tool_result) must NOT blank out history;
- a raw-string assistant message keeps the assistant role, not User prompt.

Run: python3 test_transcript.py
"""

import json
import os
import subprocess
import tempfile
import time
import unittest

import server


class TailReaderTest(unittest.TestCase):
    def test_huge_last_line_not_blanked(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write("early line 1\n")
            fh.write("early line 2\n")
            fh.write("X" * 600_000 + "\n")  # one line bigger than the old 400KB window
            path = fh.name
        try:
            lines = server._read_tail_lines(path, max_bytes=400_000, max_lines=600)
            self.assertTrue(lines, "tail reader returned nothing for a huge last line")
            self.assertTrue(any(set(l) == {"X"} for l in lines), "huge last line missing")
        finally:
            os.unlink(path)


class TranscriptParseTest(unittest.TestCase):
    def test_background_terminal_footer_alone_is_idle(self):
        text = """工作结果已经回复。\n1 background terminal running · /ps to view · /stop to close\n› """
        self.assertEqual(server.infer_status(text, "codex"), "idle")

    def test_leftover_todo_panel_alone_is_idle(self):
        text = """◼ 整理最后结果\n已经完成本轮回复。\n› """
        self.assertEqual(server.infer_status(text, "claude"), "idle")

    def test_prose_mentioning_background_control_is_not_runtime_chrome(self):
        text = """The documentation mentions /ps to view for debugging.\n› """
        self.assertEqual(server.infer_status(text, "codex"), "idle")

    def _write(self, entries):
        fh = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        for e in entries:
            fh.write(json.dumps(e) + "\n")
        fh.close()
        return fh.name

    def test_assistant_string_content_role(self):
        path = self._write([
            {"type": "user", "message": {"role": "user", "content": "hello there"}},
            {"type": "assistant", "message": {"role": "assistant", "content": "hi back as string"}},
        ])
        try:
            blocks = server.parse_transcript_tail(path)
        finally:
            os.unlink(path)
        by_text = {b["text"]: b for b in blocks}
        self.assertEqual(by_text["hello there"]["label"], "User prompt")
        self.assertEqual(by_text["hi back as string"]["label"], "AI output")
        self.assertEqual(by_text["hi back as string"]["role"], "assistant")

    def test_end_turn_marks_final_and_tool_use_does_not(self):
        """轮次是否结束必须来自 transcript 的 stop_reason, 不是回复里写没写「Summary」。

        否则没写 Summary 标题的短回复/Codex 窗口, AI 明明答完了卡片还在转圈、不置顶。
        end_turn 是 Claude 说完一轮的结构化事实; 中途还要接着调工具的
        assistant 消息一律是 tool_use, 那种进度播报不能算答完。
        """
        path = self._write([
            {"type": "user", "message": {"role": "user", "content": "跑一下测试"}},
            {"type": "assistant", "message": {
                "role": "assistant", "stop_reason": "tool_use",
                "content": [{"type": "text", "text": "正在修, 稍等"}],
            }},
            {"type": "assistant", "message": {
                "role": "assistant", "stop_reason": "tool_use",
                "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "pytest"}}],
            }},
            {"type": "assistant", "message": {
                "role": "assistant", "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "都过了。"}],
            }},
        ])
        try:
            blocks = server.parse_transcript_tail(path)
        finally:
            os.unlink(path)
        by_text = {b["text"]: b for b in blocks}
        self.assertFalse(by_text["正在修, 稍等"].get("final"), "中途进度播报被误标成答完了")
        self.assertTrue(by_text["都过了。"].get("final"), "end_turn 的终结回复没有标 final")

    def test_final_block_completes_a_cards_job_without_human_summary(self):
        """有 final 就能收工, 不再要求回复里出现「Summary」标题。"""
        job = {
            "id": "cards-web-final-test",
            "source": "card-dashboard",
            "status": "running",
            "task_preview": "跑一下测试",
            "created_at": "2020-01-01T00:00:00+00:00",  # echo grace 早已过
        }
        blocks = [
            {"role": "user", "label": "User prompt", "text": "跑一下测试"},
            {"role": "assistant", "label": "AI output", "text": "都过了。", "final": True},
        ]
        self.assertTrue(server.cards_job_has_visible_response(job, blocks))
        # 中途播报(没有 final、也没有 Summary 标题)不能提前收工
        mid = [
            {"role": "user", "label": "User prompt", "text": "跑一下测试"},
            {"role": "assistant", "label": "AI output", "text": "正在修, 稍等"},
        ]
        self.assertFalse(server.cards_job_has_visible_response(job, mid))

    def test_isMeta_user_entry_is_labeled_system_notice_not_user_prompt(self):
        # Reproduces a real report: Claude's own harness auto-injects
        # a "[Your previous response had no visible output...]" continuation
        # nudge as a role="user" transcript entry when a turn produced no
        # visible text. Its text does not match any of
        # _is_system_injected_user's known string prefixes (there is no fixed
        # list that could ever enumerate every such harness message), but the
        # entry itself is flagged "isMeta": true - that authoritative signal
        # must be enough on its own to keep it out of "User prompt".
        path = self._write([
            {
                "type": "user",
                "isMeta": True,
                "message": {
                    "role": "user",
                    "content": "[Your previous response had no visible output. Please continue and produce a user-visible response.]",
                },
            },
            {"type": "user", "message": {"role": "user", "content": "real human question"}},
        ])
        try:
            blocks = server.parse_transcript_tail(path)
        finally:
            os.unlink(path)
        by_text = {b["text"]: b for b in blocks}
        self.assertEqual(by_text["[Your previous response had no visible output. Please continue and produce a user-visible response.]"]["role"], "system")
        self.assertEqual(by_text["[Your previous response had no visible output. Please continue and produce a user-visible response.]"]["label"], "系统通知")
        self.assertEqual(by_text["real human question"]["label"], "User prompt")

    def test_blocks_shapes(self):
        path = self._write([
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "doing a thing"},
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
            ]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "content": [{"type": "text", "text": "total 0"}]},
            ]}},
        ])
        try:
            labels = [b["label"] for b in server.parse_transcript_tail(path)]
        finally:
            os.unlink(path)
        self.assertEqual(labels, ["AI output", "Tool", "Tool result"])

    def test_tool_use_summaries_are_specific(self):
        path = self._write([
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/example.md"}},
                {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}},
                {"type": "tool_use", "name": "Workflow", "input": {"description": "audit example module"}},
            ]}},
        ])
        try:
            blocks = server.parse_transcript_tail(path)
        finally:
            os.unlink(path)
        texts = [b["text"] for b in blocks]
        self.assertIn("Read file\n/tmp/example.md", texts)
        self.assertIn("Shell command\npytest -q", texts)
        # Workflow 调用有自己的「工作流」条目（进度由 attach_subagent_status 接上）。
        self.assertIn(("工作流", "audit example module"), [(b["label"], b["text"]) for b in blocks])

    def test_trim_transcript_blocks_to_visible_screen(self):
        blocks = [
            {"role": "assistant", "label": "AI output", "text": "earlier setup"},
            {
                "role": "assistant",
                "label": "AI output",
                "text": "总结:报告已写到 out/review/example_quality_report.md,判定 PASS。检查做得扎实: 修复之后会重跑 schema validation。",
            },
            {"role": "user", "label": "User prompt", "text": "later prompt from another pane"},
            {"role": "assistant", "label": "AI output", "text": "later answer from another pane"},
        ]
        capture = """
        总结:报告已写到 out/review/example_quality_report.md
        PASS。检查做得扎实: 修复之后会重跑 schema validation
        """
        trimmed = server.trim_transcript_blocks_to_screen(blocks, capture)
        self.assertEqual(trimmed[-1]["text"], blocks[1]["text"])
        self.assertNotIn("later prompt from another pane", "\n".join(b["text"] for b in trimmed))

    def test_trim_prefers_latest_visible_screen_block_over_longer_older_match(self):
        blocks = [
            {
                "role": "assistant",
                "label": "AI output",
                "text": (
                    "核实过了,文档格式和内容都对。结果摘要: "
                    "示例模块甲、示例模块乙、配置加载 wiring、数据导入层校验都有证据。"
                ),
            },
            {"role": "user", "label": "User prompt", "text": "还需要再做一轮检查吗？要不要开 workflow？"},
            {
                "role": "assistant",
                "label": "AI output",
                "text": (
                    "暂时不需要。前面已经做过两轮覆盖较广的检查。"
                    "如果有具体怀疑的地方,可以再做一次针对性的小检查。"
                ),
            },
        ]
        capture = """
        核实过了,文档格式和内容都对
        示例模块甲、示例模块乙、配置加载 wiring
        数据导入层校验都有证据
        ❯ 还需要再做一轮检查吗？要不要开 workflow？
        ● 暂时不需要。前面已经做过两轮覆盖较广的检查
        如果有具体怀疑的地方,可以再做一次针对性的小检查
        """
        trimmed = server.trim_transcript_blocks_to_screen(blocks, capture)
        self.assertEqual(trimmed[-1]["text"], blocks[2]["text"])

    def test_trim_keeps_short_exchange_after_older_confident_anchor(self):
        blocks = [
            {
                "role": "assistant",
                "label": "AI output",
                "text": (
                    "上一轮长回答第一段包含足够多的屏幕匹配内容。"
                    "上一轮长回答第二段也会形成独立的匹配片段。"
                ),
            },
            {
                "role": "user",
                "label": "User prompt",
                "text": "CARDS_SEND_REGRESSION_001 请只回复 OK",
            },
            {"role": "assistant", "label": "AI output", "text": "OK"},
        ]
        capture = """
        ● 上一轮长回答第一段包含足够多的屏幕匹配内容
          上一轮长回答第二段也会形成独立的匹配片段

        ❯ CARDS_SEND_REGRESSION_001 请只回复
          OK

        ● OK
        """

        trimmed = server.trim_transcript_blocks_to_screen(blocks, capture)

        self.assertEqual(
            [(block["role"], block["text"]) for block in trimmed[-2:]],
            [("user", blocks[1]["text"]), ("assistant", "OK")],
        )

    def test_live_screen_tail_appends_focus_output_after_transcript_anchor(self):
        blocks = [
            {"role": "assistant", "label": "AI output", "text": "之前的回答"},
            {
                "role": "user",
                "label": "User prompt",
                "text": "请看一下 3 号窗口，它说好像遇到了一个解析异常",
            },
        ]
        capture = """
        ● 之前的回答

        ❯ 请看一下 3 号窗口，它说好像遇到了一个解析异常

        ● Reading 1 file, listing 1 directory, running 5 shell commands…
          ⎿  $ grep -n 'parse error' example.jsonl

        ● 我先直接去看一下 3 号窗口现在屏幕上的内容,只读,不做任何交互。

        ✶ Newspapering… (5m 55s · ↓ 9.5k tokens · thinking some more with xhigh effort)
        """
        merged = server.merge_live_screen_tail(blocks, capture, pane_kind="Claude")
        texts = "\n".join(block["text"] for block in merged)
        self.assertIn("我先直接去看一下 3 号窗口", texts)
        self.assertEqual(merged[-1]["role"], "assistant")

    def test_live_screen_tail_does_not_duplicate_short_reply_after_prompt_anchor(self):
        blocks = [
            {
                "role": "user",
                "label": "User prompt",
                "text": "CARDS_SEND_REGRESSION_002 请只回复 OK",
            },
            {"role": "assistant", "label": "AI output", "text": "OK"},
        ]
        capture = """
        ❯ CARDS_SEND_REGRESSION_002 请只回复 OK

        ● OK
        """

        merged = server.merge_live_screen_tail(blocks, capture, pane_kind="Claude")

        self.assertEqual(merged, blocks)

    def test_live_screen_tail_sticky_anchor_survives_duplicate_line(self):
        blocks = [
            {"role": "assistant", "label": "AI output", "text": "之前的回答"},
            {"role": "assistant", "label": "AI output", "text": "系统已经完成了初始化检查，准备继续"},
        ]
        identity = "test-pid:123"
        capture_poll_1 = """
        ● 之前的回答

        ● 系统已经完成了初始化检查，准备继续

        ● 正在处理第一步新进展，这是这一轮独有的新内容
        """
        merged_1 = server.merge_live_screen_tail(
            blocks, capture_poll_1, pane_kind="Claude", pane_identity=identity,
        )
        self.assertIn("第一步新进展", merged_1[-1]["text"])
        self.assertTrue(merged_1[-1].get("pending"))

        # Same transcript state (nothing flushed to JSONL yet), but the screen
        # has since scrolled to also show a second, near-identical confirmation
        # line after the first-step progress. A naive re-scan (the match loop
        # has no `break`, so it always lands on the *last* match) would jump the
        # anchor past this later duplicate and silently drop the first-step
        # progress from the response. The sticky cache (transcript unchanged
        # since the last poll) should hold the anchor at the first occurrence.
        capture_poll_2 = """
        ● 之前的回答

        ● 系统已经完成了初始化检查，准备继续

        ● 正在处理第一步新进展，这是这一轮独有的新内容

        ● 系统已经完成了初始化检查，准备继续

        ● 正在处理第二步新进展，这是这一轮独有的新内容
        """
        merged_2 = server.merge_live_screen_tail(
            blocks, capture_poll_2, pane_kind="Claude", pane_identity=identity,
        )
        texts_2 = "\n".join(block["text"] for block in merged_2)
        self.assertIn("第一步新进展", texts_2)
        self.assertIn("第二步新进展", texts_2)

    def test_shared_live_transcript_detection_ignores_dead_panes(self):
        original = dict(server._claude_map_cache)
        try:
            server._claude_map_cache.clear()
            server._claude_map_cache.update({
                "%1": {"last_good": "/tmp/shared.jsonl", "path": "/tmp/shared.jsonl"},
                "%2": {"last_good": "/tmp/shared.jsonl", "path": "/tmp/shared.jsonl"},
                "%dead": {"last_good": "/tmp/shared.jsonl", "path": "/tmp/shared.jsonl"},
                "%3": {"last_good": "/tmp/other.jsonl", "path": "/tmp/other.jsonl"},
            })
            shared = server._shared_live_transcript_panes("/tmp/shared.jsonl", "%1", live_ids={"%1", "%2", "%3"})
            self.assertEqual(shared, ["%2"])
        finally:
            server._claude_map_cache.clear()
            server._claude_map_cache.update(original)

    def test_bulk_child_command_probe_uses_one_process_snapshot(self):
        original_run = server.subprocess.run
        original_cache = dict(server._CHILD_CMD_CACHE)
        calls = []

        def fake_run(args, **kwargs):
            calls.append(list(args))
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=(
                    "11 100 claude\n"
                    "12 200 sleep\n"
                    "13 200 codex\n"
                    "14 300 python\n"
                ),
                stderr="",
            )

        try:
            server._CHILD_CMD_CACHE.clear()
            server.subprocess.run = fake_run
            found = server._foreground_child_pid_commands({"100", "200", "300"})
            self.assertEqual(found["100"], ("11", "claude"))
            self.assertEqual(found["200"], ("13", "codex"))
            self.assertEqual(found["300"], ("", ""))
            self.assertEqual(len(calls), 1)

            again = server._foreground_child_pid_commands({"100", "200", "300"})
            self.assertEqual(again, found)
            self.assertEqual(len(calls), 1, "fresh bulk results should be cached")
        finally:
            server.subprocess.run = original_run
            server._CHILD_CMD_CACHE.clear()
            server._CHILD_CMD_CACHE.update(original_cache)

    def test_shared_live_transcript_detection_ignores_pane_reused_by_codex(self):
        """A live tmux pane id is not enough to keep a Claude map entry alive.

        Window/pane ids are reused.  If a former Claude pane now runs Codex,
        its persisted ``last_good`` must not make a real Claude pane fall back
        to screen-only mode with a false ``shared-transcript`` collision.
        """
        original_cache = dict(server._claude_map_cache)
        original_live = server._live_claude_context
        original_save = server._save_claude_map
        try:
            server._claude_map_cache.clear()
            server._claude_map_cache.update({
                "%1": {"last_good": "/tmp/shared.jsonl", "path": "/tmp/shared.jsonl"},
                # Still live, but absent from pane_cwd because it is Codex now.
                "%2": {"last_good": "/tmp/shared.jsonl", "path": "/tmp/shared.jsonl"},
            })
            server._live_claude_context = lambda ttl=2.0: {
                "live_ids": {"%1", "%2"},
                "pane_cwd": {"%1": "/project/claude"},
                "cwd_counts": {"/project/claude": 1},
            }
            server._save_claude_map = lambda: None

            shared = server._shared_live_transcript_panes("/tmp/shared.jsonl", "%1")

            self.assertEqual(shared, [])
            self.assertNotIn("%2", server._claude_map_cache)
        finally:
            server._claude_map_cache.clear()
            server._claude_map_cache.update(original_cache)
            server._live_claude_context = original_live
            server._save_claude_map = original_save

    def test_shared_live_transcript_detection_ignores_pane_reused_by_other_claude_project(self):
        """A pane_id reused by an unrelated Claude project (not Codex) must not
        fabricate a collision either.

        classify() still says "Claude" for the new occupant, so the
        Codex-reuse guard above (which relies on the stale pane dropping out
        of claude_ids/pane_cwd entirely) does not fire. Both live panes are
        genuinely Claude; only their cwd tells them apart. Reproduces a real
        incident: two panes (projects A and C) both showed degraded/shared-transcript against pane_ids that had long
        since been recycled into unrelated projects B and D.
        """
        original_cache = dict(server._claude_map_cache)
        original_live = server._live_claude_context
        original_save = server._save_claude_map
        try:
            server._claude_map_cache.clear()
            server._claude_map_cache.update({
                # %1 is the real, current owner of this transcript.
                "%1": {"last_good": "/tmp/shared.jsonl", "path": "/tmp/shared.jsonl", "cwd": "/project/a"},
                # %2 is live and classified as Claude, but it is a DIFFERENT
                # project now; this cache entry is a leftover from whichever
                # earlier Claude session used to occupy pane_id %2.
                "%2": {"last_good": "/tmp/shared.jsonl", "path": "/tmp/shared.jsonl", "cwd": "/project/a"},
            })
            server._live_claude_context = lambda ttl=2.0: {
                "live_ids": {"%1", "%2"},
                "claude_ids": {"%1", "%2"},
                "pane_cwd": {"%1": "/project/a", "%2": "/project/b"},
                "cwd_counts": {"/project/a": 1, "/project/b": 1},
            }
            server._save_claude_map = lambda: None

            shared = server._shared_live_transcript_panes("/tmp/shared.jsonl", "%1")

            self.assertEqual(shared, [])
        finally:
            server._claude_map_cache.clear()
            server._claude_map_cache.update(original_cache)
            server._live_claude_context = original_live
            server._save_claude_map = original_save

    def test_shared_live_transcript_detection_still_catches_real_collision(self):
        """The cwd cross-check must not suppress a genuine collision: two live
        Claude panes that really do show the same conversation share the same
        live cwd too."""
        original_cache = dict(server._claude_map_cache)
        original_live = server._live_claude_context
        original_save = server._save_claude_map
        try:
            server._claude_map_cache.clear()
            server._claude_map_cache.update({
                "%1": {"last_good": "/tmp/shared.jsonl", "path": "/tmp/shared.jsonl", "cwd": "/project/a"},
                "%2": {"last_good": "/tmp/shared.jsonl", "path": "/tmp/shared.jsonl", "cwd": "/project/a"},
            })
            server._live_claude_context = lambda ttl=2.0: {
                "live_ids": {"%1", "%2"},
                "claude_ids": {"%1", "%2"},
                "pane_cwd": {"%1": "/project/a", "%2": "/project/a"},
                "cwd_counts": {"/project/a": 2},
            }
            server._save_claude_map = lambda: None

            shared = server._shared_live_transcript_panes("/tmp/shared.jsonl", "%1")

            self.assertEqual(shared, ["%2"])
        finally:
            server._claude_map_cache.clear()
            server._claude_map_cache.update(original_cache)
            server._live_claude_context = original_live
            server._save_claude_map = original_save

    def test_same_cwd_distinct_transcripts_use_full_history(self):
        path = self._write([
            {"type": "user", "message": {"role": "user", "content": "earlier user turn that is not on screen"}},
            {"type": "assistant", "message": {"role": "assistant", "content": "visible assistant answer from the right transcript"}},
        ])
        original_cache = dict(server._claude_map_cache)
        original_alt = server.pane_is_alt_screen
        original_resolve = server.claude_transcript_for_pane
        original_live = server._live_claude_context
        try:
            server._claude_map_cache.clear()
            server._claude_map_cache.update({
                "%13": {"last_good": path, "path": path},
                "%24": {"last_good": "/tmp/different.jsonl", "path": "/tmp/different.jsonl"},
            })
            server.pane_is_alt_screen = lambda _pane: True
            server.claude_transcript_for_pane = lambda _pane, _capture: path
            server._live_claude_context = lambda ttl=2.0: {
                "live_ids": {"%13", "%24"},
                "pane_cwd": {"%13": "/same/project", "%24": "/same/project"},
                "cwd_counts": {"/same/project": 2},
            }
            pane = server.Pane(
                pane_id="%13",
                target="secretary_web:13.0",
                session="secretary_web",
                window_index=13,
                pane_index=0,
                window_name="same-project-a",
                command="claude",
                cwd="/same/project",
                title="Claude",
                active=False,
                kind="Claude",
                project="same-project",
                preview="",
                status="idle",
            )
            blocks, meta = server.blocks_for_pane_with_meta(pane, "visible assistant answer from the right transcript")
            self.assertEqual([b["label"] for b in blocks], ["User prompt", "AI output"])
            self.assertIn("earlier user turn", blocks[0]["text"])
            self.assertEqual(meta["source"], "claude-transcript")
            self.assertEqual(meta["quality"], "full")
        finally:
            os.unlink(path)
            server._claude_map_cache.clear()
            server._claude_map_cache.update(original_cache)
            server.pane_is_alt_screen = original_alt
            server.claude_transcript_for_pane = original_resolve
            server._live_claude_context = original_live

    def test_process_identity_mismatch_drops_stale_claude_cache(self):
        path = self._write([
            {"type": "user", "message": {"role": "user", "content": "stale transcript from old pane process"}},
        ])
        original_cache = dict(server._claude_map_cache)
        original_alt = server.pane_is_alt_screen
        original_identity = server._pane_agent_process_identity
        try:
            server._claude_map_cache.clear()
            server._claude_map_cache["%13"] = {
                "last_good": path,
                "path": path,
                "proc_identity": "111:old",
            }
            server.pane_is_alt_screen = lambda _pane: True
            server._pane_agent_process_identity = lambda _pane: "222:new"
            pane = server.Pane(
                pane_id="%13",
                target="secretary_web:13.0",
                session="secretary_web",
                window_index=13,
                pane_index=0,
                window_name="same-project-a",
                command="claude",
                cwd="/same/project",
                title="Claude",
                active=False,
                kind="Claude",
                project="same-project",
                preview="",
                status="idle",
            )
            blocks, meta = server.blocks_for_pane_with_meta(pane, "✻ Thinking for 2s")
            self.assertNotIn("stale transcript", "\n".join(b["text"] for b in blocks))
            self.assertEqual(meta["source"], "tmux-screen")
            self.assertEqual(meta["reason"], "no-confident-transcript")
        finally:
            os.unlink(path)
            server._claude_map_cache.clear()
            server._claude_map_cache.update(original_cache)
            server.pane_is_alt_screen = original_alt
            server._pane_agent_process_identity = original_identity


class PaneAgentProcessIdentityTest(unittest.TestCase):
    """Regression: a pane kept showing a stale, days-old
    conversation after the Claude session running in it had genuinely
    restarted (confirmed live: a fresh session with its own new JSONL
    transcript was already 30+ minutes old, yet the dashboard was still
    serving a transcript from a session that had ended hours earlier).

    Root cause: #{pane_pid} is fixed at pane creation (normally the login
    shell) and never changes across however many times a program running
    inside that shell restarts - only #{pane_current_command} tracks the
    live foreground process name. _pane_agent_process_identity() used to
    gate its foreground-child lookup on that tmux-reported command string;
    once it read "claude"/"codex", the code assumed pane_pid was already the
    agent process and used it directly - but pane_pid was still the same
    long-lived shell PID, so the "process identity" token never changed
    across a Claude restart in that same shell, and
    claude_transcript_for_pane()'s cache-invalidation-on-identity-mismatch
    logic (already correct, see TranscriptCandidateScopeTest above) never
    had a reason to fire.

    Fixed by checking pane_pid's own /proc comm directly (_is_shell_process)
    instead of trusting tmux's #{pane_current_command}: only look for a
    foreground child when pane_pid is genuinely a shell; otherwise use
    pane_pid itself, unchanged from before. This is narrower than "always
    look for any claude/codex/node child" - a pane whose top-level process
    already IS the agent can itself have a claude/codex/node *child* (a
    subprocess, a spawned worker), and unconditionally preferring that child
    would rebind identity to a transient process on a pane that was already
    correctly identified (see the third test below)."""

    def _spawn_shell_with_renamed_child(self, comm: str = "claude"):
        """Start a real bash process (comm=bash, mirrors a persistent login
        shell) with a real child process renamed via prctl(PR_SET_NAME) so
        /proc/<pid>/comm genuinely reads e.g. "claude" - argv[0] tricks like
        `exec -a claude` do NOT affect /proc/pid/comm, only the kernel-level
        process name set via prctl does. `python3 ... & wait` stops bash's
        exec tail-call optimization, which would otherwise replace bash with
        the child instead of keeping both as separate real processes."""
        script = (
            "import ctypes,time;"
            "libc=ctypes.CDLL('libc.so.6');"
            f"libc.prctl(15,b'{comm}',0,0,0);"
            "time.sleep(6)"
        )
        proc = subprocess.Popen(["bash", "-c", f'python3 -c "{script}" & wait'])
        child_pid = self._wait_for_named_child(proc.pid, comm)
        self.assertIsNotNone(child_pid, "test setup failed: renamed child never appeared")
        return proc, child_pid

    def _wait_for_named_child(self, parent_pid: int, comm: str, timeout: float = 3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            kids = subprocess.run(
                ["pgrep", "-P", str(parent_pid)], capture_output=True, text=True
            ).stdout.split()
            for kid in kids:
                try:
                    with open(f"/proc/{kid}/comm", encoding="utf-8") as fh:
                        if fh.read().strip() == comm:
                            return kid
                except Exception:
                    continue
            time.sleep(0.05)
        return None

    def _mock_pane_pid(self, pid):
        # Clear the 1s-TTL process-identity cache so each test computes fresh
        # (tests reuse pane_id "%13" across methods within <1s).
        server._PROC_IDENTITY_CACHE.clear()
        original = server.run_tmux
        server.run_tmux = lambda args: type(
            "R", (), {"returncode": 0, "stdout": str(pid), "stderr": ""}
        )()
        return original

    def _make_pane(self, pane_id="%13"):
        return server.Pane(
            pane_id=pane_id, target=f"secretary_web:{pane_id.strip('%')}.0", session="secretary_web",
            window_index=13, pane_index=0, window_name="test", command="claude",
            cwd="/tmp", title="Claude", active=False, kind="Claude", project="test",
            preview="", status="idle",
        )

    def test_uses_child_agent_process_when_pane_pid_is_a_real_shell(self):
        # Shape of a long-lived pane: pane_pid is a persistent
        # shell (comm=bash) hosting a claude process as its child.
        proc, child_pid = self._spawn_shell_with_renamed_child("claude")
        try:
            original_run_tmux = self._mock_pane_pid(proc.pid)
            try:
                token = server._pane_agent_process_identity(self._make_pane())
            finally:
                server.run_tmux = original_run_tmux
        finally:
            proc.kill()
            proc.wait()

        self.assertTrue(token)
        self.assertEqual(token.split(":")[0], str(child_pid))
        self.assertNotEqual(token.split(":")[0], str(proc.pid))

    def test_falls_back_to_pane_pid_when_it_is_not_a_shell(self):
        # pane_pid itself is not a shell (this test process, comm=python3/
        # pytest) - no child lookup should happen, identity must resolve to
        # pane_pid directly.
        self_pid = os.getpid()
        original_run_tmux = self._mock_pane_pid(self_pid)
        try:
            token = server._pane_agent_process_identity(self._make_pane("%20"))
        finally:
            server.run_tmux = original_run_tmux

        self.assertEqual(token.split(":")[0], str(self_pid))

    def test_does_not_rebind_to_a_transient_child_when_pane_pid_is_already_the_agent(self):
        # Advisor-flagged risk: a pane whose top-level process already IS
        # claude (no shell wrapper) can itself have a claude/codex/node
        # child (a subprocess, a spawned worker). Unconditionally preferring
        # any such child would rebind identity to that transient process and
        # cause spurious cache churn on a pane that was already correctly
        # identified - this must NOT happen.
        import ctypes

        read_fd, write_fd = os.pipe()
        outer_pid = os.fork()
        if outer_pid == 0:
            os.close(read_fd)
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            libc.prctl(15, b"claude", 0, 0, 0)  # renamed BEFORE forking the inner child
            inner_pid = os.fork()
            if inner_pid == 0:
                libc.prctl(15, b"node", 0, 0, 0)
                os.write(write_fd, b"1")
                time.sleep(6)
                os._exit(0)
            os.waitpid(inner_pid, 0)
            os._exit(0)
        os.close(write_fd)
        os.read(read_fd, 1)  # blocks until the inner child has renamed itself
        os.close(read_fd)

        try:
            original_run_tmux = self._mock_pane_pid(outer_pid)
            try:
                token = server._pane_agent_process_identity(self._make_pane("%21"))
            finally:
                server.run_tmux = original_run_tmux
        finally:
            import signal

            os.kill(outer_pid, signal.SIGKILL)
            os.waitpid(outer_pid, 0)

        self.assertEqual(token.split(":")[0], str(outer_pid))

    def test_claude_transcript_for_pane_drops_cache_once_the_real_agent_pid_changes(self):
        # End-to-end: a stale cache entry bound to the shell's identity (what
        # the pre-fix code produced) must be invalidated once the fixed
        # identity function reports the real, different agent-process token.
        path = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False).name
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": "stale content from old claude run"}}) + "\n")
        proc, _child_pid = self._spawn_shell_with_renamed_child("claude")
        original_cache = dict(server._claude_map_cache)
        try:
            server._claude_map_cache.clear()
            server._claude_map_cache["%13"] = {
                "last_good": path,
                "path": path,
                "proc_identity": f"{proc.pid}:stale-shell-token",
            }
            original_run_tmux = self._mock_pane_pid(proc.pid)
            try:
                server.claude_transcript_for_pane(self._make_pane(), "some on-screen text with no confident match")
            finally:
                server.run_tmux = original_run_tmux
            self.assertNotIn("stale-shell-token", str(server._claude_map_cache.get("%13", {}).get("proc_identity", "")))
        finally:
            proc.kill()
            proc.wait()
            os.unlink(path)
            server._claude_map_cache.clear()
            server._claude_map_cache.update(original_cache)


class ClaudeMapBoundedStalenessTest(unittest.TestCase):
    """Regression: even after the
    process-identity hard-reset was fixed, _claude_map_cache was still a
    correctness-load-bearing cache - its sticky "last_good still matches >=2
    snips, keep it without a full rescan" fast path could ride a
    wrong-but-partially-overlapping transcript for as long as the (constant)
    process identity and the partial content overlap both held. The fix adds
    a bounded max-staleness backstop (_CLAUDE_MAP_FULL_RESOLVE_MAX_AGE):
    force a full candidate rescan even when last_good still matches, once the
    last full scan is older than that window. This makes the cache a latency
    optimization with a bounded drift ceiling rather than something whose
    correctness depends on every invalidation trigger being individually
    right.

    The scenario is deliberately the WORST case for the old fast path:
    process identity never changes (same claude process, e.g. a /clear +
    resume of a different session) and the stale transcript keeps matching
    >=2 of the on-screen snippets, so neither the identity reset nor the
    "last_good no longer matches" trigger ever fires."""

    def setUp(self):
        self._orig = {
            "screen": server._screen_match_snippets,
            "blob": server._transcript_norm_blob,
            "recent": server._recent_transcripts,
            "projdir": server._claude_project_dir_for_cwd,
            "identity": server._pane_agent_process_identity,
            "save": server._save_claude_map,
            "time": server.time.time,
            "cache": dict(server._claude_map_cache),
        }
        # Two candidate transcripts. STALE matches snips {s1,s2}; CORRECT
        # matches {s1,s2,s3} - so the sticky fast path (>=2 hits) keeps STALE,
        # but a full scan picks CORRECT (3 > 2, a strict unique winner).
        self._stale = "/fake/proj/STALE.jsonl"
        self._correct = "/fake/proj/CORRECT.jsonl"
        self._snips = ["snip-one-distinctive", "snip-two-distinctive", "snip-three-distinctive"]
        blobs = {
            self._stale: "".join(self._snips[:2]),
            self._correct: "".join(self._snips[:3]),
        }
        server._screen_match_snippets = lambda _text: list(self._snips)
        server._transcript_norm_blob = lambda p: blobs.get(str(p), "")
        server._recent_transcripts = lambda **kw: [self._correct, self._stale]
        server._claude_project_dir_for_cwd = lambda _cwd: "/fake/proj"
        server._pane_agent_process_identity = lambda _pane: "const-identity:1"
        server._save_claude_map = lambda: None
        self._clock = [1_000_000.0]
        server.time.time = lambda: self._clock[0]
        server._claude_map_cache.clear()

    def tearDown(self):
        server._screen_match_snippets = self._orig["screen"]
        server._transcript_norm_blob = self._orig["blob"]
        server._recent_transcripts = self._orig["recent"]
        server._claude_project_dir_for_cwd = self._orig["projdir"]
        server._pane_agent_process_identity = self._orig["identity"]
        server._save_claude_map = self._orig["save"]
        server.time.time = self._orig["time"]
        server._claude_map_cache.clear()
        server._claude_map_cache.update(self._orig["cache"])

    def _pane(self):
        return server.Pane(
            pane_id="%13", target="secretary_web:13.0", session="secretary_web",
            window_index=13, pane_index=0, window_name="t", command="claude",
            cwd="/fake/proj", title="Claude", active=False, kind="Claude",
            project="t", preview="", status="idle",
        )

    def test_fast_path_keeps_last_good_within_the_staleness_window(self):
        pane = self._pane()
        # First resolve: full scan picks CORRECT and stamps full_scan_ts.
        self.assertEqual(server.claude_transcript_for_pane(pane, "screen"), self._correct)
        # Now force the cache to point at STALE as last_good, as if a genuine
        # switch had briefly happened, keeping the fresh full_scan_ts.
        entry = server._claude_map_cache["%13"]
        entry["last_good"] = self._stale
        entry["path"] = self._stale
        # A change of on-screen sig (new poll) within the window: the sticky
        # fast path keeps STALE because it still matches >=2 snips and the
        # last full scan is recent.
        self._clock[0] += 5.0  # < _CLAUDE_MAP_FULL_RESOLVE_MAX_AGE (20s)
        self.assertEqual(server.claude_transcript_for_pane(pane, "screen"), self._stale)

    def test_full_rescan_forced_after_staleness_window_corrects_drift(self):
        pane = self._pane()
        self.assertEqual(server.claude_transcript_for_pane(pane, "screen"), self._correct)
        entry = server._claude_map_cache["%13"]
        entry["last_good"] = self._stale
        entry["path"] = self._stale
        # Advance PAST the max-staleness window: the sticky fast path must be
        # bypassed and a full rescan must run, re-picking CORRECT even though
        # STALE still matches >=2 snips and the process identity never changed.
        self._clock[0] += server._CLAUDE_MAP_FULL_RESOLVE_MAX_AGE + 1.0
        self.assertEqual(server.claude_transcript_for_pane(pane, "screen"), self._correct)


class HistoryPaginationTest(unittest.TestCase):
    """Phase 3a: on-demand /api/history_before pagination.

    Covers server.history_before / server._all_transcript_blocks_cached (the
    (path, mtime, size)-keyed full-transcript cache) and
    server.transcript_path_for_pane_id, all purely additive and never on the
    parse_transcript_tail hot path."""

    def _write(self, entries):
        fh = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        for e in entries:
            fh.write(json.dumps(e) + "\n")
        fh.close()
        return fh.name

    def _multi_block_transcript(self, n_pairs):
        entries = []
        for i in range(n_pairs):
            entries.append({"type": "user", "message": {"role": "user", "content": f"user turn {i}"}})
            entries.append({"type": "assistant", "message": {"role": "assistant", "content": f"assistant reply {i}"}})
        return self._write(entries)

    def setUp(self):
        server._history_blocks_cache.clear()

    def test_history_before_normal_pagination_walks_full_transcript(self):
        path = self._multi_block_transcript(5)  # 10 blocks total, indices 0..9
        try:
            page1 = server.history_before(path, cursor=None, count=4)
            self.assertEqual(len(page1["blocks"]), 4)
            self.assertEqual(page1["total"], 10)
            self.assertEqual(page1["next_cursor"], 6)
            self.assertTrue(page1["has_more"])
            self.assertEqual(page1["blocks"][0]["text"], "user turn 3")
            self.assertEqual(page1["blocks"][-1]["text"], "assistant reply 4")

            page2 = server.history_before(path, cursor=page1["next_cursor"], count=4)
            self.assertEqual(len(page2["blocks"]), 4)
            self.assertEqual(page2["next_cursor"], 2)
            self.assertTrue(page2["has_more"])
            self.assertEqual(page2["blocks"][0]["text"], "user turn 1")
            self.assertEqual(page2["blocks"][-1]["text"], "assistant reply 2")

            page3 = server.history_before(path, cursor=page2["next_cursor"], count=4)
            self.assertEqual(len(page3["blocks"]), 2, "only 2 blocks remain before index 2")
            self.assertIsNone(page3["next_cursor"])
            self.assertFalse(page3["has_more"])
            self.assertEqual([b["text"] for b in page3["blocks"]], ["user turn 0", "assistant reply 0"])

            # Walking pages back-to-front and re-flattening must reproduce the
            # exact original chronological order, with no gap or duplicate.
            reconstructed = [b["text"] for b in page3["blocks"] + page2["blocks"] + page1["blocks"]]
            expected = [b["text"] for b in server.parse_transcript_tail(path, max_blocks=10_000)]
            self.assertEqual(reconstructed, expected)
        finally:
            os.unlink(path)

    def test_history_before_reaches_very_beginning_in_one_page(self):
        path = self._multi_block_transcript(2)  # 4 blocks total
        try:
            page = server.history_before(path, cursor=None, count=50)
            self.assertEqual(len(page["blocks"]), 4)
            self.assertEqual(page["total"], 4)
            self.assertIsNone(page["next_cursor"])
            self.assertFalse(page["has_more"])
        finally:
            os.unlink(path)

    def test_history_before_cache_invalidates_on_file_change(self):
        path = self._multi_block_transcript(1)  # 2 blocks total
        try:
            first = server.history_before(path, cursor=None, count=50)
            self.assertEqual(first["total"], 2)
            with open(path, "a") as fh:
                fh.write(json.dumps({
                    "type": "assistant",
                    "message": {"role": "assistant", "content": "appended later"},
                }) + "\n")
            second = server.history_before(path, cursor=None, count=50)
            self.assertEqual(second["total"], 3, "cache must re-parse after mtime/size change")
            self.assertEqual(second["blocks"][-1]["text"], "appended later")
        finally:
            os.unlink(path)

    def test_transcript_path_for_pane_id_none_when_pane_not_found(self):
        original = server.pane_by_id
        try:
            server.pane_by_id = lambda _pane_id: None
            self.assertIsNone(server.transcript_path_for_pane_id("%does-not-exist"))
        finally:
            server.pane_by_id = original

    def test_transcript_path_for_pane_id_none_for_non_claude_pane(self):
        original = server.pane_by_id
        try:
            server.pane_by_id = lambda _pane_id: server.Pane(
                pane_id="%7",
                target="secretary_web:7.0",
                session="secretary_web",
                window_index=7,
                pane_index=0,
                window_name="shell-window",
                command="bash",
                cwd="/tmp",
                title="bash",
                active=False,
                kind="Shell",
                project="tmp",
                preview="",
                status="idle",
            )
            self.assertIsNone(server.transcript_path_for_pane_id("%7"))
        finally:
            server.pane_by_id = original

    def test_transcript_path_for_pane_id_none_when_no_confident_match(self):
        original_pane_by_id = server.pane_by_id
        original_capture = server.capture
        original_resolve = server.claude_transcript_for_pane
        try:
            server.pane_by_id = lambda _pane_id: server.Pane(
                pane_id="%13",
                target="secretary_web:13.0",
                session="secretary_web",
                window_index=13,
                pane_index=0,
                window_name="claude-window",
                command="claude",
                cwd="/tmp",
                title="Claude",
                active=False,
                kind="Claude",
                project="tmp",
                preview="",
                status="idle",
            )
            server.capture = lambda _target, history=240: "some ambiguous screen text"
            server.claude_transcript_for_pane = lambda _pane, _capture_text: None
            self.assertIsNone(server.transcript_path_for_pane_id("%13"))
        finally:
            server.pane_by_id = original_pane_by_id
            server.capture = original_capture
            server.claude_transcript_for_pane = original_resolve


class TranscriptCandidateScopeTest(unittest.TestCase):
    """Regression: a pane's transcript-matching candidate pool
    must be scoped to its OWN Claude project directory (derived from cwd),
    not every project on the machine — otherwise a short generic snippet
    (e.g. a phrase from this user's standard reply format, which recurs
    across many unrelated sessions) can win a "confident" match against the
    wrong, older transcript belonging to a totally different project."""

    def test_project_dir_encoding_matches_claude_convention(self):
        with tempfile.TemporaryDirectory() as home:
            original_home = os.environ.get("HOME")
            os.environ["HOME"] = home
            try:
                proj = os.path.join(home, ".claude", "projects", "-srv-work-demo-user")
                os.makedirs(proj)
                found = server._claude_project_dir_for_cwd("/srv/work/demo-user")
                self.assertIsNotNone(found)
                self.assertEqual(str(found), proj)
            finally:
                if original_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = original_home

    def test_missing_project_dir_returns_none(self):
        with tempfile.TemporaryDirectory() as home:
            original_home = os.environ.get("HOME")
            os.environ["HOME"] = home
            try:
                self.assertIsNone(server._claude_project_dir_for_cwd("/nowhere/such/path"))
            finally:
                if original_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = original_home

    def test_recent_transcripts_respects_dirs_scope(self):
        with tempfile.TemporaryDirectory() as base:
            own = os.path.join(base, "own-project")
            other = os.path.join(base, "other-project")
            os.makedirs(own)
            os.makedirs(other)
            own_file = os.path.join(own, "a.jsonl")
            other_file = os.path.join(other, "b.jsonl")
            with open(own_file, "w") as fh:
                fh.write("{}\n")
            with open(other_file, "w") as fh:
                fh.write("{}\n")
            from pathlib import Path

            scoped = server._recent_transcripts(dirs=[Path(own)])
            scoped_names = {p.name for p in scoped}
            self.assertIn("a.jsonl", scoped_names)
            self.assertNotIn("b.jsonl", scoped_names)


class SyncPaneJobStatusIdleCompletionTest(unittest.TestCase):
    """Regression: a job created via /api/send stayed stuck
    showing "处理中" (running) forever once the pane actually finished and
    went idle, because sync_pane_job_status() only ever transitioned a
    "running" job to "stale" after PANE_JOB_STALE_SECONDS (6h) - nothing
    marked it "completed" just because the pane itself settled back to idle.
    Confirmed against a real pane in production: a job created early in the morning was
    still showing "running" 8+ minutes after the pane's own status field and
    on-screen text ("Worked for 1m 01s", "Goal achieved") made clear the work
    was long done."""

    def setUp(self):
        from pathlib import Path

        event_ledger = server.event_ledger
        self._event_ledger = event_ledger
        # Isolate every path event_ledger keeps as a module-level global, not
        # just JOBS_DIR - upsert_job()/append_event() also touch LOCK_FILE/
        # EVENTS_FILE/COUNTER_FILE, and leaving those pointed at the real
        # ledger let this test write into production data and, combined with
        # server.JOB_CACHE not being cleared, made test_event_api.py's tests
        # fail when run in the same process (mirrors test_event_api.py's own isolation pattern).
        self._orig = {
            "LEDGER": event_ledger.LEDGER,
            "EVENTS_FILE": event_ledger.EVENTS_FILE,
            "JOBS_DIR": event_ledger.JOBS_DIR,
            "COUNTER_FILE": event_ledger.COUNTER_FILE,
            "LOCK_FILE": event_ledger.LOCK_FILE,
            "OFFSETS_FILE": event_ledger.OFFSETS_FILE,
        }
        self._tmpdir = tempfile.mkdtemp()
        self._orig_prefs = server.PREFS
        server.PREFS = Path(self._tmpdir) / "prefs.json"
        event_ledger.LEDGER = Path(self._tmpdir) / "ledger"
        event_ledger.EVENTS_FILE = event_ledger.LEDGER / "events.jsonl"
        event_ledger.JOBS_DIR = event_ledger.LEDGER / "jobs"
        event_ledger.COUNTER_FILE = event_ledger.LEDGER / "next-event-id.txt"
        event_ledger.LOCK_FILE = event_ledger.LEDGER / ".lock"
        # 偏移索引也是模块级全局；漏了它，测试会往真实的 event-offsets.json 写入
        # 指向不存在事件的偏移。
        event_ledger.OFFSETS_FILE = event_ledger.LEDGER / "event-offsets.json"
        event_ledger.JOBS_DIR.mkdir(parents=True, exist_ok=True)
        server.JOB_CACHE.clear()
        server.NEWEST_CARDS_JOB_CACHE.clear()
        server.PROVIDER_RUNTIME_CACHE.clear()
        server.JOB_IDLE_SINCE.clear()
        self._orig_completion_idle_seconds = server.JOB_COMPLETION_IDLE_SECONDS
        # These tests care whether a job *eventually* completes once the pane
        # settles idle, not the exact hysteresis duration (covered by
        # JobCompletionIdleHysteresisTest below) - collapse it to 0 so a
        # single sync_pane_job_status() call behaves like the old immediate
        # transition.
        server.JOB_COMPLETION_IDLE_SECONDS = 0.0

    def tearDown(self):
        for key, value in self._orig.items():
            setattr(self._event_ledger, key, value)
        server.JOB_CACHE.clear()
        server.NEWEST_CARDS_JOB_CACHE.clear()
        server.PROVIDER_RUNTIME_CACHE.clear()
        server.JOB_IDLE_SINCE.clear()
        server.IDLE_OBSERVED_REPORTED.clear()
        server.PREFS = self._orig_prefs
        server.JOB_COMPLETION_IDLE_SECONDS = self._orig_completion_idle_seconds
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_running_job_completes_once_pane_goes_idle(self):
        event_ledger = self._event_ledger
        job = event_ledger.upsert_job(
            "job-idle-completion-test",
            source="card-dashboard",
            status="running",
            pane="%99",
            target="secretary_web:99.0",
            message="pane status inferred as running",
        )
        self.assertEqual(job["status"], "running")

        result = server.sync_pane_job_status("%99", "secretary_web:99.0", "idle")

        self.assertEqual(result.get("status"), "completed")
        stored = event_ledger.get_job("job-idle-completion-test")
        self.assertEqual(stored["status"], "completed")
        self.assertTrue(stored.get("completed_at"))

    def test_idle_screen_never_completes_a_leader_or_supervisor_job(self):
        """Only the owner's verify/close/collect finishes those jobs (an idle
        screen must never mark a leader worker job completed)."""
        event_ledger = self._event_ledger
        event_ledger.upsert_job(
            "lead-worker-job", source="secretary-bus-leader", status="running",
            pane="%97", target="secretary_web:97.0", message="dispatched",
        )
        result = server.sync_pane_job_status("%97", "secretary_web:97.0", "idle")
        self.assertEqual(result.get("status"), "running")
        self.assertEqual(event_ledger.get_job("lead-worker-job")["status"], "running")
        kinds = [json.loads(line)["kind"] for line in event_ledger.EVENTS_FILE.read_text().splitlines()]
        self.assertEqual(kinds.count("pane_idle_observed"), 1)
        # Later polls of the same idle episode do not append more observations.
        for _ in range(3):
            server.sync_pane_job_status("%97", "secretary_web:97.0", "idle")
        kinds = [json.loads(line)["kind"] for line in event_ledger.EVENTS_FILE.read_text().splitlines()]
        self.assertEqual(kinds.count("pane_idle_observed"), 1)
        # A new idle episode after the pane worked again is reported again.
        server.sync_pane_job_status("%97", "secretary_web:97.0", "running")
        server.sync_pane_job_status("%97", "secretary_web:97.0", "idle")
        kinds = [json.loads(line)["kind"] for line in event_ledger.EVENTS_FILE.read_text().splitlines()]
        self.assertEqual(kinds.count("pane_idle_observed"), 2)

    def test_running_job_stays_running_while_pane_still_running(self):
        event_ledger = self._event_ledger
        event_ledger.upsert_job(
            "job-still-running-test",
            source="card-dashboard",
            status="running",
            pane="%98",
            target="secretary_web:98.0",
            message="pane status inferred as running",
        )

        result = server.sync_pane_job_status("%98", "secretary_web:98.0", "running")

        self.assertEqual(result.get("status"), "running")

    def test_build_panes_response_refreshes_stale_running_job_pill(self):
        # Regression for the gap an external Codex review found: fixing
        # sync_pane_job_status() alone was not enough, because /api/panes
        # (the card GRID) never called it - only /api/capture (a pane's
        # detail view) did. A card could show "处理中" forever unless someone
        # happened to open that exact pane.
        event_ledger = self._event_ledger
        event_ledger.upsert_job(
            "job-grid-stale-test",
            source="card-dashboard",
            status="running",
            pane="%97",
            target="secretary_web:97.0",
            message="pane status inferred as running",
        )
        fake_pane = server.Pane(
            pane_id="%97",
            target="secretary_web:97.0",
            session="secretary_web",
            window_index=97,
            pane_index=0,
            window_name="grid-test",
            command="claude",
            cwd="/tmp",
            title="Claude",
            active=False,
            kind="Claude",
            project="grid-test",
            preview="",
            status="idle",  # the pane itself is already idle/done
        )
        original_list_panes = server.list_panes
        server.list_panes = lambda *a, **k: [fake_pane]
        try:
            panes = server.build_panes_response("secretary_web")
        finally:
            server.list_panes = original_list_panes

        self.assertEqual(len(panes), 1)
        self.assertEqual(panes[0]["job_status"], "completed")
        stored = event_ledger.get_job("job-grid-stale-test")
        self.assertEqual(stored["status"], "completed")

    def test_build_panes_keeps_job_and_runtime_status_as_separate_signals(self):
        event_ledger = self._event_ledger
        event_ledger.upsert_job(
            "job-runtime-signal-test",
            source="secretary-bus-supervisor",
            status="running",
            pane="%96",
            target="worker-runtime",
        )
        fake_pane = server.Pane(
            pane_id="%96", target="secretary_web:96.0", session="secretary_web",
            window_index=96, pane_index=0, window_name="runtime-test", command="codex",
            cwd="/tmp", title="Codex", active=False, kind="Codex", project="runtime-test",
            preview="", status="idle",
        )
        original_list_panes = server.list_panes
        original_runtime = server.provider_runtime_observation
        server.list_panes = lambda *a, **k: [fake_pane]
        server.provider_runtime_observation = lambda *_args, **_kwargs: {
            "status": "running", "source": "codex_rollout_fd", "confidence": "high"
        }
        try:
            panes = server.build_panes_response("secretary_web")
        finally:
            server.list_panes = original_list_panes
            server.provider_runtime_observation = original_runtime

        self.assertEqual(panes[0]["status"], "idle", "terminal observation remains explicit")
        self.assertEqual(panes[0]["runtime_status"], "running")
        self.assertEqual(panes[0]["runtime_status_source"], "codex_rollout_fd")
        self.assertEqual(panes[0]["job_status"], "running")

    def test_visible_pending_human_summary_completes_cards_job_while_pane_still_running(self):
        event_ledger = self._event_ledger
        job = event_ledger.upsert_job(
            "job-visible-response-test",
            source="card-dashboard",
            status="running",
            pane="%97",
            target="secretary_web:97.0",
            task_preview="请检查并给我最终回复",
        )
        blocks = [
            {"role": "assistant", "label": "AI output", "text": "旧回复\n\n## Summary", "pending": False},
            {"role": "user", "label": "User prompt", "text": "请检查并给我最终回复", "pending": True},
            {"role": "assistant", "label": "AI output", "text": "已经完成。\n\n## Summary", "pending": True},
        ]

        result = server.sync_cards_job_completion_from_blocks(
            "%97", "secretary_web:97.0", blocks, server.job_summary(job)
        )

        self.assertEqual(result.get("status"), "completed")
        stored = event_ledger.get_job("job-visible-response-test")
        self.assertEqual(stored["status"], "completed")
        self.assertTrue(stored.get("completed_at"))

    def test_old_nonpending_duplicate_does_not_complete_new_repeated_prompt(self):
        job = {
            "id": "job-repeated-prompt-test",
            "source": "card-dashboard",
            "status": "sent",
            "task_preview": "继续",
        }
        old_blocks = [
            {"role": "user", "label": "User prompt", "text": "继续", "pending": False},
            {"role": "assistant", "label": "AI output", "text": "旧结果\n\n## Summary", "pending": False},
        ]

        self.assertFalse(server.cards_job_has_visible_response(job, old_blocks))

    def test_summary_before_current_prompt_is_not_completion_evidence(self):
        job = {
            "id": "job-current-prompt-test",
            "source": "card-dashboard",
            "status": "running",
            "task_preview": "再检查一次",
        }
        blocks = [
            {"role": "assistant", "label": "AI output", "text": "旧结果\n\n## Summary", "pending": True},
            {"role": "user", "label": "User prompt", "text": "再检查一次", "pending": True},
        ]

        self.assertFalse(server.cards_job_has_visible_response(job, blocks))

    def test_codex_nonpending_response_is_persisted_after_echo_grace(self):
        event_ledger = self._event_ledger
        job = event_ledger.upsert_job(
            "job-codex-response-started",
            source="card-dashboard",
            status="running",
            pane="%95",
            target="secretary_web:95.0",
            task_preview="检查状态",
            created_at="2026-08-05T10:00:00+08:00",
        )
        blocks = [
            {"role": "user", "label": "User prompt", "text": "检查状态", "pending": False},
            {"role": "assistant", "label": "AI output", "text": "我先核对真实链路。", "pending": False},
        ]

        result = server.sync_cards_job_response_started_from_blocks(
            "%95", "secretary_web:95.0", blocks, server.job_summary(job)
        )

        self.assertTrue(result.get("response_started_at"))
        stored = event_ledger.get_job("job-codex-response-started")
        self.assertEqual(stored["response_started_at"], result["response_started_at"])
        server.invalidate_job_cache()
        rebuilt = server.latest_job_summary_for_pane("%95", include_terminal=False)
        self.assertEqual(rebuilt["response_started_at"], result["response_started_at"])

    def test_codex_nonpending_human_summary_completes_after_echo_grace(self):
        job = {
            "id": "job-codex-final",
            "source": "card-dashboard",
            "status": "running",
            "task_preview": "请给最终结果",
            "created_at": "2026-08-05T10:00:00+08:00",
        }
        blocks = [
            {"role": "user", "label": "User prompt", "text": "请给最终结果", "pending": False},
            {"role": "assistant", "label": "AI output", "text": "完成。\n\n## Summary", "pending": False},
        ]
        self.assertTrue(server.cards_job_has_visible_response(job, blocks))

    def test_human_summary_phrase_inside_prose_is_not_final(self):
        job = {
            "id": "job-prose-summary",
            "source": "card-dashboard",
            "status": "running",
            "task_preview": "检查规则",
            "created_at": "2026-08-05T10:00:00+08:00",
        }
        blocks = [
            {"role": "user", "label": "User prompt", "text": "检查规则", "pending": False},
            {"role": "assistant", "label": "AI output", "text": "规则中提到了 Summary 这个词，但还没完成。", "pending": False},
        ]
        self.assertFalse(server.cards_job_has_visible_response(job, blocks))

    def test_waiting_user_job_eventually_becomes_stale(self):
        event_ledger = self._event_ledger
        job = event_ledger.upsert_job(
            "job-waiting-stale",
            source="card-dashboard",
            status="waiting_user",
            pane="%94",
            target="secretary_web:94.0",
            updated_at="2026-08-05T10:00:00+08:00",
        )
        self.assertEqual(job["status"], "waiting_user")
        original = server.PANE_JOB_STALE_SECONDS
        server.PANE_JOB_STALE_SECONDS = 0
        try:
            server.invalidate_job_cache()
            result = server.sync_pane_job_status("%94", "secretary_web:94.0", "shell")
        finally:
            server.PANE_JOB_STALE_SECONDS = original
        self.assertEqual(result["status"], "stale")

    def test_newer_terminal_cards_job_prevents_older_active_job_resurfacing(self):
        event_ledger = self._event_ledger
        old = event_ledger.upsert_job(
            "job-old-running-shadow-test",
            source="card-dashboard",
            status="running",
            pane="%93",
            target="secretary_web:93.0",
            task_preview="旧消息",
            created_at="2026-07-10T20:01:00+08:00",
        )
        newer = event_ledger.upsert_job(
            "job-new-completed-shadow-test",
            source="card-dashboard",
            status="completed",
            pane="%93",
            target="secretary_web:93.0",
            task_preview="新消息",
            created_at="2026-07-10T21:28:00+08:00",
            completed_at="2026-07-10T21:30:00+08:00",
        )
        self.assertEqual(old["status"], "running")
        self.assertEqual(newer["status"], "completed")
        server.invalidate_job_cache()

        result = server.sync_pane_job_status("%93", "secretary_web:93.0", "running")

        self.assertEqual(result, {})
        self.assertEqual(event_ledger.get_job("job-old-running-shadow-test")["status"], "completed")
        self.assertEqual(
            server.latest_job_summary_for_pane("%93", include_terminal=True)["id"],
            "job-new-completed-shadow-test",
        )

    def test_new_cards_job_supersedes_all_older_active_cards_jobs(self):
        event_ledger = self._event_ledger
        for job_id, status, created_at in (
            ("job-old-running", "running", "2026-07-10T17:44:00+08:00"),
            ("job-old-waiting", "waiting_user", "2026-07-10T17:45:00+08:00"),
        ):
            event_ledger.upsert_job(
                job_id,
                source="card-dashboard",
                status=status,
                pane="%92",
                target="secretary_web:92.0",
                created_at=created_at,
            )
        newest = event_ledger.upsert_job(
            "job-new-sent",
            source="card-dashboard",
            status="sent",
            pane="%92",
            target="secretary_web:92.0",
            created_at="2026-07-10T22:00:00+08:00",
        )

        closed = server.complete_older_cards_jobs("%92", newest)

        self.assertCountEqual(closed, ["job-old-running", "job-old-waiting"])
        self.assertEqual(event_ledger.get_job("job-old-running")["status"], "completed")
        self.assertEqual(event_ledger.get_job("job-old-waiting")["status"], "completed")
        self.assertEqual(event_ledger.get_job("job-new-sent")["status"], "sent")


class JobCompletionIdleHysteresisTest(unittest.TestCase):
    """Regression: a pane went sent -> running ->
    completed in ~2-3 seconds while the AI was still mid-turn (confirmed live:
    the job's own event trail showed "running" and then "completed" two seconds
    later, yet the pane kept visibly thinking minutes later). A single
    "idle" sample is not proof of completion - infer_status()'s vocabulary of
    "still working" indicators is finite, and real gaps (message just sent,
    between a tool call and the next spinner frame) can momentarily read as
    idle. sync_pane_job_status() must require idle to hold continuously for
    JOB_COMPLETION_IDLE_SECONDS before completing a running job."""

    def setUp(self):
        from pathlib import Path

        event_ledger = server.event_ledger
        self._event_ledger = event_ledger
        self._orig = {
            "LEDGER": event_ledger.LEDGER,
            "EVENTS_FILE": event_ledger.EVENTS_FILE,
            "JOBS_DIR": event_ledger.JOBS_DIR,
            "COUNTER_FILE": event_ledger.COUNTER_FILE,
            "LOCK_FILE": event_ledger.LOCK_FILE,
            "OFFSETS_FILE": event_ledger.OFFSETS_FILE,
        }
        self._tmpdir = tempfile.mkdtemp()
        event_ledger.LEDGER = Path(self._tmpdir) / "ledger"
        event_ledger.EVENTS_FILE = event_ledger.LEDGER / "events.jsonl"
        event_ledger.JOBS_DIR = event_ledger.LEDGER / "jobs"
        event_ledger.COUNTER_FILE = event_ledger.LEDGER / "next-event-id.txt"
        event_ledger.LOCK_FILE = event_ledger.LEDGER / ".lock"
        # 偏移索引也是模块级全局；漏了它，测试会往真实的 event-offsets.json 写入
        # 指向不存在事件的偏移。
        event_ledger.OFFSETS_FILE = event_ledger.LEDGER / "event-offsets.json"
        event_ledger.JOBS_DIR.mkdir(parents=True, exist_ok=True)
        server.JOB_CACHE.clear()
        server.JOB_IDLE_SINCE.clear()
        self._orig_completion_idle_seconds = server.JOB_COMPLETION_IDLE_SECONDS
        server.JOB_COMPLETION_IDLE_SECONDS = 6.0
        self._orig_time = server.time.time
        self._fake_now = 1_000_000.0
        server.time.time = lambda: self._fake_now

    def tearDown(self):
        server.time.time = self._orig_time
        for key, value in self._orig.items():
            setattr(self._event_ledger, key, value)
        server.JOB_CACHE.clear()
        server.JOB_IDLE_SINCE.clear()
        server.JOB_COMPLETION_IDLE_SECONDS = self._orig_completion_idle_seconds
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_single_idle_sample_does_not_complete_a_running_job(self):
        event_ledger = self._event_ledger
        event_ledger.upsert_job(
            "job-hysteresis-single-sample",
            source="card-dashboard",
            status="running",
            pane="%96",
            target="secretary_web:96.0",
            message="pane status inferred as running",
        )

        result = server.sync_pane_job_status("%96", "secretary_web:96.0", "idle")

        self.assertEqual(result.get("status"), "running")
        stored = event_ledger.get_job("job-hysteresis-single-sample")
        self.assertEqual(stored["status"], "running")

    def test_completes_once_idle_persists_past_threshold(self):
        event_ledger = self._event_ledger
        event_ledger.upsert_job(
            "job-hysteresis-persists",
            source="card-dashboard",
            status="running",
            pane="%95",
            target="secretary_web:95.0",
            message="pane status inferred as running",
        )

        first = server.sync_pane_job_status("%95", "secretary_web:95.0", "idle")
        self.assertEqual(first.get("status"), "running")

        self._fake_now += server.JOB_COMPLETION_IDLE_SECONDS + 0.5
        second = server.sync_pane_job_status("%95", "secretary_web:95.0", "idle")

        self.assertEqual(second.get("status"), "completed")
        stored = event_ledger.get_job("job-hysteresis-persists")
        self.assertEqual(stored["status"], "completed")

    def test_resumed_activity_resets_the_idle_streak(self):
        # A background agent dispatch: idle flickers briefly, then the pane
        # is observed running again before the threshold elapses. The job
        # must stay "running", not accumulate idle time across the gap.
        event_ledger = self._event_ledger
        event_ledger.upsert_job(
            "job-hysteresis-reset",
            source="card-dashboard",
            status="running",
            pane="%94",
            target="secretary_web:94.0",
            message="pane status inferred as running",
        )

        server.sync_pane_job_status("%94", "secretary_web:94.0", "idle")
        self._fake_now += server.JOB_COMPLETION_IDLE_SECONDS - 1.0
        server.sync_pane_job_status("%94", "secretary_web:94.0", "running")
        self._fake_now += server.JOB_COMPLETION_IDLE_SECONDS - 1.0
        result = server.sync_pane_job_status("%94", "secretary_web:94.0", "idle")

        self.assertEqual(result.get("status"), "running")
        stored = event_ledger.get_job("job-hysteresis-reset")
        self.assertEqual(stored["status"], "running")


class ScreenMatchSnippetWindowingTest(unittest.TestCase):
    """Regression: capture() now asks tmux to join wrapped
    lines (-J), so what used to be several separate wrapped physical lines
    (each contributing its own match snippet) can arrive as one long logical
    line. _screen_match_snippets must window a long line into multiple
    fragments instead of only taking its first 60 chars, or joining wraps
    would silently starve transcript-matching of signal it used to get for
    free from the old per-physical-line splitting."""

    def test_short_line_yields_one_snippet(self):
        text = "这是一条不算很长但足够独特的普通对话行用于测试"
        snips = server._screen_match_snippets(text)
        self.assertEqual(len(snips), 1)

    def test_long_joined_line_yields_multiple_windows_covering_the_tail(self):
        # Simulate a paragraph that used to be 3 separate wrapped physical
        # lines and is now one long -J-joined logical line.
        head = "第一段折行内容甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥" * 2
        tail = "只有加窗口切片才能在这条长行的尾部找到的独特短语标记ZZZMARKERTAIL"
        long_line = head + tail
        snips = server._screen_match_snippets(long_line)
        self.assertGreater(len(snips), 1, "a long joined line should yield more than one window")
        self.assertTrue(
            any("ZZZMARKERTAIL" in s for s in snips),
            "windowing must reach the tail of a long joined line, not just its first 60 chars",
        )

    def test_per_line_window_count_is_bounded(self):
        pathological = "无意义重复填充字符测试内容" * 40  # ~480 chars, one physical line
        snips = server._screen_match_snippets(pathological)
        self.assertLessEqual(len(snips), server._SNIPPET_MAX_WINDOWS_PER_LINE)


class FileEditDiffLineTest(unittest.TestCase):
    """Regression: diffs of files whose content lines start
    with "|" (e.g. markdown tables in notes.md) were losing every line
    break, because FILE_EDIT_DIFF_LINE_RE required whitespace-or-end right
    after the +/- marker, but the marker sits directly against the pipe with
    no separating space in this format."""

    def test_matches_table_row_diff_lines(self):
        self.assertTrue(server.FILE_EDIT_DIFF_LINE_RE.match("38  | unchanged table row"))
        self.assertTrue(server.FILE_EDIT_DIFF_LINE_RE.match("39 +| added table row"))
        self.assertTrue(server.FILE_EDIT_DIFF_LINE_RE.match("46 -| removed table row"))

    def test_still_matches_plain_code_diff_lines(self):
        self.assertTrue(server.FILE_EDIT_DIFF_LINE_RE.match("39 + someCode();"))
        self.assertTrue(server.FILE_EDIT_DIFF_LINE_RE.match("41 - oldCode();"))

    def test_does_not_match_plain_wrapped_continuation(self):
        self.assertFalse(server.FILE_EDIT_DIFF_LINE_RE.match("    some wrapped continuation text"))

    def test_normalize_keeps_table_diff_rows_on_separate_lines(self):
        # Reconstructs the shape of a real "Edited notes.md (+2 -1)" table
        # diff: each numbered row must survive as its own line, while a long
        # cell's own terminal-wrapped continuation still merges into it.
        raw = (
            "Edited notes.md (+2 -1)\n"
            "    38  | ↳ moduleA | short desc\n"
            "    39 +| ↳ 示例播放组件 | 补一层前端恢复逻辑，避免同一条目\n"
            "          没有新命令时浏览器播放静音。\n"
            "    40  | ↳ moduleB | desc\n"
        )
        out = server.normalize_file_edit_output(raw)
        lines = out.splitlines()
        self.assertEqual(lines[0], "Edited notes.md (+2 -1)")
        self.assertTrue(lines[1].startswith("38  | ↳ moduleA"))
        added_line = next(l for l in lines if l.startswith("39 +|"))
        self.assertIn("避免同一条目", added_line)
        self.assertIn("没有新命令时浏览器播放静音", added_line)
        self.assertTrue(any(l.startswith("40  | ↳ moduleB") for l in lines))


class ModelStatusLineTest(unittest.TestCase):
    """Regression + bug-hunt for is_model_status_line / strip_inline_model_status.
    Both functions share one regex:

        (?:gpt-[\\w.-]+|claude[\\w.-]*)(?:\\s+[\\w.-]+){0,3}\\s+·\\s+...

    Positive samples below follow the layout of Codex footer lines as read with
    `tmux capture-pane -p -J`, with synthetic project paths, covering Chinese
    project paths, a truncated "~" path, the
    "Goal achieved (Nm)" / "Goal blocked (...)" trailer, and a " · Main
    [default]" git-branch segment squeezed in before the trailer.
    """

    # --- footer-shaped samples: must be recognized and stripped -------------

    REAL_STATUS_LINES = [
        # Codex footer, path collapsed to bare "~", right-justified trailer.
        "  gpt-5.5 high · ~               Goal achieved (44m)  ",
        # Chinese project slug, no branch segment.
        "  gpt-5.5 high · ~/projects/示例项目一-aaaaaa"
        "                                                                                                                                                                                               Goal achieved (1h 17m)  ",
        # Chinese project slug + " · Main [default]" branch segment before trailer.
        "  gpt-5.5 high · ~/projects/示例项目二-bbbbbb · Main [default]"
        "                                                                                                                                                   Goal achieved (3h 3m)  ",
        # Chinese slug with digits/mixed punctuation in it.
        "  gpt-5.5 high · ~/projects/示例项目三-第50版-cccccc · Main [default]"
        "                                                                                                                                     Goal achieved (21m)  ",
        # No trailer at all yet (still running), just padding.
        "  gpt-5.5 high · ~/projects/示例项目四-dddddd     ",
        # "Goal blocked" trailer variant, not just "Goal achieved".
        "  gpt-5.5 high · ~/projects/示例项目五-eeeeee · Main [default]"
        "                                                                                                                                             Goal blocked (/goal resume)  ",
    ]

    def test_real_captured_footer_lines_are_recognized(self):
        for line in self.REAL_STATUS_LINES:
            with self.subTest(line=line.strip()[:40]):
                self.assertTrue(server.is_model_status_line(line))

    def test_strip_inline_model_status_removes_real_footer_embedded_in_block(self):
        text = "\n".join(
            ["助手输出的第一行", self.REAL_STATUS_LINES[2], "助手输出的最后一行"]
        )
        cleaned = server.strip_inline_model_status(text)
        self.assertIn("助手输出的第一行", cleaned)
        self.assertIn("助手输出的最后一行", cleaned)
        self.assertNotIn("Goal achieved", cleaned)
        self.assertNotIn("示例项目二", cleaned)

    def test_claude_style_status_line_recognized(self):
        # Task description's Claude-shape sample: "model · Ns · ↓ Nk tokens".
        self.assertTrue(server.is_model_status_line("claude-4 · 12s · ↓ 3k tokens"))

    # --- rejections: no recognized prefix, or no "·" at all ------------------

    def test_plain_lines_without_prefix_or_dot_are_not_status_lines(self):
        for line in [
            "",
            "   ",
            "这是普通的一行文字,没有模型状态",
            "claude-3 opus is fast",  # has "claude" prefix but no "·" at all
            "· 这一行以中间点开头,但没有模型前缀",
        ]:
            with self.subTest(line=line):
                self.assertFalse(server.is_model_status_line(line))

    # --- documented limitation: visible Cards only parse gpt-/claude- -------
    # Task explicitly says this need not be fixed, only clearly reported.
    # New model families (qwen/deepseek/grok/...) are simply invisible to this
    # heuristic: their footer lines are NOT stripped and will leak into the
    # cleaned block text verbatim if such a pane kind is ever added.

    def test_non_listed_model_prefixes_are_not_recognized_known_limitation(self):
        for line in [
            "qwen-max high · ~/projects/foo",
            "deepseek-v3 · ~/projects/foo",
            "grok-4 fast · ~/projects/foo",
            "gemini-pro · ~/projects/foo",
        ]:
            with self.subTest(line=line):
                self.assertFalse(
                    server.is_model_status_line(line),
                    "Cards intentionally parse only visible Codex/Claude sessions",
                )

    # --- FIXED: bare prose lines that start with a recognized -----
    # brand word and contain a "·" within the next few tokens used to be
    # indistinguishable from an actual footer line, so real assistant/user
    # content got silently deleted (confirmed through the full parse_blocks()
    # pipeline, not just the isolated function). Fixed by restricting the
    # filler-word slot in the shared `_MODEL_STATUS_PREFIX` to an effort-level
    # whitelist (see is_model_status_line's docstring/comment). These were
    # `@unittest.expectedFailure` (xfail) while the bug was open; now that the
    # fix landed they pass as plain regression tests.

    def test_KNOWN_BUG_prose_line_starting_with_brand_word_is_misidentified(self):
        false_positives = [
            "Gemini 官方文档提到 · 使用限制为 60 RPM",
            "GPT-4o is great · but expensive for our use case",
            "Claude 最近更新了 · 系统提示词的处理方式",
            "gemini 的免费额度 · 每天有限",
        ]
        for line in false_positives:
            with self.subTest(line=line):
                self.assertFalse(
                    server.is_model_status_line(line),
                    "a normal sentence that merely starts with a brand word and uses "
                    "'·' a few tokens later must not be treated as a footer status line",
                )

    def test_KNOWN_BUG_strip_inline_model_status_deletes_real_prose_line(self):
        text = "这是一段正常的助手输出,讨论模型选择。\nGemini 免费额度每天有限 · 建议谨慎使用\n以上是我的建议。"
        cleaned = server.strip_inline_model_status(text)
        self.assertIn(
            "Gemini 免费额度每天有限",
            cleaned,
            "strip_inline_model_status must not delete real prose that merely "
            "mentions a model brand name followed by '·' later in the sentence",
        )

    def test_KNOWN_BUG_parse_blocks_silently_drops_prose_line_end_to_end(self):
        # End-to-end repro through the real pipeline entry point (not just the
        # isolated regex), matching how this would actually manifest to a user.
        text = "● 综合评测显示两款模型各有优势\nGemini 免费额度每天有限 · 建议谨慎使用\n以上是我的建议。"
        blocks = server.parse_blocks(text, pane_kind="Claude")
        joined = "\n".join(b["text"] for b in blocks)
        self.assertIn(
            "Gemini 免费额度每天有限",
            joined,
            "a real content line must survive parse_blocks even if it starts with "
            "a model brand word and contains '·' later in the same sentence",
        )

    # --- interpunct lookalikes: only U+00B7 "·" triggers, confirming the ------
    # blast radius of the false-positive above doesn't extend to visually
    # similar dot characters common in CJK text (fullwidth katakana middle
    # dot U+30FB "・", hyphenation point U+2027 "‧").

    def test_lookalike_dot_characters_do_not_trigger(self):
        for line in [
            "gemini pro ・ 全角中点不应触发",  # U+30FB
            "gemini pro ‧ 连字符号不应触发",  # U+2027
        ]:
            with self.subTest(line=line):
                self.assertFalse(server.is_model_status_line(line))

    # --- whitespace / case tolerance -----------------------------------------

    def test_case_insensitive_and_leading_trailing_whitespace_tolerated(self):
        self.assertTrue(server.is_model_status_line("  CLAUDE-3.5-SONNET · running  "))
        self.assertFalse(server.is_model_status_line("\tGemini-Pro · idle\t"))

    def test_strip_inline_model_status_handles_literal_backslash_n(self):
        # Some raw/unescaped-JSON text carries literal two-char "\n" sequences
        # instead of real newlines; the function's regex explicitly supports
        # both via (?:\\n|\n)+.
        text = r"first line\nclaude-3.5 · ~/projects/foo\nlast line"
        cleaned = server.strip_inline_model_status(text)
        self.assertIn("first line", cleaned)
        self.assertIn("last line", cleaned)
        self.assertNotIn("claude-3.5", cleaned)


class VolatileStatusLineTest(unittest.TestCase):
    """Regression + bug-hunt for is_volatile_status_line and its 9 sub-checks
   . is_volatile_status_line ORs together: AI_RUNNING_STATUS_RE,
    WORKFLOW_RUNNING_STATUS_RE, SPINNER_LINE_RE, ZH_THINKING_RE, three inline
    Working(/Running(/Thinking( checks, is_claude_chrome_line and
    is_claude_tool_summary_line. It decides whether a line is a transient status
    prompt that must be filtered out of previews / transcript blocks. A false
    positive here is data loss: the line is dropped in preview_from, stripped in
    parse_blocks (before block split), and can drop a whole assistant block
    (parse_blocks push(): all-lines-volatile -> block discarded).

    Most sample lines below copy the exact shape of real Claude/Codex status
    lines rather than hand-invented ones (real captures have shapes you do not
    predict); they contain only UI chrome.
    """

    # --- true positives: real transient status lines MUST be volatile --------

    def test_real_spinner_lines_are_volatile(self):
        # Real spinner shapes (Germinating, Osmosing), plus the
        # canonical form documented in the SPINNER_LINE_RE comment. These route
        # through SPINNER_LINE_RE, which deliberately does NOT use a thinking-word
        # vocabulary (Claude cycles dozens of random gerunds).
        for line in [
            "✽ Germinating… (15s · thinking)",
            "✻ Osmosing… (3m 17s · ↓ 7.1k tokens · thinking with low effort)",
            "✻ Cogitating… (12s · ↓ 3k tokens · esc to interrupt)",
            "Frolicking... (8s · ↓ 2k tokens)",  # 3-dot ascii ellipsis variant
        ]:
            with self.subTest(line=line):
                self.assertTrue(server.SPINNER_LINE_RE.match(line.strip()))
                self.assertTrue(server.is_volatile_status_line(line))

    def test_real_workflow_status_lines_are_volatile(self):
        # Real shapes: "Waiting for N dynamic workflow", and the
        # "N/M agents done" orchestration rows. Route through
        # WORKFLOW_RUNNING_STATUS_RE.
        for line in [
            "✻ Waiting for 1 dynamic workflow to finish · 4 messages hidden (/focus to show)",
            "◯ example-status-check… 0/3 agents done · 1m",
            "◯ example-review  示例复核任务   2/3 agents done · 13m 45s · ↓ 350.9k tokens",
        ]:
            with self.subTest(line=line):
                self.assertTrue(server.WORKFLOW_RUNNING_STATUS_RE.search(line.strip()))
                self.assertTrue(server.is_volatile_status_line(line))

    def test_real_chrome_and_tool_summary_lines_are_volatile(self):
        # Captured live: "✻ Worked/Cooked/Baked/Brewed for …" (Claude chrome
        # status, is_claude_chrome_line) and the "Read/Called/Ran N …" tool
        # summaries (is_claude_tool_summary_line).
        for line in [
            "✻ Worked for 41s",
            "✻ Cooked for 4m 26s · 21 messages hidden (/focus to show)",
            "✻ Brewed for 1m 5s",
            "Read 1 file, called 1 tool, ran 1 shell command",
            "Called 1 tool, ran 2 shell commands",
            "Ran 4 shell commands",
        ]:
            with self.subTest(line=line):
                self.assertTrue(server.is_volatile_status_line(line))

    def test_background_agent_wait_line_survives_chrome_filtering_and_reads_running(self):
        # Root cause of the "job completed while pane still
        # processing" bug: is_claude_chrome_line()
        # discarded ANY line containing "hidden (/focus to show)" outright,
        # including the compound form Claude Code actually renders while a
        # subagent/workflow is dispatched: a genuine "still working" prefix
        # glued to that decorative suffix. Because clean_display_line() ran
        # before infer_status() ever saw the line, WORKFLOW_RUNNING_STATUS_RE
        # matching in isolation (see test_real_workflow_status_lines_are_volatile
        # above) never actually fired through the real pipeline - the pane
        # read as "idle" the whole time it was genuinely waiting.
        running_lines = [
            "✻ Waiting for 1 background agent to finish · 18 messages hidden (/focus to show)",
            "✻ Waiting for 3 background agents to finish · 5 messages hidden (/focus to show)",
            "✻ Waiting for 1 dynamic workflow to finish · 4 messages hidden (/focus to show)",
            "Running 2 agents…",
        ]
        for line in running_lines:
            with self.subTest(line=line):
                self.assertFalse(server.is_claude_chrome_line(line))
                self.assertIsNotNone(server.clean_display_line(line))
                self.assertEqual(server.infer_status(line + "\n❯ ", "claude"), "running")

        # The past-tense "already finished" chrome forms must still be
        # dropped as pure decoration - only the still-waiting forms are
        # exempted.
        already_done_lines = [
            "✻ Cooked for 4m 26s · 21 messages hidden (/focus to show)",
            "✻ Worked for 41s · 3 messages hidden (/focus to show)",
        ]
        for line in already_done_lines:
            with self.subTest(line=line):
                self.assertTrue(server.is_claude_chrome_line(line))
                self.assertIsNone(server.clean_display_line(line))

    def test_inline_working_running_thinking_and_zh_thinking(self):
        # The three inline anchors plus the Chinese standalone-thinking regex.
        for line in [
            "Working (esc to interrupt)",
            "● Running (12s)",
            "Thinking (reticulating)",
            "思考中",
            "✻ 分析中",
        ]:
            with self.subTest(line=line):
                self.assertTrue(server.is_volatile_status_line(line))

    def test_ai_running_status_requires_more_than_bare_duration_paren(self):
        # AI_RUNNING_STATUS_RE is also used by infer_status, so a false positive
        # here can both delete formal content and mark an idle pane as running.
        # A lone duration such as "(3h)" is common prose; real status rows pair
        # elapsed time with tokens/arrows/esc text or a "·" status segment.
        false_positives = [
            "Running the benchmark (3h)",
            "Reading the docs (about 5h)",
            "Writing the migration note (2m)",
        ]
        for line in false_positives:
            with self.subTest(line=line):
                self.assertFalse(server.AI_RUNNING_STATUS_RE.match(line))
                self.assertFalse(server.is_volatile_status_line(line))
                self.assertEqual(server.infer_status(f"{line}\n›", "claude"), "idle")

    def test_ai_running_status_keeps_real_interrupt_and_token_shapes(self):
        for line in [
            "Working (esc to interrupt)",
            "+ Thundering...(7m 17s · ↓21.4k tokens)",
            "Reading docs (15s · thinking)",
            "Editing files (↓ 3k tokens)",
        ]:
            with self.subTest(line=line):
                self.assertTrue(server.AI_RUNNING_STATUS_RE.match(line))
                self.assertTrue(server.is_volatile_status_line(line))
                self.assertEqual(server.infer_status(f"{line}\n›", "claude"), "running")

    def test_bare_duration_running_prompt_still_counts_as_running(self):
        # This shape is intentionally preserved through the narrower inline
        # Running/Working/Thinking checks, not AI_RUNNING_STATUS_RE.
        self.assertFalse(server.AI_RUNNING_STATUS_RE.match("● Running (12s)"))
        self.assertTrue(server.is_volatile_status_line("● Running (12s)"))
        self.assertEqual(server.infer_status("● Running (12s)\n›", "claude"), "running")

    # --- false positives (the real bug): formal content misjudged volatile ---

    def test_formal_content_ending_in_time_paren_is_not_volatile(self):
        # THE BUG . SPINNER_LINE_RE's old `[…\.]{1,3}` accepted
        # a SINGLE sentence-ending period, so any short (<=5 word) English line
        # ending "Sentence. (N <s/m/h-word>)" was judged volatile and silently
        # dropped from the UI. A real spinner ellipsis is always `…`/`...`, never
        # one period. These are formal AI-output lines that must stay visible.
        for line in [
            "All tests pass. (2 minutes)",
            "Deploy succeeded. (2 minutes downtime)",
            "Ready to merge. (2 hunks)",
            "Analysis complete. (2 hours saved)",
            "Migration done. (3 seconds)",
            "Restart needed. (5 minutes)",
            "See the summary. (5 sections)",
            "Merged to main. (3 stale)",
            "Saved. (3s)",
            "Done. (2m)",
        ]:
            with self.subTest(line=line):
                self.assertFalse(
                    server.is_volatile_status_line(line),
                    f"formal content wrongly judged volatile (would be dropped): {line!r}",
                )

    def test_content_with_interior_digit_is_not_volatile(self):
        # Guard for the natural boundary: an interior number breaks the word-run
        # before the period, so these were never affected -- lock that in.
        for line in [
            "It cost 3 dollars. (5 saved)",
            "Saved 5 records. (3 shards)",
        ]:
            with self.subTest(line=line):
                self.assertFalse(server.is_volatile_status_line(line))

    def test_prose_and_tool_echo_lines_are_not_volatile(self):
        # Ordinary prose / tool-echo line shapes. None should be filtered.
        for line in [
            "● 已完成 ✅ 结果确认 ok=True，文件名和大小都一致。",
            "• Ran sed -n '1,220p' docs/plan.md 2>/dev/null || true",
            "• Explored",
            "• 找到了。旧的示例材料不在当前的 pipeline 里",
            "- Fig 1：示例指标图 + 分布直方图 + 高低值标注",
            "  1. archive/example-bundle/README.md",
        ]:
            with self.subTest(line=line):
                self.assertFalse(server.is_volatile_status_line(line))

    # --- end-to-end: the user-visible data-loss seam (parse_blocks) ----------

    def test_formal_content_survives_block_split(self):
        # Drives the real seam: parse_blocks strips volatile lines before block
        # split (server.py ~L1651) AND drops an assistant block whose every line
        # is volatile (~L1629). Pre-fix, "● All tests pass. (2 minutes)" produced
        # ZERO blocks -- the line vanished from the transcript. Lock that it now
        # survives as an AI-output block.
        blocks = server.parse_blocks("● All tests pass. (2 minutes)", "Claude")
        self.assertTrue(
            any("All tests pass. (2 minutes)" in b["text"] for b in blocks),
            "formal AI-output line was dropped from parsed blocks (data loss)",
        )

    def test_real_spinner_is_filtered_from_block_split(self):
        # The true-positive side of the same seam: a genuine spinner line must
        # still be filtered out entirely (guards against over-loosening the fix).
        blocks = server.parse_blocks(
            "● Cogitating… (12s · ↓ 3k tokens · esc to interrupt)", "Claude"
        )
        self.assertEqual(blocks, [], "spinner line leaked into transcript blocks")


class IsQueuedMessageLineTest(unittest.TestCase):
    """is_queued_message_line (server.py ~L770): per-line predicate used to
    filter queued-message chrome out of pane_activity_signature (~L919) and
    out of a block's raw text in parse_blocks' push() (~L1618)."""

    def test_header_line_is_recognized(self):
        self.assertTrue(
            server.is_queued_message_line("Messages to be submitted after next tool call")
        )
        self.assertTrue(
            server.is_queued_message_line("  Messages to be submitted after next tool call  ")
        )

    def test_arrow_prefixed_line_is_recognized(self):
        self.assertTrue(server.is_queued_message_line("↳ 排队消息内容"))
        self.assertTrue(server.is_queued_message_line("  ↳ 缩进过的排队消息"))

    def test_plain_content_is_not_recognized(self):
        for line in [
            "",
            "   ",
            "这是一段普通的助手输出",
            "❯ 这是一条已经提交的普通示例提问",  # a normal submitted "❯ " prompt
            "参考 ↳ 这个",  # contains "↳ " but does not START with it
        ]:
            with self.subTest(line=line):
                self.assertFalse(server.is_queued_message_line(line))


class ExtractQueuedMessagesTest(unittest.TestCase):
    """extract_queued_messages (server.py ~L775): pulls messages the user
    queued while Claude was thinking out of a raw tmux capture, so
    blocks_for_pane can show them as pending role:user/label:排队中 blocks
    before Claude consumes them. Zero coverage before
    this pass. All constructed-input assertions below were verified by
    actually running the function (not just reasoned about) before being
    committed here.
    """

    # --- baseline: the documented, working shape ------------------------------

    def test_well_formed_multi_message_queue_is_extracted(self):
        text = (
            "Messages to be submitted after next tool call\n"
            "  ↳ 第一条\n"
            "  ↳ 第二条\n"
            "  ↳ 第三条\n"
        )
        self.assertEqual(server.extract_queued_messages(text), ["第一条", "第二条", "第三条"])

    def test_no_header_present_yields_no_messages(self):
        text = "● 普通的助手输出\n  ↳ 这行长得像排队消息但前面没有 header\n"
        self.assertEqual(server.extract_queued_messages(text), [])

    def test_blank_line_before_first_message_is_tolerated(self):
        # Task edge case: header immediately followed by a blank line, THEN the
        # first "↳ " line. Verified: blank is a no-op (falsy), in_queue survives.
        text = "Messages to be submitted after next tool call\n\n  ↳ 消息A\n"
        self.assertEqual(server.extract_queued_messages(text), ["消息A"])

    # --- task edge case: message content itself starts with "↳ " -------------

    def test_message_content_starting_with_arrow_is_preserved_verbatim(self):
        # If the user's own queued text happens to start with "↳ " (e.g. they
        # typed "↳ 参考这个"), only the ONE outer prefix the terminal added is
        # stripped (2 chars); the user's literal "↳ " is not further mangled.
        text = (
            "Messages to be submitted after next tool call\n"
            "  ↳ ↳ 参考这个和上次一样\n"
            "  ↳ 第二条排队消息\n"
        )
        self.assertEqual(
            server.extract_queued_messages(text),
            ["↳ 参考这个和上次一样", "第二条排队消息"],
        )

    # --- REAL BUG, FIXED THIS PASS: a divider between the header and the -----
    # first "↳ " line used to discard the ENTIRE queue, not just one line.
    # Before the fix: any non-blank, non-"↳ " line flipped in_queue False
    # permanently, so `extract_queued_messages` returned [] for this input.
    # Plausible in practice: this exact CLI wraps other footer panels (e.g.
    # "Press up to edit queued messages") in a
    # horizontal-rule border, so a border sitting between the header and the
    # list is a realistic layout, not a contrived one.

    def test_divider_between_header_and_first_message_no_longer_drops_queue(self):
        text = (
            "Messages to be submitted after next tool call\n"
            "──────────────────────────────\n"
            "  ↳ 消息A\n"
        )
        self.assertEqual(server.extract_queued_messages(text), ["消息A"])

    # --- REAL BUG, FIXED THIS PASS: a long queued message that terminal ------
    # soft-wraps across physical lines (no repeated "↳ " on the continuation,
    # exactly like this same CLI is observed doing for ordinary "❯ " prompts --
    # see the real capture in ExtractQueuedMessagesSingleMessageRealCaptureTest
    # below, where a single prompt wraps across 6 physical lines with only a
    # 2-space indent marking the continuation) used to BOTH truncate the
    # in-progress message AND permanently exit queue-scanning, silently
    # dropping every subsequent, correctly "↳ "-prefixed message too.

    def test_soft_wrapped_long_message_merges_without_dropping_later_messages(self):
        text = (
            "Messages to be submitted after next tool call\n"
            "  ↳ 这是一条很长很长的排队消息第一部分继续\n"
            "  写完的续行没有前缀符号\n"
            "  ↳ 第二条真实排队消息不应该被吞掉\n"
        )
        self.assertEqual(
            server.extract_queued_messages(text),
            ["这是一条很长很长的排队消息第一部分继续写完的续行没有前缀符号", "第二条真实排队消息不应该被吞掉"],
        )

    def test_soft_wrap_merge_also_works_for_english_content(self):
        text = (
            "Messages to be submitted after next tool call\n"
            "  ↳ please refactor the payment module to\n"
            "  handle partial refunds correctly\n"
            "  ↳ also check the currency rounding\n"
        )
        self.assertEqual(
            server.extract_queued_messages(text),
            [
                "please refactor the payment module to handle partial refunds correctly",
                "also check the currency rounding",
            ],
        )

    # --- guard against over-fixing: a divider AFTER at least one message -----
    # has already been captured must still end the queue, not get treated as
    # more tolerated whitespace. Otherwise trailing page chrome after the
    # list's closing border (e.g. the "auto mode on" footer, which is itself
    # indented and would otherwise pass the soft-wrap-continuation check) gets
    # silently glued onto the last real queued message.

    def test_trailing_divider_then_footer_chrome_does_not_corrupt_last_message(self):
        text = (
            "Messages to be submitted after next tool call\n"
            "  ↳ 第一条\n"
            "  ↳ 第二条\n"
            "──────────────────────────────\n"
            "  ⏵⏵ auto mode on (shift+tab to cycle)\n"
        )
        self.assertEqual(server.extract_queued_messages(text), ["第一条", "第二条"])


class ExtractQueuedMessagesSingleMessageRealCaptureTest(unittest.TestCase):
    """Single queued message, fixed with footer-corroborated extraction.

    When exactly ONE message is queued while Claude is thinking, the CLI does
    NOT render the documented
    "Messages to be submitted after next tool call" header at all, and does
    NOT prefix the message with "↳ ". Instead the queued message appears as
    an indented "❯ <text>" line directly below the spinner ("· Swirling...
    (Ns · thinking)"), and a distinct footer "Press up to edit queued
    messages" appears at the bottom of the screen (stable across captures, not
    a single-frame flicker).

    The structure below reproduces that layout with neutral placeholder text;
    before the footer-corroborated fix, extract_queued_messages() returned an
    empty result for it.

    The fix deliberately does NOT make every indented "❯ " line a queued
    message. It only extracts that shape when the queue-edit footer is present
    and the candidate sits after a volatile/thinking line newer than the latest
    column-0 submitted prompt.
    """

    REAL_SHAPE_SINGLE_QUEUED_MESSAGE = (
        "✻ Done for 1m 32s\n"
        "\n"
        "❯ 当前这一轮已提交的正常用户提示\n"
        "  这一行是它的软换行续行,没有任何前缀\n"
        "\n"
        "· Thinking… (1m 1s · thinking)\n"
        "\n"
        "  ❯ 用户思考期间又排队的第二条消息\n"
        "\n"
        "──────────────────────────────────────────────\n"
        "❯ Press up to edit queued messages\n"
        "──────────────────────────────────────────────\n"
    )

    def test_single_queued_message_without_header_should_still_be_extracted(self):
        msgs = server.extract_queued_messages(self.REAL_SHAPE_SINGLE_QUEUED_MESSAGE)
        self.assertIn("用户思考期间又排队的第二条消息", msgs)

    def test_single_queued_message_line_is_not_recognized_by_is_queued_message_line(self):
        # Documents WHY extract_queued_messages misses it: the line itself
        # doesn't match the per-line predicate either (no header, no "↳ ").
        self.assertFalse(
            server.is_queued_message_line("  ❯ 用户思考期间又排队的第二条消息")
        )

    def test_long_wrapped_prompt_and_rating_prompt_keep_the_same_shape(self):
        # A longer soft-wrapped submitted prompt, a session-rating prompt and a
        # trailing status footer must not change what is extracted.
        wrapped_capture = (
            "✻ Crunched for 1m 32s\n"
            "\n"
            "❯ 这是一段比较长的示例提问，用来模拟用户一次\n"
            "  输入了很多内容，终端把它软换行成好几行，其中\n"
            "  也会出现英文词，比如 alpha、beta、gamma\n"
            "  这样的词，还有数字 1.2 之类的内容，这些续行\n"
            "  都没有任何前缀，它们属于同一条已提交的提示，\n"
            "  不应该被当成排队消息\n"
            "\n"
            "· Swirling… (1m 1s · thinking)\n"
            "\n"
            "  ❯ 这是思考期间排队的另一条示例消息\n"
            "\n"
            "● How is Claude doing this session? (optional)\n"
            "  1: Bad    2: Fine   3: Good   0: Dismiss\n"
            "\n"
            "──────────────────────────────────────────────\n"
            "❯ Press up to edit queued messages\n"
            "──────────────────────────────────────────────\n"
            "  ⏵⏵ auto mode on (shift+tab to cycle) · esc…\n"
            "                                        focus\n"
        )
        self.assertEqual(
            server.extract_queued_messages(wrapped_capture),
            ["这是思考期间排队的另一条示例消息"],
        )
        self.assertEqual(
            server.extract_queued_messages(self.REAL_SHAPE_SINGLE_QUEUED_MESSAGE),
            ["用户思考期间又排队的第二条消息"],
        )

    def test_indented_prompt_without_queue_footer_is_not_extracted(self):
        text = (
            "· Thinking… (1m 1s · thinking)\n"
            "\n"
            "  ❯ 这可能只是正文里缩进展示的一行\n"
        )
        self.assertEqual(server.extract_queued_messages(text), [])

    def test_column_zero_submitted_prompt_before_footer_is_not_extracted(self):
        text = (
            "· Thinking… (1m 1s · thinking)\n"
            "\n"
            "❯ 已经提交的普通用户提示\n"
            "\n"
            "──────────────────────────────────────────────\n"
            "❯ Press up to edit queued messages\n"
            "──────────────────────────────────────────────\n"
        )
        self.assertEqual(server.extract_queued_messages(text), [])


class ExtractQueuedMessagesParseBlocksInteractionTest(unittest.TestCase):
    """Informational, cross-function: because extract_queued_messages misses
    the single-message real format (see above), the ONLY thing that can make
    that queued message visible at all is the generic "❯ " User-prompt marker
    in parse_blocks / merge_live_screen_tail picking it up incidentally. This
    class locks in how fragile that incidental path is -- not a claim about
    parse_blocks correctness in general (out of this pass's scope), just
    documentation of the actual, verified behavior of the interaction.
    """

    def test_queued_line_survives_only_because_trailing_content_follows_it(self):
        # Minimal structural repro (independent of spinner/status-line
        # regexes owned by a different pass, so this does not couple to
        # concurrent edits elsewhere in server.py): a queued "❯ " line with
        # something after it on screen gets kept as a (mislabeled, plain
        # "User prompt") block.
        with_trailing = (
            "❯ 已提交的当前这一轮提示，正在处理中\n"
            "\n"
            "  ❯ 用户思考期间又排队的第二条消息\n"
            "\n"
            "❯ Press up to edit queued messages\n"
        )
        blocks = server.parse_blocks(with_trailing, pane_kind="Claude")
        texts = [b["text"] for b in blocks]
        self.assertIn("用户思考期间又排队的第二条消息", texts)
        # It has no distinguishing pending/排队中 marker -- it looks exactly
        # like an already-submitted, already-consumed prompt.
        self.assertTrue(all(b["label"] == "User prompt" for b in blocks))

    # Pinned to parse_blocks' CURRENT "pop last user-role block as an
    # unsubmitted draft" rule (~L1686), which this pass does not own or touch.
    # If a future pass changes that pop logic and this starts passing, that's
    # good news (XPASS) -- under `pytest` it stays green either way, but a
    # direct `python3 test_transcript.py` run treats an unexpected pass as a
    # failure, so just delete this decorator+test at that point.
    @unittest.expectedFailure
    def test_queued_line_disappears_entirely_when_it_is_the_last_thing_on_screen(self):
        # Same content, minus the trailing footer line. parse_blocks' own
        # "last user-role block = an unsubmitted draft, pop it" rule
        # (server.py ~L1686) removes the queued message outright the moment
        # it happens to be the last visible content -- which is a common
        # poll timing, not a rare one. Ideally a queued-but-not-yet-consumed
        # message should never be fully invisible; that's not true today.
        without_trailing = (
            "❯ 已提交的当前这一轮提示，正在处理中\n"
            "\n"
            "  ❯ 用户思考期间又排队的第二条消息\n"
        )
        blocks = server.parse_blocks(without_trailing, pane_kind="Claude")
        texts = [b["text"] for b in blocks]
        self.assertIn("用户思考期间又排队的第二条消息", texts)


class BoundedTranscriptCacheTest(unittest.TestCase):
    def test_lru_enforces_entry_and_total_byte_limits(self):
        cache = server._BoundedLRUCache(max_entries=2, max_bytes=6, max_item_bytes=4)
        self.assertTrue(cache.put("a", "A", 3))
        self.assertTrue(cache.put("b", "B", 3))
        self.assertEqual(cache.get("a"), "A", "read should promote a to most-recent")
        self.assertTrue(cache.put("c", "C", 3))
        self.assertIsNone(cache.get("b"), "least-recent b should be evicted")
        self.assertEqual(cache.get("a"), "A")
        self.assertEqual(cache.get("c"), "C")
        self.assertEqual(len(cache), 2)
        self.assertEqual(cache.total_bytes, 6)

    def test_oversized_history_is_served_but_not_retained(self):
        old_cache = server._history_blocks_cache
        server._history_blocks_cache = server._BoundedLRUCache(
            max_entries=4,
            max_bytes=1024,
            max_item_bytes=1,
        )
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write(json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": "still returned"},
            }) + "\n")
            path = fh.name
        try:
            page = server.history_before(path, cursor=None, count=10)
            self.assertEqual(page["blocks"][0]["text"], "still returned")
            self.assertEqual(len(server._history_blocks_cache), 0)
        finally:
            server._history_blocks_cache = old_cache
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
