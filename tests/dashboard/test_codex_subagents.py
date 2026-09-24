#!/usr/bin/env python3
"""Codex 子智能体：只读子智能体自己的 rollout，不看屏幕。

Run: python3 -m pytest test_codex_subagents.py -q
"""

import json
import os
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

import codex_subagents
import server

NOW = time.time()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def item(ts, payload, kind="response_item"):
    return {"timestamp": iso(ts), "type": kind, "payload": payload}


def event(ts, name):
    return item(ts, {"type": name}, kind="event_msg")


def exec_call(ts, cmd, call_id="c1"):
    script = 'text(await tools.exec_command({cmd:"' + cmd + '","max_output_tokens":9000}));'
    return item(ts, {"type": "custom_tool_call", "name": "exec", "call_id": call_id, "input": script})


def write(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


class CodexSessionFixture:
    def __init__(self, root: Path):
        day = time.localtime(NOW)
        self.day_dir = root / "sessions" / f"{day.tm_year:04d}" / f"{day.tm_mon:02d}" / f"{day.tm_mday:02d}"
        self.parent = self.day_dir / "rollout-2026-09-23T10-00-00-parent-thread.jsonl"
        write(self.parent, [item(NOW - 100, {"id": "parent-thread", "thread_source": "user"}, kind="session_meta")])

    def child(self, name, rows, *, forked_rows=()):
        meta = {"id": f"child-{name}", "parent_thread_id": "parent-thread", "agent_path": f"/root/{name}",
                "agent_nickname": "Nova", "thread_source": "subagent",
                "subagent_history_start_ordinal": 1 + len(forked_rows)}
        path = self.day_dir / f"rollout-2026-09-23T10-01-00-child-{name}.jsonl"
        write(path, [item(NOW - 50, meta, kind="session_meta"), *forked_rows, *rows])
        return path


class ChildStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = CodexSessionFixture(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_running_child_ignores_history_copied_from_parent(self):
        # fork 出来的子智能体开头复制了父会话历史（含已完成的回合和工具调用），不能算它自己的。
        forked = [event(NOW - 50, "task_started"), exec_call(NOW - 50, "ls", "old"), event(NOW - 50, "task_complete")]
        path = self.fx.child("example_review", [
            event(NOW - 40, "task_started"),
            exec_call(NOW - 30, "rg -n spawn_agent server.py"),
        ], forked_rows=forked)
        state = codex_subagents.child_state(path, NOW)
        self.assertEqual((state.status, state.tool_calls), ("running", 1))
        self.assertEqual(state.activity, "运行命令：rg -n spawn_agent server.py")
        self.assertEqual((state.tool_use_id, state.description), ("/root/example_review", "example_review（Nova）"))

        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event(NOW - 5, "task_complete")) + "\n")
        self.assertEqual(codex_subagents.child_state(path, NOW).status, "completed")

    def test_aborted_turn_is_stopped(self):
        path = self.fx.child("x", [event(NOW - 40, "task_started"), event(NOW - 30, "turn_aborted")])
        self.assertEqual(codex_subagents.child_state(path, NOW).status, "stopped")

    def test_children_are_found_by_parent_thread(self):
        self.fx.child("a", [event(NOW - 40, "task_started")])
        write(self.fx.day_dir / "rollout-other.jsonl",
              [item(NOW, {"id": "z", "parent_thread_id": "someone-else", "thread_source": "subagent"}, kind="session_meta")])
        codex_subagents._LISTINGS.clear()
        kids = codex_subagents.children_of(self.fx.parent, NOW)
        self.assertEqual([k.tool_use_id for k in kids], ["/root/a"])

    def test_tool_descriptions(self):
        d = codex_subagents.describe_codex_tool
        self.assertEqual(d("apply_patch", "*** Begin Patch\n*** Update File: src/a.py\n*** Add File: b.md\n"), "修改文件 a.py 等 2 个文件")
        self.assertEqual(d("send_message", '{"target":"reviewer","message":"gAAAA"}'), "给 reviewer 发消息")
        self.assertEqual(d("sleep", '{"duration_ms":40000}'), "等待 40 秒")


class ParentTimelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = CodexSessionFixture(Path(self.tmp.name))
        write(self.fx.parent, [
            item(NOW - 100, {"id": "parent-thread", "thread_source": "user"}, kind="session_meta"),
            item(NOW - 60, {"type": "function_call", "name": "spawn_agent", "call_id": "s1",
                            "arguments": json.dumps({"task_name": "review", "message": "gAAAA-encrypted"})}),
            item(NOW - 59, {"type": "function_call_output", "call_id": "s1", "output": '{"task_name":"/root/review"}'}),
            item(NOW - 58, {"type": "function_call", "name": "wait_agent", "call_id": "w1", "arguments": '{"timeout_ms":30000}'}),
            item(NOW - 28, {"type": "function_call_output", "call_id": "w1", "output": '{"message":"Wait timed out.","timed_out":true}'}),
            item(NOW - 27, {"type": "function_call", "name": "followup_task", "call_id": "f1",
                            "arguments": '{"target":"review","message":"gAAAA"}'}),
            item(NOW - 27, {"type": "function_call_output", "call_id": "f1", "output": ""}),
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def test_spawn_becomes_subagent_entry_and_waits_are_quiet(self):
        blocks = server.parse_codex_rollout_tail(str(self.fx.parent))
        self.assertEqual([(b["label"], b["text"]) for b in blocks],
                         [("子智能体", "codex · review"), ("Tool", "给 review 发消息")])
        self.assertEqual(blocks[0]["agent_ref"], "/root/review")
        self.assertEqual(server._parse_codex_rollout_all_blocks(str(self.fx.parent)), blocks)

    def test_running_child_gets_chip_and_live_panel(self):
        self.fx.child("review", [event(NOW - 55, "task_started"), exec_call(NOW - 20, "pytest -q")])
        codex_subagents._LISTINGS.clear()
        blocks = server.attach_codex_subagent_status(server.parse_codex_rollout_tail(str(self.fx.parent)), str(self.fx.parent))
        self.assertEqual(blocks[0]["agent"]["status"], "running")
        self.assertEqual(blocks[-1]["role"], "agents")
        self.assertEqual(blocks[-1]["agents"][0]["activity"], "运行命令：pytest -q")
        history = server.codex_history_before(str(self.fx.parent), None)
        self.assertNotIn("agents", [b["role"] for b in history["blocks"]])


class ProcChildrenTest(unittest.TestCase):
    def test_lists_a_child_without_spawning_pgrep(self):
        child = subprocess.Popen(["sleep", "5"])
        try:
            self.assertIn(str(child.pid), server._proc_children([str(os.getpid())]) or [])
        finally:
            child.kill()
            child.wait()


if __name__ == "__main__":
    unittest.main(verbosity=2)
