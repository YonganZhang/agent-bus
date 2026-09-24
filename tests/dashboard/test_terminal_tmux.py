#!/usr/bin/env python3
"""Terminal endpoints against a REAL, private tmux server, plus the Git-less plan/archive paths.

Every tmux call goes to a throw-away server on its own socket (server.TMUX_SOCKET
is pointed at it), so the live secretary_web session and its clients are never
touched.  The first focus bug (``-t =session`` answered with an empty string in
tmux 3.7) only showed up against real tmux; mocks had agreed with the code.

Run: python3 -m pytest tests/dashboard/test_terminal_tmux.py -q
"""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import server
from plan_stub import enable_plan_integration, write_archive_prompt

SESSION = "cardtest"
ROWS = 120
PRINTER = (
    "i=1; while [ $i -le %d ]; do "
    "printf '\\033[1;31mrow %%03d\\033[0m \\033[38;5;208mo\\033[39m \\033[38;2;1;2;3mt\\033[39m\\n' $i; "
    "i=$((i+1)); done; exec sleep 600" % ROWS
)
ROW_RE = re.compile(r"row (\d{3})")
PLAN_REL = Path("_wiki-methodology") / "_top" / "_task_plan.md"
FIXTURE_PLAN = Path(__file__).resolve().parent / "fixtures" / "plan_panel_task_plan.md"


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;:]*m", "", text)


