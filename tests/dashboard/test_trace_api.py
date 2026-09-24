#!/usr/bin/env python3
"""Regression tests for the on-demand Cards trace API integration."""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

import server


def _api_get(path: str) -> tuple[int, str, bytes]:
    old_authorized = server.Handler.authorized
    old_log_message = server.Handler.log_message
    server.Handler.authorized = lambda _handler: True
    server.Handler.log_message = lambda _handler, _fmt, *_args: None
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
        conn.request("GET", path)
        response = conn.getresponse()
        status = response.status
        content_type = response.getheader("Content-Type") or ""
        body = response.read()
        conn.close()
        return status, content_type, body
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        server.Handler.authorized = old_authorized
        server.Handler.log_message = old_log_message


def _pane(kind: str = "Codex") -> server.Pane:
    return server.Pane(
        pane_id="%41",
        target="secretary_web:7.0",
        session="secretary_web",
        window_index=7,
        pane_index=0,
        window_name="trace-test",
        command=kind.lower(),
        cwd="/work/test",
        title="trace-test",
        active=True,
        kind=kind,
        project="test",
        preview="",
        status="idle",
        pane_pid="4242",
        pane_start_time="4242:99",
    )


class TraceResponseForPaneTest(unittest.TestCase):
    def setUp(self) -> None:
        self.old_pane_by_id = server.pane_by_id
        self.old_codex_resolver = server.codex_rollout_path_for_pane_id
        self.old_claude_resolver = server.transcript_path_for_pane_id
        self.old_shared = server._shared_live_transcript_panes
        self.old_build_trace = server.trace_data.build_trace
        self.old_agent_identity = server._compute_pane_agent_process_identity
        server._compute_pane_agent_process_identity = (
            lambda pane: f"agent:{pane.pane_pid}:{pane.pane_start_time}"
        )
        server.TRACE_CACHE.clear()

    def tearDown(self) -> None:
        server.pane_by_id = self.old_pane_by_id
        server.codex_rollout_path_for_pane_id = self.old_codex_resolver
        server.transcript_path_for_pane_id = self.old_claude_resolver
        server._shared_live_transcript_panes = self.old_shared
        server.trace_data.build_trace = self.old_build_trace
        server._compute_pane_agent_process_identity = self.old_agent_identity
        server.TRACE_CACHE.clear()

    def test_codex_trace_uses_exact_resolved_rollout_and_bounds_span_limit(self) -> None:
        pane = _pane("Codex")
        calls: list[tuple[str, str, int]] = []
        server.pane_by_id = lambda _pane_id: pane
        server.codex_rollout_path_for_pane_id = lambda _pane_id: (
            "/safe/rollout.jsonl",
            {"reason": "", "match_quality": "structural"},
        )
        server.trace_data.build_trace = lambda source, path, max_spans: (
            calls.append((source, path, max_spans))
            or {"trace_id": "trace-1", "spans": [], "summary": {}}
        )

        status, payload = server.trace_response_for_pane(
            pane.pane_id,
            expected_pid=pane.pane_pid,
            expected_start_time=pane.pane_start_time,
            max_spans=999_999,
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["trace"]["trace_id"], "trace-1")
        self.assertTrue(payload["trace"]["identity"]["exact"])
        self.assertEqual(payload["trace"]["identity"]["pane_target"], pane.target)
        self.assertFalse(payload["trace"]["identity"]["absolute_path_exposed"])
        self.assertNotIn("/safe/rollout.jsonl", json.dumps(payload))
        self.assertEqual(calls, [("codex", "/safe/rollout.jsonl", server.TRACE_MAX_SPANS)])

    def test_known_source_signature_returns_small_unchanged_receipt(self) -> None:
        pane = _pane("Codex")
        calls: list[str] = []
        server.pane_by_id = lambda _pane_id: pane
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl") as fh:
            fh.write('{"type":"session_meta"}\n')
            fh.flush()
            server.codex_rollout_path_for_pane_id = lambda _pane_id: (fh.name, {"reason": ""})
            server.trace_data.build_trace = lambda *_args, **_kwargs: (
                calls.append("build")
                or {"session_id": "trace-session", "spans": [], "summary": {}}
            )
            status, first = server.trace_response_for_pane(
                pane.pane_id,
                expected_pid=pane.pane_pid,
                expected_start_time=pane.pane_start_time,
            )
            self.assertEqual(status, 200)
            self.assertFalse(first["unchanged"])
            self.assertEqual(calls, ["build"])

            server.trace_data.build_trace = lambda *_args, **_kwargs: calls.append("unexpected")
            status, second = server.trace_response_for_pane(
                pane.pane_id,
                expected_pid=pane.pane_pid,
                expected_start_time=pane.pane_start_time,
                known_source_signature=str(first["source_signature"]),
            )

        self.assertEqual(status, 200)
        self.assertTrue(second["available"])
        self.assertTrue(second["unchanged"])
        self.assertNotIn("trace", second)
        self.assertEqual(second["source_signature"], first["source_signature"])
        self.assertEqual(second["identity"]["pane_instance_quality"], "exact")
        self.assertEqual(calls, ["build"])

    def test_log_append_changes_content_signature_not_session_identity(self) -> None:
        pane = _pane("Codex")
        calls: list[str] = []
        server.pane_by_id = lambda _pane_id: pane
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl") as fh:
            fh.write('{"type":"session_meta"}\n')
            fh.flush()
            server.codex_rollout_path_for_pane_id = lambda _pane_id: (
                fh.name,
                {"reason": "", "match_quality": "structural"},
            )
            server.trace_data.build_trace = lambda *_args, **_kwargs: (
                calls.append("build")
                or {"session_id": "stable-session", "spans": [], "summary": {}}
            )
            status, first = server.trace_response_for_pane(
                pane.pane_id,
                expected_pid=pane.pane_pid,
                expected_start_time=pane.pane_start_time,
            )
            fh.write('{"type":"event_msg","payload":{"type":"task_started"}}\n')
            fh.flush()
            status_after, second = server.trace_response_for_pane(
                pane.pane_id,
                expected_pid=pane.pane_pid,
                expected_start_time=pane.pane_start_time,
            )

        self.assertEqual((status, status_after), (200, 200))
        self.assertNotEqual(first["source_signature"], second["source_signature"])
        self.assertEqual(first["session_signature"], second["session_signature"])
        self.assertEqual(calls, ["build", "build"])

    def test_stale_pane_identity_is_rejected_before_resolving_a_log(self) -> None:
        pane = _pane("Codex")
        resolver_calls: list[str] = []
        server.pane_by_id = lambda _pane_id: pane
        server.codex_rollout_path_for_pane_id = lambda pane_id: (
            resolver_calls.append(pane_id) or "/should/not/read",
            {},
        )

        status, payload = server.trace_response_for_pane(
            pane.pane_id,
            expected_pid="old-pid",
            expected_start_time=pane.pane_start_time,
        )

        self.assertEqual(status, 409)
        self.assertEqual(payload["reason"], "stale-pane-instance")
        self.assertEqual(resolver_calls, [])

    def test_complete_identity_is_required(self) -> None:
        pane = _pane("Codex")
        server.pane_by_id = lambda _pane_id: pane
        for pid, start in (("", ""), (pane.pane_pid, ""), ("", pane.pane_start_time)):
            with self.subTest(pid=pid, start=start):
                status, payload = server.trace_response_for_pane(
                    pane.pane_id,
                    expected_pid=pid,
                    expected_start_time=start,
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["reason"], "pane-identity-required")

    def test_pane_reuse_during_parse_discards_the_old_trace(self) -> None:
        old_pane = _pane("Codex")
        new_pane = _pane("Codex")
        new_pane.pane_pid = "5252"
        lookups = 0

        def pane_lookup(_pane_id):
            nonlocal lookups
            lookups += 1
            return old_pane if lookups == 1 else new_pane

        server.pane_by_id = pane_lookup
        server.codex_rollout_path_for_pane_id = lambda _pane_id: ("/safe/rollout.jsonl", {})
        server.trace_data.build_trace = lambda *_args, **_kwargs: {
            "session_id": "old-session",
            "spans": [],
            "summary": {},
        }
        status, payload = server.trace_response_for_pane(
            old_pane.pane_id,
            expected_pid=old_pane.pane_pid,
            expected_start_time=old_pane.pane_start_time,
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["reason"], "stale-pane-instance")

    def test_agent_restart_inside_same_shell_discards_old_trace(self) -> None:
        pane = _pane("Codex")
        identities = iter(["agent-old", "agent-new"])
        server._compute_pane_agent_process_identity = lambda _pane: next(identities)
        server.pane_by_id = lambda _pane_id: pane
        server.codex_rollout_path_for_pane_id = lambda _pane_id: (
            "/safe/rollout.jsonl",
            {"match_quality": "structural"},
        )
        server.trace_data.build_trace = lambda *_args, **_kwargs: {
            "session_id": "old-session",
            "spans": [],
            "summary": {},
        }

        status, payload = server.trace_response_for_pane(
            pane.pane_id,
            expected_pid=pane.pane_pid,
            expected_start_time=pane.pane_start_time,
        )

        self.assertEqual(status, 409)
        self.assertEqual(payload["reason"], "stale-pane-instance")

    def test_shared_claude_transcript_fails_closed(self) -> None:
        pane = _pane("Claude")
        build_calls: list[str] = []
        server.pane_by_id = lambda _pane_id: pane
        server.transcript_path_for_pane_id = lambda _pane_id: "/safe/session.jsonl"
        server._shared_live_transcript_panes = lambda _path, _pane_id: ["%99"]
        server.trace_data.build_trace = lambda *_args, **_kwargs: build_calls.append("called")

        status, payload = server.trace_response_for_pane(
            pane.pane_id,
            expected_pid=pane.pane_pid,
            expected_start_time=pane.pane_start_time,
        )

        self.assertEqual(status, 200)
        self.assertFalse(payload["available"])
        self.assertEqual(payload["reason"], "shared-transcript")
        self.assertEqual(build_calls, [])

    def test_claude_sharing_that_begins_during_parse_is_rechecked(self) -> None:
        pane = _pane("Claude")
        shared_checks = 0
        server.pane_by_id = lambda _pane_id: pane
        server.transcript_path_for_pane_id = lambda _pane_id: "/safe/session.jsonl"

        def shared(_path, _pane_id):
            nonlocal shared_checks
            shared_checks += 1
            return [] if shared_checks == 1 else ["%99"]

        server._shared_live_transcript_panes = shared
        server.trace_data.build_trace = lambda *_args, **_kwargs: {
            "session_id": "session-safe",
            "spans": [],
            "summary": {},
        }
        status, payload = server.trace_response_for_pane(
            pane.pane_id,
            expected_pid=pane.pane_pid,
            expected_start_time=pane.pane_start_time,
        )
        self.assertEqual(status, 200)
        self.assertFalse(payload["available"])
        self.assertEqual(payload["reason"], "shared-transcript")
        self.assertEqual(shared_checks, 2)

    def test_unsupported_pane_and_missing_log_are_normal_empty_states(self) -> None:
        pane = _pane("Shell")
        server.pane_by_id = lambda _pane_id: pane
        status, payload = server.trace_response_for_pane(
            "%41",
            expected_pid=pane.pane_pid,
            expected_start_time=pane.pane_start_time,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["reason"], "unsupported-pane-kind")

        pane = _pane("Codex")
        server.pane_by_id = lambda _pane_id: pane
        server.codex_rollout_path_for_pane_id = lambda _pane_id: (None, {"reason": "anything"})
        status, payload = server.trace_response_for_pane(
            "%41",
            expected_pid=pane.pane_pid,
            expected_start_time=pane.pane_start_time,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["reason"], "no-provider-log")

        server.codex_rollout_path_for_pane_id = lambda _pane_id: (
            None,
            {"reason": "ambiguous-rollout"},
        )
        status, payload = server.trace_response_for_pane(
            "%41",
            expected_pid=pane.pane_pid,
            expected_start_time=pane.pane_start_time,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["reason"], "ambiguous-rollout")

    def test_parser_error_does_not_leak_exception_text_or_path(self) -> None:
        pane = _pane("Codex")
        secret_path = "/home/user/.codex/sessions/secret-token.jsonl"
        server.pane_by_id = lambda _pane_id: pane
        server.codex_rollout_path_for_pane_id = lambda _pane_id: (secret_path, {})

        def fail(*_args, **_kwargs):
            raise ValueError(f"bad payload in {secret_path}: sk-secret")

        server.trace_data.build_trace = fail
        status, payload = server.trace_response_for_pane(
            pane.pane_id,
            expected_pid=pane.pane_pid,
            expected_start_time=pane.pane_start_time,
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["reason"], "trace-parse-failed")
        self.assertNotIn(secret_path, str(payload))
        self.assertNotIn("sk-secret", str(payload))


class TraceHttpRouteTest(unittest.TestCase):
    def test_static_assets_use_exact_allowlisted_routes_and_types(self) -> None:
        status, content_type, body = _api_get("/cards/trace_view.js")
        self.assertEqual(status, 200)
        self.assertIn("application/javascript", content_type)
        self.assertIn(b"CardsTraceView", body)

        status, content_type, body = _api_get("/cards/trace_view.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", content_type)
        self.assertIn(b".ctv", body)

        status, _content_type, _body = _api_get("/cards/trace_view.txt")
        self.assertEqual(status, 404)

    def test_trace_route_requires_complete_identity(self) -> None:
        old_pane_by_id = server.pane_by_id
        pane = _pane("Codex")
        server.pane_by_id = lambda _pane_id: pane
        try:
            status, content_type, body = _api_get("/cards/api/trace?pane=%2541")
        finally:
            server.pane_by_id = old_pane_by_id
        self.assertEqual(status, 400)
        self.assertIn("application/json", content_type)
        self.assertEqual(json.loads(body)["reason"], "pane-identity-required")


if __name__ == "__main__":
    unittest.main()
