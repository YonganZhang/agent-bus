#!/usr/bin/env python3
"""Which conversation a Claude card shows, and how far.

- An authoritative (stamped) transcript is shown whole: a screen scrolled up
  ("Jump to bottom"), in copy mode or behind a picker must not cut it.
- Claude's own session list (``claude agents --json``, matched by pid) wins
  over a stamp that did not follow a reopened / resumed window, and the
  background reconcile rewrites such a stamp.

Run: python3 -m pytest test_pane_identity.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server


def claude_pane(**overrides) -> server.Pane:
    values = dict(
        pane_id="%8", target="secretary_web:8.0", session="secretary_web", window_index=8, pane_index=0,
        window_name="demo", command="claude", cwd="/work/demo", title="Claude", active=False, kind="Claude",
        project="demo", preview="", status="idle", pane_pid="4242", ai_session_id="old-session",
        ai_transcript="", ai_alive=True,
    )
    values.update(overrides)
    return server.Pane(**values)


def user(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def assistant(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": text}}


class IdentityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self.tmp.name) / "projects"
        patcher = mock.patch.object(server, "CLAUDE_PROJECTS_DIR", self.projects)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def transcript(self, session_id: str, rows: list[dict], folder: str = "-work-demo") -> str:
        path = self.projects / folder / f"{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        return str(path)

    def test_scrolled_up_screen_does_not_cut_a_stamped_transcript(self) -> None:
        path = self.transcript("live", [
            user("first question about the sample task"),
            assistant("helpers started in the background, results will follow shortly"),
            user("please continue"),
            assistant("final answer written after checking every result"),
        ])
        pane = claude_pane(ai_session_id="live", ai_transcript=path)
        # The TUI is scrolled up: the older answer is on screen, the newest is not.
        screen = "\n".join([
            "● helpers started in the background, results will follow shortly",
            "  Jump to bottom (ctrl+end)",
            "─" * 60, "❯ ", "─" * 60, "  ⏵⏵ bypass permissions on",
        ])
        with mock.patch.object(server, "pane_is_alt_screen", return_value=True), \
             mock.patch.object(server, "official_claude_record", return_value={"sessionId": "live"}), \
             mock.patch.object(server, "_shared_live_transcript_panes", return_value=[]):
            blocks, meta = server.blocks_for_pane_with_meta(pane, screen)
        self.assertEqual(meta["quality"], "full")
        self.assertIn("final answer written", blocks[-1]["text"])

    def test_idle_session_gets_no_live_tail_from_an_earlier_program(self) -> None:
        # 改前: 窗口先跑过 Codex、再换成 Claude；Claude 空闲时，屏幕里残留的 Codex 输出被当成
        # "还没落盘的新内容"追加在卡片末尾。
        path = self.transcript("live", [user("please take over"), assistant("taken over, summary written")])
        pane = claude_pane(ai_session_id="live", ai_transcript=path)
        screen = "\n".join([
            "› earlier message typed into the previous program",
            "• an answer printed by the previous program before the switch",
            "─" * 60, "❯ please take over", "● taken over, summary written",
            "─" * 60, "❯ ", "─" * 60, "  ⏵⏵ bypass permissions on",
        ])
        with mock.patch.object(server, "pane_is_alt_screen", return_value=True), \
             mock.patch.object(server, "official_claude_record", return_value={"sessionId": "live", "status": "idle"}), \
             mock.patch.object(server, "_shared_live_transcript_panes", return_value=[]):
            blocks, _meta = server.blocks_for_pane_with_meta(pane, screen)
        self.assertFalse(any(block.get("pending") for block in blocks))
        self.assertIn("summary written", blocks[-1]["text"])

    def test_scrolled_back_screen_adds_no_live_tail(self) -> None:
        recorded = [
            {"role": "user", "label": "User prompt", "text": "first question about the sample task"},
            {"role": "assistant", "label": "AI output", "text": "newest recorded answer that is not on screen"},
        ]
        screen = "\n".join([
            "❯ first question about the sample task",
            "● an older paragraph that the reader scrolled back to",
            "  Jump to bottom (ctrl+end)",
            "─" * 60, "❯ ", "─" * 60, "  ⏵⏵ bypass permissions on",
        ])
        self.assertTrue(server.claude_view_scrolled_back(screen))
        self.assertEqual(server.merge_live_screen_tail(recorded, screen), recorded)

    def test_text_pasted_inside_a_recorded_prompt_is_not_new_output(self) -> None:
        pasted = "a long note pasted from another tool into the prompt"
        recorded = [
            {"role": "user", "label": "User prompt", "text": f"please look at this: {pasted} and tell me more"},
            {"role": "assistant", "label": "AI output", "text": "the reply to that pasted note"},
        ]
        screen = "\n".join([
            "● the reply to that pasted note",
            f"● {pasted}",
            "─" * 60, "❯ ", "─" * 60, "  ⏵⏵ bypass permissions on",
        ])
        merged = server.merge_live_screen_tail(recorded, screen)
        self.assertFalse(any(block.get("pending") for block in merged))

    def test_official_session_wins_over_a_stale_stamp(self) -> None:
        stale = self.transcript("old-session", [user("an old conversation")])
        live = self.transcript("new-session", [user("the conversation this window runs now")])
        pane = claude_pane(ai_session_id="old-session", ai_transcript=stale)
        with mock.patch.object(server, "official_claude_record", return_value={"sessionId": "new-session", "cwd": "/work/demo"}):
            self.assertEqual(server.stamped_transcript_for_pane(pane), live)
        with mock.patch.object(server, "official_claude_record", return_value=None):
            self.assertEqual(server.stamped_transcript_for_pane(pane), stale)  # no official record: keep the stamp

    def test_confirmed_session_whose_file_moved_into_a_worktree_is_found(self) -> None:
        moved = self.transcript("wt-session", [user("work inside a worktree")], folder="-work-demo--claude-worktrees-fix")
        pane = claude_pane(ai_session_id="wt-session", ai_transcript=str(self.projects / "-work-demo" / "wt-session.jsonl"))
        with mock.patch.object(server, "official_claude_record", return_value={"sessionId": "wt-session", "cwd": "/work/demo"}):
            self.assertEqual(server.stamped_transcript_for_pane(pane), moved)

    def test_same_session_in_two_directories_prefers_the_process_cwd(self) -> None:
        self.transcript("shared", [user("a")], folder="-work-other")
        here = self.transcript("shared", [user("b")], folder="-work-demo")
        os.utime(here, (1, 1))  # older file, but it is the one in the process's cwd
        self.assertEqual(server.claude_transcript_for_session("shared", "/work/demo"), here)

    def test_reconcile_rewrites_only_stale_stamps(self) -> None:
        live = self.transcript("new-session", [user("x")])
        panes = [claude_pane(), claude_pane(pane_id="%9", ai_session_id="same")]
        records = {"%8": {"sessionId": "new-session", "cwd": "/work/demo"}, "%9": {"sessionId": "same"}}
        calls: list[list[str]] = []
        events: list[str] = []

        def run_tmux(args: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(server, "list_panes", return_value=panes), \
             mock.patch.object(server, "official_claude_record", side_effect=lambda pane: records[pane.pane_id]), \
             mock.patch.object(server, "run_tmux", side_effect=run_tmux), \
             mock.patch.object(server.event_ledger, "append_event", side_effect=lambda kind, **_kw: events.append(kind)):
            fixed = server.reconcile_claude_stamps("secretary_web")
        self.assertEqual(fixed, [{"pane": "%8", "old_session_id": "old-session", "new_session_id": "new-session"}])
        self.assertIn(["set-option", "-p", "-t", "%8", "@ai_session_id", "new-session"], calls)
        self.assertIn(["set-option", "-p", "-t", "%8", "@ai_transcript", live], calls)
        self.assertFalse(any("%9" in call for call in calls))
        self.assertEqual(events, ["pane_identity_restamped"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
