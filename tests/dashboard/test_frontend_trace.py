#!/usr/bin/env python3
"""Static regression checks for the Cards trace-view integration."""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2] / "dashboard"
HTML = ROOT / "index.html"


class FrontendTraceIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.source = HTML.read_text(encoding="utf-8")

    def test_inline_javascript_syntax(self) -> None:
        start = self.source.index("  <script>\n", self.source.index("trace_view.js"))
        end = self.source.index("\n  </script>", start)
        script = self.source[start + len("  <script>\n"):end]
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8") as fh:
            fh.write(script)
            fh.flush()
            result = subprocess.run(
                ["node", "--check", fh.name],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_trace_is_a_separate_on_demand_view(self) -> None:
        self.assertIn('id="traceSessionView"', self.source)
        self.assertIn('id="traceView"', self.source)
        self.assertIn('id="timeline"', self.source)
        self.assertIn('src="/cards/trace_view.js"', self.source)
        self.assertIn('href="/cards/trace_view.css"', self.source)
        self.assertIn("body.trace-open .timeline-wrap", self.source)
        self.assertIn("visibility: hidden", self.source)
        self.assertIn("pointer-events: none", self.source)
        self.assertIn(".detail > .timeline-wrap { grid-row: 3; grid-column: 1; }", self.source)
        trace_style = self.source[
            self.source.index("    .trace-session-view {"):
            self.source.index("    .trace-session-head {")
        ]
        self.assertIn("grid-column: 1", trace_style)

    def test_trace_request_has_exact_identity_and_race_suppression(self) -> None:
        start = self.source.index("    async function selectTracePane(")
        end = self.source.index("\n    function setTraceOpen(", start)
        function_source = self.source[start:end]
        self.assertIn("pane_pid: pane.pane_pid", function_source)
        self.assertIn("pane_start_time: pane.pane_start_time", function_source)
        self.assertIn("new AbortController()", function_source)
        self.assertIn("requestSeq !== state.traceRequestSeq", function_source)
        self.assertIn("tracePaneInstanceKey", function_source)
        self.assertIn("/api/trace?", function_source)
        self.assertIn('params.set("known_source_signature"', function_source)
        self.assertIn("data.unchanged", function_source)
        self.assertIn("traceViewController.update(next.trace)", function_source)
        self.assertIn("source_changed_during_parse", function_source)
        self.assertIn("wasCurrentInstance", function_source)
        self.assertIn("!wasCurrentInstance", function_source)
        self.assertIn("当前视图保持不变", function_source)

    def test_low_rate_poll_is_bound_to_open_exact_pane_and_stops(self) -> None:
        poll_start = self.source.index("    function clearTracePoll()")
        poll_end = self.source.index("\n    function rememberTraceScroll", poll_start)
        poll_source = self.source[poll_start:poll_end]
        self.assertIn("!state.traceOpen", poll_source)
        self.assertIn("state.traceCurrentKey !== instanceKey", poll_source)
        self.assertIn("document.hidden ? 15_000 : 5_000", poll_source)
        self.assertIn("tracePaneInstanceKey(item) === instanceKey", poll_source)
        close_start = self.source.index("    function setTraceOpen(")
        close_end = self.source.index("\n    function setSharedFilesOpen", close_start)
        self.assertIn("clearTracePoll();", self.source[close_start:close_end])

    def test_export_is_user_triggered_and_uses_sanitized_controller_payload(self) -> None:
        self.assertIn('id="traceExportHtml"', self.source)
        self.assertIn('id="traceExportNdjson"', self.source)
        export_start = self.source.index("    function exportCurrentTrace(")
        export_end = self.source.index("\n    function setTraceOpen", export_start)
        export_source = self.source[export_start:export_end]
        self.assertIn("traceViewController.export(format)", export_source)
        self.assertIn("new Blob([exported.content]", export_source)
        self.assertNotIn("fetch(", export_source)
        self.assertIn('exportCurrentTrace("html")', self.source)
        self.assertIn('exportCurrentTrace("ndjson")', self.source)

    def test_pane_reuse_invalidates_trace_and_cache_is_bounded_to_live_instances(self) -> None:
        start = self.source.index("    async function loadPanes(")
        end = self.source.index("\n    async function refreshPanesInBackground", start)
        load_source = self.source[start:end]
        self.assertIn("selectedInstanceBeforeLoad", load_source)
        self.assertIn("selectedInstanceAfterLoad !== selectedInstanceBeforeLoad", load_source)
        self.assertIn("liveTraceKeys", load_source)
        self.assertIn("state.traceByPaneInstance.delete(key)", load_source)

    def test_card_overview_uses_only_cached_trace_facts(self) -> None:
        start = self.source.index("    function traceOverviewForPane(")
        end = self.source.index("\n    async function loadPanes", start)
        overview = self.source[start:end]
        self.assertIn("summary.tool_count", overview)
        self.assertIn("summary.error_count", overview)
        self.assertIn("summary.incomplete_count", overview)
        self.assertIn("lastTurn?.duration_ms", overview)
        self.assertIn("按需读取", overview)

    def test_high_frequency_refresh_loop_never_fetches_trace(self) -> None:
        start = self.source.index("    async function refreshLoop()")
        end = self.source.index("\n    applyFontSize();", start)
        refresh_source = self.source[start:end]
        self.assertNotIn("/api/trace", refresh_source)
        self.assertNotIn("selectTracePane(", refresh_source)

    def test_switching_panes_refreshes_trace_without_blocking_capture(self) -> None:
        start = self.source.index("    async function switchToPane(")
        end = self.source.index("\n    // O(1)", start)
        switch_source = self.source[start:end]
        self.assertIn("if (state.traceOpen) selectTracePane(pane);", switch_source)
        self.assertNotIn("await selectTracePane", switch_source)
        self.assertIn("await loadCapture", switch_source)


if __name__ == "__main__":
    unittest.main()
