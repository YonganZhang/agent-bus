#!/usr/bin/env python3
"""Regression tests for Phase 3b: Codex rollout-JSONL parsing + pane->rollout
resolution + /api/history_before pagination.

Covers:
- parse_codex_rollout_tail / _parse_codex_rollout_all_blocks: normal message
  parsing, a function_call/function_call_output pair, custom_tool_call(_output),
  system-injected content filtered out, developer-role messages dropped.
- _score_rollout_candidates: the confidence-based disambiguation scoring in
  isolation (synthetic candidate texts, no real /proc fds needed).
- codex_rollout_for_pane: 0/1/many-candidate resolution behavior, monkeypatching
  the fd-discovery layer so no real live process is required.
- codex_history_before / codex_rollout_path_for_pane_id: the pagination
  endpoint's backing functions, parallel to Claude's Phase 3a tests.

Run: python3 -m pytest test_codex_rollout.py -q
"""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace

import server


def _write_rollout(entries):
    fh = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    for e in entries:
        fh.write(json.dumps(e) + "\n")
    fh.close()
    return fh.name


def _msg(role, text, item_type=None):
    if item_type is None:
        item_type = "output_text" if role == "assistant" else "input_text"
    return {
        "type": "response_item",
        "payload": {"type": "message", "role": role, "content": [{"type": item_type, "text": text}]},
    }


class CodexSystemInjectedFilterTest(unittest.TestCase):
    def test_known_markers_are_filtered(self):
        for marker in (
            "<environment_context>\n  cwd: /tmp",
            "# AGENTS.md instructions\n\n<INSTRUCTIONS>\nsync contract...",
            "<subagent_notification>\ndone",
            "<codex_internal_context somefield=1>",
            "<turn_aborted>\nThe user interrupted",
            "<skill>\n<name>demo-skill</name>",
        ):
            with self.subTest(marker=marker[:30]):
                self.assertTrue(server._is_codex_system_injected_user(marker))

    def test_real_user_text_is_not_filtered(self):
        for txt in ("继续", "好的，请开始", "请查看这张图片：/tmp/x.png", "/goal 目标：完成示例功能"):
            with self.subTest(txt=txt):
                self.assertFalse(server._is_codex_system_injected_user(txt))


class CodexJsonLoadsMaybeTest(unittest.TestCase):
    def test_json_string_is_decoded(self):
        self.assertEqual(
            server._codex_json_loads_maybe('{"cmd": "pwd", "workdir": "/tmp"}'),
            {"cmd": "pwd", "workdir": "/tmp"},
        )

    def test_plain_text_falls_back_to_original_string(self):
        raw = "Chunk ID: abc123\nWall time: 0.1 seconds\nOutput:\nhello\n"
        self.assertEqual(server._codex_json_loads_maybe(raw), raw)

    def test_non_string_passthrough(self):
        self.assertEqual(server._codex_json_loads_maybe(None), None)
        self.assertEqual(server._codex_json_loads_maybe({"a": 1}), {"a": 1})


