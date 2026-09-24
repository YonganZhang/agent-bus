#!/usr/bin/env python3
"""Regression tests for the browser-side AI-reply media (image/video) renderer.

Covers URL extraction edge cases in extractAiMedia()/aiMediaKind() (Markdown
images, bare URLs, query-string filename hints, CJK punctuation directly
following a URL with no whitespace, dedup, the 8-item cap). Follows the same
"slice the real function source out of index.html and run it under node"
approach as test_frontend_markdown.py so these tests exercise the exact code
the browser loads, not a reimplementation.

There is deliberately no host-based SSRF blocklist (localhost/RFC1918/
link-local); only the protocol allowlist (http/https only) applies.
"""

from pathlib import Path
import json
import subprocess
import unittest


# What server.render_index() injects in production (see frontend_config()).
FRONTEND_CONFIG = {
    "localArtifactRoot": "/srv/workspace/",
    "publicShareHost": "share.example.com",
    "publicShareDir": "share-public",
}


def _media_function_source() -> str:
    html = (Path(__file__).resolve().parents[2] / "dashboard" / "index.html").read_text(encoding="utf-8")
    start = html.index("    function renderInlineMarkdown(value)")
    end = html.index("\n    const MARKDOWN_BOX_COPY_THRESHOLD", start)
    return html[start:end]


