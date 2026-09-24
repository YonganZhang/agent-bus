#!/usr/bin/env python3
"""Tests for the bounded, read-only local trace parser."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import trace_data


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _claude_message(kind: str, timestamp: str, content: object, **extra: object) -> dict:
    return {
        "type": kind,
        "timestamp": timestamp,
        "message": {"role": kind, "content": content},
        **extra,
    }


def _codex_entry(kind: str, timestamp: str, payload: dict) -> dict:
    return {"type": kind, "timestamp": timestamp, "payload": payload}


class _RootsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        base = Path(self.tempdir.name)
        self.claude_root = base / ".claude" / "projects"
        self.codex_root = base / ".codex" / "sessions"
        self.claude_root.mkdir(parents=True)
        self.codex_root.mkdir(parents=True)
        self.patchers = [
            mock.patch.object(trace_data, "CLAUDE_ROOT", self.claude_root),
            mock.patch.object(trace_data, "CODEX_ROOT", self.codex_root),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tempdir.cleanup()


class ClaudeTraceTest(_RootsTestCase):
    def test_claude_tree_subagent_join_stable_ids_and_redaction(self) -> None:
        transcript = self.claude_root / "project-a" / "session-1.jsonl"
        entries = [
            _claude_message(
                "user",
                "2026-07-20T00:00:00Z",
                "inspect /home/alice/private.txt with Bearer abcdefghijklmnop",
            ),
            _claude_message(
                "assistant",
                "2026-07-20T00:00:01Z",
                [
                    {"type": "text", "text": "I will inspect it."},
                    {
                        "type": "tool_use",
                        "id": "tool-task-1",
                        "name": "Task",
                        "input": {
                            "description": "bounded helper",
                            "prompt": "SECRET_KEY=do-not-return full raw prompt",
                        },
                    },
                ],
                uuid="assistant-1",
            ),
            _claude_message(
                "user",
                "2026-07-20T00:00:02Z",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-task-1",
                        "content": "done; token=topsecret",
                    }
                ],
            ),
            _claude_message(
                "assistant",
                "2026-07-20T00:00:03Z",
                [{"type": "text", "text": "Finished without exposing /etc/passwd."}],
                uuid="assistant-2",
            ),
        ]
        _write_jsonl(transcript, entries)
        subagent = (
            transcript.parent
            / transcript.stem
            / "subagents"
            / "agent-helper.jsonl"
        )
        _write_jsonl(
            subagent,
            [
                _claude_message(
                    "user",
                    "2026-07-20T00:00:01.200Z",
                    "helper task",
                    parentToolUseId="tool-task-1",
                ),
                _claude_message(
                    "assistant",
                    "2026-07-20T00:00:01.700Z",
                    "helper done",
                ),
            ],
        )

        first = trace_data.build_trace("claude-code", transcript)
        second = trace_data.build_trace("claude-code", transcript)

        self.assertEqual(first, second)
        earlier_named_subagent = subagent.with_name("agent-aaa.jsonl")
        _write_jsonl(
            earlier_named_subagent,
            [
                _claude_message(
                    "user", "2026-07-20T00:00:01.300Z", "another helper"
                )
            ],
        )
        with_added_child = trace_data.build_trace("claude-code", transcript)
        self.assertTrue(
            {span["id"] for span in first["spans"]}.issubset(
                {span["id"] for span in with_added_child["spans"]}
            ),
            "adding another subagent must not renumber existing span IDs",
        )
        self.assertLessEqual(len(first["spans"]), trace_data.HARD_MAX_SPANS)
        self.assertGreaterEqual(first["summary"]["turn_count"], 2)
        self.assertGreaterEqual(first["summary"]["llm_count"], 2)
        self.assertEqual(first["summary"]["tool_count"], 1)
        self.assertFalse(first["truncated"])

        task_span = next(
            span
            for span in first["spans"]
            if span["kind"] == "TOOL_CALL" and span["name"] == "Task"
        )
        child_turns = [
            span
            for span in first["spans"]
            if span["kind"] == "AGENT_TURN" and span["name"] == "agent turn"
        ]
        self.assertTrue(child_turns)
        self.assertEqual(child_turns[0]["parent_id"], task_span["id"])
        self.assertEqual(child_turns[0]["join_quality"], "structural")
        self.assertEqual(task_span["status"], "ok")
        self.assertFalse(task_span["incomplete"])

        serialized = json.dumps(first, ensure_ascii=False)
        for forbidden in (
            "/home/alice",
            "/etc/passwd",
            "abcdefghijklmnop",
            "topsecret",
            "do-not-return",
            str(self.claude_root),
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertNotIn("prompt", task_span["input_summary"].lower())
        self.assertIn("[PATH]", serialized)

    def test_malformed_and_oversized_records_are_counted_not_returned(self) -> None:
        transcript = self.claude_root / "project-a" / "session-2.jsonl"
        transcript.parent.mkdir(parents=True)
        with transcript.open("wb") as fh:
            fh.write(b"{not json}\n")
            fh.write(b'{"oversized":"' + b"x" * (trace_data.MAX_LINE_BYTES + 100) + b'"}\n')
            valid = _claude_message(
                "user", "2026-07-20T00:00:00Z", "valid prompt"
            )
            fh.write((json.dumps(valid) + "\n").encode())

        result = trace_data.build_trace("claude-code", transcript)

        self.assertEqual(result["warnings"]["malformed_records"], 1)
        self.assertEqual(result["warnings"]["oversized_records"], 1)
        self.assertEqual(result["summary"]["turn_count"], 1)
        self.assertNotIn("x" * 100, json.dumps(result))

    def test_partial_trailing_record_keeps_prior_spans_and_marks_incomplete(self) -> None:
        transcript = self.claude_root / "project-a" / "session-live.jsonl"
        _write_jsonl(
            transcript,
            [_claude_message("user", "2026-07-20T00:00:00Z", "valid prompt")],
        )
        with transcript.open("ab") as fh:
            fh.write(b'{"type":"assistant","message":')

        result = trace_data.build_trace("claude-code", transcript)

        self.assertEqual(result["summary"]["turn_count"], 1)
        self.assertFalse(result["warnings"]["input_truncated"])
        self.assertEqual(result["warnings"]["trailing_partial_records"], 1)
        self.assertEqual(result["warnings"]["malformed_records"], 0)
        self.assertTrue(result["spans"][0]["incomplete"])
        self.assertEqual(result["summary"]["error_count"], 0)
        self.assertEqual(result["summary"]["status"], "incomplete")
        self.assertTrue(
            all(span["status"] != "error" for span in result["spans"]),
            "an in-flight trailing record is incomplete, not a completed program error",
        )
        self.assertTrue(
            all(not span["error_type"] for span in result["spans"]),
            "incomplete nodes must not fabricate an error category",
        )


class CodexTraceTest(_RootsTestCase):
    def test_codex_common_events_and_completed_tool(self) -> None:
        rollout = self.codex_root / "2026" / "07" / "20" / "rollout-test.jsonl"
        entries = [
            _codex_entry(
                "session_meta",
                "2026-07-20T00:00:00Z",
                {
                    "id": "thread-secret",
                    "cwd": "/mnt/private/repository",
                    "base_instructions": "raw system payload must not escape",
                },
            ),
            _codex_entry(
                "event_msg", "2026-07-20T00:00:01Z", {"type": "task_started"}
            ),
            _codex_entry(
                "response_item",
                "2026-07-20T00:00:01.100Z",
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "<environment_context>\nPATH=/private/bin",
                        }
                    ],
                },
            ),
            _codex_entry(
                "response_item",
                "2026-07-20T00:00:01.200Z",
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "run safely with sk-proj-abcdefghijklmnop",
                        }
                    ],
                },
            ),
            _codex_entry(
                "response_item",
                "2026-07-20T00:00:02Z",
                {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "call-1",
                    "arguments": json.dumps(
                        {"cmd": "API_TOKEN=secret-value cat /var/private/file"}
                    ),
                },
            ),
            _codex_entry(
                "response_item",
                "2026-07-20T00:00:03Z",
                {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "Process exited with code 0\nBearer abcdefghijklmnop",
                },
            ),
            _codex_entry(
                "response_item",
                "2026-07-20T00:00:04Z",
                {
                    "type": "reasoning",
                    "summary": [{"text": "private chain of thought"}],
                    "encrypted_content": "opaque-secret",
                },
            ),
            _codex_entry(
                "response_item",
                "2026-07-20T00:00:05Z",
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "completed"}],
                },
            ),
            _codex_entry(
                "event_msg", "2026-07-20T00:00:06Z", {"type": "task_complete"}
            ),
        ]
        _write_jsonl(rollout, entries)

        result = trace_data.build_trace("codex", rollout)

        self.assertEqual(result["summary"]["turn_count"], 1)
        self.assertEqual(result["summary"]["tool_count"], 1)
        self.assertGreaterEqual(result["summary"]["llm_count"], 2)
        self.assertEqual(result["summary"]["status"], "ok")
        tool = next(span for span in result["spans"] if span["kind"] == "TOOL_CALL")
        self.assertEqual(tool["status"], "ok")
        self.assertFalse(tool["incomplete"])
        self.assertGreater(tool["duration_ms"], 0)
        reasoning = next(
            span for span in result["spans"] if span["name"] == "reasoning"
        )
        self.assertEqual(reasoning["output_summary"], "")

        serialized = json.dumps(result, ensure_ascii=False)
        for forbidden in (
            "/mnt/private",
            "/var/private",
            "/private/bin",
            "abcdefghijklmnop",
            "secret-value",
            "private chain of thought",
            "opaque-secret",
            "raw system payload",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_unclosed_tool_and_turn_are_incomplete(self) -> None:
        rollout = self.codex_root / "2026" / "07" / "20" / "rollout-open.jsonl"
        _write_jsonl(
            rollout,
            [
                _codex_entry(
                    "event_msg",
                    "2026-07-20T00:00:01Z",
                    {"type": "task_started"},
                ),
                _codex_entry(
                    "response_item",
                    "2026-07-20T00:00:02Z",
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "open-call",
                        "arguments": json.dumps({"cmd": "pwd"}),
                    },
                ),
            ],
        )

        result = trace_data.build_trace("codex", rollout)

        tool = next(span for span in result["spans"] if span["kind"] == "TOOL_CALL")
        turn = next(span for span in result["spans"] if span["kind"] == "AGENT_TURN")
        self.assertTrue(tool["incomplete"])
        self.assertTrue(tool["approx"])
        self.assertEqual(tool["status"], "unknown")
        self.assertTrue(turn["incomplete"])
        self.assertEqual(result["summary"]["status"], "incomplete")
        self.assertEqual(result["summary"]["error_count"], 0)
        self.assertTrue(
            all(span["status"] != "error" for span in result["spans"]),
            "open work is not evidence that an operation failed",
        )
        self.assertTrue(all(not span["error_type"] for span in result["spans"]))

    def test_error_taxonomy_uses_logged_evidence(self) -> None:
        rollout = self.codex_root / "2026" / "07" / "20" / "rollout-errors.jsonl"
        entries = [
            _codex_entry(
                "event_msg",
                "2026-07-20T00:00:00Z",
                {"type": "task_started"},
            ),
            _codex_entry(
                "response_item",
                "2026-07-20T00:00:01Z",
                {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "command",
                    "arguments": json.dumps({"cmd": "false"}),
                },
            ),
            _codex_entry(
                "response_item",
                "2026-07-20T00:00:02Z",
                {
                    "type": "function_call_output",
                    "call_id": "command",
                    "output": "Process exited with code 2",
                },
            ),
            _codex_entry(
                "response_item",
                "2026-07-20T00:00:03Z",
                {
                    "type": "function_call",
                    "name": "remote_tool",
                    "call_id": "timeout",
                    "arguments": "{}",
                },
            ),
            _codex_entry(
                "response_item",
                "2026-07-20T00:00:04Z",
                {
                    "type": "function_call_output",
                    "call_id": "timeout",
                    "is_error": True,
                    "output": "deadline exceeded while waiting",
                },
            ),
            _codex_entry(
                "response_item",
                "2026-07-20T00:00:05Z",
                {
                    "type": "function_call",
                    "name": "remote_tool",
                    "call_id": "interrupted",
                    "arguments": "{}",
                },
            ),
            _codex_entry(
                "response_item",
                "2026-07-20T00:00:06Z",
                {
                    "type": "function_call_output",
                    "call_id": "interrupted",
                    "status": "failed",
                    "output": "operation cancelled by user",
                },
            ),
            _codex_entry(
                "event_msg",
                "2026-07-20T00:00:07Z",
                {"type": "model_error", "message": "upstream model failed"},
            ),
            _codex_entry(
                "event_msg",
                "2026-07-20T00:00:08Z",
                {"type": "task_complete"},
            ),
        ]
        _write_jsonl(rollout, entries)

        result = trace_data.build_trace("codex", rollout)

        errors = [
            span
            for span in result["spans"]
            if span["status"] == "error" and span["kind"] != "SESSION"
        ]
        by_name = {span["name"]: span["error_type"] for span in errors}
        self.assertEqual(by_name["exec_command"], "command_error")
        timeout = next(span for span in errors if "deadline exceeded" in span["error_message"])
        interrupted = next(
            span for span in errors if "cancelled by user" in span["error_message"]
        )
        model = next(span for span in errors if span["name"] == "model error")
        self.assertEqual(timeout["error_type"], "timeout")
        self.assertEqual(interrupted["error_type"], "interrupted")
        self.assertEqual(model["error_type"], "model_error")
        self.assertEqual(result["summary"]["error_count"], len(errors))
        self.assertEqual(result["summary"]["status"], "error")

    def test_explicit_token_usage_is_exact_but_cost_remains_unknown(self) -> None:
        rollout = self.codex_root / "2026" / "07" / "20" / "rollout-usage.jsonl"
        _write_jsonl(
            rollout,
            [
                _codex_entry(
                    "event_msg",
                    "2026-07-20T00:00:00Z",
                    {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 120,
                                "cached_input_tokens": 40,
                                "output_tokens": 30,
                                "reasoning_tokens": 7,
                                "total_tokens": 157,
                            }
                        },
                    },
                )
            ],
        )

        result = trace_data.build_trace("codex", rollout)

        self.assertEqual(result["usage"]["tokens"]["quality"], "exact")
        self.assertEqual(result["usage"]["tokens"]["input_tokens"], 120)
        self.assertEqual(result["usage"]["tokens"]["cached_input_tokens"], 40)
        self.assertEqual(result["usage"]["tokens"]["output_tokens"], 30)
        self.assertEqual(result["usage"]["tokens"]["reasoning_tokens"], 7)
        self.assertEqual(result["usage"]["tokens"]["total_tokens"], 157)
        self.assertEqual(result["usage"]["cost"]["quality"], "unknown")
        self.assertIsNone(result["usage"]["cost"]["amount"])
        self.assertIsNone(result["usage"]["cost"]["currency"])

    def test_absent_usage_stays_unknown_instead_of_being_estimated(self) -> None:
        rollout = self.codex_root / "2026" / "07" / "20" / "rollout-no-usage.jsonl"
        _write_jsonl(
            rollout,
            [
                _codex_entry(
                    "event_msg",
                    "2026-07-20T00:00:00Z",
                    {"type": "task_started"},
                ),
                _codex_entry(
                    "event_msg",
                    "2026-07-20T00:00:01Z",
                    {"type": "task_complete"},
                ),
            ],
        )

        result = trace_data.build_trace("codex", rollout)

        self.assertEqual(result["usage"]["tokens"]["quality"], "unknown")
        for key in (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "cache_creation_input_tokens",
            "reasoning_tokens",
            "total_tokens",
        ):
            self.assertIsNone(result["usage"]["tokens"][key])
        self.assertEqual(result["usage"]["cost"]["quality"], "unknown")

    def test_new_error_and_agent_fields_are_redacted(self) -> None:
        rollout = self.codex_root / "2026" / "07" / "20" / "rollout-redaction.jsonl"
        _write_jsonl(
            rollout,
            [
                _codex_entry(
                    "event_msg",
                    "2026-07-20T00:00:00Z",
                    {"type": "task_started"},
                ),
                _codex_entry(
                    "event_msg",
                    "2026-07-20T00:00:01Z",
                    {
                        "type": "model_error",
                        "agent_name": "token=agent-private-value",
                        "message": (
                            "Bearer private-bearer-value at /srv/private/session "
                            "github_pat_abcdefghijklmnopqrstuvwxyz"
                        ),
                    },
                ),
                _codex_entry(
                    "event_msg",
                    "2026-07-20T00:00:02Z",
                    {"type": "task_complete"},
                ),
            ],
        )

        result = trace_data.build_trace("codex", rollout)
        serialized = json.dumps(result, ensure_ascii=False)

        for forbidden in (
            "agent-private-value",
            "private-bearer-value",
            "/srv/private/session",
            "github_pat_abcdefghijklmnopqrstuvwxyz",
        ):
            self.assertNotIn(forbidden, serialized)
        model_error = next(
            span for span in result["spans"] if span["name"] == "model error"
        )
        self.assertEqual(model_error["error_type"], "model_error")
        self.assertIn("[REDACTED]", serialized)
        self.assertIn("[PATH]", serialized)

    def test_span_cap_is_hard_and_sets_truncated(self) -> None:
        rollout = self.codex_root / "2026" / "07" / "20" / "rollout-many.jsonl"
        entries = [
            _codex_entry(
                "event_msg", "2026-07-20T00:00:00Z", {"type": "task_started"}
            )
        ]
        for index in range(20):
            entries.append(
                _codex_entry(
                    "response_item",
                    f"2026-07-20T00:00:{index + 1:02d}Z",
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": f"reply {index}"}],
                    },
                )
            )
        _write_jsonl(rollout, entries)

        result = trace_data.build_trace("codex", rollout, max_spans=4)

        self.assertEqual(len(result["spans"]), 4)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["summary"]["span_count"], 4)


class PathPolicyTest(_RootsTestCase):
    def test_adapter_registry_recognizes_supported_and_rejects_unknown_formats(self) -> None:
        self.assertEqual(set(trace_data.TRACE_ADAPTERS), {"claude-code", "codex"})
        for source, adapter in trace_data.TRACE_ADAPTERS.items():
            self.assertIsInstance(adapter, trace_data.TraceAdapter)
            self.assertEqual(adapter.source, source)
            self.assertTrue(callable(adapter.root_provider))
            self.assertTrue(callable(adapter.accepts_path))
            self.assertTrue(callable(adapter.parse))

        claude_file = self.claude_root / "project" / "session.jsonl"
        codex_wrong_format = self.codex_root / "2026" / "events.jsonl"
        _write_jsonl(claude_file, [])
        _write_jsonl(codex_wrong_format, [])

        with self.assertRaisesRegex(ValueError, "source must be"):
            trace_data.build_trace("unknown-agent", claude_file)
        with self.assertRaises(trace_data.TracePathError):
            trace_data.build_trace("codex", codex_wrong_format)

    def test_quality_summary_counts_every_join_confidence(self) -> None:
        builder = trace_data._Builder(session_key="quality-test", max_spans=20)
        root_id = builder.add(
            record_key="session",
            parent_id=None,
            kind="SESSION",
            name="test session",
            start_ms=0,
            incomplete=True,
        )
        self.assertIsNotNone(root_id)
        for index, quality in enumerate(
            ("structural", "semi", "heuristic", "orphan"), start=1
        ):
            builder.add(
                record_key=f"node-{quality}",
                parent_id=root_id,
                kind="AGENT_TURN",
                name=quality,
                start_ms=index,
                end_ms=index + 1,
                join_quality=quality,
                approx=quality in {"semi", "heuristic", "orphan"},
            )
        budget = trace_data._Budget()
        trace_data._finalize_root(builder, root_id, budget)

        result = trace_data._render_trace(
            "codex",
            "quality-session",
            builder,
            budget,
            trace_data._TokenUsage(),
        )

        self.assertEqual(
            result["quality"]["join_quality"],
            {"structural": 1, "semi": 1, "heuristic": 1, "orphan": 1},
        )
        self.assertEqual(result["quality"]["signals"]["approx"], 3)
        self.assertEqual(result["quality"]["time_quality"], "approximate")
        self.assertEqual(result["quality"]["completeness"], "closed")
        for label in ("structural", "semi", "heuristic", "orphan"):
            self.assertIn(label, result["quality"]["reasons"])
            self.assertTrue(result["quality"]["reasons"][label])

    def test_summary_truncation_does_not_create_a_synthetic_path(self) -> None:
        text = ("a" * (trace_data.MAX_SUMMARY_CHARS - 3)) + " / " + ("b" * 20)
        sanitized = trace_data._sanitize_text(text)
        self.assertFalse(trace_data._contains_sensitive_text(sanitized))
        self.assertLessEqual(len(sanitized), trace_data.MAX_SUMMARY_CHARS)

    def test_structured_nonzero_exit_fields_are_command_errors(self) -> None:
        for payload in (
            {"exit_code": 1},
            {"returncode": 2},
            {"result": {"return_code": 3}},
            {"output": {"code": 4}},
            {"success": False},
        ):
            with self.subTest(payload=payload):
                self.assertTrue(trace_data._output_is_error(payload, ""))
        self.assertFalse(trace_data._output_is_error({"exit_code": 0, "success": True}, ""))

    def test_extended_credentials_and_high_entropy_values_are_redacted(self) -> None:
        samples = (
            "Authorization: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==",
            "Cookie: session=topsecretvalue",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signaturevalue",
            "xox" + "b-123456789012-abcdefghijklmnopqrstuvwxyz",  # placeholder, split so scanners skip it
            "random=AbCDefghijklMNOPqrstUVWXyz0123456789+/=",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                sanitized = trace_data._sanitize_text(sample)
                self.assertNotEqual(sanitized, sample)
                self.assertFalse(trace_data._contains_sensitive_text(sanitized))

    def test_control_characters_cannot_split_a_secret_past_redaction(self) -> None:
        split_token = "sk-\x01abcdefghijklmnopqrstuvwxyz0123456789"
        sanitized = trace_data._sanitize_text(split_token)
        self.assertIn("[REDACTED]", sanitized)
        self.assertFalse(trace_data._contains_sensitive_text(sanitized))

    def test_outside_root_wrong_provider_and_non_rollout_are_rejected(self) -> None:
        outside = Path(self.tempdir.name) / "outside.jsonl"
        _write_jsonl(outside, [])
        claude_file = self.claude_root / "project" / "session.jsonl"
        codex_non_rollout = self.codex_root / "2026" / "not-a-rollout.jsonl"
        _write_jsonl(claude_file, [])
        _write_jsonl(codex_non_rollout, [])

        with self.assertRaises(trace_data.TracePathError):
            trace_data.build_trace("claude-code", outside)
        with self.assertRaises(trace_data.TracePathError):
            trace_data.build_trace("codex", claude_file)
        with self.assertRaises(trace_data.TracePathError):
            trace_data.build_trace("codex", codex_non_rollout)
        with self.assertRaises(ValueError):
            trace_data.build_trace("kimi-code", claude_file)

    def test_symlink_escape_is_rejected(self) -> None:
        outside = Path(self.tempdir.name) / "outside.jsonl"
        _write_jsonl(outside, [])
        link = self.claude_root / "project" / "session.jsonl"
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")

        with self.assertRaises(trace_data.TracePathError):
            trace_data.build_trace("claude-code", link)


if __name__ == "__main__":
    unittest.main()