class CodexRolloutParseTest(unittest.TestCase):
    def test_normal_user_assistant_and_function_call_pair(self):
        path = _write_rollout([
            {"type": "session_meta", "payload": {"cwd": "/tmp", "id": "abc"}},
            _msg("user", "please run pwd"),
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "call_1",
                    "arguments": json.dumps({"cmd": "pwd", "workdir": "/tmp"}),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "Chunk ID: x\nWall time: 0.0 seconds\nProcess exited with code 0\nOutput:\n/tmp\n",
                },
            },
            _msg("assistant", "ran pwd, got /tmp"),
        ])
        try:
            blocks = server.parse_codex_rollout_tail(path)
        finally:
            os.unlink(path)
        labels = [b["label"] for b in blocks]
        self.assertEqual(labels, ["User prompt", "Tool", "Tool result", "AI output"])
        self.assertEqual(blocks[0]["text"], "please run pwd")
        self.assertIn("Shell command", blocks[1]["text"])
        self.assertIn("pwd", blocks[1]["text"])
        self.assertIn("/tmp", blocks[2]["text"])
        self.assertEqual(blocks[3]["text"], "ran pwd, got /tmp")
        self.assertEqual(blocks[3]["role"], "assistant")

    def test_custom_tool_call_apply_patch_pair(self):
        path = _write_rollout([
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "apply_patch",
                    "call_id": "call_2",
                    "input": "*** Begin Patch\n*** Update File: a.py\n@@\n-old\n+new\n*** End Patch",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call_2",
                    "output": "Exit code: 0\nOutput:\nSuccess. Updated the following files:\nM a.py\n",
                },
            },
        ])
        try:
            blocks = server.parse_codex_rollout_tail(path)
        finally:
            os.unlink(path)
        self.assertEqual([b["label"] for b in blocks], ["Tool", "Tool result"])
        self.assertIn("apply_patch", blocks[0]["text"])
        self.assertIn("Begin Patch", blocks[0]["text"])
        self.assertIn("Success", blocks[1]["text"])

    def test_system_injected_user_content_is_labeled_not_user_prompt(self):
        path = _write_rollout([
            _msg("user", "<environment_context>\n  cwd: /tmp\n</environment_context>"),
            _msg("user", "real question from the human"),
        ])
        try:
            blocks = server.parse_codex_rollout_tail(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]["label"], "系统通知")
        self.assertEqual(blocks[1]["label"], "User prompt")
        self.assertEqual(blocks[1]["text"], "real question from the human")

    def test_developer_role_message_is_dropped_entirely(self):
        path = _write_rollout([
            _msg("developer", "<permissions instructions>\nsandbox_mode is danger-full-access"),
            _msg("user", "hello"),
        ])
        try:
            blocks = server.parse_codex_rollout_tail(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["text"], "hello")

    def test_reasoning_with_empty_summary_yields_no_block(self):
        path = _write_rollout([
            {
                "type": "response_item",
                "payload": {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "gAAA..."},
            },
            _msg("assistant", "final answer"),
        ])
        try:
            blocks = server.parse_codex_rollout_tail(path)
        finally:
            os.unlink(path)
        self.assertEqual([b["label"] for b in blocks], ["AI output"])

    def test_reasoning_with_summary_text_becomes_ai_step(self):
        path = _write_rollout([
            {
                "type": "response_item",
                "payload": {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "thinking about the plan"}]},
            },
        ])
        try:
            blocks = server.parse_codex_rollout_tail(path)
        finally:
            os.unlink(path)
        self.assertEqual([b["label"] for b in blocks], ["AI step"])
        self.assertEqual(blocks[0]["text"], "thinking about the plan")


class ScoreRolloutCandidatesTest(unittest.TestCase):
    """Confidence-based disambiguation scoring in isolation, same bar as
    Claude's claude_transcript_for_pane (best_hits >= 2 and best_hits >
    second_hits)."""

    def test_clear_unique_winner(self):
        snips = ["distinctivefragmentone", "distinctivefragmenttwo", "unrelatedbit"]
        blobs = {
            "/a/candidate1.jsonl": "distinctivefragmentonedistinctivefragmenttwo",
            "/a/candidate2.jsonl": "nothingmatcheshere",
        }
        winner, best, second = server._score_rollout_candidates(snips, blobs)
        self.assertEqual(winner, "/a/candidate1.jsonl")
        self.assertEqual(best, 2)
        self.assertEqual(second, 0)

    def test_tie_yields_no_confident_winner(self):
        snips = ["distinctivefragmentone", "distinctivefragmenttwo"]
        blobs = {
            "/a/candidate1.jsonl": "distinctivefragmentonedistinctivefragmenttwo",
            "/a/candidate2.jsonl": "distinctivefragmentonedistinctivefragmenttwo",
        }
        winner, best, second = server._score_rollout_candidates(snips, blobs)
        self.assertIsNone(winner)
        self.assertEqual(best, 2)
        self.assertEqual(second, 2)

    def test_single_hit_below_confidence_bar(self):
        snips = ["distinctivefragmentone", "distinctivefragmenttwo"]
        blobs = {
            "/a/candidate1.jsonl": "distinctivefragmentone",
            "/a/candidate2.jsonl": "nothingmatcheshere",
        }
        winner, best, second = server._score_rollout_candidates(snips, blobs)
        self.assertIsNone(winner, "best_hits=1 must not be treated as confident")
        self.assertEqual(best, 1)


