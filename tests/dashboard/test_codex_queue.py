#!/usr/bin/env python3
"""Regression tests for Codex's inline queued-message rendering."""
from __future__ import annotations

import unittest

import server


class CodexQueueExtractionTest(unittest.TestCase):
    def test_inline_prompt_before_queue_footer_is_extracted(self) -> None:
        capture = (
            "❯ 已提交的问题\n"
            "\n"
            "● AI正在处理\n"
            "\n"
            "› 这是排队的问题\n"
            "tab to queue message 39% context left\n"
        )
        self.assertEqual(server.extract_codex_queued_messages(capture), ["这是排队的问题"])

    def test_empty_codex_prompt_is_not_extracted(self) -> None:
        capture = "› Ask Codex to do anything\n gpt-5.6-luna medium · ~\n"
        self.assertEqual(server.extract_codex_queued_messages(capture), [])

    def test_old_prompt_outside_short_footer_region_is_not_extracted(self) -> None:
        capture = "\n".join(
            ["› old line"] + [f"history line {i}" for i in range(20)]
            + ["› Ask Codex to do anything", "tab to queue message 39% context left"]
        )
        self.assertEqual(server.extract_codex_queued_messages(capture), [])

    def test_blocks_mark_queue_and_do_not_duplicate_same_prompt(self) -> None:
        pane = server.Pane(
            pane_id="%test",
            target="secretary_web:900.0",
            session="secretary_web",
            window_index=900,
            pane_index=0,
            window_name="test",
            command="codex",
            cwd="/tmp",
            title="test",
            active=True,
            kind="Codex",
            project="test",
            preview="",
            status="",
        )
        capture = "❯ 已提交的问题\n\n● AI正在处理\n\n› 这是排队的问题\ntab to queue message 39% context left\n"
        blocks, _meta = server.blocks_for_pane_with_meta(pane, capture)
        matches = [b for b in blocks if b.get("text") == "这是排队的问题"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].get("label"), "排队中")
        self.assertTrue(matches[0].get("pending"))


if __name__ == "__main__":
    unittest.main()
