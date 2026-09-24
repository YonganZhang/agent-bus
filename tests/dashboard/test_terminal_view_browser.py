#!/usr/bin/env python3
"""Browser regression for the in-Cards tmux view: no scroll jitter.

The real index.html is served by the real Handler on a private port; every
/cards/api/* call is answered by Playwright route interception with a scripted
pane and scripted /api/terminal/capture output, so nothing touches live tmux.

Checks (reported symptom: "终端视图一直往下抖动到最下方"):
- idle AI + typing 50 characters into the composer: the terminal's scrollTop never
  changes and the terminal DOM is not touched;
- reading older output (scrolled up) while new output arrives: scrollTop unchanged;
- scrolling back to the bottom resumes following; End / double-click jump to latest.

Run: python3 -m pytest tests/dashboard/test_terminal_view_browser.py -q
(skips without Playwright for Python or its Chromium: python3 -m playwright install chromium)
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import unittest
from dataclasses import asdict
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import server

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - environment without Playwright
    sync_playwright = None


PANE = server.Pane(
    pane_id="%9", target="secretary_web:9.0", session="secretary_web", window_index=9, pane_index=0,
    window_name="demo", command="claude", cwd="/tmp", title="", active=True, kind="Claude", project="demo",
    preview="", status="idle", pane_pid="4242", pane_start_time="777",
)


class FakeTerminal:
    def __init__(self) -> None:
        self.lines = [f"\x1b[32m⏺\x1b[39m line {i} " + "text " * 8 for i in range(300)]
        self.captures = 0
        self.lock = threading.Lock()

    def payload(self, query: dict[str, list[str]]) -> dict[str, object]:
        with self.lock:
            self.captures += 1
            lines = list(self.lines)
        total = len(lines)
        want = int(query.get("lines", ["600"])[0])
        end = min(total, int(query.get("before", [str(total)])[0]))
        start = max(0, end - want)
        rows = lines[start:end]
        digest = hashlib.sha256(json.dumps([start, end, rows]).encode()).hexdigest()
        body = {"pane": "%9", "width": 120, "height": 40, "history_size": total - 40, "history_limit": 10000,
                "total_lines": total, "first_line": start, "end_line": end, "joined": True,
                "cursor": {"x": 0, "y": 0, "visible": True}, "alternate_on": False, "in_mode": False, "hash": digest}
        if query.get("if_hash", [""])[0] == digest:
            body["unchanged"] = True
        else:
            body["lines"] = rows
        return body

    def append(self, count: int) -> None:
        with self.lock:
            base = len(self.lines)
            self.lines.extend(f"new output {base + i}" for i in range(count))


@unittest.skipIf(sync_playwright is None, "Playwright for Python is not installed")
class TerminalViewScrollTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - only a missing browser is a skip
            cls.pw.stop()
            if "Executable doesn't exist" in str(exc):
                raise unittest.SkipTest("Playwright Chromium is not installed (python3 -m playwright install chromium)")
            raise
        cls.old_authorized = server.Handler.authorized
        cls.old_log = server.Handler.log_message
        server.Handler.authorized = lambda _handler: True
        server.Handler.log_message = lambda _handler, _fmt, *_args: None
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=lambda: cls.httpd.serve_forever(poll_interval=0.01), daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server.Handler.authorized = cls.old_authorized
        server.Handler.log_message = cls.old_log

    def setUp(self) -> None:
        self.term = FakeTerminal()
        self.resizes: list[dict] = []
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 800})
        self.context.add_init_script(
            "localStorage.setItem('tmuxCardViewMode', 'terminal');"
        )
        self.context.route("**/cards/api/**", self.fake_api)
        self.page = self.context.new_page()
        self.errors: list[str] = []
        self.page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.page.goto(f"http://127.0.0.1:{self.httpd.server_port}/cards/?pane=%259")
        self.page.wait_for_function(
            "() => document.getElementById('terminalPanel').classList.contains('visible')"
            " && document.querySelectorAll('#terminalText .tl').length > 250", timeout=15000)
        self.page.wait_for_timeout(800)

    def tearDown(self) -> None:
        self.context.close()
        self.assertEqual(self.errors, [])

    def fake_api(self, route) -> None:
        url = urlparse(route.request.url)
        path = url.path.removeprefix("/cards")
        query = parse_qs(url.query)
        if path == "/api/terminal/capture":
            body: object = self.term.payload(query)
        elif path == "/api/terminal/resize":
            self.resizes.append(json.loads(route.request.post_data or "{}"))
            body = {"resized": False, "reason": "a real terminal client is using this window"}
        elif path == "/api/panes":
            pane = {**asdict(PANE), "has_git": False, "plan_available": False, "plan_reason": "没有计划", "archive_blocker": ""}
            body = {"panes": [pane], "snapshot_at": "", "snapshot_age_ms": 0, "stale": False, "refreshing": False}
        elif path == "/api/events":
            body = {"events": [], "last_id": 0, "head_id": 0}
        elif path == "/api/jobs":
            body = {"jobs": []}
        elif path == "/api/history_before":
            body = {"blocks": [], "next_cursor": None, "has_more": False, "total": 0, "transcript_id": "", "reason": ""}
        elif path == "/api/capture":
            blocks = [{"role": "user" if i % 2 else "assistant", "label": "User prompt" if i % 2 else "AI output",
                       "text": f"对话第 {i} 条 " + "内容 " * 30} for i in range(80)]
            body = {"pane": "%9", "blocks": blocks, "raw_hash": "dialog-v1", "status": "idle"}
        else:
            body = {"ok": True}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def wrap(self) -> dict[str, float]:
        return self.page.evaluate(
            "() => { const w = document.querySelector('.timeline-wrap');"
            " return { top: w.scrollTop, height: w.scrollHeight, client: w.clientHeight }; }")

    def watch(self) -> None:
        self.page.evaluate("""() => {
          window.__tops = [];
          window.__mutations = 0;
          const w = document.querySelector('.timeline-wrap');
          w.addEventListener('scroll', () => window.__tops.push(w.scrollTop));
          new MutationObserver((list) => { window.__mutations += list.length; })
            .observe(document.getElementById('terminalText'), { childList: true, subtree: true, characterData: true });
        }""")

    def test_typing_while_idle_never_moves_or_redraws_the_terminal(self) -> None:
        start = self.wrap()
        self.assertAlmostEqual(start["top"], start["height"] - start["client"], delta=2)  # opened at the bottom
        self.watch()
        polls = self.term.captures
        self.page.click("#composerText")
        samples = []
        for char in "终端视图不应该因为打字而抖动abcdefghijklmnopqrstuvwxyz0123456789ABCD"[:50]:
            self.page.keyboard.type(char)
            samples.append(self.page.evaluate("() => document.querySelector('.timeline-wrap').scrollTop"))
        self.page.wait_for_timeout(1500)
        samples.append(self.wrap()["top"])
        self.assertEqual(len(set(samples)), 1, samples)
        self.assertEqual(samples[0], start["top"])
        self.assertEqual(self.page.evaluate("() => window.__tops"), [])
        self.assertEqual(self.page.evaluate("() => window.__mutations"), 0)
        self.assertGreater(self.term.captures - polls, 2)  # it kept polling (unchanged) meanwhile
        self.assertEqual(len(self.page.input_value("#composerText")), 50)
        self.assertEqual(self.page.inner_text("#detailTitle"), "demo")  # the header names the pane in terminal mode too

    def test_reading_older_output_is_not_disturbed_by_new_output(self) -> None:
        self.page.evaluate("() => { const w = document.querySelector('.timeline-wrap'); w.scrollTop = 1200; }")
        self.page.wait_for_timeout(400)
        before = self.wrap()["top"]
        self.term.append(25)
        self.page.wait_for_function(
            "() => document.getElementById('terminalText').textContent.includes('new output 324')", timeout=5000)
        self.page.wait_for_timeout(1200)
        after = self.wrap()
        self.assertEqual(after["top"], before)
        self.assertGreater(after["height"] - after["client"], after["top"] + 100)  # really not at the bottom
        # scrolling back to the bottom resumes following
        self.page.evaluate("() => { const w = document.querySelector('.timeline-wrap'); w.scrollTop = w.scrollHeight; }")
        self.page.wait_for_timeout(300)
        self.term.append(10)
        self.page.wait_for_function(
            "() => document.getElementById('terminalText').textContent.includes('new output 334')", timeout=5000)
        self.page.wait_for_timeout(300)
        now = self.wrap()
        self.assertAlmostEqual(now["top"], now["height"] - now["client"], delta=2)

    def test_viewer_size_is_requested_and_a_refusal_falls_back_to_reflow(self) -> None:
        self.page.wait_for_timeout(900)
        self.assertEqual(len(self.resizes), 1, self.resizes)  # debounced: one request, not one per event
        ask = self.resizes[0]
        grid = self.page.evaluate("() => terminalGrid()")
        self.assertEqual((ask["pane"], ask["cols"], ask["rows"]), ("%9", grid["cols"], grid["rows"]))
        self.assertTrue(40 <= ask["cols"] <= 250 and 12 <= ask["rows"] <= 120)
        # the fake pane is 120 columns: whether it fits decides pre (as-is) vs reflow
        fit = self.page.evaluate("() => document.getElementById('terminalText').classList.contains('fit')")
        self.assertEqual(fit, 120 <= grid["rawCols"])
        self.page.set_viewport_size({"width": 700, "height": 800})  # narrower than 120 columns now
        self.page.wait_for_timeout(1200)
        self.assertFalse(self.page.evaluate("() => document.getElementById('terminalText').classList.contains('fit')"))
        self.assertEqual(len(self.resizes), 2)
        self.assertLess(self.resizes[1]["cols"], ask["cols"])

    def test_back_to_dialog_lands_at_the_newest_message(self) -> None:
        self.page.evaluate("() => { const w = document.querySelector('.timeline-wrap'); w.scrollTop = 300; }")  # reading old output
        self.page.wait_for_timeout(200)
        self.page.click("#detailPlanTools [data-detail-terminal]")  # "对话"
        self.page.wait_for_function(
            "() => !document.getElementById('terminalPanel').classList.contains('visible')"
            " && document.querySelectorAll('#timeline .block').length >= 40", timeout=10000)
        self.page.wait_for_timeout(800)
        now = self.wrap()
        self.assertGreater(now["height"], now["client"] * 2)
        self.assertAlmostEqual(now["top"], now["height"] - now["client"], delta=4)
        self.assertEqual(self.page.inner_text("#detailPlanTools [data-detail-terminal]"), "终端")

    def test_end_key_and_double_click_jump_to_latest(self) -> None:
        for trigger in ("end", "dblclick"):
            self.page.evaluate("() => { const w = document.querySelector('.timeline-wrap'); w.scrollTop = 600; }")
            self.page.wait_for_timeout(300)
            if trigger == "end":
                self.page.evaluate("() => document.activeElement?.blur()")
                self.page.keyboard.press("End")
            else:
                box = self.page.locator(".timeline-wrap").bounding_box()
                self.page.mouse.dblclick(box["x"] + box["width"] - 30, box["y"] + 200)  # blank area right of the text
            self.page.wait_for_timeout(400)
            now = self.wrap()
            self.assertAlmostEqual(now["top"], now["height"] - now["client"], delta=2, msg=trigger)


if __name__ == "__main__":
    unittest.main(verbosity=2)