class CodexRolloutForPaneTest(unittest.TestCase):
    """codex_rollout_for_pane resolution, with _codex_rollout_fd_candidates
    monkeypatched so no real live process / /proc access is needed."""

    def _pane(self, pane_id="%14"):
        return server.Pane(
            pane_id=pane_id,
            target=f"secretary_web:14.0",
            session="secretary_web",
            window_index=14,
            pane_index=0,
            window_name="codex-window",
            command="node",
            cwd="/tmp",
            title="codex",
            active=False,
            kind="Codex",
            project="tmp",
            preview="",
            status="idle",
        )

    def setUp(self):
        server._codex_rollout_cache.clear()
        self._orig_candidates = server._codex_rollout_fd_candidates
        self._orig_identity = server._pane_agent_process_identity
        self._orig_run_tmux = server.run_tmux
        # The unit under test only needs a stable pane pid before it reaches
        # the monkeypatched fd-discovery layer.  Depending on a real live %14
        # made this nominally isolated test fail whenever tmux reused/removed
        # that pane id.
        server.run_tmux = lambda _args: SimpleNamespace(returncode=0, stdout="111\n", stderr="")

    def tearDown(self):
        server._codex_rollout_fd_candidates = self._orig_candidates
        server._pane_agent_process_identity = self._orig_identity
        server.run_tmux = self._orig_run_tmux
        server._codex_rollout_cache.clear()

    def test_zero_candidates_reports_no_rollout_yet(self):
        server._pane_agent_process_identity = lambda _pane: "111:aaa"
        server._codex_rollout_fd_candidates = lambda _pane_pid: []
        path, meta = server.codex_rollout_for_pane(self._pane(), "some screen text")
        self.assertIsNone(path)
        self.assertEqual(meta["quality"], "degraded")
        self.assertEqual(meta["reason"], "no-rollout-yet")

    def test_single_candidate_is_used_directly_without_screen_match(self):
        server._pane_agent_process_identity = lambda _pane: "111:bbb"
        server._codex_rollout_fd_candidates = lambda _pane_pid: ["/tmp/only-candidate.jsonl"]
        path, meta = server.codex_rollout_for_pane(self._pane(), "irrelevant screen text")
        self.assertEqual(path, "/tmp/only-candidate.jsonl")
        self.assertEqual(meta["quality"], "full")

    def test_multi_candidate_confident_screen_match_picks_correct_file(self):
        cand_a = _write_rollout([_msg("assistant", "the quick brown fox jumps over the lazy dog today. a second distinctive sentence follows here.")])
        cand_b = _write_rollout([_msg("assistant", "completely unrelated content about something else entirely")])
        try:
            server._blob_cache.clear()
            server._pane_agent_process_identity = lambda _pane: "111:ccc"
            server._codex_rollout_fd_candidates = lambda _pane_pid: [cand_a, cand_b]
            # Two distinct lines so _screen_match_snippets yields >=2 snippets
            # (a single-line screen can only ever produce one snippet, which
            # can never clear the best_hits >= 2 confidence bar).
            screen = "the quick brown fox jumps over the lazy dog today\na second distinctive sentence follows here"
            path, meta = server.codex_rollout_for_pane(self._pane(), screen)
            self.assertEqual(path, cand_a)
            self.assertEqual(meta["quality"], "full")
        finally:
            os.unlink(cand_a)
            os.unlink(cand_b)

    def test_multi_candidate_ambiguous_screen_reports_ambiguous_not_guess(self):
        shared_text = "shared duplicate boilerplate line seen everywhere and another shared distinctive followup line too"
        cand_a = _write_rollout([_msg("assistant", shared_text)])
        cand_b = _write_rollout([_msg("assistant", shared_text)])
        try:
            server._blob_cache.clear()
            server._pane_agent_process_identity = lambda _pane: "111:ddd"
            server._codex_rollout_fd_candidates = lambda _pane_pid: [cand_a, cand_b]
            # Two distinct lines, both hitting both candidates equally (a true
            # tie: best_hits == second_hits == 2) - must NOT guess a winner.
            screen = "shared duplicate boilerplate line seen everywhere\nanother shared distinctive followup line too"
            path, meta = server.codex_rollout_for_pane(self._pane(), screen)
            self.assertIsNone(path)
            self.assertEqual(meta["quality"], "degraded")
            self.assertEqual(meta["reason"], "ambiguous-rollout")
        finally:
            os.unlink(cand_a)
            os.unlink(cand_b)

    def test_result_is_cached_per_process_identity(self):
        calls = {"n": 0}

        def counting_candidates(_pane_pid):
            calls["n"] += 1
            return ["/tmp/cached-candidate.jsonl"]

        server._pane_agent_process_identity = lambda _pane: "111:eee"
        server._codex_rollout_fd_candidates = counting_candidates
        server.codex_rollout_for_pane(self._pane(), "text")
        server.codex_rollout_for_pane(self._pane(), "text")
        self.assertEqual(calls["n"], 1, "second call within TTL must hit the identity cache")


