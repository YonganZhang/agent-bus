#!/usr/bin/env python3
"""Regression test for the queued-message duplicate-render bug.

Trigger (confirmed
against the raw JSONL transcript, which shows exactly one
``queue-operation enqueue`` + one ``remove`` for the text, i.e. genuinely one
user action): while a message sits in Claude's on-screen queue and then gets
dequeued, the terminal screen can transition through a state where the same
line is visible both in a queue-formatted line (picked up by
``extract_queued_messages``) and as an already-plain, non-queue ``❯`` prompt
line (picked up generically by ``merge_live_screen_tail`` via
``parse_blocks``). Before the fix, ``blocks_for_pane_with_meta`` appended a
block for each unconditionally, rendering the same real user action twice
(a third, frontend-only duplicate from the Cards composer's optimistic echo
is out of scope for this Python-side test).

This test drives ``blocks_for_pane_with_meta`` so that a plain ``❯`` line
already lands as a pending block via ``merge_live_screen_tail`` (the first
scanner), then stubs ``extract_queued_messages`` (the second scanner) to
report that exact same text, and asserts the fix collapses this to a single
rendered block instead of two.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


TRANSCRIPT_LINES = [
    '{"type":"user","message":{"role":"user","content":"现在进展如何？给我一个总结"}}',
    '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"示例仓库的分支已经合并完成，本地与远端一致。之前另一个窗口在做示例模块的重构。"}]}}',
]

QUEUED_TEXT = "补充一下：是示例模块乙，不是甲"


def build_capture_text() -> str:
    # A plain, non-indented "❯ ..." line with no nearby queue footer: this is
    # what merge_live_screen_tail's generic scanner (parse_blocks) picks up
    # as ordinary new screen content, independent of the queue-specific path.
    # A trailing "● ..." reply line is required so parse_blocks does not treat
    # the queued line as the user's still-unsent draft (its "final terminal
    # prompt line is a draft, not a submitted message" heuristic pops it
    # otherwise -- confirmed by direct experimentation against parse_blocks).
    lines = [
        "❯ 现在进展如何？给我一个总结",
        "",
        "● 示例仓库的分支已经合并完成，本地与远端一致。之前另一个窗口在做示例模块的重构。",
        "",
        f"❯ {QUEUED_TEXT}",
        "",
        "● 好的，已按模块乙继续",
    ]
    return "\n".join(lines)


class QueuedMessageDedupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
        )
        self.tmp.write("\n".join(TRANSCRIPT_LINES) + "\n")
        self.tmp.close()
        self.transcript_path = self.tmp.name
        self.pane = server.Pane(
            pane_id="%test",
            target="secretary_web:900.0",
            session="secretary_web",
            window_index=900,
            pane_index=0,
            window_name="test",
            command="claude",
            cwd="/tmp",
            title="test",
            active=True,
            kind="Claude",
            project="test",
            preview="",
            status="",
        )

    def tearDown(self) -> None:
        Path(self.transcript_path).unlink(missing_ok=True)

    def _run(self, *, stub_queued: bool):
        capture_text = build_capture_text()
        queued_return = [QUEUED_TEXT] if stub_queued else []
        with (
            patch.object(server, "pane_is_alt_screen", return_value=True),
            patch.object(server, "claude_transcript_for_pane", return_value=self.transcript_path),
            patch.object(server, "_shared_live_transcript_panes", return_value=[]),
            patch.object(server, "extract_queued_messages", return_value=queued_return),
        ):
            return server.blocks_for_pane_with_meta(self.pane, capture_text)

    def test_precondition_generic_scanner_alone_already_sees_the_line(self) -> None:
        """Sanity check the fixture: with the queue-specific scanner silent,
        merge_live_screen_tail's generic scrape must still surface the plain
        "❯ ..." line as a pending block, otherwise the main test below would
        pass vacuously (nothing to dedupe against)."""
        blocks, meta = self._run(stub_queued=False)
        self.assertEqual(meta["source"], "claude-transcript")
        occurrences = [b for b in blocks if QUEUED_TEXT in (b.get("text") or "")]
        self.assertEqual(
            len(occurrences), 1,
            f"expected the generic live-screen scanner alone to surface exactly one "
            f"pending block for the fixture's plain '❯' line, got {occurrences}",
        )
        self.assertTrue(occurrences[0].get("pending"))

    def test_queued_message_renders_at_most_once_when_both_scanners_agree(self) -> None:
        blocks, meta = self._run(stub_queued=True)
        self.assertEqual(meta["source"], "claude-transcript")
        occurrences = [b for b in blocks if QUEUED_TEXT in (b.get("text") or "")]
        self.assertEqual(
            len(occurrences),
            1,
            f"extract_queued_messages must not add a second block when the "
            f"generic scanner already rendered this text; got {occurrences}",
        )


if __name__ == "__main__":
    unittest.main()
