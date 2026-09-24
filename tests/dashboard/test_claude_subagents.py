#!/usr/bin/env python3
"""子智能体实时状态：只读 Claude 自己的子智能体日志，不看屏幕。

Run: python3 -m pytest test_claude_subagents.py -q
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import claude_subagents
import server

T0 = datetime(2026, 9, 23, 10, 40, tzinfo=timezone.utc).timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def assistant(ts, *content, stop=None):
    return {"type": "assistant", "timestamp": iso(ts), "message": {"role": "assistant", "content": list(content), "stop_reason": stop}}


def tool_use(name, **inp):
    return {"type": "tool_use", "id": f"toolu_{name}", "name": name, "input": inp}


class SessionFixture:
    """A parent transcript plus its ``<session>/subagents`` directory."""

    def __init__(self, root: Path):
        self.transcript = root / "sess.jsonl"
        self.transcript.write_text("", encoding="utf-8")
        self.subagents = root / "sess" / "subagents"
        self.subagents.mkdir(parents=True)

    def add_agent(self, agent_id, tool_use_id, *entries, description="Normalize skills"):
        meta = {"agentType": "fast-worker", "description": description, "toolUseId": tool_use_id, "requestShape": "background"}
        (self.subagents / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
        self.append(agent_id, *entries)

    def append(self, agent_id, *entries):
        with open(self.subagents / f"agent-{agent_id}.jsonl", "a", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


class LoadSubagentsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = SessionFixture(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, now, refs=None, notes=None):
        refs = {"toolu_parent"} if refs is None else refs
        return claude_subagents.load_subagents(self.session.transcript, refs, notes, now)

    def test_running_agent_reports_latest_tool_and_keeps_it_while_thinking(self):
        self.session.add_agent("a1", "toolu_parent", assistant(T0, tool_use("Read", file_path="/x/demo-skill/SKILL.md")))
        [state] = self.load(T0 + 5)
        self.assertEqual((state.status, state.activity, state.tool_calls), ("running", "读取 SKILL.md", 1))

        # 增量读取：追加的新行必须被看到，旧行不能重复计数。
        self.session.append("a1", assistant(T0 + 10, {"type": "thinking", "thinking": "..."}))
        [state] = self.load(T0 + 12)
        self.assertEqual(state.activity, "思考中（上一步：读取 SKILL.md）")
        self.assertEqual(state.tool_calls, 1)

    def test_end_turn_means_completed(self):
        self.session.add_agent(
            "a1", "toolu_parent",
            assistant(T0, tool_use("Bash", command="pytest -q", description="Run tests")),
            assistant(T0 + 30, {"type": "text", "text": "## 结论\n完成"}, stop="end_turn"),
        )
        [state] = self.load(T0 + 40)
        self.assertEqual(state.status, "completed")
        self.assertEqual(claude_subagents.live_batch([state]), [])

    def test_parent_notification_supplies_terminal_state_without_end_turn(self):
        self.session.add_agent("a1", "toolu_parent", assistant(T0, tool_use("Grep", pattern="TODO")))
        [state] = self.load(T0 + 5, notes={"a1": ("killed", T0 + 3)})
        self.assertEqual(state.status, "killed")

    def test_silent_unfinished_agent_is_stale_not_running(self):
        self.session.add_agent("a1", "toolu_parent", assistant(T0, tool_use("Read", file_path="/x/a.md")))
        [state] = self.load(T0 + claude_subagents.STALE_SECONDS + 1)
        self.assertEqual(state.status, "stale")

    def test_old_agent_not_in_timeline_is_skipped(self):
        self.session.add_agent("old", "toolu_elsewhere", assistant(T0, tool_use("Read", file_path="/x/a.md")))
        log = self.session.subagents / "agent-old.jsonl"
        os.utime(log, (T0, T0))
        self.assertEqual(self.load(T0 + claude_subagents.RECENT_SECONDS + 60), [])
        self.assertEqual(len(self.load(T0 + claude_subagents.RECENT_SECONDS + 60, refs={"toolu_elsewhere"})), 1)


class TimelineIntegrationTest(unittest.TestCase):
    """父会话时间线：子智能体条目带状态，运行中时末尾有实时面板。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = SessionFixture(Path(self.tmp.name))
        entries = [
            assistant(T0, {"type": "tool_use", "id": "toolu_A", "name": "Agent",
                           "input": {"description": "Normalize skills", "subagent_type": "fast-worker", "prompt": "..."}}),
            {"type": "user", "timestamp": iso(T0 + 1), "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_A", "content": [
                    {"type": "text", "text": "Async agent launched successfully. (internal)\nagentId: a1"}]}]}},
        ]
        with open(self.session.transcript, "w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_agent_call_becomes_subagent_entry_and_receipt_is_dropped(self):
        blocks = server.parse_transcript_tail(str(self.session.transcript))
        self.assertEqual([(b["label"], b["text"]) for b in blocks], [("子智能体", "fast-worker · Normalize skills")])
        self.assertEqual(blocks[0]["agent_ref"], "toolu_A")
        # 分页历史与实时尾部必须按同一规则生成，否则翻页游标会错位。
        self.assertEqual(server._parse_transcript_all_blocks(str(self.session.transcript)), blocks)

    def test_running_subagent_gets_status_and_live_panel(self):
        self.session.add_agent("a1", "toolu_A", assistant(T0 + 2, tool_use("Read", file_path="/x/notes.md")))
        blocks = server.attach_subagent_status(server.parse_transcript_tail(str(self.session.transcript)),
                                               str(self.session.transcript), now=T0 + 5)
        self.assertEqual(blocks[0]["agent"]["status"], "running")
        panel = blocks[-1]
        self.assertEqual(panel["role"], "agents")
        self.assertEqual(panel["agents"][0]["activity"], "读取 notes.md")
        self.assertIn("1 个运行中", panel["text"])
        history = server.attach_subagent_status(blocks[:1], str(self.session.transcript), now=T0 + 5, live_panel=False)
        self.assertNotIn("agents", [b["role"] for b in history])

    def test_completion_notification_is_readable_and_panel_disappears(self):
        self.session.add_agent("a1", "toolu_A",
                               assistant(T0 + 2, {"type": "text", "text": "done"}, stop="end_turn"))
        note = ("<task-notification>\n<task-id>a1</task-id>\n<tool-use-id>toolu_A</tool-use-id>\n"
                "<status>completed</status>\n<summary>Agent \"Normalize skills\" finished</summary>\n"
                "<result>## 结论\n改好了</result>\n</task-notification>")
        with open(self.session.transcript, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "timestamp": iso(T0 + 3),
                                 "message": {"role": "user", "content": note}}, ensure_ascii=False) + "\n")
        blocks = server.attach_subagent_status(server.parse_transcript_tail(str(self.session.transcript)),
                                               str(self.session.transcript), now=T0 + 5)
        self.assertEqual(blocks[0]["agent"]["status"], "completed")
        self.assertEqual(blocks[-1]["label"], "子智能体结果")
        self.assertTrue(blocks[-1]["text"].startswith('**Agent "Normalize skills" finished**（已完成）'))
        self.assertIn("改好了", blocks[-1]["text"])
        self.assertNotIn("agents", [b["role"] for b in blocks])