def _run(js_assertions: str) -> subprocess.CompletedProcess:
    script = (
        "globalThis.AGENT_BUS_CONFIG = " + json.dumps(FRONTEND_CONFIG) + ";\n"
        "function escapeHtml(s) { return String(s); }\n"
        "const BASE = '/cards';\n"
        + _media_function_source()
        + "\n"
        + js_assertions
    )
    return subprocess.run(
        ["node", "-e", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


class ExtractAiMediaTest(unittest.TestCase):
    def _assert_urls(self, text: str, expected_urls) -> None:
        js = (
            "const items = extractAiMedia(" + json.dumps(text) + ");\n"
            "const urls = items.map((i) => i.url);\n"
            "const expected = " + json.dumps(expected_urls) + ";\n"
            "if (JSON.stringify(urls) !== JSON.stringify(expected)) {\n"
            "  throw new Error('got ' + JSON.stringify(urls) + ' expected ' + JSON.stringify(expected));\n"
            "}\n"
        )
        result = _run(js)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_markdown_image_is_extracted(self) -> None:
        self._assert_urls(
            "![figure](https://example.com/a.png)",
            ["https://example.com/a.png"],
        )

    def test_bare_url_is_extracted(self) -> None:
        self._assert_urls(
            "结果图：https://example.com/a.jpg 看一下",
            ["https://example.com/a.jpg"],
        )

    def test_filename_query_param_is_recognized(self) -> None:
        self._assert_urls(
            "https://example.com/file?filename=figure.png",
            ["https://example.com/file?filename=figure.png"],
        )

    def test_extension_survives_trailing_query(self) -> None:
        self._assert_urls(
            "https://example.com/a.png?token=xyz",
            ["https://example.com/a.png?token=xyz"],
        )

    def test_chinese_punctuation_tail_is_stripped(self) -> None:
        self._assert_urls(
            "看这张图：https://example.com/a.png，谢谢",
            ["https://example.com/a.png"],
        )

    def test_parenthesized_media_urls_are_not_joined_with_following_labels(self) -> None:
        self._assert_urls(
            "截图：\n"
            "首页 (https://share.example.com/demo/home.png) 市场 "
            "(https://share.example.com/demo/market.png)",
            [
                "https://share.example.com/demo/home.png",
                "https://share.example.com/demo/market.png",
            ],
        )

    def test_fenced_code_block_is_ignored(self) -> None:
        self._assert_urls(
            "```\nhttps://example.com/a.png\n```",
            [],
        )

    def test_inline_code_is_ignored(self) -> None:
        self._assert_urls(
            "见 `https://example.com/a.png`",
            [],
        )

    def test_duplicate_url_collapses_to_one(self) -> None:
        self._assert_urls(
            "https://example.com/a.png 再看一次 https://example.com/a.png",
            ["https://example.com/a.png"],
        )

    def test_duplicate_video_url_creates_only_one_media_item(self) -> None:
        self._assert_urls(
            "https://example.com/demo.mp4 再贴一次 https://example.com/demo.mp4",
            ["https://example.com/demo.mp4"],
        )

    def test_local_server_video_path_is_mapped_to_authenticated_preview(self) -> None:
        self._assert_urls(
            "/srv/workspace/projects/demo/final.mp4",
            ["http://cards.test/cards/local-files/preview/workspace/projects/demo/final.mp4"],
        )

    def test_more_than_eight_urls_caps_at_eight(self) -> None:
        urls = [f"https://example.com/{i}.png" for i in range(12)]
        text = " ".join(urls)
        js = (
            "const items = extractAiMedia(" + json.dumps(text) + ");\n"
            "if (items.length !== 8) throw new Error('expected 8 got ' + items.length);\n"
        )
        result = _run(js)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


class AiMediaKindTest(unittest.TestCase):
    def _assert_kind(self, url: str, expected_kind: str) -> None:
        js = (
            "const kind = aiMediaKind(" + json.dumps(url) + ");\n"
            "if (kind !== " + json.dumps(expected_kind) + ") {\n"
            "  throw new Error('got ' + JSON.stringify(kind) + ' expected ' + " + json.dumps(expected_kind) + ");\n"
            "}\n"
        )
        result = _run(js)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_file_protocol_rejected(self) -> None:
        self._assert_kind("file:///etc/passwd", "")

    def test_data_protocol_rejected(self) -> None:
        self._assert_kind("data:image/png;base64,aaaa", "")

    def test_javascript_protocol_rejected(self) -> None:
        self._assert_kind("javascript:alert(1)", "")

    def test_blob_protocol_rejected(self) -> None:
        self._assert_kind("blob:https://example.com/uuid", "")

    def test_public_https_image_allowed(self) -> None:
        self._assert_kind("https://share.example.com/demo/figure.png", "image")

    def test_card_preview_path_image_allowed(self) -> None:
        self._assert_kind("/cards/files/preview/2026-08-15/figure.png", "image")

    def test_card_preview_path_video_allowed(self) -> None:
        self._assert_kind("/cards/files/preview/2026-08-15/demo.mp4", "video")

    def test_uppercase_extension_recognized(self) -> None:
        self._assert_kind("https://example.com/a.PNG", "image")

    def test_public_video_allowed(self) -> None:
        self._assert_kind("https://example.com/a.mp4", "video")

    def test_youtube_recognized(self) -> None:
        self._assert_kind("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube")

    def test_loopback_and_private_hosts_are_allowed(self) -> None:
        # Deliberate: there is no host blocklist, because it would
        # blocked images served from the dashboard's own local/tunnel address.
        # Any http(s) URL with a recognized extension renders, regardless of host.
        self._assert_kind("http://127.0.0.1:7795/cards/uploads/a.png", "image")
        self._assert_kind("http://192.168.1.1/a.png", "image")


class RenderAiMediaTest(unittest.TestCase):
    def test_duplicate_video_url_renders_one_video_element(self) -> None:
        result = _run(
            "const html = renderAiMediaEmbeds("
            + json.dumps("https://example.com/demo.mp4 https://example.com/demo.mp4")
            + ");\n"
            "const count = (html.match(/<video\\b/g) || []).length;\n"
            "if (count !== 1) throw new Error('expected one video element, got ' + count + ': ' + html);\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


class LocalArtifactLinkTest(unittest.TestCase):
    def test_html_paths_have_open_and_download_actions(self) -> None:
        path = "/srv/workspace/projects/网页/report.html"
        for text in (path, f"`{path}`", f"[查看网页](<{path}>)"):
            with self.subTest(text=text):
                result = _run(
                    "const html = renderInlineMarkdown(" + json.dumps(text) + ");\n"
                    "const links = html.match(/<a\\b[^>]+>/g) || [];\n"
                    "if (links.length !== 2) throw new Error(html);\n"
                    "if (!links[0].includes('/local-files/preview/') || links[0].includes(' download=')) throw new Error(html);\n"
                    "if (!links[1].includes('download=\"report.html\"') || links[1].includes('/preview/')) throw new Error(html);\n"
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_published_html_uses_both_authenticated_file_actions(self) -> None:
        result = _run(
            "const state = { selected: '%55', panes: [{pane_id: '%55', cwd: '/srv/workspace/projects/demo'}] };\n"
            "const html = renderInlineMarkdown('[研究网页](https://share.example.com/demo/map.html#chapter)');\n"
            "if (!html.includes('/preview/workspace/share-public/demo/map.html#chapter')) throw new Error(html);\n"
            "if (!html.includes('download=\"map.html\"')) throw new Error(html);\n"
            "if ((html.match(/<a\\b/g)||[]).length !== 2) throw new Error(html);\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_selected_pane_does_not_turn_web_markdown_into_relative_file(self) -> None:
        result = _run(
            "const state = { selected: '%55', panes: [{pane_id: '%55', cwd: '/srv/workspace/projects/demo'}] };\n"
            "const html = renderInlineMarkdown('[打开网页](https://share.example.com/demo/map.html)');\n"
            "if (html.includes('/workspace/projects/demo/')) throw new Error(html);\n"
            "if (!html.includes('/workspace/share-public/demo/map.html')) throw new Error(html);\n"
            "if (!html.includes('>打开网页</a>')) throw new Error(html);\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_existing_cards_html_download_url_gets_view_action(self) -> None:
        result = _run(
            "const html = renderInlineMarkdown('http://cards.test/cards/files/reports/a.htm');\n"
            "if (!html.includes('/cards/files/preview/reports/a.htm') || !html.includes('download=\"a.htm\"')) throw new Error(html);\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_other_websites_keep_their_original_link(self) -> None:
        result = _run(
            "const html = renderInlineMarkdown('[网站](https://example.com/a.html)');\n"
            "if (html.includes('download=') || !html.includes('href=\"https://example.com/a.html\"')) throw new Error(html);\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_html_view_keeps_query_and_fragment_without_nested_links(self) -> None:
        result = _run(
            "const html = renderInlineMarkdown('[网页 https://example.com/a](https://share.example.com/demo/map.html?chapter=2#node)');\n"
            "if (!html.includes('map.html?chapter=2#node')) throw new Error(html);\n"
            "if ((html.match(/<a\\b/g) || []).length !== 2) throw new Error(html);\n"
            "if (!html.includes('>网页 https://example.com/a</a>')) throw new Error(html);\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_web_link_labels_preserve_inline_code_and_bold(self) -> None:
        for host in ("share.example.com", "example.com"):
            with self.subTest(host=host):
                result = _run(
                    "const html = renderInlineMarkdown(" + json.dumps(f"[查看 `report` **网页**](https://{host}/demo/map.html)") + ");\n"
                    "if (!html.includes('<code>report</code>') || !html.includes('<strong>网页</strong>') || html.includes('\\u0000')) throw new Error(html);\n"
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_inline_code_pdf_path_becomes_download_link(self) -> None:
        path = "/srv/workspace/projects/论文/最终报告.pdf"
        result = _run(
            "const html = renderInlineMarkdown(" + json.dumps(f"下载：`{path}`") + ");\n"
            "if (!html.includes('class=\"local-artifact-link\"')) throw new Error(html);\n"
            "if (!html.includes('/cards/local-files/workspace/projects/%E8%AE%BA%E6%96%87/%E6%9C%80%E7%BB%88%E6%8A%A5%E5%91%8A.pdf')) throw new Error(html);\n"
            "if (!html.includes('download=\"最终报告.pdf\"')) throw new Error(html);\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_window_relative_pdf_path_uses_selected_pane_cwd(self) -> None:
        relative = "_paper/drafts/main.pdf"
        cwd = "/srv/workspace/projects/demo-archive"
        result = _run(
            "const state = { selected: '%29', panes: [{ pane_id: '%29', cwd: "
            + json.dumps(cwd)
            + " }] };\n"
            "const html = renderInlineMarkdown(" + json.dumps(relative) + ");\n"
            "if (!html.includes('class=\"local-artifact-link\"')) throw new Error(html);\n"
            "if (!html.includes('/cards/local-files/workspace/projects/demo-archive/_paper/drafts/main.pdf')) throw new Error(html);\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_relative_path_cannot_escape_workspace_or_enter_hidden_area(self) -> None:
        cwd = "/srv/workspace/projects/demo"
        result = _run(
            "const state = { selected: '%29', panes: [{ pane_id: '%29', cwd: "
            + json.dumps(cwd)
            + " }] };\n"
            "for (const path of ['../../../../etc/report.pdf', '../../.codex/report.pdf']) {\n"
            "  const html = renderInlineMarkdown(path);\n"
            "  if (html.includes('local-artifact-link')) throw new Error(path + ': ' + html);\n"
            "}\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_sensitive_or_unsupported_path_stays_plain_text(self) -> None:
        for path in (
            "/srv/workspace/project/api-credentials.env",
            "/srv/workspace/project/secrets/report.pdf",
        ):
            with self.subTest(path=path):
                result = _run(
                    "const html = renderInlineMarkdown(" + json.dumps(f"`{path}`") + ");\n"
                    "if (html.includes('local-artifact-link')) throw new Error(html);\n"
                )
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