class CodexHistoryPaginationTest(unittest.TestCase):
    """codex_history_before / codex_rollout_path_for_pane_id: same contract
    shape as Claude's Phase 3a history_before / transcript_path_for_pane_id."""

    def _multi_block_rollout(self, n_pairs):
        entries = []
        for i in range(n_pairs):
            entries.append(_msg("user", f"user turn {i}"))
            entries.append(_msg("assistant", f"assistant reply {i}"))
        return _write_rollout(entries)

    def setUp(self):
        server._codex_history_blocks_cache.clear()

    def test_pagination_walks_full_rollout_and_reconstructs_order(self):
        path = self._multi_block_rollout(5)  # 10 blocks total
        try:
            page1 = server.codex_history_before(path, cursor=None, count=4)
            self.assertEqual(len(page1["blocks"]), 4)
            self.assertEqual(page1["total"], 10)
            self.assertEqual(page1["next_cursor"], 6)
            self.assertTrue(page1["has_more"])

            page2 = server.codex_history_before(path, cursor=page1["next_cursor"], count=4)
            self.assertEqual(page2["next_cursor"], 2)

            page3 = server.codex_history_before(path, cursor=page2["next_cursor"], count=4)
            self.assertIsNone(page3["next_cursor"])
            self.assertFalse(page3["has_more"])

            reconstructed = [b["text"] for b in page3["blocks"] + page2["blocks"] + page1["blocks"]]
            expected = [b["text"] for b in server._parse_codex_rollout_all_blocks(path)]
            self.assertEqual(reconstructed, expected)
        finally:
            os.unlink(path)

    def test_cache_invalidates_on_file_change(self):
        path = self._multi_block_rollout(1)
        try:
            first = server.codex_history_before(path, cursor=None, count=50)
            self.assertEqual(first["total"], 2)
            with open(path, "a") as fh:
                fh.write(json.dumps(_msg("assistant", "appended later")) + "\n")
            second = server.codex_history_before(path, cursor=None, count=50)
            self.assertEqual(second["total"], 3)
            self.assertEqual(second["blocks"][-1]["text"], "appended later")
        finally:
            os.unlink(path)

    def test_rollout_path_for_pane_id_none_when_pane_not_found(self):
        original = server.pane_by_id
        try:
            server.pane_by_id = lambda _pane_id: None
            path, meta = server.codex_rollout_path_for_pane_id("%does-not-exist")
            self.assertIsNone(path)
            self.assertEqual(meta["reason"], "not-codex")
        finally:
            server.pane_by_id = original

    def test_rollout_path_for_pane_id_none_for_non_codex_pane(self):
        original = server.pane_by_id
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
            path, meta = server.codex_rollout_path_for_pane_id("%13")
            self.assertIsNone(path)
            self.assertEqual(meta["reason"], "not-codex")
        finally:
            server.pane_by_id = original


if __name__ == "__main__":
    unittest.main()
