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


class NestedListTest(unittest.TestCase):
    def render(self, markdown: str) -> str:
        html = _index_html()
        start = html.index("    function renderMarkdown(text)")
        end = html.index("\n    }\n", start) + len("\n    }\n")
        script = f"""
const renderInlineMarkdown = (text) => String(text);
const escapeHtml = (text) => String(text);
const renderMarkdownCopyBox = (tag, inner) => `<${{tag}}>${{inner}}</${{tag}}>`;
const renderDenseInlineListIfAny = () => "";
const parseMarkdownTable = () => null;
const parseBoxBanner = () => null, parseBoxDrawingTable = () => null, collectDenseNameRun = () => null, renderDenseNameList = () => "";
{html[start:end]}
process.stdout.write(renderMarkdown({markdown!r}));
"""
        result = subprocess.run(["node", "-e", script], check=False, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return result.stdout

    def test_ordered_list_indented_under_a_bullet_stays_nested(self) -> None:
        # an indented 1..3 under a bullet used to escape to the top level
        # and turn the following bullets into sub-items of "3."
        html = self.render("- 全局技能：流程——\n  1. 读规则；\n  2. 看现状；\n  3. 汇报。\n- 项目规则文件：固定位置\n- 第一次归档：一次问完")
        self.assertEqual(html, "<ul><li>全局技能：流程——<ol><li>读规则；</li><li>看现状；</li><li>汇报。</li></ol></li>"
                               "<li>项目规则文件：固定位置</li><li>第一次归档：一次问完</li></ul>")

    def test_deeper_nesting_and_continuations(self) -> None:
        html = self.render("1. 第一步\n   - 细节 a\n     - 更细\n   - 细节 b\n     续一行\n2. 第二步")
        self.assertEqual(html, "<ol><li>第一步<ul><li>细节 a<ul><li>更细</li></ul></li><li>细节 b<p>续一行</p></li></ul></li>"
                               "<li>第二步</li></ol>")

    def test_flush_left_bullets_after_a_numbered_item_are_still_its_subitems(self) -> None:
        html = self.render("1. 做 A\n- 子项一\n- 子项二\n2. 做 B")
        self.assertEqual(html, "<ol><li>做 A<ul><li>子项一</li><li>子项二</li></ul></li><li>做 B</li></ol>")

    def test_plain_lists_are_unchanged(self) -> None:
        self.assertEqual(self.render("- a\n- b"), "<ul><li>a</li><li>b</li></ul>")
        self.assertEqual(self.render("1. a\n2. b\n\n正文"), "<ol><li>a</li><li>b</li></ol><p>正文</p>")
        self.assertEqual(self.render("- a\n1. b"), "<ul><li>a</li></ul><ol><li>b</li></ol>")


class MarkdownStyleTest(unittest.TestCase):
    def test_inline_code_and_tables_in_the_dark_theme(self) -> None:
        html = _index_html()
        code = html[html.index("    .markdown code {\n      border: 1px solid rgba(248, 248, 242, .14);"):]
        code = code[:code.index("}")]
        self.assertIn("box-decoration-break: clone;", code)  # a wrapped pill keeps its border on every line
        self.assertNotIn("solid #dfe4d8", html)               # no light-theme borders left in the dark theme
        cell = html[html.index("    .markdown th code,\n    .markdown td code {"):]
        cell = cell[:cell.index("}")]
        for rule in ("white-space: normal;", "word-break: normal;", "overflow-wrap: break-word;"):
            self.assertIn(rule, cell)


if __name__ == "__main__":
    unittest.main()
