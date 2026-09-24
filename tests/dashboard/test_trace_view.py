import json
import subprocess
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2] / "dashboard"
JS = ROOT / "trace_view.js"
CSS = ROOT / "trace_view.css"


def run_node(source):
    completed = subprocess.run(
        ["node", "-e", source],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


class TraceViewTests(unittest.TestCase):
    def test_javascript_syntax(self):
        subprocess.run(["node", "--check", str(JS)], check=True, capture_output=True, text=True)

    def test_normalizes_nested_and_flat_spans(self):
        script = textwrap.dedent(
            """
            require("./trace_view.js");
            const t = globalThis.CardsTraceView._test;
            const model = t.normalizeTrace({
              id: "trace-9",
              title: "Agent run",
              spans: [
                {
                  id: "root",
                  type: "turn",
                  start_ms: 100,
                  duration_ms: 100,
                  children: [
                    { id: "model", type: "llm", start_ms: 110, end_ms: 150 },
                    { id: "tool", type: "tool_call", start_ms: 155, duration_ms: 20 }
                  ]
                },
                {
                  id: "worker",
                  parent_id: "root",
                  type: "agent",
                  status: "failed",
                  start_ms: 180,
                  end_ms: 195,
                  heuristic: true
                }
              ]
            });
            process.stdout.write(JSON.stringify({
              roots: model.roots,
              count: model.nodes.length,
              workerParent: model.byId.worker.parentId,
              workerDepth: model.byId.worker.depth,
              workerError: model.byId.worker.isError,
              badges: model.byId.worker.badges,
              summary: model.summary,
              duration: model.duration
            }));
            """
        )
        result = json.loads(run_node(script))
        self.assertEqual(result["roots"], ["root"])
        self.assertEqual(result["count"], 4)
        self.assertEqual(result["workerParent"], "root")
        self.assertEqual(result["workerDepth"], 1)
        self.assertTrue(result["workerError"])
        self.assertIn("heuristic", result["badges"])
        self.assertEqual(
            result["summary"],
            {"turn": 1, "llm": 1, "tool": 1, "agent": 1, "error": 1},
        )
        self.assertEqual(result["duration"], 100)

    def test_missing_parent_and_inferred_timing_are_badged(self):
        script = textwrap.dedent(
            """
            require("./trace_view.js");
            const model = globalThis.CardsTraceView._test.normalizeTrace({
              nodes: [{
                id: "orphan",
                parent_id: "missing",
                kind: "tool",
                start: 10,
                duration: 5
              }]
            });
            process.stdout.write(JSON.stringify({
              root: model.roots[0],
              parent: model.byId.orphan.parentId,
              end: model.byId.orphan.end,
              badges: model.byId.orphan.badges
            }));
            """
        )
        result = json.loads(run_node(script))
        self.assertEqual(result["root"], "orphan")
        self.assertIsNone(result["parent"])
        self.assertEqual(result["end"], 15)
        self.assertCountEqual(result["badges"], ["approx", "detached"])

    def test_cards_parser_shape_exposes_summaries_and_quality(self):
        script = textwrap.dedent(
            """
            require("./trace_view.js");
            const model = globalThis.CardsTraceView._test.normalizeTrace({
              source: "claude-code",
              session_id: "session-safe",
              summary: {
                turn_count: 4,
                llm_count: 3,
                tool_count: 2,
                error_count: 1,
                duration_ms: 1200,
                status: "incomplete"
              },
              spans: [{
                id: "child-turn",
                kind: "AGENT_TURN",
                name: "agent turn",
                input_summary: "delegated work",
                output_summary: "done",
                join_quality: "orphan"
              }]
            });
            process.stdout.write(JSON.stringify({
              id: model.id,
              title: model.title,
              status: model.status,
              duration: model.duration,
              summary: model.summary,
              node: model.nodes[0]
            }));
            """
        )
        result = json.loads(run_node(script))
        self.assertEqual(result["id"], "session-safe")
        self.assertEqual(result["title"], "claude-code session")
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["duration"], 1200)
        self.assertEqual(
            result["summary"],
            {"turn": 4, "llm": 3, "tool": 2, "agent": 1, "error": 1},
        )
        self.assertEqual(result["node"]["input"], "delegated work")
        self.assertEqual(result["node"]["output"], "done")
        self.assertIn("heuristic", result["node"]["badges"])

    def test_filter_keeps_context_ancestors(self):
        script = textwrap.dedent(
            """
            require("./trace_view.js");
            const t = globalThis.CardsTraceView._test;
            const model = t.normalizeTrace({
              nodes: [{
                id: "turn",
                type: "turn",
                children: [{
                  id: "model",
                  type: "llm",
                  children: [{ id: "tool", type: "tool" }]
                }]
              }]
            });
            const rows = t.flattenVisible(model, "tool", new Set());
            process.stdout.write(JSON.stringify(rows.map(row => ({
              id: row.node.id,
              context: row.context
            }))));
            """
        )
        result = json.loads(run_node(script))
        self.assertEqual(
            result,
            [
                {"id": "turn", "context": True},
                {"id": "model", "context": True},
                {"id": "tool", "context": False},
            ],
        )

    def test_relative_duration_geometry(self):
        script = textwrap.dedent(
            """
            require("./trace_view.js");
            const geometry = globalThis.CardsTraceView._test.barGeometry(
              { start: 125, end: 150, duration: 25 },
              { minStart: 100, maxEnd: 200, maxDuration: 100 }
            );
            process.stdout.write(JSON.stringify(geometry));
            """
        )
        self.assertEqual(json.loads(run_node(script)), {"left": 25, "width": 25})

    def test_search_reports_each_supported_hit_field(self):
        script = textwrap.dedent(
            """
            require("./trace_view.js");
            const t = globalThis.CardsTraceView._test;
            const model = t.normalizeTrace({
              nodes: [
                { id: "name", name: "needle-name", kind: "turn" },
                { id: "input", name: "input span", kind: "turn", input: "needle-input" },
                { id: "output", name: "output span", kind: "llm", output: "needle-output" },
                { id: "tool", name: "needle-tool", kind: "tool_call" },
                { id: "error", name: "failed span", kind: "turn", error_message: "needle-error" },
                { id: "agent", name: "worker span", kind: "agent", agent_name: "needle-agent" }
              ]
            });
            const queries = {
              name: "needle-name",
              input: "needle-input",
              output: "needle-output",
              tool: "needle-tool",
              error: "needle-error",
              agent: "needle-agent"
            };
            const result = {};
            Object.keys(queries).forEach(field => {
              const matches = t.searchTrace(model, queries[field]);
              result[field] = matches.length ? matches[0] : null;
            });
            process.stdout.write(JSON.stringify(result));
            """
        )
        result = json.loads(run_node(script))
        for field, match in result.items():
            self.assertIsNotNone(match, msg=f"{field} was not searchable")
            self.assertIn(field, match["fields"])

    def test_error_path_contains_ancestors_but_not_unrelated_siblings(self):
        script = textwrap.dedent(
            """
            require("./trace_view.js");
            const t = globalThis.CardsTraceView._test;
            const model = t.normalizeTrace({
              nodes: [{
                id: "root",
                kind: "turn",
                children: [
                  {
                    id: "middle",
                    kind: "llm",
                    children: [{ id: "failed", kind: "tool", status: "failed" }]
                  },
                  { id: "sibling", kind: "tool" }
                ]
              }]
            });
            const errors = model.nodes.filter(node => node.isError).map(node => node.id);
            const path = t.pathIds(model, errors);
            process.stdout.write(JSON.stringify({
              errors,
              path: Object.keys(path).sort()
            }));
            """
        )
        result = json.loads(run_node(script))
        self.assertEqual(result["errors"], ["failed"])
        self.assertEqual(result["path"], ["failed", "middle", "root"])

    def test_swimlanes_group_by_agent_and_keep_heuristic_quality(self):
        script = textwrap.dedent(
            """
            require("./trace_view.js");
            const t = globalThis.CardsTraceView._test;
            const model = t.normalizeTrace({
              nodes: [
                { id: "main", kind: "turn", start_ms: 0, duration_ms: 100 },
                {
                  id: "claude",
                  kind: "agent",
                  agent_name: "Claude",
                  start_ms: 10,
                  duration_ms: 50,
                  join_quality: "heuristic",
                  children: [{
                    id: "claude-tool",
                    kind: "tool",
                    start_ms: 20,
                    duration_ms: 10
                  }]
                },
                {
                  id: "codex",
                  kind: "agent",
                  agent_name: "Codex",
                  start_ms: 15,
                  duration_ms: 40
                }
              ]
            });
            const lanes = t.buildSwimlanes(model);
            process.stdout.write(JSON.stringify(lanes.map(lane => ({
              name: lane.name,
              ids: lane.blocks.map(block => block.id),
              heuristic: lane.blocks
                .filter(block => block.badges.includes("heuristic"))
                .map(block => block.id)
            }))));
            """
        )
        lanes = json.loads(run_node(script))
        by_name = {lane["name"]: lane for lane in lanes}
        self.assertEqual(set(by_name), {"主智能体", "Claude", "Codex"})
        self.assertEqual(by_name["主智能体"]["ids"], ["main"])
        self.assertEqual(by_name["Claude"]["ids"], ["claude", "claude-tool"])
        self.assertEqual(by_name["Claude"]["heuristic"], ["claude"])
        self.assertEqual(by_name["Codex"]["ids"], ["codex"])

    def test_html_and_ndjson_exports_are_bounded_safe_shapes(self):
        script = textwrap.dedent(
            r"""
            require("./trace_view.js");
            const t = globalThis.CardsTraceView._test;
            const payload = '<img src=x onerror="alert(1)"> & "quoted"';
            const model = t.normalizeTrace({
              id: "safe-session",
              title: "<unsafe-title>",
              nodes: [{
                id: "span-1",
                kind: "llm",
                agent_name: "Codex",
                output: payload
              }]
            });
            const html = t.exportTrace(model, "html");
            const ndjson = t.exportTrace(model, "ndjson");
            const lines = ndjson.content.trim().split("\n").map(JSON.parse);
            process.stdout.write(JSON.stringify({
              htmlMime: html.mime,
              htmlName: html.filename,
              rawTagInHtml: html.content.includes(payload),
              escapedTagInHtml: html.content.includes("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;"),
              scriptTagInHtml: /<script\\b/i.test(html.content),
              ndjsonMime: ndjson.mime,
              ndjsonName: ndjson.filename,
              lineCount: lines.length,
              metadataType: lines[0].type,
              spanType: lines[1].type,
              spanOutput: lines[1].output
            }));
            """
        )
        result = json.loads(run_node(script))
        self.assertEqual(result["htmlMime"], "text/html;charset=utf-8")
        self.assertEqual(result["htmlName"], "trace-safe-session.html")
        self.assertFalse(result["rawTagInHtml"])
        self.assertTrue(result["escapedTagInHtml"])
        self.assertFalse(result["scriptTagInHtml"])
        self.assertEqual(result["ndjsonMime"], "application/x-ndjson;charset=utf-8")
        self.assertEqual(result["ndjsonName"], "trace-safe-session.ndjson")
        self.assertEqual(result["lineCount"], 2)
        self.assertEqual(result["metadataType"], "trace")
        self.assertEqual(result["spanType"], "span")
        self.assertEqual(
            result["spanOutput"],
            '<img src=x onerror="alert(1)"> & "quoted"',
        )

    def test_export_refuses_payload_that_still_looks_like_a_credential(self):
        script = textwrap.dedent(
            """
            require("./trace_view.js");
            const t = globalThis.CardsTraceView._test;
            const model = t.normalizeTrace({
              id: "unsafe-session",
              nodes: [{
                id: "span-1",
                kind: "tool",
                output: "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456"
              }]
            });
            let result = { refused: false, message: "" };
            try {
              t.exportTrace(model, "ndjson");
            } catch (error) {
              result = { refused: true, message: String(error.message || error) };
            }
            process.stdout.write(JSON.stringify(result));
            """
        )
        result = json.loads(run_node(script))
        self.assertTrue(result["refused"])
        self.assertIn("拒绝", result["message"])

    def test_two_thousand_node_normalization_and_search_stay_responsive(self):
        script = textwrap.dedent(
            """
            require("./trace_view.js");
            const { performance } = require("node:perf_hooks");
            const t = globalThis.CardsTraceView._test;
            const nodes = Array.from({ length: 2000 }, (_, index) => ({
              id: "span-" + index,
              parent_id: index ? "span-" + (index - 1) : null,
              kind: index % 5 === 0 ? "tool" : "llm",
              name: "performance needle " + index,
              start_ms: index,
              duration_ms: 1
            }));
            const started = performance.now();
            const model = t.normalizeTrace({ id: "large", nodes });
            const normalized = performance.now();
            const matches = t.searchTrace(model, "performance needle");
            const searched = performance.now();
            process.stdout.write(JSON.stringify({
              count: model.nodes.length,
              matchCount: matches.length,
              normalizeMs: normalized - started,
              searchMs: searched - normalized,
              totalMs: searched - started
            }));
            """
        )
        result = json.loads(run_node(script))
        self.assertEqual(result["count"], 2000)
        self.assertEqual(result["matchCount"], 500)
        self.assertLess(result["totalMs"], 5000)

    def test_public_contract_and_data_safety(self):
        source = JS.read_text(encoding="utf-8")
        self.assertIn("global.CardsTraceView", source)
        for method in (
            "loading",
            "empty",
            "error",
            "render",
            "update",
            "export",
            "getState",
            "restoreState",
            "clear",
            "destroy",
        ):
            self.assertIn(f"{method}:", source)
        self.assertNotIn(".innerHTML", source)
        self.assertNotIn("fetch(", source)
        self.assertNotIn("Timeline", source)
        self.assertNotIn("Composer", source)
        self.assertIn(".textContent", source)

    def test_css_is_namespaced_and_responsive(self):
        source = CSS.read_text(encoding="utf-8")
        self.assertIn("@media (max-width: 920px)", source)
        self.assertIn("@media (prefers-reduced-motion: reduce)", source)
        self.assertIn(":focus-visible", source)
        self.assertIn(".ctv-duration-bar", source)
        selectors = [
            line.strip()
            for line in source.splitlines()
            if line.strip().endswith("{") and not line.strip().startswith("@")
        ]
        self.assertTrue(selectors)
        for selector in selectors:
            self.assertTrue(
                selector.startswith(".ctv") or selector.startswith("to "),
                msg=f"Unscoped selector: {selector}",
            )


if __name__ == "__main__":
    unittest.main()
