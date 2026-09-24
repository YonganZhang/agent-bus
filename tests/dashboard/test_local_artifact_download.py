#!/usr/bin/env python3
"""Security and HTTP regression tests for AI-linked workspace artifacts."""

from __future__ import annotations

import http.client
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import server


class LocalArtifactDownloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.outside = tempfile.TemporaryDirectory()
        self.old_root = server.LOCAL_ARTIFACT_ROOT
        server.LOCAL_ARTIFACT_ROOT = Path(self.tmp.name)
        self.root = Path(self.tmp.name)
        (self.root / "projects" / "论文").mkdir(parents=True)
        self.pdf = self.root / "projects" / "论文" / "最终报告.pdf"
        self.pdf.write_bytes(b"%PDF-1.4\ncard-test\n")

    def tearDown(self) -> None:
        server.LOCAL_ARTIFACT_ROOT = self.old_root
        self.outside.cleanup()
        self.tmp.cleanup()

    def test_resolves_regular_artifact_inside_workspace(self) -> None:
        resolved = server.local_artifact_path_from_request(
            "workspace", "projects/论文/最终报告.pdf"
        )
        self.assertEqual(resolved, self.pdf.resolve())

    def test_rejects_traversal_hidden_sensitive_and_unsupported_paths(self) -> None:
        (self.root / ".hidden").mkdir()
        (self.root / ".hidden" / "report.pdf").write_bytes(b"hidden")
        (self.root / "projects" / "api-credentials.pdf").write_bytes(b"secret")
        (self.root / "projects" / "secrets").mkdir()
        (self.root / "projects" / "secrets" / "report.pdf").write_bytes(b"secret")
        (self.root / "projects" / "notes.txt").write_text("text", encoding="utf-8")

        rejected = [
            ("other", "projects/论文/最终报告.pdf"),
            ("workspace", "../outside.pdf"),
            ("workspace", ".hidden/report.pdf"),
            ("workspace", "projects/api-credentials.pdf"),
            ("workspace", "projects/secrets/report.pdf"),
            ("workspace", "projects/notes.txt"),
        ]
        for alias, name in rejected:
            with self.subTest(alias=alias, name=name):
                self.assertIsNone(server.local_artifact_path_from_request(alias, name))

    def test_rejects_symlink_escape(self) -> None:
        outside_pdf = Path(self.outside.name) / "outside.pdf"
        outside_pdf.write_bytes(b"outside")
        link = self.root / "projects" / "outside.pdf"
        link.symlink_to(outside_pdf)
        self.assertIsNone(
            server.local_artifact_path_from_request("workspace", "projects/outside.pdf")
        )

    def test_html_can_open_inline_and_download_unchanged(self) -> None:
        html = self.root / "projects" / "论文" / "交互网页.html"
        html.write_text("<!doctype html><meta charset='utf-8'><h1>网页</h1><script>document.title='loaded'</script>", encoding="utf-8")
        with patch.object(server.Handler, "authorized", return_value=True), patch.object(server.Handler, "log_message"), patch.object(server, "SHARED_FILES_DIR", self.root):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
            thread.start()
            try:
                name = quote("projects/论文/交互网页.html", safe="/")
                for prefix in ("/cards/local-files/", "/cards/files/"):
                    for preview in (False, True):
                        with self.subTest(prefix=prefix, preview=preview):
                            path = prefix + ("preview/" if preview else "") + ("workspace/" if "local-files" in prefix else "") + name
                            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=2)
                            conn.request("GET", path)
                            response = conn.getresponse()
                            self.assertEqual(response.status, 200)
                            self.assertEqual(response.read(), html.read_bytes())
                            self.assertEqual(response.getheader("Content-Type"), "text/html" if preview else "application/octet-stream")
                            self.assertIn("no-transform", response.getheader("Cache-Control"))
                            if preview:
                                self.assertEqual(response.getheader("Content-Disposition"), "inline")
                                policy = response.getheader("Content-Security-Policy")
                                self.assertIn("sandbox allow-scripts", policy)
                                self.assertNotIn("allow-same-origin", policy)
                                self.assertIn("connect-src 'none'", policy)
                            else:
                                self.assertIn("attachment", response.getheader("Content-Disposition"))
                            conn.close()
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)

    def test_svg_preview_cannot_run_scripts_as_this_site(self) -> None:
        # 改前: SVG 预览不带沙箱，新标签页打开时脚本以本站来源运行，可同源调用 /api/send。
        svg = self.root / "projects" / "论文" / "图.svg"
        svg.write_text("<svg xmlns='http://www.w3.org/2000/svg'><script>fetch('/cards/api/send')</script></svg>",
                       encoding="utf-8")
        with patch.object(server.Handler, "authorized", return_value=True), patch.object(server.Handler, "log_message"), patch.object(server, "SHARED_FILES_DIR", self.root):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
            thread.start()
            try:
                name = quote("projects/论文/图.svg", safe="/")
                for path in (f"/cards/local-files/preview/workspace/{name}", f"/cards/files/preview/{name}"):
                    with self.subTest(path=path):
                        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=2)
                        conn.request("GET", path)
                        response = conn.getresponse()
                        response.read()
                        self.assertEqual((response.status, response.getheader("Content-Type")), (200, "image/svg+xml"))
                        policy = response.getheader("Content-Security-Policy") or ""
                        self.assertTrue(policy.startswith("sandbox;"), policy)
                        self.assertIn("default-src 'none'", policy)
                        conn.close()
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)

    def test_download_endpoint_streams_pdf_with_unicode_filename(self) -> None:
        old_authorized = server.Handler.authorized
        old_log_message = server.Handler.log_message
        server.Handler.authorized = lambda _handler: True
        server.Handler.log_message = lambda _handler, _fmt, *_args: None
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(
            target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True
        )
        thread.start()
        try:
            relative = quote("projects/论文/最终报告.pdf", safe="/")
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=2)
            conn.request("GET", f"/cards/local-files/workspace/{relative}")
            response = conn.getresponse()
            body = response.read()
            disposition = response.getheader("Content-Disposition", "")
            conn.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(body, self.pdf.read_bytes())
            self.assertEqual(response.getheader("Content-Type"), "application/pdf")
            self.assertIn("attachment", disposition)
            self.assertIn("filename*=UTF-8''", disposition)
            self.assertIn("%E6%9C%80%E7%BB%88%E6%8A%A5%E5%91%8A.pdf", disposition)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)
            server.Handler.authorized = old_authorized
            server.Handler.log_message = old_log_message


if __name__ == "__main__":
    unittest.main()