class RealTmux:
    """A private tmux server on its own socket."""

    def __init__(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="cards-tmux-")
        self.socket = os.path.join(self.dir, "s")
        self.clients: list[subprocess.Popen] = []

    def __call__(self, *args: str, check: bool = True) -> str:
        env = {k: v for k, v in os.environ.items() if k not in {"TMUX", "TMUX_PANE"}}
        cp = subprocess.run(["tmux", "-S", self.socket, "-f", os.devnull, *args],
                            capture_output=True, text=True, timeout=10, env=env)
        if check and cp.returncode != 0:
            raise RuntimeError(f"tmux {args}: {cp.stderr}")
        return cp.stdout.strip()

    def attach(self, session: str) -> str:
        """Attach a real tmux client (under a pty from script(1)); returns its client pid."""
        before = set(self("list-clients", "-F", "#{client_pid}").split())
        env = {k: v for k, v in os.environ.items() if k not in {"TMUX", "TMUX_PANE"}}
        proc = subprocess.Popen(
            ["script", "-qfc", f"tmux -S {self.socket} attach -t {session}", os.devnull],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
        )
        self.clients.append(proc)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            new = set(self("list-clients", "-F", "#{client_pid}").split()) - before
            if new:
                return new.pop()
            time.sleep(0.05)
        raise RuntimeError("tmux client never attached")

    def close(self) -> None:
        for proc in self.clients:
            proc.kill()
            proc.wait(timeout=5)
        self("kill-server", check=False)
        shutil.rmtree(self.dir, ignore_errors=True)


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class RealTmuxTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmux = RealTmux()
        self.addCleanup(self.tmux.close)
        self.tmux("new-session", "-d", "-s", SESSION, "-x", "60", "-y", "10", "sleep 600")
        self.tmux("set-option", "-g", "history-limit", "500")
        self.tmux("new-window", "-t", f"={SESSION}:", PRINTER)  # window 1, created after the limit
        self.printer = self.tmux("display-message", "-p", "-t", f"={SESSION}:1", "#{pane_id}")
        self.first = self.tmux("display-message", "-p", "-t", f"={SESSION}:0", "#{pane_id}")
        deadline = time.monotonic() + 10
        while "row %03d" % ROWS not in self.tmux("capture-pane", "-p", "-t", self.printer):
            if time.monotonic() > deadline:
                raise AssertionError("printer never finished")
            time.sleep(0.05)
        for name, value in (("TMUX_SOCKET", self.tmux.socket), ("TMUX_LABEL", ""), ("DEFAULT_SESSION", SESSION)):
            patcher = mock.patch.object(server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class TerminalCaptureRealTmuxTest(RealTmuxTestBase):
    def test_full_capture_has_every_row_with_ansi_colours_and_geometry(self) -> None:
        data = server.terminal_capture(self.printer)
        self.assertEqual((data["width"], data["height"], data["history_limit"]), (60, 10, 500))
        self.assertEqual(data["total_lines"], data["history_size"] + 10)
        self.assertEqual((data["first_line"], data["end_line"]), (0, data["total_lines"]))
        rows = data["lines"]
        self.assertEqual(len(rows), data["total_lines"])
        self.assertEqual([int(m.group(1)) for m in map(ROW_RE.search, rows) if m], list(range(1, ROWS + 1)))
        self.assertIn("\x1b[1m", rows[0])
        self.assertIn("\x1b[31mrow 001", rows[0])
        self.assertIn("\x1b[38;5;208mo", rows[0])  # 256 colours
        self.assertIn("\x1b[38;2;1;2;3mt", rows[0])  # true colour
        self.assertEqual(strip_ansi(rows[0]), "row 001 o t")
        self.assertEqual(data["cursor"]["y"], ROWS - data["history_size"])  # the line after the last row
        self.assertFalse(data["alternate_on"])
        self.assertRegex(data["hash"], r"^[0-9a-f]{64}$")

    def test_tail_and_before_paging_line_up_with_the_full_capture(self) -> None:
        full = server.terminal_capture(self.printer)
        total = full["total_lines"]
        tail = server.terminal_capture(self.printer, lines=5)
        self.assertEqual((tail["first_line"], tail["lines"]), (total - 5, full["lines"][-5:]))
        page = server.terminal_capture(self.printer, lines=20, before=total - 30)
        self.assertEqual((page["first_line"], page["end_line"]), (total - 50, total - 30))
        self.assertEqual(page["lines"], full["lines"][total - 50:total - 30])
        head = server.terminal_capture(self.printer, lines=20, before=7)  # clipped at the oldest row
        self.assertEqual((head["first_line"], head["lines"]), (0, full["lines"][:7]))
        self.assertEqual(server.terminal_capture(self.printer, lines=20, before=0)["lines"], [])
        beyond = server.terminal_capture(self.printer, lines=3, before=total + 99)
        self.assertEqual(beyond["lines"], full["lines"][-3:])

    def test_polling_the_newest_rows_is_one_tmux_call(self) -> None:
        full = server.terminal_capture(self.printer)  # learns the pane height
        calls: list[list[str]] = []
        real = server.run_tmux

        def counting(args: list[str]):
            calls.append(args)
            return real(args)

        with mock.patch.object(server, "run_tmux", side_effect=counting):
            tail = server.terminal_capture(self.printer, lines=25, join=True)
            self.assertEqual(len(calls), 1)
            self.assertIn(";", calls[0])  # geometry and rows in one atomic tmux command
            server._TERMINAL_HEIGHT_HINT[self.printer] = 3  # stale hint (pane resized): falls back and still right
            again = server.terminal_capture(self.printer, lines=25, join=True)
        self.assertEqual(again["lines"], tail["lines"])
        self.assertEqual(server._TERMINAL_HEIGHT_HINT[self.printer], 10)
        self.assertEqual([strip_ansi(r) for r in tail["lines"]], [strip_ansi(r).rstrip() for r in full["lines"][-25:]])

    def test_same_content_is_reported_unchanged_without_rows(self) -> None:
        first = server.terminal_capture(self.printer, lines=40)
        again = server.terminal_capture(self.printer, lines=40, if_hash=first["hash"])
        self.assertIs(again.get("unchanged"), True)
        self.assertNotIn("lines", again)
        other = server.terminal_capture(self.printer, lines=40, if_hash="0" * 64)
        self.assertEqual(other["lines"], first["lines"])

    def test_new_output_changes_the_tail(self) -> None:
        before = server.terminal_capture(self.printer, lines=12)
        self.tmux("send-keys", "-t", self.first, "echo fresh-output-line", "Enter")
        deadline = time.monotonic() + 5
        while "fresh-output-line" not in self.tmux("capture-pane", "-p", "-t", self.first):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        # the first pane runs sleep, so the typed text is echoed by the tty only
        after = server.terminal_capture(self.first, lines=12)
        self.assertTrue(any("fresh-output-line" in row for row in after["lines"]))
        self.assertEqual(server.terminal_capture(self.printer, lines=12)["hash"], before["hash"])

    def test_join_rewraps_soft_wrapped_rows_into_logical_lines(self) -> None:
        long_line = "w" * 100 + "中文尾巴"
        self.tmux("new-window", "-t", f"={SESSION}:",
                  f"printf '\\033[32m{long_line}\\033[0m   \\nshort   \\n'; exec sleep 600")
        narrow = self.tmux("display-message", "-p", "-t", f"={SESSION}:2", "#{pane_id}")
        deadline = time.monotonic() + 5
        while "short" not in self.tmux("capture-pane", "-p", "-t", narrow):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        physical = server.terminal_capture(narrow)
        joined = server.terminal_capture(narrow, join=True)
        self.assertEqual((physical["width"], joined["joined"], physical["joined"]), (60, True, False))
        plain = [strip_ansi(row) for row in joined["lines"]]
        self.assertIn(long_line, plain)  # 108 cells on a 60-wide pane: one logical line again
        self.assertIn("short", plain)    # trailing spaces dropped
        self.assertFalse(any(strip_ansi(row) == long_line for row in physical["lines"]))
        self.assertLess(len(joined["lines"]), len(physical["lines"]))
        self.assertEqual((joined["first_line"], joined["end_line"]), (physical["first_line"], physical["end_line"]))
        self.assertIn("\x1b[32m" + "w" * 10, joined["lines"][plain.index(long_line)])
        self.assertNotEqual(joined["hash"], physical["hash"])
        pane = narrow.replace("%", "%25")
        status, data = api_get(f"/api/terminal/capture?pane={pane}&join=1")
        self.assertEqual((status, data["joined"]), (200, True))
        self.assertIn(long_line, [strip_ansi(row) for row in data["lines"]])

    def test_bad_unknown_and_foreign_panes_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            server.terminal_capture("1; kill-server")
        with self.assertRaises(FileNotFoundError):
            server.terminal_capture("%99999")
        self.tmux("new-session", "-d", "-s", "other", "sleep 600")
        foreign = self.tmux("display-message", "-p", "-t", "=other:", "#{pane_id}")
        with self.assertRaises(FileNotFoundError):
            server.terminal_capture(foreign)

    def test_capture_never_moves_the_session(self) -> None:
        window = self.tmux("display-message", "-p", "-t", f"={SESSION}:", "#{window_index}")
        server.terminal_capture(self.first)
        self.assertEqual(self.tmux("display-message", "-p", "-t", f"={SESSION}:", "#{window_index}"), window)


class TerminalResizeRealTmuxTest(RealTmuxTestBase):
    def size(self, target: str) -> tuple[str, str]:
        return (self.tmux("display-message", "-p", "-t", target, "#{window_width}x#{window_height}"),
                "window-size " + self.tmux("display-message", "-p", "-t", target, "#{window-size}"))

    def test_resize_sets_the_window_to_the_viewer_and_focus_hands_it_back(self) -> None:
        result = server.terminal_resize(self.printer, 90, 30, session=SESSION)
        self.assertEqual((result["resized"], result["cols"], result["rows"], result["previous"]), (True, 90, 30, [60, 10]))
        self.assertEqual(self.size(self.printer), ("90x30", "window-size manual"))  # tmux: resize-window => manual
        again = server.terminal_resize(self.printer, 90, 30, session=SESSION)
        self.assertEqual((again["resized"], again["reason"]), (False, "already this size"))
        self.assertEqual(server.terminal_capture(self.printer, lines=5)["width"], 90)
        server.terminal_focus(self.printer, session=SESSION)
        self.assertEqual(self.size(self.printer)[1], "window-size latest")

    def test_an_active_real_client_on_that_window_wins(self) -> None:
        self.tmux.attach(SESSION)  # the session shows the printer window (1)
        result = server.terminal_resize(self.printer, 90, 30, session=SESSION)
        self.assertIs(result["resized"], False)
        self.assertEqual(result["reason"], "a real terminal client is using this window")
        self.assertEqual(len(result["clients"]), 1)
        self.assertEqual(self.size(self.printer)[1], "window-size latest")
        # a client looking at another window does not block
        self.tmux("select-window", "-t", f"={SESSION}:0")
        self.assertIs(server.terminal_resize(self.printer, 90, 30, session=SESSION)["resized"], True)

    def test_bad_arguments_are_refused(self) -> None:
        for cols, rows in ((39, 20), (251, 20), (80, 11), (80, 121), ("80", 20), (80.0, 20), (True, 20), (None, 20)):
            with self.assertRaises(ValueError, msg=(cols, rows)):
                server.terminal_resize(self.printer, cols, rows, session=SESSION)
        with self.assertRaises(ValueError):
            server.terminal_resize("1;kill-server", 80, 20, session=SESSION)
        with self.assertRaises(FileNotFoundError):
            server.terminal_resize("%99999", 80, 20, session=SESSION)
        self.assertEqual(self.size(self.printer), ("60x10", "window-size latest"))

    def test_endpoint_validation_and_cross_site(self) -> None:
        body = json.dumps({"pane": self.printer, "cols": 30, "rows": 20}).encode()
        self.assertEqual(api_post("/api/terminal/resize", body, {"Content-Type": "application/json"})[0], 400)
        body = json.dumps({"pane": self.printer, "cols": 100, "rows": 20}).encode()
        self.assertEqual(api_post("/api/terminal/resize", body, {"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(api_post("/api/terminal/resize", body, {"Content-Type": "text/plain"})[0], 403)
        status, data = api_post("/cards/api/terminal/resize", body, {"Content-Type": "application/json"})
        self.assertEqual((status, data["resized"]), (200, True))


class TerminalFocusRealTmuxTest(RealTmuxTestBase):
    def current(self, session: str = SESSION) -> str:
        return self.tmux("display-message", "-p", "-t", f"={session}:", "#{pane_id}")

    def test_focus_switches_the_shared_session_and_verifies_it(self) -> None:
        self.assertEqual(self.current(), self.printer)  # new-window made window 1 current
        result = server.terminal_focus(self.first, session=SESSION)
        self.assertEqual(self.current(), self.first)
        self.assertEqual((result["mode"], result["sessions"], result["affected_clients"]), ("shared", [SESSION], []))

    def test_attached_client_is_listed_and_reported_as_affected(self) -> None:
        pid = self.tmux.attach(SESSION)
        clients = server.terminal_clients(SESSION)
        self.assertEqual([c["pid"] for c in clients], [pid])
        self.assertIs(clients[0]["ttyd"], False)  # its parent is script(1), not ttyd
        self.assertEqual(clients[0]["pane_id"], self.printer)
        self.assertTrue(clients[0]["last_activity"])
        result = server.terminal_focus(self.first, session=SESSION)
        self.assertEqual([c["tty"] for c in result["affected_clients"]], [clients[0]["tty"]])
        self.assertIn("1 个客户端", result["note"])
        self.assertEqual(server.terminal_clients(SESSION)[0]["pane_id"], self.first)

    def test_grouped_ttyd_session_moves_alone(self) -> None:
        self.tmux("new-session", "-d", "-t", SESSION, "-s", f"{SESSION}-ttyd")
        ttyd_pid = self.tmux.attach(f"{SESSION}-ttyd")
        other_pid = self.tmux.attach(SESSION)
        with mock.patch.object(server, "_parent_is_ttyd", side_effect=lambda pid: pid == ttyd_pid):
            clients = server.terminal_clients(SESSION)
            self.assertEqual({c["pid"] for c in clients}, {ttyd_pid, other_pid})  # the group is found
            result = server.terminal_focus(self.first, session=SESSION)
            status = server.terminal_status(self.first, session=SESSION)
        self.assertEqual((result["mode"], result["sessions"]), ("grouped", [f"{SESSION}-ttyd"]))
        self.assertEqual(self.current(f"{SESSION}-ttyd"), self.first)
        self.assertEqual(self.current(), self.printer)  # the shared session did not move
        self.assertEqual((status["attach_mode"], status["terminal"]["source"], status["match"]), ("grouped", "ttyd-client", True))

    def test_status_without_browsers_uses_the_session_pane(self) -> None:
        status = server.terminal_status(self.printer, session=SESSION)
        self.assertEqual((status["terminal"]["source"], status["terminal"]["pane_id"]), ("session-active-pane", self.printer))
        self.assertEqual((status["match"], status["clients"]), (True, []))
        other = server.terminal_status(self.first, session=SESSION)
        self.assertFalse(other["match"])
        self.assertIn("pane_id", {m["field"] for m in other["mismatches"]})

    def test_missing_session_fails_loud(self) -> None:
        with self.assertRaises(RuntimeError):
            server.terminal_clients("no-such-session")


def api_get(path: str, headers: dict[str, str] | None = None) -> tuple[int, dict]:
    old_authorized = server.Handler.authorized
    old_log_message = server.Handler.log_message
    server.Handler.authorized = lambda _handler: True
    server.Handler.log_message = lambda _handler, _fmt, *_args: None
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
        conn.request("GET", path, headers=headers or {})
        response = conn.getresponse()
        status, payload = response.status, json.loads(response.read().decode("utf-8"))
        conn.close()
        return status, payload
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        server.Handler.authorized = old_authorized
        server.Handler.log_message = old_log_message


class TerminalCaptureEndpointTest(RealTmuxTestBase):
    def test_endpoint_returns_rows_and_refuses_cross_site_and_bad_numbers(self) -> None:
        pane = self.printer.replace("%", "%25")
        status, data = api_get(f"/cards/api/terminal/capture?pane={pane}&lines=200", {"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 200, data)
        self.assertEqual(len(data["lines"]), min(200, data["total_lines"]))
        status, data = api_get(f"/api/terminal/capture?pane={pane}&lines=3&before=20")
        self.assertEqual((status, data["first_line"], len(data["lines"])), (200, 17, 3))
        self.assertEqual(api_get(f"/api/terminal/capture?pane={pane}", {"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(api_get(f"/api/terminal/capture?pane={pane}&lines=-1")[0], 400)
        self.assertEqual(api_get(f"/api/terminal/capture?pane={pane}&before=x")[0], 400)
        self.assertEqual(api_get("/api/terminal/capture?pane=abc")[0], 400)
        self.assertEqual(api_get("/api/terminal/capture?pane=%2599999")[0], 404)


def api_post(path: str, body: bytes, headers: dict[str, str]) -> tuple[int, dict]:
    old_authorized = server.Handler.authorized
    old_log_message = server.Handler.log_message
    server.Handler.authorized = lambda _handler: True
    server.Handler.log_message = lambda _handler, _fmt, *_args: None
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
        conn.request("POST", path, body=body, headers=headers)
        response = conn.getresponse()
        status, payload = response.status, json.loads(response.read().decode("utf-8"))
        conn.close()
        return status, payload
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        server.Handler.authorized = old_authorized
        server.Handler.log_message = old_log_message


def fake_pane(cwd: object, kind: str = "Claude", ai_alive: bool = True) -> server.Pane:
    return server.Pane(
        pane_id="%901", target="secretary_web:9.0", session="secretary_web", window_index=9, pane_index=0,
        window_name="w", command="claude", cwd=str(cwd), title="", active=False, kind=kind, project="p",
        preview="", status="", pane_pid="4242", pane_start_time="777", ai_alive=ai_alive,
    )


class GitlessPlanAndArchiveTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        self.projects = self.home / "projects"
        self.project = self.projects / "demo-proj"
        (self.project / "sub" / "deep").mkdir(parents=True)
        for patcher in (
            mock.patch.dict(os.environ, {"HOME": str(self.home), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}),
            mock.patch.object(server, "PROJECTS_DIR", self.projects),
            mock.patch.object(server, "PREFS", self.home / "prefs.json"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        enable_plan_integration(self, write_archive_prompt(self.home))
        with server._GIT_LOCK:
            server._GIT_ROOT_CACHE.clear()
            server._TOP_PLAN_CACHE.clear()
        server._PLAN_FILE_FOR_PROJECT.clear()

    def add_plan(self, where: Path) -> Path:
        plan = where / PLAN_REL
        plan.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURE_PLAN, plan)
        return plan

    def test_search_root_without_git(self) -> None:
        deep = str(self.project / "sub" / "deep")
        self.assertEqual(server.plan_search_root(deep), str(self.project))
        self.assertEqual(server.plan_search_root(deep, "/repo"), "/repo")
        self.assertEqual(server.plan_search_root(str(self.home / "notes")), str(self.home))
        self.assertEqual(server.plan_search_root(str(self.projects)), str(self.home))
        self.assertEqual(server.plan_search_root("/elsewhere/x"), "/elsewhere/x")
        self.assertIsNone(server.plan_search_root(""))

    def test_plan_found_from_a_subdirectory_of_a_project_without_git(self) -> None:
        plan = self.add_plan(self.project)
        pane = fake_pane(self.project / "sub" / "deep")
        fields = server.pane_panel_fields(pane, None, None)
        self.assertEqual(fields, {"has_git": False, "plan_available": True, "plan_reason": "", "archive_blocker": ""})
        with mock.patch.object(server, "pane_by_id", return_value=pane):
            ctx = server.plan_context_for_pane("%901")
            self.assertEqual((ctx["root"], ctx["project_dir"], ctx["plan"]), (None, str(self.project), str(plan)))
            payload = server.plan_payload_for_pane("%901")  # plan CLI on a Git-less project
            self.assertEqual(payload["git"]["state"], "none")
            self.assertTrue(payload["tasks"])
            with self.assertRaises(server.PlanApiError) as caught:
                server.plan_track_for_pane("%901")
            self.assertEqual(caught.exception.status, 409)

    def test_plan_outside_the_project_is_not_picked_up(self) -> None:
        self.add_plan(self.projects)  # above ~/projects/<project>: out of bounds
        fields = server.pane_panel_fields(fake_pane(self.project / "sub"), None, None)
        self.assertEqual((fields["plan_available"], fields["plan_reason"]), (False, server.PLAN_MISSING_REASON))

    def test_archive_blocker_reasons(self) -> None:
        cwd = self.project
        self.assertEqual(server.archive_blocker_for(fake_pane(cwd)), "")  # no Git is fine
        self.assertIn("没有 AI 进程", server.archive_blocker_for(fake_pane(cwd, kind="Shell")))
        self.assertIn("已退出", server.archive_blocker_for(fake_pane(cwd, ai_alive=False)))
        self.assertEqual(server.archive_blocker_for(fake_pane(cwd), {"state": "running"}), "归档进行中，等结果出来再发")
        self.assertEqual(server.archive_blocker_for(fake_pane(cwd), {"state": "done"}), "")

    def test_archive_that_creates_the_repository_counts_its_commits(self) -> None:
        plan = self.add_plan(self.project)
        pane = fake_pane(self.project / "sub")
        run = server.archive_baseline(pane)
        self.assertEqual(run["base"], {"head": "", "pending": None, "ahead": None, "no_git": True})
        self.assertIsNone(run["root"])
        self.assertEqual(run["plan_path"], str(plan))
        server.git_root_for_cwd(pane.cwd)  # cache "no repo" like a poll would
        git = lambda *a: subprocess.run(["git", "-C", str(self.project), *a], check=True, capture_output=True)  # noqa: E731
        git("init", "-q", "-b", "main")
        git("add", "-A")
        git("-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "first")
        (self.project / "later.txt").write_text("x", encoding="utf-8")
        plan.write_text(plan.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        git("add", "-A")
        git("-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "second")
        (self.project / "untracked.txt").write_text("y", encoding="utf-8")
        server.save_archive_run("%901", run)
        done = server.evaluate_archive_run("%901", run)
        result = done["result"]
        self.assertNotIn("error", result)
        self.assertEqual((result["new_commits"], result["pending"], result["git_created"], result["plan_changed"]),
                         (2, 1, True, True))

    def test_archive_without_any_repository_reports_no_git(self) -> None:
        run = server.archive_baseline(fake_pane(self.project))
        server.save_archive_run("%901", run)
        result = server.evaluate_archive_run("%901", run)["result"]
        self.assertEqual(result, {"new_commits": 0, "pending": None, "ahead": None, "head": "", "no_git": True,
                                  "plan_changed": False})
        self.assertEqual(server.archive_runs_snapshot()["%901"]["state"], "done")


if __name__ == "__main__":
    unittest.main(verbosity=2)
