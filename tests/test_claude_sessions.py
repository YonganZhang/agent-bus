#!/usr/bin/env python3
"""claude agents --json binding (shared by Cards and provider_state)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import claude_sessions  # noqa: E402


class RecordForTest(unittest.TestCase):
    records = [
        {"pid": 10, "sessionId": "s-1", "status": "busy"},
        {"pid": 20, "sessionId": "s-dup", "status": "idle"},
        {"pid": 30, "sessionId": "s-dup", "status": "waiting", "waitingFor": "dialog open"},
    ]

    def test_pid_in_the_pane_tree_is_exact(self) -> None:
        self.assertEqual(claude_sessions.record_for(self.records, {"5", "30"})["status"], "waiting")

    def test_session_id_only_when_unique(self) -> None:
        self.assertEqual(claude_sessions.record_for(self.records, set(), "s-1")["pid"], 10)
        # 同一会话在两个窗口里 resume：两个 pid 同一个 sessionId，不能按 id 认。
        self.assertIsNone(claude_sessions.record_for(self.records, set(), "s-dup"))

    def test_truncated_array_keeps_complete_records(self) -> None:
        records, error = claude_sessions._decode('[{"pid": 1, "status": "idle"}, {"pid": 2, "sta')
        self.assertEqual(records, [{"pid": 1, "status": "idle"}])
        self.assertIn("truncated", error)


if __name__ == "__main__":
    unittest.main()
