#!/usr/bin/env python3
"""Regression tests for the browser-side Markdown renderer."""

from pathlib import Path
import re
import subprocess
import unittest


def _index_html() -> str:
    return (Path(__file__).resolve().parents[2] / "dashboard" / "index.html").read_text(encoding="utf-8")


def _function_source(html: str, name: str, end_marker: str) -> str:
    start = html.index(f"    function {name}")
    return html[start : html.index(end_marker, start)]


class FrontendMarkdownTest(unittest.TestCase):
    def test_terminal_wrapped_quote_lines_stay_one_paragraph(self) -> None:
        html = (Path(__file__).resolve().parents[2] / "dashboard" / "index.html").read_text(encoding="utf-8")
        start = html.index("    function renderMarkdown(text)")
        end = html.index("\n    function kindClass(", start)
        function_source = html[start:end]
        script = f"""
const renderInlineMarkdown = text => String(text);
const renderMarkdownCopyBox = (tag, innerHtml) => `<${{tag}}>${{innerHtml}}</${{tag}}>`;
const renderDenseInlineListIfAny = () => "";
const parseMarkdownTable = () => null;
const parseBoxBanner = () => null;
const parseBoxDrawingTable = () => null;
const collectDenseNameRun = () => null;
const renderDenseNameList = () => "";
{function_source}

const html = renderMarkdown(
  "> The example sentence is wrapped by the terminal\\n" +
  "> across two physical lines.\\n\\n" +
  "> 这是一段示例引用，被终端折成了两\\n" +
  "> 行，渲染时应该重新合并成一段。"
);
const paragraphs = [...html.matchAll(/<blockquote><p>/g)].length;
const wrappedParagraphs = [...html.matchAll(/<p>/g)].length;
if (paragraphs !== 2 || wrappedParagraphs !== 2) {{
  throw new Error(`terminal wraps became paragraphs: ${{html}}`);
}}
if (!html.includes("terminal across") || !html.includes("折成了两行")) {{
  throw new Error(`wrapped text was not reflowed: ${{html}}`);
}}

const explicitBreak = renderMarkdown(
  "> First paragraph line one\\n" +
  "> line two\\n" +
  ">\\n" +
  "> Second paragraph"
);
if ((explicitBreak.match(/<p>/g) || []).length !== 2) {{
  throw new Error(`explicit quote paragraph break was lost: ${{explicitBreak}}`);
}}
"""
        result = subprocess.run(
            ["node", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_directional_workflow_renders_vertically(self) -> None:
        html = _index_html()
        start = html.index("    function renderMarkdown(text)")
        end = html.index("\n    function kindClass(", start)
        function_source = html[start:end]
        script = f"""
const renderInlineMarkdown = text => String(text);
const escapeHtml = text => String(text);
const renderMarkdownCopyBox = (tag, innerHtml) => `<${{tag}}>${{innerHtml}}</${{tag}}>`;
const renderDenseInlineListIfAny = () => "";
const parseMarkdownTable = () => null;
const parseBoxBanner = () => null;
const parseBoxDrawingTable = () => null;
const collectDenseNameRun = () => null;
const renderDenseNameList = () => "";
{function_source}
const output = renderMarkdown(
  "第一步：准备示例数据\\n" +
  "↓\\n" +
  "第二步：实现示例功能 ← 当前\\n" +
  "↓\\n" +
  "第三步：补充测试\\n" +
  "↓\\n" +
  "第四步：整理文档"
);
if (!output.includes('class="terminal-flow"') || (output.match(/terminal-flow-step/g) || []).length !== 4)
  throw new Error(`directional workflow was not structured: ${{output}}`);
"""
        result = subprocess.run(["node", "-e", script], check=False, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


    def test_every_table_renders_inside_a_scroll_wrap(self) -> None:
        """A table must never be its own scroll box.

        A `<table>` that carries `overflow-x:auto` plus a `min-width` larger
        than the phone viewport cannot scroll (content box and scroll content
        are the same width) and overflows the card instead, which is how the
        right-hand columns became unreachable on Android.
        """
        html = _index_html()
        producers = re.findall(r"html: (?:wrapMarkdownTable\()?`<table", html)
        self.assertTrue(producers, "no table producers found")
        for producer in producers:
            self.assertIn("wrapMarkdownTable(", producer)
        self.assertIn('return `<div class="md-table-wrap">${tableHtml}</div>`;', html)

    def test_mobile_css_does_not_make_the_table_its_own_scroll_box(self) -> None:
        html = _index_html()
        mobile = html[html.index("    @media (max-width: 680px) {") :]
        mobile = mobile[: mobile.index("\n    .block.user .block-label")]
        table_rule_start = mobile.find("      .markdown table {")
        self.assertEqual(table_rule_start, -1, "mobile CSS re-added a bare `.markdown table` scroll box")
        self.assertIn(".markdown .md-table-wrap > table {", mobile)
        wrap_rule = html[html.index("    .markdown .md-table-wrap {") :]
        wrap_rule = wrap_rule[: wrap_rule.index("}")]
        self.assertIn("overflow-x: auto", wrap_rule)
        self.assertIn("max-width: 100%", wrap_rule)
        self.assertNotIn("min-width", wrap_rule)

    def test_horizontal_drag_inside_a_scrollable_block_does_not_switch_panes(self) -> None:
        """The pane-switch swipe must yield to a wide table or code block."""
        html = _index_html()
        self.assertIn("function horizontalScrollHost(target)", html)
        self.assertIn("function hostHasRoomFor(host, dx)", html)
        self.assertIn("scrollHost = horizontalScrollHost(event.target);", html)
        self.assertIn('if (axis === "x" && hostHasRoomFor(scrollHost, dx)) {', html)


if __name__ == "__main__":
    unittest.main()
