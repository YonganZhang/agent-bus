#!/usr/bin/env python3
"""Browser-side rendering of the live subagent panel."""

from pathlib import Path
import subprocess
import unittest


class FrontendSubagentPanelTest(unittest.TestCase):
    def test_panel_counts_escapes_and_ticks_only_running_rows(self) -> None:
        html = (Path(__file__).resolve().parents[2] / "dashboard" / "index.html").read_text(encoding="utf-8")
        start = html.index("    function agentStatusText(")
        end = html.index("\n    function renderBlock(", start)
        function_source = html[start:end]
        script = f"""
const escapeHtml = value => String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
{function_source}
const now = Date.now() / 1000;
const html = renderAgentsBlock({{ role: "agents", agents: [
  {{ type: "fast-worker", description: "Normalize <core>", status: "running", activity: "读取 SKILL.md", tool_calls: 3, started_at: now - 75, updated_at: now }},
  {{ type: "reviewer", description: "Review diff", status: "completed", activity: "已交差", tool_calls: 9, started_at: now - 700, updated_at: now - 100 }},
] }});
const expect = (cond, msg) => {{ if (!cond) throw new Error(msg + "\\n" + html); }};
expect(html.includes("子智能体 · 1 个运行中 / 共 2 个"), "header count");
expect(html.includes("Normalize &lt;core&gt;") && !html.includes("<core>"), "description escaped");
expect((html.match(/data-agent-started=/g) || []).length === 1, "only the running row ticks");
expect(html.includes("已运行 1 分 15 秒"), "running elapsed");
expect(html.includes("用时 10 分 0 秒"), "finished duration");
expect(renderAgentChip(null) === "", "no chip without agent");
"""
        result = subprocess.run(["node", "-e", script], check=False, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
