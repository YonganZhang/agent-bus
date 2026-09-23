#!/usr/bin/env python3
"""状态推断的结构化回归。

用真实窗口抓屏(脱敏后)做 characterization：解析层一旦改动，这 53 个 case 的判定
不能漂移。另外单独钉住几个"按正文词猜状态"会翻车的最小形态——它们是把结构判定
换成文案匹配时最先坏掉的地方。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pane_status_cases.json"
RULE = "─" * 96


def claude_screen(*body: str, chrome: str = "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents") -> str:
    """一屏典型的 Claude：对话区 + ─❯─ 输入框 + 底部 chrome。"""
    return "\n".join([*body, RULE, "❯", RULE, chrome])


def codex_screen(*body: str) -> str:
    return "\n".join([*body, "› Ask Codex to do anything", "  gpt-5.6-luna medium · ~/<proj> · Main [default]"])


class PaneStatusFixtureTest(unittest.TestCase):
    def test_real_screens_keep_their_status(self) -> None:
        cases = json.loads(FIXTURES.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(cases), 40, "fixture 太少就守不住回归")
        drift = [
            (c["window"], c["expected_status"], server.infer_status(c["capture_tail"], c["command"]))
            for c in cases
            if server.infer_status(c["capture_tail"], c["command"]) != c["expected_status"]
        ]
        self.assertEqual(drift, [], f"状态判定漂移: {drift}")

    def test_fixture_agrees_with_claude_official_status(self) -> None:
        """有官方状态可对照的窗口必须对得上——官方是唯一不会随 UI 改版漂移的裁判。"""
        cases = json.loads(FIXTURES.read_text(encoding="utf-8"))
        equiv = {"running": "busy", "idle": "idle", "shell": "idle", "waiting": "waiting"}
        bad = [
            (c["window"], c["expected_status"], c["official_status"])
            for c in cases
            if c.get("official_status")
            and equiv.get(c["expected_status"]) != c["official_status"]
        ]
        self.assertEqual(bad, [], f"与 claude agents --json 不一致: {bad}")


class StructureBeatsWordMatchTest(unittest.TestCase):
    """屏幕上有输入框 = 会话在等你打字。正文里出现什么词都不该改变这个事实。"""

    def test_background_command_failure_notice_is_not_session_trouble(self) -> None:
        # Claude 会把后台命令的结果播报到对话流里。裸扫 "failed" 会把整个会话
        # 判成 needs attention。
        screen = claude_screen(
            '✻ Cooked for 13m · done · 10 messages hidden (/focus to show)',
            '● Background command "<name>" was stopped',
            '● Background command "<name>" failed with exit code 144',
        )
        self.assertEqual(server.infer_status(screen, "bash"), "idle")

    def test_error_words_quoted_in_the_conversation_stay_idle(self) -> None:
        for body in ("error: something went wrong", "Traceback (most recent call last):",
                     "the build failed twice", "permission denied on /<path>"):
            with self.subTest(body=body):
                self.assertEqual(server.infer_status(claude_screen("<text>", body), "bash"), "idle")

    def test_no_input_box_means_the_screen_really_is_broken(self) -> None:
        """没有输入框 = 不在交互态,这时错误词才真的说明出事了。"""
        screen = "\n".join(["<text>", "Traceback (most recent call last):", '  File "x.py", line 1', "RuntimeError: boom"])
        self.assertEqual(server.infer_status(screen, "bash"), "needs attention")

    def test_busy_chrome_below_the_input_box_still_counts(self) -> None:
        """`esc to interrupt` 长在输入框下方的 chrome 里,切区时不能把它切丢。"""
        screen = claude_screen(
            "<text>", "✽ Hatching… (3m 58s · ↓ 12.9k tokens)",
            chrome="  ⏵⏵ auto mode on (shift+tab to cycle) · esc to interrupt · ← for agents",
        )
        self.assertEqual(server.infer_status(screen, "bash"), "running")

    def test_codex_working_line_is_busy_and_bare_prompt_is_idle(self) -> None:
        self.assertEqual(server.infer_status(codex_screen("<text>", "• Working (7s • esc to interrupt)"), "bash"), "running")
        self.assertEqual(server.infer_status(codex_screen("<text>", "<text>"), "bash"), "idle")

    def test_codex_failure_notice_in_conversation_stays_idle(self) -> None:
        self.assertEqual(server.infer_status(codex_screen("<text>", "the previous run failed"), "bash"), "idle")

    def test_dead_agent_with_a_stale_prompt_needs_attention(self) -> None:
        self.assertEqual(
            server.infer_status(codex_screen("<text>"), "codex", ai_alive=False),
            "needs attention",
        )

    def test_a_real_picker_still_wins(self) -> None:
        """选择器是真的在等人,不能被"有输入框就 idle"覆盖掉。"""
        screen = claude_screen("Do you want to proceed?", "❯ 1. Yes", "  2. No (esc)")
        self.assertEqual(server.infer_status(screen, "bash"), "waiting")


if __name__ == "__main__":
    unittest.main()


class StampedIdentityTest(unittest.TestCase):
    """hook 钉的会话身份怎么用，以及什么时候**不能**用。"""

    def _pane(self, pane_id, session_id="", transcript=""):
        return server.Pane(
            pane_id=pane_id, target=f"secretary_web:1.0", session="secretary_web",
            window_index=1, pane_index=0, window_name="w", command="claude",
            cwd="/tmp", title="", active=False, kind="Claude", project="",
            preview="", status="idle", pane_pid="1",
            ai_session_id=session_id, ai_transcript=transcript,
        )

    def test_a_stamped_path_that_does_not_exist_yet_falls_back_to_this_panes_own_screen(self) -> None:
        """会话刚起或刚被 resume 到新目录时，transcript 还没落盘。

        这时**不能**退回内容指纹匹配：屏幕上那点内容会把它匹配到别人的 transcript 上，
        于是两个窗口显示同一段对话——比显示不出来更糟。
        """
        pane = self._pane("%99", "66666666-7777-4888-8999-aaaaaaaaaaaa", "/nonexistent/x.jsonl")
        screen = claude_screen("<text>", "✻ Cooked for 1m · done")
        with mock.patch.object(server, "pane_is_alt_screen", return_value=True), \
             mock.patch.object(server, "claude_transcript_for_pane") as guessed:
            _, meta = server.blocks_for_pane_with_meta(pane, screen)
        self.assertEqual(meta["quality"], "pending-transcript")
        self.assertEqual(meta["reason"], "transcript-not-yet-written")
        guessed.assert_not_called()   # 关键：一次都不许去猜

    def test_two_panes_sharing_one_session_id_are_not_merged(self) -> None:
        """同一个会话 id 可以同时属于两个窗口（同一条会话被 --resume 开了两次）。
        只有 hook 给的完整路径（含 cwd）能区分它们。"""
        shared_id = "66666666-7777-4888-8999-aaaaaaaaaaaa"
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / f"{shared_id}.jsonl"
            real.write_text('{"type":"user","message":{"role":"user","content":"hi"}}\n', encoding="utf-8")
            has_file = self._pane("%1", shared_id, str(real))
            no_file = self._pane("%2", shared_id, str(Path(tmp) / "other" / f"{shared_id}.jsonl"))
            with mock.patch.object(server, "pane_is_alt_screen", return_value=True):
                self.assertEqual(server.stamped_transcript_for_pane(has_file), str(real))
                self.assertEqual(server.stamped_transcript_for_pane(no_file), "")


class ActivitySignatureTest(unittest.TestCase):
    def test_signature_does_not_depend_on_how_much_scrollback_was_captured(self) -> None:
        """卡片列表抓 40 行、卡片详情抓 5000 行，同一个 pane 的活跃度指纹必须一样。

        不一样的话"内容没变"永远不成立，一个 idle 的窗口会被永久锁死显示 running。
        """
        # 真实的浅抓屏是 capture(history=40)，深抓屏是 capture(history=5000)：
        # 浅的那份是深的那份的后缀，且至少 40 行。
        body = [f"历史第 {i} 行" for i in range(200)]
        tail = ["✻ Cooked for 1m · done", RULE, "❯", RULE, "  ⏵⏵ auto mode on"]
        deep = "\n".join(body + tail)
        shallow = "\n".join((body + tail)[-40:])
        self.assertEqual(
            server.pane_activity_signature(deep),
            server.pane_activity_signature(shallow),
        )


class WaitingMustBeStructuralTest(unittest.TestCase):
    """"在等你操作"必须来自紧贴输入框的选择器，不能来自 AI 写在回答里的字眼。

    误判的代价是用户被叫过去，结果发现没什么要处理的——这类噪音几次就会让人不再相信状态。
    """

    DONE = "✻ Cooked for 1m · done"

    def test_words_quoted_in_a_finished_answer_do_not_mean_waiting(self) -> None:
        for body in ("do you want me to continue?",
                     "提示用户 press enter to continue 即可",
                     "用 arrow keys to navigate 选择",
                     "Enter to confirm 是那个按键"):
            with self.subTest(body=body):
                self.assertEqual(server.infer_status(claude_screen(body, self.DONE), "bash"), "idle")

    def test_an_option_list_quoted_in_a_finished_answer_is_not_a_picker(self) -> None:
        screen = claude_screen("三个方案:", "❯ 1. 方案甲", "  2. 方案乙", self.DONE)
        self.assertEqual(server.infer_status(screen, "bash"), "idle")

    def test_a_real_picker_still_reads_as_waiting(self) -> None:
        self.assertEqual(
            server.infer_status(claude_screen("<text>", "Do you want to proceed?", "❯ 1. Yes", "  2. No (esc)"), "bash"),
            "waiting")
        self.assertEqual(
            server.infer_status(codex_screen_picker(), "bash"),
            "waiting")


def codex_screen_picker() -> str:
    return "\n".join(["<text>", "› 1. Resume goal", "  2. Leave paused",
                      "  Press enter to confirm or esc to go back"])


class DeadAgentMustStillAlertTest(unittest.TestCase):
    """AI 死了但输入框还留在屏幕上时，不能显示成正常的空闲卡片。

    Codex 是 inline 渲染，进程退出后最后一屏留在 scrollback 里。只看屏幕分不出
    "在等你打字" 和 "已经崩了"——`database is locked` 崩溃正是这套恢复逻辑要处理的场景，
    结果卡片显示 idle，告警一条都不会响。
    """

    DEAD_CODEX = "\n".join([
        "  <text>",
        "› Ask Codex to do anything",
        "  gpt-5.6-luna medium · ~/<proj>",
        "[AI session exited with status 1]",
        "[Shell is ready. Resume with:] codex resume 01900000-0000-7000-8000-000000000002",
        "user@host:~$ codex resume --last",
        "Error: error returned from database: (code: 5) database is locked",
        "user@host:~$ ",
    ])

    def test_a_dead_agent_with_a_leftover_input_box_still_alerts(self) -> None:
        self.assertEqual(server.infer_status(self.DEAD_CODEX, "codex", ai_alive=False), "needs attention")

    def test_the_same_screen_with_a_live_agent_stays_idle(self) -> None:
        live = "\n".join(["<text>", "› Ask Codex to do anything", "  gpt-5.6-luna medium · ~/<proj>"])
        self.assertEqual(server.infer_status(live, "codex", ai_alive=True), "idle")

    def test_liveness_defaults_to_alive_for_existing_callers(self) -> None:
        """老调用方不传这个参数，行为不能变。"""
        live = "\n".join(["<text>", "› Ask Codex to do anything", "  gpt-5.6-luna medium · ~/<proj>"])
        self.assertEqual(server.infer_status(live, "codex"), "idle")


class BackgroundWorkStillCountsAsRunningTest(unittest.TestCase):
    """"还在跑"的信号常长在一行 chrome 的尾巴上，而那种行会被内容过滤整行丢掉。

    结果是后台任务没跑完的窗口显示成空闲，用户以为可以关了。
    """

    def test_shells_still_running_on_a_done_line_reads_as_running(self) -> None:
        screen = claude_screen(
            "<text>",
            "✻ Sautéed for 39s · done 9:17 pm · 2 messages hidden (/focus to show) · 2 shells still running",
            chrome="  ⏵⏵ auto mode on · 2 shells · ← for agents",
        )
        self.assertEqual(server.infer_status(screen, "bash"), "running")

    def test_a_plain_done_line_is_still_idle(self) -> None:
        screen = claude_screen("<text>", "✻ Cooked for 1m · done 9:17 pm · 2 messages hidden (/focus to show)")
        self.assertEqual(server.infer_status(screen, "bash"), "idle")


class MonitorOnlyBusyIsAnsweredTest(unittest.TestCase):
    """Claude 回完话后只剩后台监控挂着时，官方状态仍是 busy；回复其实已完成、在等用户。

    改前卡片一律跟官方 busy 显示"工作中"。后台 shell、子智能体、工作流仍算处理中。
    """

    MONITOR_DONE = claude_screen(
        "<text>",
        "✻ Worked for 41s · done 1:12 am · 1 monitor still running",
        chrome="  ⏵⏵ bypass permissions on · 1 monitor · ← for agents · ↓ to manage",
    )

    def status(self, screen: str, *, recorded: str = "idle", subagents: int = 0) -> str:
        with mock.patch.object(server, "claude_agent_record", return_value={"status": "busy"}), \
             mock.patch.object(server, "transcript_activity_status", return_value=recorded), \
             mock.patch.object(server, "running_subagent_count", return_value=subagents):
            return server.infer_pane_status("%x", screen, "claude", "Claude", True, "/t.jsonl", "1", "s")

    def test_finished_turn_with_only_a_monitor_is_idle_with_a_hint(self) -> None:
        self.assertEqual(self.status(self.MONITOR_DONE), "idle")
        self.assertEqual(server.claude_background_label(self.MONITOR_DONE), "后台监控 1 个")

    def test_shells_subagents_or_an_unfinished_turn_still_run(self) -> None:
        shells = claude_screen("<text>", "✻ Worked for 41s · done 1:12 am · 1 monitor still running · 2 shells still running",
                               chrome="  ⏵⏵ bypass permissions on · 1 monitor · 2 shells · ← for agents")
        self.assertEqual(self.status(shells), "running")
        self.assertEqual(self.status(self.MONITOR_DONE, subagents=1), "running")
        self.assertEqual(self.status(self.MONITOR_DONE, recorded="running"), "running")
        no_monitor = claude_screen("<text>", "✻ Worked for 41s · done 1:12 am")
        self.assertEqual(self.status(no_monitor), "running")  # busy for a reason we cannot see: trust Claude


class TranscriptActivityTest(unittest.TestCase):
    """忙闲以 Claude 自己写的落盘记录为准，屏幕只补记录里没有的东西。

    判据是「最后一条 assistant 之后还有没有 turn_duration」。选它而不是「有没有新记录」，
    是因为 user / attachment / queue-operation 是用户排进队列的消息——AI 未必开始处理，
    按"有新记录就算在跑"会把三个空闲窗口判成忙。
    """

    def _write(self, tmp, *rows):
        path = Path(tmp) / "t.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        return str(path)

    ASSISTANT = {"type": "assistant", "message": {"role": "assistant", "content": []}}
    TURN_END = {"type": "system", "subtype": "turn_duration"}

    def test_turn_duration_after_the_last_assistant_means_the_turn_ended(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, {"type": "user"}, self.ASSISTANT, self.TURN_END,
                               {"type": "cost-state"})
            self.assertEqual(server.transcript_activity_status(path), "idle")

    def test_an_assistant_record_after_the_last_turn_end_means_still_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, self.ASSISTANT, self.TURN_END, {"type": "user"}, self.ASSISTANT)
            self.assertEqual(server.transcript_activity_status(path), "running")

    def test_local_model_command_no_response_record_does_not_reopen_turn(self) -> None:
        """Local `/model` emits a synthetic assistant acknowledgement only."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                self.ASSISTANT,
                self.TURN_END,
                {"type": "user", "message": {"content": "<command-name>/model</command-name>"}},
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "No response requested."}]}},
            )
            self.assertEqual(server.transcript_activity_status(path), "idle")

    def test_a_queued_user_message_is_not_the_agent_working(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, self.ASSISTANT, self.TURN_END,
                               {"type": "last-prompt"}, {"type": "queue-operation"},
                               {"type": "user"}, {"type": "attachment"})
            self.assertEqual(server.transcript_activity_status(path), "idle")

    def test_no_usable_signal_returns_none_so_the_screen_decides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, {"type": "mode"}, {"type": "cost-state"})
            self.assertIsNone(server.transcript_activity_status(path))
        self.assertIsNone(server.transcript_activity_status("/nonexistent/x.jsonl"))

    def test_background_shells_on_screen_can_still_overrule_a_finished_turn(self) -> None:
        """记录里没有"后台 shell 还在跑"这回事，那个信息只在屏幕上。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, self.ASSISTANT, self.TURN_END, {"type": "cost-state"})
            screen = claude_screen(
                "<text>",
                "✻ Sautéed for 39s · done 9:17 pm · 2 messages hidden (/focus to show) · 2 shells still running",
                chrome="  ⏵⏵ auto mode on · 2 shells · ← for agents")
            self.assertEqual(
                server.infer_pane_status("%99", screen, "claude", "Claude", True, path), "running")

    def test_a_finished_turn_with_a_quiet_screen_is_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, self.ASSISTANT, self.TURN_END, {"type": "cost-state"})
            screen = claude_screen("<text>", "✻ Cooked for 1m · done 9:17 pm")
            self.assertEqual(
                server.infer_pane_status("%98", screen, "claude", "Claude", True, path), "idle")