def user_text(ts, text):
    return {"type": "user", "timestamp": iso(ts), "message": {"role": "user", "content": text}}


def write_transcript(path, entries):
    with open(path, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


class InjectedUserContentTest(unittest.TestCase):
    """Claude 注入到 user 轮次里的机器文本按种类显示，不再原样贴 XML。"""

    def parse(self, *texts):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            write_transcript(path, [user_text(T0 + i, t) for i, t in enumerate(texts)])
            tail = server.parse_transcript_tail(str(path))
            self.assertEqual(server._parse_transcript_all_blocks(str(path)), tail)
            return [(b["role"], b["label"], b["text"]) for b in tail]

    def test_slash_command_and_its_output(self):
        blocks = self.parse(
            "<local-command-caveat>Caveat: The messages below were generated by the user</local-command-caveat>",
            "<command-name>/model</command-name>\n<command-message>model</command-message>\n<command-args>opus</command-args>",
            "<local-command-stdout>Set model to \x1b[1mOpus 5.5\x1b[22m</local-command-stdout>",
            "<local-command-stdout></local-command-stdout>",
        )
        self.assertEqual(blocks, [("user", "斜杠命令", "/model opus"), ("system", "命令输出", "Set model to Opus 5.5")])

    def test_monitor_event_and_skill_load(self):
        blocks = self.parse(
            "<task-notification>\n<task-id>b25</task-id>\n<summary>Monitor event: \"deploy\"</summary>\n"
            "<event>[09:38] build failed</event>\n</task-notification>",
            "Base directory for this skill: /home/u/.claude/skills/demo-skill\n\n# 示例技能\n正文很长",
        )
        self.assertEqual(blocks, [
            ("system", "监控事件", '**Monitor event: "deploy"**\n\n[09:38] build failed'),
            ("system", "加载技能", "已加载技能 demo-skill"),
        ])


class WorkflowProgressTest(unittest.TestCase):
    RUN = "wf_000000-0de"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = SessionFixture(Path(self.tmp.name))
        script = "export const meta = {\n  name: 'audit-skills',\n  description: '逐个检查技能',\n}\n"
        receipt = (f"Workflow launched in background. Task ID: wdemo1 | Summary: 逐个检查技能 | "
                   f"Transcript dir: {self.session.subagents}/workflows/{self.RUN} | Script file: /x.js")
        self.entries = [
            assistant(T0, {"type": "tool_use", "id": "toolu_W", "name": "Workflow", "input": {"script": script}}),
            {"type": "user", "timestamp": iso(T0 + 1), "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_W", "content": receipt}]}},
        ]
        write_transcript(self.session.transcript, self.entries)
        run_dir = self.session.subagents / "workflows" / self.RUN
        run_dir.mkdir(parents=True)
        for agent_id, entries in {
            "w1": [assistant(T0 + 2, {"type": "text", "text": "done"}, stop="end_turn")],
            "w2": [assistant(T0 + 3, tool_use("Read", file_path="/x/SKILL.md"))],
        }.items():
            (run_dir / f"agent-{agent_id}.meta.json").write_text('{"agentType":"workflow-subagent"}', encoding="utf-8")
            with open(run_dir / f"agent-{agent_id}.jsonl", "w", encoding="utf-8") as fh:
                for entry in entries:
                    fh.write(json.dumps(entry) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def blocks(self):
        path = str(self.session.transcript)
        return server.attach_subagent_status(server.parse_transcript_tail(path), path, now=T0 + 5)

    def test_running_workflow_shows_progress_and_panel(self):
        blocks = self.blocks()
        self.assertEqual((blocks[0]["label"], blocks[0]["text"]), ("工作流", "audit-skills · 逐个检查技能"))
        self.assertEqual(blocks[0]["agent"]["status"], "running")
        self.assertEqual(blocks[0]["agent"]["activity"], "1/2 个子任务完成 · 最近：读取 SKILL.md")
        self.assertEqual(blocks[-1]["role"], "agents")
        self.assertNotIn("Workflow launched", "\n".join(b["text"] for b in blocks))

    def test_workflow_ends_only_on_parent_notification(self):
        note = ("<task-notification>\n<task-id>wdemo1</task-id>\n<status>completed</status>\n"
                "<summary>Workflow \"audit-skills\" completed</summary>\n</task-notification>")
        write_transcript(self.session.transcript, self.entries + [user_text(T0 + 4, note)])
        blocks = self.blocks()
        self.assertEqual(blocks[0]["agent"]["status"], "completed")
        self.assertNotIn("agents", [b["role"] for b in blocks])



class SubagentPanelRegressionTest(unittest.TestCase):
    """子智能体在跑时，卡片不能显示"完成"，同一段内容也不能重复出现。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = SessionFixture(Path(self.tmp.name))
        write_transcript(self.session.transcript, [
            user_text(T0, "请并行处理这四件事，做完之后逐一核对再统一向我汇报结果"),
            assistant(T0 + 1, {"type": "tool_use", "id": "toolu_A", "name": "Agent",
                               "input": {"description": "Normalize skills", "subagent_type": "fast-worker"}}),
            assistant(T0 + 2, {"type": "text", "text": "已派出子智能体，四路都完成后我会逐一核对再汇报。" * 3}, stop="end_turn"),
            {"type": "system", "subtype": "turn_duration", "timestamp": iso(T0 + 3)},
        ])
        self.session.add_agent("a1", "toolu_A", assistant(T0 + 4, tool_use("Read", file_path="/x/a.md")))

    def tearDown(self):
        self.tmp.cleanup()

    def test_pane_stays_running_while_subagents_work(self):
        path = str(self.session.transcript)
        claude_subagents._NOTES.clear()
        server._SUBAGENT_COUNT_CACHE.clear()
        now = T0 + 10
        original_time = server.time.time
        server.time.time = lambda: now
        try:
            self.assertEqual(server.transcript_activity_status(path), "idle")
            self.assertEqual(server.running_subagent_count(path), 1)
            screen = "● 已派出子智能体\n\n" + "\n".join(["─" * 40, "❯", "─" * 40, "  ⏵⏵ bypass permissions on"])
            self.assertEqual(server.infer_pane_status("%x", screen, "claude", "Claude", True, path), "running")
        finally:
            server.time.time = original_time

    def test_background_waiting_line_is_not_pushed_out_by_the_panel(self):
        panel = ["  ● main"] + [f"  ◯ fast-worker  Reading f{i}.md" for i in range(10)]
        screen = "\n".join(["● 回复", "✻ Waiting for 10 background agents to finish · 6 messages hidden", "",
                            "─" * 40, "❯", "─" * 40, "  ⏵⏵ bypass permissions on · ← for agents"] + panel)
        self.assertEqual(server.infer_status(screen, "claude"), "running")

    def test_reply_already_in_transcript_is_not_repeated_from_screen(self):
        reply = "已派出子智能体，四路都完成后我会逐一核对再汇报。" * 3
        blocks = server.parse_transcript_tail(str(self.session.transcript))
        # 屏幕上同一段回复夹了右侧面板的 ✕，前面还有聚焦模式的折叠摘要行。
        screen = "\n".join([
            "❯ 请并行处理这四件事，做完之后逐一核对再统一向我汇报结果",
            "  Ran 1 agent, ran 3 shell commands",
            "● " + reply[:20] + " ✕ " + reply[20:],
            "",
            "✻ Waiting for 10 background agents to finish",
            "─" * 40, "❯", "─" * 40, "  ⏵⏵ bypass permissions on · ← for agents",
            "  ● main",
        ] + [f"  ◯ fast-worker  Reading f{i}.md" for i in range(10)])
        merged = server.merge_live_screen_tail(blocks, screen)
        self.assertEqual([b for b in merged if b.get("pending")], [])


class ReviewFollowupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = SessionFixture(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_foreground_agent_result_ends_it_without_end_turn(self):
        write_transcript(self.session.transcript, [
            assistant(T0, {"type": "tool_use", "id": "toolu_F", "name": "Agent",
                           "input": {"description": "Quick scan", "subagent_type": "scout"}}),
            {"type": "user", "timestamp": iso(T0 + 5), "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_F", "content": "[Request interrupted by user]"}]}},
        ])
        self.session.add_agent("f1", "toolu_F", assistant(T0 + 1, tool_use("Grep", pattern="x")))
        path = str(self.session.transcript)
        blocks = server.attach_subagent_status(server.parse_transcript_tail(path), path, now=T0 + 6)
        self.assertEqual(blocks[0]["agent"]["status"], "completed")
        self.assertEqual(blocks[1]["label"], "子智能体结果")
        self.assertNotIn("agents", [b["role"] for b in blocks])

    def test_history_page_sees_notifications_from_newer_pages(self):
        note = ("<task-notification>\n<task-id>a1</task-id>\n<status>killed</status>\n"
                "<summary>Agent \"Normalize skills\" stopped</summary>\n</task-notification>")
        write_transcript(self.session.transcript, [
            assistant(T0, {"type": "tool_use", "id": "toolu_A", "name": "Agent",
                           "input": {"description": "Normalize skills", "subagent_type": "fast-worker"}}),
        ] + [assistant(T0 + 1 + i, {"type": "text", "text": f"step {i}"}) for i in range(5)]
          + [user_text(T0 + 10, note)])
        self.session.add_agent("a1", "toolu_A", assistant(T0 + 1, tool_use("Read", file_path="/x/a.md")))
        page = server.history_before(str(self.session.transcript), cursor=1, count=1)
        self.assertEqual(page["blocks"][0]["agent"]["status"], "killed")

    def test_synthetic_no_response_record_is_not_a_reply(self):
        write_transcript(self.session.transcript, [
            user_text(T0, "<command-name>/model</command-name>\n<command-args></command-args>"),
            assistant(T0 + 1, {"type": "text", "text": "No response requested."}),
        ])
        blocks = server.parse_transcript_tail(str(self.session.transcript))
        self.assertEqual([(b["role"], b["text"]) for b in blocks], [("user", "/model")])

    def test_concurrent_polls_do_not_double_count(self):
        import threading
        self.session.add_agent("c1", "toolu_C", *[assistant(T0 + i, tool_use("Read", file_path=f"/x/{i}.md")) for i in range(200)])
        log_path = self.session.subagents / "agent-c1.jsonl"
        threads = [threading.Thread(target=claude_subagents._log_for, args=(log_path,)) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        log = claude_subagents._log_for(log_path)
        self.assertEqual((log.tool_calls, log.offset), (200, log_path.stat().st_size))


if __name__ == "__main__":
    unittest.main(verbosity=2)
