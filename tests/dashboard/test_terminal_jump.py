#!/usr/bin/env python3
"""Cards <-> web terminal (e.g. ttyd) jump.

Server: /api/terminal/focus switches only what the ttyd client shows (tmux is
mocked -- a real call would move the user's terminal), /api/terminal/status
reports where the terminal is and whether it matches a Cards pane.
Frontend: ?pane= deep link, the in-Cards tmux view (title-bar buttons, per-pane
memory, row merging), the ESC float group, and closing the mobile composer
without sending -- the real index.html functions run in node.  Only the "打开完整
终端页" menu item calls the focus API; 终端 itself never leaves /cards.

Run: python3 -m pytest tests/dashboard/test_terminal_jump.py -q
"""

from __future__ import annotations

import http.client
import json
import subprocess
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import server

INDEX = (Path(__file__).resolve().parents[2] / "dashboard" / "index.html")


def api_request(method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, dict]:
    old_authorized = server.Handler.authorized
    old_log_message = server.Handler.log_message
    server.Handler.authorized = lambda _handler: True
    server.Handler.log_message = lambda _handler, _fmt, *_args: None
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
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


def make_pane(pane_id: str = "%1024", window_index: int = 9, **overrides) -> server.Pane:
    fields = dict(
        pane_id=pane_id, target=f"secretary_web:{window_index}.0", session="secretary_web",
        window_index=window_index, pane_index=0, window_name=f"win-{window_index}", command="claude",
        cwd=f"/work/{window_index}", title="", active=False, kind="Claude", project="demo", preview="",
        status="idle", pane_pid=f"{window_index}00", pane_start_time="123",
    )
    fields.update(overrides)
    return server.Pane(**fields)


def client(tty: str, session: str, pane_id: str, *, ttyd: bool, window_index: int = 1, activity: str = "") -> dict:
    return {"tty": tty, "pid": "1", "session": session, "window_index": window_index, "window_name": "w",
            "pane_id": pane_id, "last_activity": activity or "2026-09-24T10:00:00+00:00", "ttyd": ttyd}


class FakeTmux:
    """Records tmux calls; ``display-message -t =<session>`` answers ``shown``."""

    def __init__(self, shown: str = "%1024") -> None:
        self.calls: list[list[str]] = []
        self.shown = shown

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        out = self.shown + "\n" if args[0] == "display-message" else ""
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")


class TerminalFocusTest(unittest.TestCase):
    def focus(self, clients: list[dict], shown: str = "%1024") -> tuple[dict, FakeTmux]:
        fake = FakeTmux(shown)
        with mock.patch.object(server, "pane_by_id", return_value=make_pane()), \
                mock.patch.object(server, "terminal_clients", return_value=clients), \
                mock.patch.object(server, "run_tmux", fake):
            return server.terminal_focus("%1024", "900", "123"), fake

    def test_shared_session_is_switched_and_every_client_on_it_is_reported(self) -> None:
        clients = [client("/dev/pts/1", "secretary_web", "%5", ttyd=True),
                   client("/dev/pts/2", "secretary_web", "%5", ttyd=False)]
        result, fake = self.focus(clients)
        self.assertEqual(fake.calls[0], ["set-option", "-w", "-t", "%1024", "window-size", "latest"])  # undo a Cards resize
        self.assertEqual(fake.calls[1:3], [["select-window", "-t", "=secretary_web:9"], ["select-pane", "-t", "%1024"]])
        self.assertEqual((result["mode"], result["sessions"]), ("shared", ["secretary_web"]))
        self.assertEqual([c["tty"] for c in result["affected_clients"]], ["/dev/pts/1", "/dev/pts/2"])
        self.assertIn("2 个客户端", result["note"])

    def test_no_browser_connected_still_points_the_next_ttyd_attach_at_the_pane(self) -> None:
        result, fake = self.focus([])
        self.assertEqual(fake.calls[1], ["select-window", "-t", "=secretary_web:9"])
        self.assertEqual((result["mode"], result["affected_clients"]), ("shared", []))

    def test_grouped_ttyd_session_is_switched_alone(self) -> None:
        clients = [client("/dev/pts/1", "secretary_web-ttyd-1", "%5", ttyd=True),
                   client("/dev/pts/2", "secretary_web", "%5", ttyd=False)]
        result, fake = self.focus(clients)
        targets = [call[2] for call in fake.calls if call[0] == "select-window"]
        self.assertEqual(targets, ["=secretary_web-ttyd-1:9"])
        self.assertEqual((result["mode"], result["sessions"]), ("grouped", ["secretary_web-ttyd-1"]))
        self.assertEqual([c["tty"] for c in result["affected_clients"]], ["/dev/pts/1"])

    def test_switch_that_does_not_land_on_the_pane_fails_loud(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "expected %1024"):
            self.focus([], shown="%7")

    def test_bad_pane_missing_pane_and_changed_instance(self) -> None:
        fake = FakeTmux()
        with mock.patch.object(server, "run_tmux", fake):
            with self.assertRaises(ValueError):
                server.terminal_focus("9; kill-server")
            with mock.patch.object(server, "pane_by_id", return_value=None), self.assertRaises(FileNotFoundError):
                server.terminal_focus("%404")
            with mock.patch.object(server, "pane_by_id", return_value=make_pane()), \
                    self.assertRaises(server.PaneIdentityConflict):
                server.terminal_focus("%1024", "900", "999")
        self.assertEqual(fake.calls, [])


class TerminalFocusEndpointTest(unittest.TestCase):
    def test_validation_and_cross_site_refusal(self) -> None:
        body = b'{"pane":"%1024"}'
        with mock.patch.object(server, "terminal_focus") as focus:
            status, payload = api_request("POST", "/api/terminal/focus", body, {"Content-Type": "text/plain"})
            self.assertEqual((status, payload["error"]), (403, "write requests must be application/json"))
            status, _ = api_request("POST", "/api/terminal/focus", body,
                                    {"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"})
            self.assertEqual(status, 403)
            focus.assert_not_called()
        status, payload = api_request("POST", "/api/terminal/focus", b'{"pane":"abc"}', {"Content-Type": "application/json"})
        self.assertEqual((status, payload["error"]), (400, "pane must look like %12"))
        status, _ = api_request("POST", "/api/terminal/focus", b'["%1"]', {"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        with mock.patch.object(server, "pane_by_id", return_value=None):
            status, _ = api_request("POST", "/api/terminal/focus", b'{"pane":"%404"}', {"Content-Type": "application/json"})
        self.assertEqual(status, 404)

    def test_own_page_request_reaches_focus_with_the_pane_identity(self) -> None:
        body = json.dumps({"pane": "%1024", "pane_pid": "900", "pane_start_time": "123"}).encode()
        with mock.patch.object(server, "terminal_focus", return_value={"ok": True, "mode": "shared"}) as focus:
            status, payload = api_request("POST", "/cards/api/terminal/focus", body,
                                          {"Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"})
        self.assertEqual((status, payload["ok"]), (200, True))
        focus.assert_called_once_with("%1024", "900", "123")


class TerminalClientsTest(unittest.TestCase):
    def test_only_clients_of_the_session_or_its_group_are_listed_and_ttyd_is_detected(self) -> None:
        listing = "\n".join([
            "/dev/pts/1\t11\tsecretary_web\t\t9\tw9\t%1024\t1790247484",
            "/dev/pts/2\t12\tother\t\t0\tw0\t%1\t1790247000",
            "garbage",
        ])

        def fake(args: list[str]) -> subprocess.CompletedProcess[str]:
            out = "\n" if args[0] == "display-message" else listing
            return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

        with mock.patch.object(server, "run_tmux", fake), \
                mock.patch.object(server, "_parent_is_ttyd", side_effect=lambda pid: pid == "11"):
            clients = server.terminal_clients("secretary_web")
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0]["tty"], "/dev/pts/1")
        self.assertEqual(clients[0]["window_index"], 9)
        self.assertTrue(clients[0]["ttyd"])
        self.assertTrue(str(clients[0]["last_activity"]).startswith("2026-09-24"))

    def test_missing_session_is_an_error(self) -> None:
        def fake(args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="can't find session")

        with mock.patch.object(server, "run_tmux", fake), self.assertRaisesRegex(RuntimeError, "can't find session"):
            server.terminal_clients("secretary_web")


class TerminalStatusTest(unittest.TestCase):
    panes = {"%1024": make_pane("%1024", 9), "%5": make_pane("%5", 5, kind="Codex", command="codex")}

    def status(self, clients: list[dict], pane: str = "") -> dict:
        identity = lambda p: {"provider": p.kind.lower(), "session_id": f"sid-{p.pane_id}", "session_source": "pane-stamp"}  # noqa: E731
        with mock.patch.object(server, "terminal_clients", return_value=clients), \
                mock.patch.object(server, "pane_by_id", side_effect=self.panes.get), \
                mock.patch.object(server, "pane_ai_identity", side_effect=identity), \
                mock.patch.object(server, "active_pane", return_value={"pane_id": "%5"}):
            return server.terminal_status(pane)

    def test_terminal_follows_the_most_recent_ttyd_client(self) -> None:
        clients = [client("/dev/pts/1", "secretary_web", "%5", ttyd=True, activity="2026-09-24T09:00:00+00:00"),
                   client("/dev/pts/2", "secretary_web", "%1024", ttyd=True, activity="2026-09-24T10:00:00+00:00"),
                   client("/dev/pts/3", "secretary_web", "%5", ttyd=False, activity="2026-09-24T11:00:00+00:00")]
        payload = self.status(clients)
        self.assertEqual(payload["terminal"]["source"], "ttyd-client")
        self.assertEqual(payload["terminal"]["pane_id"], "%1024")
        self.assertEqual((payload["ttyd_clients"], len(payload["clients"]), payload["attach_mode"]), (2, 3, "shared"))
        self.assertNotIn("match", payload)
        for key in ("window_index", "window_name", "pane_pid", "pane_current_path", "provider", "session_id"):
            self.assertIn(key, payload["terminal"])

    def test_without_a_browser_the_session_active_pane_is_what_ttyd_will_show(self) -> None:
        payload = self.status([])
        self.assertEqual((payload["terminal"]["source"], payload["terminal"]["pane_id"]), ("session-active-pane", "%5"))

    def test_match_true_and_mismatch_fields(self) -> None:
        ttyd = [client("/dev/pts/1", "secretary_web", "%1024", ttyd=True)]
        same = self.status(ttyd, "%1024")
        self.assertEqual((same["match"], same["mismatches"]), (True, []))
        other = self.status(ttyd, "%5")
        self.assertFalse(other["match"])
        fields = {item["field"]: (item["cards"], item["terminal"]) for item in other["mismatches"]}
        self.assertEqual(fields["pane_id"], ("%5", "%1024"))
        self.assertEqual(fields["window_index"], (5, 9))
        self.assertEqual(fields["provider"], ("codex", "claude"))
        self.assertEqual(set(fields), set(server.TERMINAL_MATCH_FIELDS))

    def test_bad_or_unknown_pane(self) -> None:
        with self.assertRaises(ValueError):
            self.status([], "abc")
        with self.assertRaises(FileNotFoundError):
            self.status([], "%404")

    def test_endpoint_refuses_cross_site_and_bad_pane(self) -> None:
        with mock.patch.object(server, "terminal_status") as status_fn:
            code, _ = api_request("GET", "/api/terminal/status", headers={"Sec-Fetch-Site": "cross-site"})
            self.assertEqual(code, 403)
            status_fn.assert_not_called()
        code, payload = api_request("GET", "/api/terminal/status?pane=abc", headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual((code, payload["error"]), (400, "pane must look like %12"))
        with mock.patch.object(server, "terminal_status", return_value={"match": True}) as status_fn:
            code, payload = api_request("GET", "/cards/api/terminal/status?pane=%251024")
        self.assertEqual((code, payload), (200, {"match": True}))
        status_fn.assert_called_once_with("%1024")


def snippet(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    return source[start:source.index(end_marker, start)]


class FrontendTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = INDEX.read_text(encoding="utf-8")

    def js(self, body: str) -> object:
        result = subprocess.run(["node", "-e", body + "\nprocess.stdout.write(JSON.stringify(__out));"],
                                check=False, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def test_deep_link_pane_opens_that_window(self) -> None:
        deep = snippet(self.source, "    function paneFromDeepLink(search)", "\n    const state = {")
        out = self.js(f"""
const location = {{ search: "?pane=%2512&x=1", pathname: "/cards/", hash: "#h" }};
const replaced = [];
const history = {{ replaceState: (_s, _t, url) => replaced.push(url) }};
const toasts = [];
const scrolled = [];
const showToast = (m) => toasts.push(m);
const requestBottomScroll = (p) => scrolled.push(p);
{deep}
const state = {{ selected: null }};
const parsed = ["?pane=%2512", "?pane=12", "?pane=%12", "?pane=abc", ""].map(paneFromDeepLink);
applyDeepLinkPane([{{ pane_id: "%3" }}, {{ pane_id: "%12" }}]);
const selected = state.selected;
applyDeepLinkPane([]);  // only the first pane list applies it
const __out = {{ parsed, replaced, selected, scrolled, toasts, again: state.selected }};
""")
        self.assertEqual(out["parsed"], ["%12", "%12", None, None, None])  # raw %12 decodes to a control char
        self.assertEqual(out["replaced"], ["/cards/?x=1#h"])
        self.assertEqual((out["selected"], out["scrolled"], out["toasts"], out["again"]), ("%12", ["%12"], [], "%12"))
        missing = self.js(f"""
const location = {{ search: "?pane=%2599", pathname: "/cards/", hash: "" }};
const history = {{ replaceState: () => {{}} }};
const toasts = [];
const showToast = (m) => toasts.push(m);
const requestBottomScroll = () => {{}};
{deep}
const state = {{ selected: null }};
applyDeepLinkPane([{{ pane_id: "%3" }}]);
const __out = {{ selected: state.selected, toasts }};
""")
        self.assertEqual(missing, {"selected": None, "toasts": ["没找到窗口 %99，打开的是当前窗口"]})
        load = snippet(self.source, "    async function loadPanes(", "\n    async function ")
        self.assertLess(load.index("applyDeepLinkPane(state.panes);"),
                        load.index("if (state.selected && !state.panes.some((pane) => pane.pane_id === state.selected))"))

    def test_closing_the_mobile_composer_keeps_the_draft_and_sends_nothing(self) -> None:
        drafts = snippet(self.source, "    let _draftPersistTimer = 0;", "\n    function loadDraft(")
        composer = snippet(self.source, "    function setMobileComposer(open, focus = false)", "\n    function applyMobileComposerState()")
        out = self.js(f"""
const stored = {{}};
const localStorage = {{ setItem: (k, v) => {{ stored[k] = v; }} }};
const textarea = {{ value: "半截草稿", blurred: 0, blur() {{ this.blurred += 1; }} }};
const document = {{ activeElement: textarea }};
const el = () => textarea;
const state = {{ selected: "%7", drafts: {{}}, mobileComposerExpanded: true }};
const sent = [];
const sendToPane = (...args) => sent.push(args);
const isMobileLayout = () => true;
const isNearBottom = () => false;
const applyMobileComposerState = () => {{}};
const updateComposerSendVisibility = () => {{}};
const scrollToBottom = () => {{}};
const keepComposerTailVisible = () => {{}};
const autoResizeComposer = () => {{}};
{drafts}
{composer}
toggleMobileComposer();
const afterToggle = {{ expanded: state.mobileComposerExpanded, value: textarea.value, sent: sent.length,
                      stored: stored.tmuxCardDrafts, blurred: textarea.blurred }};
state.mobileComposerExpanded = true;
class Element {{ closest() {{ return null; }} }}
globalThis.Element = Element;
handleOutsideMobileComposerClick({{ target: new Element() }});
const __out = {{ afterToggle, outsideExpanded: state.mobileComposerExpanded, sentTotal: sent.length, draft: state.drafts["%7"] }};
""")
        self.assertEqual(out["afterToggle"], {"expanded": False, "value": "半截草稿", "sent": 0,
                                              "stored": '{"%7":"半截草稿"}', "blurred": 1})
        self.assertEqual((out["outsideExpanded"], out["sentTotal"], out["draft"]), (False, 0, "半截草稿"))
        self.assertNotIn("submitAndCollapseMobileComposer", self.source)

    def test_esc_group_has_plan_and_terminal_and_no_separate_float_plan(self) -> None:
        control = snippet(self.source, '<div class="float-control" id="floatControl">', '<button class="float-main"')
        menu = snippet(control, '<div class="float-menu" id="floatMenu">', "</div>")
        self.assertIn('<button class="float-text float-plan" id="floatPlan" type="button" title="打开这个窗口所在项目的计划" hidden>计划</button>', menu)
        self.assertIn('<button class="float-text float-terminal" id="floatTerminal" type="button" title="在卡片内切换对话 / tmux 视图" aria-pressed="false" hidden>终端</button>', menu)
        # row-reverse: ESC sits next to the circle, then 终端, then 计划 -> reads "计划 终端 ESC ⌁".
        self.assertLess(menu.index('id="escKey"'), menu.index('id="floatTerminal"'))
        self.assertLess(menu.index('id="floatTerminal"'), menu.index('id="floatPlan"'))
        self.assertEqual(self.source.count('id="floatPlan"'), 1)  # the old stand-alone float button is gone
        self.assertNotIn(".float-control .float-plan", self.source)
        self.assertIn(".float-menu button[hidden] {\n      display: none;", self.source)
        self.assertIn('el("floatTerminal").addEventListener("click", () => {\n      if (state.selected) toggleTerminalView(state.selected);', self.source)
        self.assertIn(".float-menu .float-plan.is-disabled {", self.source)

    def test_plan_item_is_greyed_not_hidden_when_the_project_has_no_plan(self) -> None:
        render = snippet(self.source, "    function renderDetailPlanTools()", "\n    function handleDetailPlanToolsClick")
        wiring = snippet(self.source, '    el("floatPlan").addEventListener("click", () => {', "\n    });")
        helpers = snippet(self.source, "    const PLAN_MISSING_TEXT", "\n    function paneCanArchive(pane)")
        out = self.js(f"""{helpers}
const nodes = {{}};
const node = (id) => nodes[id] || (nodes[id] = {{ id, hidden: true, title: "", attrs: {{}}, classes: new Set(), innerHTML: "",
  classList: {{ toggle(c, on) {{ on ? node(id).classes.add(c) : node(id).classes.delete(c); }}, contains(c) {{ return node(id).classes.has(c); }} }},
  setAttribute(k, v) {{ this.attrs[k] = v; }}, addEventListener(type, fn) {{ this.onclick = fn; }} }});
const el = node;
const state = {{ panes: [], selected: null, sharedFilesOpen: false }};
const detailPlanToolsHtml = () => "";
const toasts = [], opened = [];
const showToast = (m) => toasts.push(m);
const openPlanPanel = (p) => opened.push(p);
const layoutFloatMenu = () => {{}};
{render}
{wiring}
    }});
const view = () => ({{ hidden: node("floatPlan").hidden, disabled: node("floatPlan").classes.has("is-disabled"),
  aria: node("floatPlan").attrs["aria-disabled"], title: node("floatPlan").title, terminalHidden: node("floatTerminal").hidden }});
renderDetailPlanTools();
const none = view();
state.panes = [{{ pane_id: "%1", git: {{ has_plan: false }} }}, {{ pane_id: "%2", git: {{ has_plan: false }}, plan_available: true }},
               {{ pane_id: "%3", plan_available: false, plan_reason: "自定义原因" }}];
state.selected = "%1"; renderDetailPlanTools(); const noPlan = view(); node("floatPlan").onclick();
state.selected = "%3"; renderDetailPlanTools(); const noGit = view();
state.selected = "%2"; renderDetailPlanTools(); const withPlan = view(); node("floatPlan").onclick();
const __out = {{ none, noPlan, noGit, withPlan, toasts, opened }};
""")
        self.assertEqual(out["none"]["hidden"], True)  # nothing selected: no window to act on
        missing = "这个项目还没有计划（让 AI 用 plan 命令初始化一份）"
        self.assertEqual(out["noPlan"], {"hidden": False, "disabled": True, "aria": "true", "title": missing,
                                         "terminalHidden": False})
        self.assertEqual((out["noGit"]["disabled"], out["noGit"]["title"]), (True, "自定义原因"))
        # plan_available from the server wins over the old git.has_plan (plans without Git)
        self.assertEqual(out["withPlan"], {"hidden": False, "disabled": False, "aria": "false",
                                           "title": "打开这个窗口所在项目的计划", "terminalHidden": False})
        self.assertEqual((out["toasts"], out["opened"]), ([missing], ["%2"]))

    def float_js(self, body: str) -> object:
        helpers = snippet(self.source, "    const FLOAT_SIZE = 42;", "\n    function layoutFloatMenu()")
        return self.js(helpers + "\n" + body)

    def test_circle_box_is_fixed_and_the_menu_hangs_off_it(self) -> None:
        control_css = snippet(self.source, "    .float-control {\n      position: fixed;", "}")
        self.assertIn("width: 42px;", control_css)
        self.assertIn("height: 42px;", control_css)
        self.assertNotIn("display: grid", control_css)  # the old grid grew a row (and width) when opened
        menu_css = snippet(self.source, "    .float-menu {\n      display: none;", "}")
        for rule in ("position: absolute;", "right: calc(100% + 8px);", "flex-direction: row-reverse;", "flex-wrap: wrap;",
                     "width: max-content;"):
            self.assertIn(rule, menu_css)
        self.assertNotRegex(self.source, r"\.float-control\.open \{")  # opening styles only the menu
        self.assertIn(".float-control.open .float-menu {\n      display: flex;", self.source)
        click = snippet(self.source, '    el("floatMain").addEventListener("click", () => {', "\n    });")
        self.assertNotIn("floatPos", click)
        self.assertNotIn(".style.", click)

    def test_menu_opens_to_the_left_wraps_and_never_leaves_the_screen(self) -> None:
        out = self.float_js("""
const vp = { width: 390, height: 844 };
const circle = (left, top) => ({ left, top, right: left + 42, bottom: top + 42, width: 42, height: 42 });
const __out = {
  rightEdgeLow: floatMenuLayout(circle(340, 700), vp),
  rightEdgeHigh: floatMenuLayout(circle(340, 60), vp),
  middle: floatMenuLayout(circle(120, 400), vp),
  leftEdge: floatMenuLayout(circle(8, 400), vp),
};""")
        self.assertEqual(out["rightEdgeLow"], {"side": "left", "maxWidth": 324, "wrapUp": True})
        self.assertEqual(out["rightEdgeHigh"], {"side": "left", "maxWidth": 324, "wrapUp": False})
        # 104px on the left: one row of two buttons at most, the rest wraps -- still on the left.
        self.assertEqual(out["middle"], {"side": "left", "maxWidth": 104, "wrapUp": False})
        # nothing fits on the left of a circle parked at the left edge: use the right side instead.
        self.assertEqual(out["leftEdge"], {"side": "right", "maxWidth": 324, "wrapUp": False})
        # left side: menu right edge = circle.left - 8, width <= maxWidth -> left edge >= 8 (inside the screen)
        self.assertGreaterEqual(340 - 8 - out["rightEdgeLow"]["maxWidth"], 8)
        self.assertGreaterEqual(120 - 8 - out["middle"]["maxWidth"], 8)
        self.assertLessEqual(8 + 42 + 8 + out["leftEdge"]["maxWidth"], 390 - 8)

    def test_clamp_keeps_the_circle_on_screen(self) -> None:
        out = self.float_js("""
const vp = { width: 390, height: 844 };
const __out = [clampFloatPos({ x: 500, y: 900 }, vp), clampFloatPos({ x: -30, y: -5 }, vp),
               clampFloatPos({ x: 120.4, y: 300.6 }, vp), clampFloatPos({ x: 700, y: 300 }, { width: 844, height: 390 })];""")
        self.assertEqual(out, [{"x": 340, "y": 794}, {"x": 8, "y": 8}, {"x": 120, "y": 301}, {"x": 700, "y": 300}])
        resize = snippet(self.source, '    window.addEventListener("resize", () => {', "\n    });")
        self.assertIn("applyFloatPos();", resize)
        self.assertIn("layoutFloatMenu();", resize)
        apply = snippet(self.source, "    function applyFloatPos()", "\n    // 轻点")
        self.assertIn("state.floatPos = clampFloatPos(state.floatPos,", apply)

    def test_a_tap_is_not_a_drag_and_a_drag_is_clamped_and_saved(self) -> None:
        drag = snippet(self.source, "    function applyFloatPos()", "\n    function applyMobileDrawerTogglePos()")
        out = self.float_js(f"""
const listeners = {{}};
const window = {{
  innerWidth: 390, innerHeight: 844,
  addEventListener: (type, fn) => {{ listeners[type] = fn; }},
  removeEventListener: (type) => {{ delete listeners[type]; }},
}};
const stored = {{}};
const localStorage = {{ setItem: (k, v) => {{ stored[k] = v; }} }};
const control = {{ style: {{}}, getBoundingClientRect: () => ({{ left: 326, top: 700, right: 368, bottom: 742, width: 42, height: 42 }}) }};
const el = () => control;
const state = {{ floatPos: null, floatDragging: false }};
let layouts = 0;
const layoutFloatMenu = () => {{ layouts += 1; }};
const scheduleMobileControlCollisionCheck = () => {{}};
const setTimeout = () => {{}};
{drag}
startFloatDrag({{ clientX: 340, clientY: 710 }});
listeners.pointermove({{ clientX: 345, clientY: 714 }});   // 6.4px jitter
listeners.pointerup();
const tap = {{ pos: state.floatPos, dragging: state.floatDragging, stored: stored.tmuxCardFloatPos || null, style: {{ ...control.style }} }};
startFloatDrag({{ clientX: 340, clientY: 710 }});
listeners.pointermove({{ clientX: 600, clientY: 2 }});     // dragged past the top-right corner
listeners.pointerup();
const __out = {{ tap, drag: {{ pos: state.floatPos, dragging: state.floatDragging, stored: stored.tmuxCardFloatPos, left: control.style.left, top: control.style.top }},
                listenersLeft: Object.keys(listeners) }};""")
        self.assertEqual(out["tap"], {"pos": None, "dragging": False, "stored": None, "style": {}})
        self.assertEqual(out["drag"], {"pos": {"x": 340, "y": 8}, "dragging": True, "stored": '{"x":340,"y":8}',
                                       "left": "340px", "top": "8px"})
        self.assertEqual(out["listenersLeft"], [])

    def tools(self, pane: dict, extra: str = "") -> str:
        helpers = snippet(self.source, "    function paneArchiveBlocker(pane)", "\n    function paneCanArchive(pane)")
        tools = snippet(self.source, "    function detailPlanToolsHtml(pane)", "\n    function renderDetailPlanTools()")
        return str(self.js(f"""
const escapeHtml = (value) => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;");
const state = {{ terminalOpen: false, selected: "%23" }};
const archiveDismissed = () => false;
const archiveResultText = () => "";
const gitPendingLevel = () => "";
{helpers}
{tools}
{extra}
const __out = detailPlanToolsHtml({json.dumps(pane, ensure_ascii=False)});"""))

    def test_title_bar_always_has_plan_archive_terminal(self) -> None:
        # window 23: no Git, no plan, live Claude -> 计划 grey with the reason, 归档 usable, "未建仓"
        pane = {"pane_id": "%23", "kind": "Claude", "ai_alive": True, "has_git": False, "plan_available": False,
                "plan_reason": "这个项目还没有计划（说一句“归档一下”或让 AI 用 plan 初始化）", "archive_blocker": ""}
        html = self.tools(pane)
        self.assertIn('<span class="dpt-nogit" title="还不是 Git 仓库（归档时 AI 可以建仓）">未建仓</span>', html)
        self.assertIn('<button type="button" class="dpt-plan is-disabled" aria-disabled="true" data-detail-plan="%23" '
                      'title="这个项目还没有计划（说一句“归档一下”或让 AI 用 plan 初始化）">计划</button>', html)
        self.assertIn('<button type="button" class="dpt-archive" data-detail-archive="%23" title="让窗口里的 AI 整理文件、提交 Git、更新计划">归档</button>', html)
        self.assertIn('data-detail-terminal="%23" title="在这里显示这个窗口的 tmux 画面" aria-pressed="false">终端</button>', html)
        shell = self.tools({**pane, "kind": "Shell", "archive_blocker": "窗口里没有 AI 进程（Claude/Codex），不能归档"})
        self.assertIn('class="dpt-archive is-disabled" aria-disabled="true" data-detail-archive="%23" title="窗口里没有 AI 进程', shell)
        opened = self.tools(pane, 'state.terminalOpen = true;')
        self.assertIn('class="dpt-terminal active" data-detail-terminal="%23" title="回到对话视图" aria-pressed="true">对话</button>', opened)
        # old server without the fields: no Git is still not an archive blocker
        legacy = self.tools({"pane_id": "%23", "kind": "Codex", "ai_alive": True})
        self.assertIn('class="dpt-archive" data-detail-archive="%23"', legacy)

    def test_greyed_buttons_explain_and_terminal_toggles_in_place(self) -> None:
        click = snippet(self.source, "    function handleDetailPlanToolsClick(event)", "\n    function ansi256Color(")
        out = self.js(f"""
const calls = [];
const showToast = (m) => calls.push(["toast", m]);
const openPlanPanel = (p) => calls.push(["plan", p]);
const requestArchive = (p) => calls.push(["archive", p]);
const toggleTerminalView = (p) => calls.push(["terminal", p]);
const dismissArchiveResult = () => {{}};
const state = {{ panes: [] }};
const el = () => ({{ open: true }});
{click}
const button = (attr, value, disabled, title) => ({{ dataset: {{ [attr]: value }}, title,
  classList: {{ contains: (c) => c === "is-disabled" && disabled }} }});
const ev = (hit) => ({{ target: {{ closest: (sel) => (sel === hit.sel ? hit.node : null) }} }});
handleDetailPlanToolsClick(ev({{ sel: "[data-detail-plan]", node: button("detailPlan", "%1", true, "没有计划") }}));
handleDetailPlanToolsClick(ev({{ sel: "[data-detail-archive]", node: button("detailArchive", "%1", true, "没有 AI") }}));
handleDetailPlanToolsClick(ev({{ sel: "[data-detail-plan]", node: button("detailPlan", "%2", false, "") }}));
handleDetailPlanToolsClick(ev({{ sel: "[data-detail-archive]", node: button("detailArchive", "%2", false, "") }}));
handleDetailPlanToolsClick(ev({{ sel: "[data-detail-terminal]", node: button("detailTerminal", "%2", false, "") }}));
const __out = calls;""")
        self.assertEqual(out, [["toast", "没有计划"], ["toast", "没有 AI"], ["plan", "%2"], ["archive", "%2"], ["terminal", "%2"]])
        toggle = snippet(self.source, "    async function setTerminalOpen(open", "\n    // \"打开完整终端页\"")
        self.assertIn("rememberTerminalPreference(paneId, state.terminalOpen);", toggle)
        self.assertNotIn("/api/terminal/focus", toggle)
        self.assertNotIn("window.location", toggle)
        self.assertIn('el("floatTerminal").addEventListener("click", () => {\n      if (state.selected) toggleTerminalView(state.selected);', self.source)
        render = snippet(self.source, "    function renderDetailPlanTools()", "\n    function handleDetailPlanToolsClick")
        self.assertIn('floatTerminal.textContent = terminalOpen ? "对话" : "终端";', render)
        switch = snippet(self.source, "    async function switchToPane(paneId", "\n    // O(1) 更新选中高亮")
        self.assertIn("syncTerminalModeForPane(paneId);", switch)
        for marker in ('id="terminalPanel"', 'id="terminalText"', 'id="composerText"', 'id="cards"'):
            self.assertIn(marker, self.source)

    def test_back_to_dialog_requests_the_bottom(self) -> None:
        toggle = snippet(self.source, "    async function setTerminalOpen(open", "\n    // \"打开完整终端页\"")
        close = toggle[toggle.index("if (!state.terminalOpen) {"):]
        self.assertLess(close.index("requestBottomScroll(paneId);"), close.index("await loadCapture(state.selected, true);"))
        sync = snippet(self.source, "    function syncTerminalModeForPane(paneId)", "\n    async function setTerminalOpen(")
        self.assertIn("requestBottomScroll(paneId);", sync)

    def test_full_terminal_page_stays_in_the_menu(self) -> None:
        self.assertIn('<button id="openFullTerminal" title="把网页终端切到这个窗口并打开', self.source)
        full = snippet(self.source, "    async function openFullTerminalPage(", "\n    // \"终端\": 只替换卡片详情的中央内容区。")
        self.assertIn("`${BASE}/api/terminal/focus`", full)
        self.assertLess(full.index("if (!res.ok || data.error) throw"), full.index("window.location.assign(FULL_TERMINAL_URL);"))
        self.assertEqual(self.source.count("/api/terminal/focus"), 1)  # only the menu item uses it
        # The menu item only exists when the server was given TMUX_CARD_TERMINAL_URL.
        self.assertIn('const FULL_TERMINAL_URL = String(AGENT_BUS_CONFIG.terminalUrl || "").trim();', self.source)
        self.assertIn('el("openFullTerminal").hidden = !FULL_TERMINAL_URL;', self.source)

    def test_terminal_url_reaches_the_page_config(self) -> None:
        with mock.patch.object(server, "TERMINAL_URL", "https://term.example.invalid/"):
            self.assertEqual(server.frontend_config()["terminalUrl"], "https://term.example.invalid/")
        with mock.patch.object(server, "TERMINAL_URL", ""):
            self.assertEqual(server.frontend_config()["terminalUrl"], "")

    def test_view_mode_is_one_global_choice(self) -> None:
        prefs = snippet(self.source, "    const VIEW_MODE_KEY", "\n    // ---- 卡片内 tmux 视图: 显示 ----")
        out = self.js(f"""
const store = {{}};
const localStorage = {{ getItem: (k) => store[k] ?? null, setItem: (k, v) => {{ store[k] = v; }} }};
{prefs}
const fresh = [terminalPreferred("%1"), terminalPreferred("%2")];
rememberTerminalPreference("%1", true);            // switching one window to 终端 ...
const afterTerminal = [terminalPreferred("%1"), terminalPreferred("%2"), terminalPreferred("%99")];
rememberTerminalPreference("%2", false);           // ... and another back to 对话
const afterDialog = [terminalPreferred("%1"), terminalPreferred("%2")];
const __out = {{ fresh, afterTerminal, afterDialog, stored: store }};""")
        self.assertEqual(out["fresh"], [False, False])  # nothing chosen yet: 对话
        self.assertEqual(out["afterTerminal"], [True, True, True])
        self.assertEqual(out["afterDialog"], [False, False])
        self.assertEqual(out["stored"], {"tmuxCardViewMode": "dialog"})
        for gone in ("defaultViewSelect", "tmuxCardDefaultView", "tmuxCardTerminalPanes", "按设备"):
            self.assertNotIn(gone, self.source)

    def merge_js(self, body: str) -> object:
        merge = snippet(self.source, "    const TERMINAL_TAIL_EXTRA = 150;", "\n    // ---- 卡片内 tmux 视图: tmux 顶部之上接对话记录")
        return self.js(merge + "\n" + body)

    def test_tail_merge_aligns_joined_lines_by_content(self) -> None:
        # join=1 returns logical lines, so row numbers cannot index them: the tail is placed by content.
        out = self.merge_js("""
const rows = (a, b) => Array.from({ length: b - a }, (_, i) => `line ${a + i}`);
const base = { history_limit: 2000, history_size: 90, width: 80, height: 10, total_lines: 100 };
const { page } = mergeTerminalTail(null, { ...base, lines: rows(0, 40), first_line: 0, end_line: 100 });
// the next tail starts with a half line ("ne 20" = the wrapped end of line 20), then lines 21..44;
// line 38 (on screen) was redrawn
const tail = ["ne 20"].concat(rows(21, 45).map((r) => r === "line 38" ? "line 38*" : r));
const next = mergeTerminalTail(page, { ...base, lines: tail, first_line: 60, end_line: 110, total_lines: 110 });
const t = next.page.lines;
const __out = { mode: next.mode, n: t.length, l20: t[20], l21: t[21], l38: t[38], last: t.at(-1),
                unique: new Set(t).size === t.length, first: next.page.firstLine };""")
        self.assertEqual(out, {"mode": "aligned", "n": 45, "l20": "line 20", "l21": "line 21", "l38": "line 38*",
                               "last": "line 44", "unique": True, "first": 0})

    def test_tail_merge_survives_full_history_and_falls_back_without_duplicates(self) -> None:
        out = self.merge_js("""
const rows = (a, b) => Array.from({ length: b - a }, (_, i) => `line ${a + i}`);
const base = { history_limit: 100, history_size: 100, width: 80, height: 10, total_lines: 110 };
const page = { lines: rows(0, 110), firstLine: 0, totalLines: 110, height: 10 };
const gapPage = { ...page, lines: [...rows(0, 93), "", "", ...rows(95, 110)] };
// full history: 7 lines dropped at the top, 7 new at the bottom; numbers did not move
const merged = mergeTerminalTail(page, { ...base, lines: rows(57, 117), first_line: 50, end_line: 110 });
const texts = merged.page.lines;
// blank lines cannot anchor; a tail that shares nothing replaces the page (no duplicates)
const blank = mergeTerminalTail(page, { ...base, lines: Array(60).fill(""), first_line: 50, end_line: 110 });
const far = mergeTerminalTail(page, { ...base, lines: rows(500, 560), first_line: 50, end_line: 110 });
// blank lines before the anchor are skipped: the first non-blank line starts the anchor
const gappy = mergeTerminalTail(gapPage, { ...base, lines: ["half", "", "line 95", ...rows(96, 112)], first_line: 90, end_line: 110 });
// the anchor sits at the very top of the page and blank lines precede it: the tail covers the page
const covered = mergeTerminalTail({ ...page, lines: rows(50, 60) }, { ...base, lines: ["half", "", "", ...rows(50, 65)], first_line: 0, end_line: 110 });
const __out = { mode: merged.mode, n: texts.length, consecutive: texts.every((t, i) => t === `line ${i}`),
                blank: [blank.mode, blank.page.lines.length], far: [far.mode, far.page.lines[0]],
                gappy: [gappy.mode, gappy.page.lines.length, gappy.page.lines.at(-1), new Set(gappy.page.lines.filter(Boolean)).size === gappy.page.lines.filter(Boolean).length],
                covered: [covered.mode, covered.page.lines.length] };""")
        self.assertEqual(out, {"mode": "aligned", "n": 117, "consecutive": True, "blank": ["replaced", 60],
                               "far": ["replaced", "line 500"], "gappy": ["aligned", 112, "line 111", True],
                               "covered": ["covered", 18]})

    def test_older_rows_are_prepended_by_content_without_duplicates(self) -> None:
        out = self.merge_js("""
const rows = (a, b) => Array.from({ length: b - a }, (_, i) => `line ${a + i}`);
// page starts with a half line "ne 600"; the older chunk overlaps it by the slack and has the full line
const page = { lines: ["ne 600", ...rows(601, 700)], firstLine: 600, totalLines: 700 };
const older = prependTerminalOlder(page, { lines: rows(0, 640), first_line: 0 });
// the page top is no longer in tmux (full history dropped it): nothing more to load from tmux
const gone = prependTerminalOlder(page, { lines: rows(900, 1000), first_line: 0 });
const __out = { n: older.lines.length, ok: older.lines.every((t, i) => t === `line ${i}`), first: older.firstLine,
                gone: [gone.noOlder, gone.lines.length] };""")
        self.assertEqual(out, {"n": 700, "ok": True, "first": 0, "gone": [True, 100]})

    def transcript_js(self, body: str) -> object:
        merge = snippet(self.source, "    function terminalPlainRow(row)", "\n    function terminalRowsMatch(")
        transcript = snippet(self.source, "    const TERMINAL_SEAM = ", "\n    function terminalTranscriptState(")
        return self.js(merge + "\n" + transcript + "\n" + body)

    def test_transcript_blocks_render_terminal_style(self) -> None:
        out = self.transcript_js(r"""
const esc = String.fromCharCode(27);
const plain = (lines) => lines.map(terminalPlainRow);
const __out = {
  user: plain(transcriptBlockLines({ role: "user", text: "帮我看看\n第二行" })),
  ai: plain(transcriptBlockLines({ role: "assistant", label: "AI output", text: "好的，已完成。\n\n- 一" })),
  tool: plain(transcriptBlockLines({ role: "tool", label: "Tool", text: "Shell command\nls -la   /tmp" })),
  result: transcriptBlockLines({ role: "tool", label: "Tool result", text: "a lot of output" }),
  other: plain(transcriptBlockLines({ role: "thinking", text: "想一想\n细节" })),
  injected: plain(transcriptBlockLines({ role: "user", text: `x${esc}[31my` })),
  userColour: transcriptBlockLines({ role: "user", text: "hi" })[0].startsWith(`${esc}[38;2;189;147;249m›`),
};""")
        self.assertEqual(out["user"], ["› 帮我看看", "  第二行", ""])
        self.assertEqual(out["ai"], ["⏺ 好的，已完成。", "", "  - 一", ""])
        self.assertEqual(out["tool"], ["  ⎿ Shell command · ls -la /tmp"])
        self.assertEqual(out["result"], [])
        self.assertEqual(out["other"], ["  · 想一想"])
        self.assertEqual(out["injected"], ["› x[31my", ""])  # a raw ESC in the transcript is dropped, not interpreted
        self.assertTrue(out["userColour"])

    def test_transcript_first_page_skips_messages_already_on_screen(self) -> None:
        out = self.transcript_js(r"""
const blocks = [
  { role: "user", text: "很早以前的问题：怎么部署" },
  { role: "assistant", text: "很早以前的回答：先跑测试" },
  { role: "tool", label: "Tool", text: "Shell command\nls" },
  { role: "user", text: "**最近**的问题：卡片为什么抖动" },
  { role: "assistant", text: "最近的回答：因为输入框量高度" },
];
const screen = ["⏺ Bash(ls)", "> 最近的问题：卡片为什么抖动", "", "⏺ 最近的回答：因为输入框量高度"];
const found = transcriptOlderThanScreen(blocks, screen).map((b) => b.text);
const none = transcriptOlderThanScreen(blocks, ["完全无关的画面"]).length;
const short = transcriptOlderThanScreen([{ role: "user", text: "好" }, { role: "assistant", text: "好" }], ["好"]).length;
const __out = { found, none, short };""")
        self.assertEqual(out["found"], ["很早以前的问题：怎么部署", "很早以前的回答：先跑测试", "Shell command\nls"])
        self.assertEqual(out["none"], 5)   # nothing matched: show everything (a duplicate beats a gap)
        self.assertEqual(out["short"], 2)  # too short to match safely: keep

    def test_older_source_uses_scrollback_first_then_transcript(self) -> None:
        out = self.transcript_js("""
const claude = { kind: "Claude" }, codex = { kind: "Codex" }, shell = { kind: "Shell" };
const __out = {
  claudeAltScreen: terminalOlderSource({ firstLine: 0, historySize: 0 }, claude),
  codexScrollback: terminalOlderSource({ firstLine: 1200 }, codex),
  codexTop: terminalOlderSource({ firstLine: 0 }, codex),
  codexDropped: terminalOlderSource({ firstLine: 300, noOlder: true }, codex),
  shellTop: terminalOlderSource({ firstLine: 0 }, shell),
  noPage: terminalOlderSource(null, claude),
};""")
        self.assertEqual(out, {"claudeAltScreen": "transcript", "codexScrollback": "tmux", "codexTop": "transcript",
                               "codexDropped": "transcript", "shellTop": "none", "noPage": "none"})

    def test_loading_older_keeps_the_reading_position(self) -> None:
        older = snippet(self.source, "    async function loadOlderTerminal(paneId)", "\n    // 切到某个窗口时")
        self.assertIn('const source = terminalOlderSource(current, pane);', older)
        self.assertIn("await loadTerminalTranscriptOlder(paneId);", older)
        self.assertEqual(older.count("wrap.scrollTop += wrap.scrollHeight - "), 2)
        render = snippet(self.source, "    function renderTerminalPage(paneId)", "\n    // 内容还没撑满可视区")
        self.assertIn("older.length ? older.concat(TERMINAL_SEAM, live) : live", render)
        # transcript pages are fetched from the same endpoint the dialog view pages with
        fetcher = snippet(self.source, "    async function loadTerminalTranscriptOlder(paneId)", "\n    // 视图模式全局记忆")
        self.assertIn("`${BASE}/api/history_before?${params.toString()}`", fetcher)
        self.assertIn("transcriptOlderThanScreen(blocks, page?.lines || [])", fetcher)

    def test_scroll_follow_double_click_and_end_jump_to_latest(self) -> None:
        scroll = snippet(self.source, "    function handleTimelineScroll()", "\n    function redirectComposerWheelToTimeline")
        self.assertIn("state.terminalFollowLatest = wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight < 40;", scroll)
        self.assertIn("if (wrap.scrollTop <= 80) loadOlderTerminal(state.selected);", scroll)
        fetcher = snippet(self.source, "    async function fetchTerminalCapture(", "\n    // 首次读最新")
        self.assertIn('new URLSearchParams({ pane: paneId, join: "1", ...params })', fetcher)
        panel = snippet(self.source, '<section class="terminal-panel" id="terminalPanel"', "</section>")
        for gone in ("terminal-view-head", "tmux 实时视图", "回到最新", "<button"):
            self.assertNotIn(gone, panel)
        self.assertNotIn("terminalBackLatest", self.source)
        wiring = snippet(self.source, '    el("terminalPanel").addEventListener("dblclick", () => {', "\n    el(\"traceToggle\")")
        self.assertIn("if (!String(window.getSelection?.() || \"\").trim()) jumpTerminalToLatest();", wiring)
        self.assertIn('if (event.key !== "End" || !state.terminalOpen', wiring)
        self.assertIn('event.target.closest("textarea, input, select, [contenteditable]")', wiring)

    def latest_js(self, body: str) -> object:
        latest = snippet(self.source, "    function loadTerminalLatest(paneId", "\n    // 往上翻到顶")
        anchor = snippet(self.source, "    function anchorTerminalBottom()", "\n    // 回到最新")
        merge = snippet(self.source, "    const TERMINAL_TAIL_EXTRA = 150;", "\n    // ---- 卡片内 tmux 视图: tmux 顶部之上接对话记录")
        script = (f"""
const wrap = {{ scrollTop: 400, scrollHeight: 1000, clientHeight: 600, writes: 0 }};
const wrapProxy = new Proxy(wrap, {{ set(t, k, v) {{ if (k === "scrollTop") t.writes += 1; t[k] = v; return true; }} }});
const document = {{ querySelector: () => wrapProxy }};
const state = {{ terminalOpen: true, selected: "%9", terminalLoadingOlder: false, terminalFollowLatest: true,
  terminalCaptureSeq: 0, terminalAbortController: null, terminalPagesByPane: new Map(), terminalUnchangedPolls: 0,
  terminalLatestInFlight: null }};
const TERMINAL_PAGE_LINES = 600;
let renders = 0, fills = 0, next = null, renderThrows = false;
const renderTerminalPage = () => {{ if (renderThrows) throw new Error("boom"); renders += 1; wrap.scrollHeight += 20; }};
const fillTerminalViewport = () => {{ fills += 1; }};
const showToast = () => {{}};
const el = () => ({{}});
const fetchTerminalCapture = async () => next;
const terminalPageState = (paneId) => state.terminalPagesByPane.get(paneId) || null;
{merge}
{anchor}
{latest}
{body}
__main().then((out) => process.stdout.write(JSON.stringify(out)));""")
        result = subprocess.run(["node", "-e", script], check=False, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def test_unchanged_content_never_touches_scroll_or_dom(self) -> None:
        out = self.latest_js("""
const __main = async () => {
  state.terminalPagesByPane.set("%9", { lines: ["a", "b"], firstLine: 0, totalLines: 2, height: 2, tailHash: "h1" });
  next = { unchanged: true, hash: "h1" };
  for (let i = 0; i < 5; i += 1) await loadTerminalLatest("%9", false);
  const idle = { writes: wrap.writes, renders, unchanged: state.terminalUnchangedPolls };
  // not following + new output: rendered, but scrollTop is left alone
  state.terminalFollowLatest = false;
  next = { lines: ["a", "b", "c"], first_line: 0, end_line: 3, total_lines: 3, height: 2, hash: "h2" };
  await loadTerminalLatest("%9", false);
  const reading = { writes: wrap.writes, renders };
  // following + new output: one direct jump to the bottom
  state.terminalFollowLatest = true;
  next = { lines: ["a", "b", "c", "d"], first_line: 0, end_line: 4, total_lines: 4, height: 2, hash: "h3" };
  await loadTerminalLatest("%9", false);
  return { idle, reading, following: { writes: wrap.writes, top: wrap.scrollTop, bottom: wrap.scrollHeight - wrap.clientHeight } };
};""")
        self.assertEqual(out["idle"], {"writes": 0, "renders": 0, "unchanged": 5})
        self.assertEqual(out["reading"], {"writes": 0, "renders": 1})
        self.assertEqual(out["following"]["writes"], 1)
        self.assertEqual(out["following"]["top"], out["following"]["bottom"])

    def test_new_content_is_always_accepted(self) -> None:
        # alt-screen (no history), history not full, row numbers unchanged while the content changes,
        # a merge that throws, malformed lines, and a render that throws: every one must move tailHash
        # on and show the new content, so the next request never repeats the old if_hash.
        out = self.latest_js("""
const console = { errors: [], error(...a) { this.errors.push(String(a[0])); } };
const page = (lines, extra = {}) => ({ lines, first_line: 0, end_line: lines.length, total_lines: lines.length, width: 80, height: 34,
  history_size: 0, history_limit: 2000, ...extra });
const __main = async () => {
  const seen = [];
  const step = async (data) => { next = data; await loadTerminalLatest("%9", false); const p = state.terminalPagesByPane.get("%9"); seen.push([p.tailHash, p.lines.at(-1)]); };
  await step({ ...page(["✻ Working 1s", "> "]), hash: "a1" });                       // alt screen, first capture
  await step({ ...page(["✻ Working 2s", "> "]), hash: "a2" });                       // same rows, content changed
  await step({ ...page(["x", "y", "z"], { history_size: 40 }), hash: "a3" });         // history not full
  const realMerge = mergeTerminalTail;
  mergeTerminalTail = () => { throw new Error("merge exploded"); };
  await step({ ...page(["after merge error"]), hash: "a4" });
  mergeTerminalTail = realMerge;
  await step({ ...page([]), lines: undefined, hash: "a5", width: 80 });               // malformed: no lines
  renderThrows = true;
  next = { ...page(["render fails"]), hash: "a6" };
  let rejected = false;
  try { await loadTerminalLatest("%9", false); } catch (_) { rejected = true; }
  renderThrows = false;
  const p = state.terminalPagesByPane.get("%9");
  return { seen, afterRenderError: [p.tailHash, p.lines.at(-1)], rejected, errors: console.errors.length };
};""")
        self.assertEqual(out["seen"], [["a1", "> "], ["a2", "> "], ["a3", "z"], ["a4", "after merge error"], ["a5", None]])
        self.assertEqual(out["afterRenderError"], ["a6", "render fails"])
        self.assertFalse(out["rejected"])
        self.assertEqual(out["errors"], 2)  # the merge and the render failure are both logged

    def test_triggers_while_a_request_is_in_flight_do_not_starve_it(self) -> None:
        # 2026-09-24: every kick aborted the in-flight request; with responses slower than the kicks
        # nothing ever completed and the browser sent the same if_hash 1582 times in 24 s.
        latest = snippet(self.source, "    function loadTerminalLatest(paneId", "\n    // 往上翻到顶")
        poll = snippet(self.source, "    const TERMINAL_POLL_FAST_MS = 300;", "\n    // ---- 卡片内 tmux 视图: 让窗口跟随查看者尺寸") \
            .replace("= 300;", "= 30;").replace("= 1000;", "= 60;")
        merge = snippet(self.source, "    const TERMINAL_TAIL_EXTRA = 150;", "\n    // ---- 卡片内 tmux 视图: tmux 顶部之上接对话记录")
        script = f"""
const state = {{ terminalOpen: true, selected: "%9", terminalLoadingOlder: false, terminalFollowLatest: true, sharedFilesOpen: false,
  terminalCaptureSeq: 0, terminalAbortController: null, terminalPagesByPane: new Map(), terminalUnchangedPolls: 0,
  terminalLastChangeAt: 0, terminalPollTimer: 0, terminalPollInFlight: false, terminalPollKick: false,
  terminalLatestInFlight: null, panes: [], thinkingSince: {{}} }};
const document = {{ hidden: false }};
const TERMINAL_PAGE_LINES = 600;
const terminalPageState = (p) => state.terminalPagesByPane.get(p) || null;
const renderTerminalPage = () => {{}}, fillTerminalViewport = () => {{}}, anchorTerminalBottom = () => {{}}, showToast = () => {{}};
const el = () => ({{}}); const paneIsProcessing = () => true;
let version = 0, inFlight = 0, maxInFlight = 0, aborted = 0; const sent = [];
const ticker = setInterval(() => {{ version += 1; }}, 15);
const fetchTerminalCapture = (paneId, params, signal) => new Promise((resolve, reject) => {{
  sent.push(params.if_hash || ""); inFlight += 1; maxInFlight = Math.max(maxInFlight, inFlight);
  const lines = Array.from({{ length: 20 }}, (_, i) => `row ${{i}} v${{version}}`);
  const t = setTimeout(() => {{ inFlight -= 1; resolve({{ lines, first_line: 0, end_line: 20, total_lines: 20, width: 80, height: 34,
    history_size: 0, history_limit: 2000, hash: "h" + sent.length }}); }}, 45);
  signal?.addEventListener("abort", () => {{ clearTimeout(t); inFlight -= 1; aborted += 1; const e = new Error("aborted"); e.name = "AbortError"; reject(e); }});
}});
{merge}
{latest}
{poll}
scheduleTerminalPoll(0);
const kicker = setInterval(() => {{ kickTerminalPoll(); loadTerminalLatest("%9", true); }}, 20);
setTimeout(() => {{
  clearInterval(kicker);
  setTimeout(() => {{
    state.terminalOpen = false; clearInterval(ticker);
    const counts = sent.reduce((m, h) => (m[h] = (m[h] || 0) + 1, m), {{}});
    process.stdout.write(JSON.stringify({{ requests: sent.length, maxRepeat: Math.max(...Object.values(counts)), maxInFlight, aborted,
      tail: state.terminalPagesByPane.get("%9")?.tailHash || null }}));
    process.exit(0);
  }}, 200);
}}, 800);
"""
        result = subprocess.run(["node", "-e", script], check=False, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out["aborted"], 0)
        self.assertEqual(out["maxInFlight"], 1)
        self.assertEqual(out["maxRepeat"], 1)        # every request carries the hash of the previous answer
        self.assertGreater(out["requests"], 8)       # and polling kept going at the fast cadence
        self.assertTrue(out["tail"])

    def test_composer_resize_restores_the_scroll_position(self) -> None:
        resize = snippet(self.source, "    function autoResizeComposer()", "\n    function isMobileLayout()")
        out = self.js(f"""
const wrap = {{ scrollTop: 900 }};
const textarea = {{ style: {{}}, get scrollHeight() {{ wrap.scrollTop = 850; return 42; }} }};  // "auto" height clamps the wrap
const document = {{ querySelector: () => wrap }};
const el = () => textarea;
const state = {{ mobileComposerExpanded: true }};
const isMobileLayout = () => false;
{resize}
autoResizeComposer();
const __out = {{ top: wrap.scrollTop, height: textarea.style.height }};""")
        self.assertEqual(out, {"top": 900, "height": "42px"})

    def test_poll_cadence(self) -> None:
        cadence = snippet(self.source, "    const TERMINAL_POLL_FAST_MS = 300;", "\n    function scheduleTerminalPoll(")
        out = self.js(cadence + """
const d = (o) => terminalPollDelay({ hidden: false, busy: false, sinceChangeMs: 99999, unchangedPolls: 99, ...o });
const __out = { idle: d({}), busy: d({ busy: true }), recent: d({ sinceChangeMs: 1000 }), warm: d({ unchangedPolls: 9 }),
                hidden: d({ hidden: true, busy: true }) };""")
        self.assertEqual(out, {"idle": 1000, "busy": 300, "recent": 300, "warm": 300, "hidden": 5000})
        self.assertIn("if (paneId === state.selected) kickTerminalPoll();", snippet(self.source, "    function markPaneRunning(", "\n    }"))
        refresh = snippet(self.source, "    async function refreshLoop()", "\n    applyFontSize();")
        self.assertIn("!state.terminalOpen) {\n          await loadCapture(state.selected, false);", refresh)
        self.assertNotIn("loadTerminalLatest", refresh)

    def display_js(self, body: str) -> object:
        ansi = snippet(self.source, "    const TERMINAL_BG = ", "\n    function terminalPageState(")
        merge = snippet(self.source, "    function terminalPlainRow(row)", "\n    function terminalRowsMatch(")
        seam = 'const TERMINAL_SEAM = "\\u0000seam"; const TERMINAL_TX = "\\u0001";'
        display = snippet(self.source, "    const TERMINAL_RULE_RE", "\n    function terminalLineNode(")
        return self.js(f"""
const escapeHtml = (value) => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
{ansi}
{merge}
{seam}
{display}
{body}""")

    def test_rule_lines_blank_trimming_and_diff(self) -> None:
        out = self.display_js(r"""
const esc = String.fromCharCode(27);
const kinds = ["────────", "  ╭────╮", "│ text │", `${esc}[38;5;244m──────${esc}[39m`, "a - b", "", "   ", "-", "====", TERMINAL_SEAM].map(terminalLineKind);
const page = { height: 6, lines: ["old", "", "", "keep", "", "", "", "> prompt", "", "", ""] };
const shown = terminalDisplayLines(page);
const empty = terminalDisplayLines({ height: 5, lines: ["", "  ", ""] });
// Claude draws separators as blank rows with a background colour: one faint line, not a stack of bars
const bg = `${esc}[48;2;55;55;55m      ${esc}[49m`;
const bars = terminalDisplayLines({ height: 9, lines: ["top", bg, bg, `  ${esc}[41m  ${esc}[0m`, bg, "", "mid", "   ", "", "end", bg] });
const fgOnly = terminalDisplayLines({ height: 3, lines: ["a", `${esc}[38;5;48m   ${esc}[39m`, "b"] });
const hasBg = [bg, `${esc}[41mx`, `${esc}[101mx`, `${esc}[38;5;48mx`, `${esc}[38;2;1;48;3mx`, "plain"].map(terminalHasBackground);
const diffTail = terminalLineDiff(["a", "b", "c"], ["a", "b", "c2", "d"]);
const diffHead = terminalLineDiff(["c", "d"], ["a", "b", "c", "d"]);
const __out = { kinds, shown, empty, bars, fgOnly, hasBg, diffTail, diffHead };""")
        self.assertEqual(out["kinds"], ["rule", "rule", "text", "rule", "text", "blank", "blank", "text", "rule", "seam"])
        # runs of whitespace-only lines collapse to one line everywhere
        self.assertEqual(out["shown"], ["old", "", "keep", "", "> prompt", ""])
        self.assertEqual(out["empty"], [])
        seam = "\u0000seam"
        self.assertEqual(out["bars"], ["top", seam, "mid", "", "end", seam])
        self.assertEqual(out["fgOnly"], ["a", "", "b"])  # a foreground colour is not a background
        self.assertEqual(out["hasBg"], [True, True, True, False, False, False])
        node = snippet(self.source, "    function terminalLineNode(line, fit = false)", "\n    function patchTerminalLines(")
        self.assertIn('if (kind === "rule") node.textContent = terminalPlainRow(line)', node)
        self.assertIn('kind !== "seam" && kind !== "blank"', node)
        self.assertEqual(out["diffTail"], {"head": 2, "remove": 1, "insert": ["c2", "d"]})
        self.assertEqual(out["diffHead"], {"head": 0, "remove": 0, "insert": ["a", "b"]})
        css = snippet(self.source, "    .terminal-lines .tl {", "\n    .terminal-lines .tl.seam")
        self.assertIn("white-space: pre-wrap;", css)
        self.assertIn("overflow-wrap: anywhere;", css)
        self.assertRegex(css, r"\.tl\.rule \{\s*white-space: pre;\s*overflow: hidden;")
        lines_css = snippet(self.source, "    .terminal-lines {", "}")
        self.assertIn('font-family: ui-monospace, "SFMono-Regular", Menlo, Consolas, "Noto Sans Mono CJK SC", "Sarasa Mono SC", monospace;', lines_css)
        self.assertIn("line-height: 1.45;", lines_css)
        patch = snippet(self.source, "    function patchTerminalLines(container, next, fit = false)", "\n    function showTerminalPlaceholder(")
        self.assertIn("（这个窗口暂时没有输出）", patch)
        self.assertIn("const diff = terminalLineDiff(prev, next);", patch)

    def test_two_column_rows_are_split_before_reflow(self) -> None:
        out = self.display_js(r"""
const esc = String.fromCharCode(27);
const plain = (s) => terminalPlainRow(s);
const wide = `${esc}[49m     ${esc}[37mnohup run (16s)${esc}[39m` + " ".repeat(90) + `${esc}[38;5;244m${"─".repeat(55)}${esc}[39m`;
const num = `${esc}[37m  ⎿  echo 6${esc}[39m` + " ".repeat(120) + `${esc}[40m ${esc}[2m 332${esc}[0m`;
const table = "| a  | b   |";
const page = { height: 5, lines: ["top", wide, num, table, `${esc}[49m${" ".repeat(150)}${esc}[38;5;244m${"─".repeat(40)}${esc}[39m`] };
const shown = terminalDisplayLines(page).map(plain);
const right = splitTerminalColumns(`${esc}[32mleft${esc}[39m` + " ".repeat(30) + "right");
const __out = { shown, right, colour: splitTerminalColumns(wide)[0].includes(`${esc}[37mnohup run (16s)`),
                blackBg: ansiToHtml(`${esc}[40m x`), black256: ansiToHtml(`${esc}[48;5;0m x`), redBg: ansiToHtml(`${esc}[41m x`) };""")
        # a >= 20-space gap splits the row into columns; a right part that is only a rule is dropped
        self.assertEqual(out["shown"], ["top", "     nohup run (16s)", "  ⎿  echo 6", "332", "| a  | b   |", "─" * 40])
        self.assertEqual(out["right"], ["\x1b[32mleft\x1b[39m", "\x1b[32m\x1b[39mright"])  # right part keeps the styles
        self.assertTrue(out["colour"])
        self.assertNotIn("background", out["blackBg"])   # black background = the terminal's own background
        self.assertNotIn("background", out["black256"])
        self.assertIn("background-color:#ff5555", out["redBg"])

    def test_fit_mode_renders_cells_like_tmux(self) -> None:
        out = self.display_js(r"""
const esc = String.fromCharCode(27);
const __out = {
  cells: ansiToHtml(`│ 中文 a⏺ ${esc}[31m表${esc}[39m │`, true),
  rule: ansiToHtml("├──────┤", true),
  plain: ansiToHtml("│ 中文 │"),
  txKind: terminalLineKind(TERMINAL_TX + "   "),
};""")
        self.assertEqual(out["cells"], '<span class="c1">│</span> <span class="c2">中</span><span class="c2">文</span> '
                                       'a<span class="c1">⏺</span> <span style="color:#ff5555"><span class="c2">表</span></span> '
                                       '<span class="c1">│</span>')
        # box drawing is cell-sized too (fallback fonts draw it 1em wide); long runs are one clipped cell block
        self.assertEqual(out["rule"], '<span class="c1">├</span><span class="cr" style="width:6ch">──────</span><span class="c1">┤</span>')
        self.assertEqual(out["plain"], "│ 中文 │")  # reflow mode: no cells
        self.assertEqual(out["txKind"], "text")
        css = snippet(self.source, "    .terminal-lines .c1 { width: 1ch; }", "\n")
        self.assertIn("1ch", css)
        self.assertIn(".terminal-lines .c2 { width: 2ch; }", self.source)
        self.assertRegex(self.source, r"\.terminal-lines\.fit \.tl \{\s*white-space: pre;")
        render = snippet(self.source, "    function renderTerminalPage(paneId)", "\n    // 内容还没撑满可视区")
        self.assertIn("const fit = page.width > 0 && page.width <= terminalGrid().rawCols;", render)
        self.assertIn("(transcript?.lines || []).map((line) => TERMINAL_TX + line)", render)
        node = snippet(self.source, "    function terminalLineNode(line, fit = false)", "\n    function patchTerminalLines(")
        self.assertIn('node.className = "tl tx";', node)
        self.assertIn("node.innerHTML = ansiToHtml(line, true);", node)

    def test_resize_requests_follow_the_viewer(self) -> None:
        needed = snippet(self.source, "    const TERMINAL_COLS = [40, 250];", "\n    function terminalGrid()") + \
            snippet(self.source, "    function terminalResizeNeeded(", "\n    function scheduleTerminalResize(")
        out = self.js(needed + """
const g = (cols, rows, rawCols = cols, rawRows = rows) => ({ cols, rows, rawCols, rawRows });
const page = { width: 361, height: 55 };
const __out = {
  wide: terminalResizeNeeded(page, g(110, 40), null, 1000),
  same: terminalResizeNeeded({ width: 111, height: 41 }, g(110, 40), null, 1000),
  justSent: terminalResizeNeeded(page, g(111, 40), { cols: 110, rows: 40, at: 0 }, 30000),
  sentLongAgo: terminalResizeNeeded(page, g(111, 40), { cols: 110, rows: 40, at: 0 }, 70000),
  tiny: terminalResizeNeeded(page, g(40, 12, 20, 3), null, 1000),
};""")
        self.assertEqual(out, {"wide": True, "same": False, "justSent": False, "sentLongAgo": True, "tiny": False})
        send = snippet(self.source, "    async function sendTerminalResize(paneId)", "\n    async function fetchTerminalCapture(")
        self.assertIn("`${BASE}/api/terminal/resize`", send)
        self.assertIn('headers: { "Content-Type": "application/json" }', send)
        self.assertIn("if (data.resized) kickTerminalPoll();", send)
        self.assertIn("console.info(", send)
        self.assertIn("state.terminalResizeTimer = setTimeout(() => sendTerminalResize(paneId), 500);", self.source)
        self.assertIn("}).observe(document.querySelector(\".timeline-wrap\"));", self.source)
        latest = snippet(self.source, "    function loadTerminalLatest(paneId", "\n    // 往上翻到顶")
        self.assertIn("const resized = current && Number(data.width || 0) !== current.width;", latest)

    def test_palette_contrast_and_sgr(self) -> None:
        out = self.display_js(r"""
const esc = String.fromCharCode(27);
const html = (s) => ansiToHtml(s);
const colour = (s) => (/color:(#[0-9a-f]{6})/.exec(html(s)) || [])[1];
const ratios = {};
for (const [name, seq] of Object.entries({ black: "30", blue: "34", grey236: "38;5;236", pureBlack: "38;2;0;0;0",
                                            darkRed: "38;2;90;0;0", red: "31", green: "32", comment: "90" })) {
  ratios[name] = contrastRatio(colour(`${esc}[${seq}mx`), TERMINAL_BG);
}
const onWhite = /color:(#[0-9a-f]{6})/.exec(html(`${esc}[48;2;255;255;255mx`))[1];
const __out = {
  ratios,
  onWhite: contrastRatio(onWhite, "#ffffff"),
  palette: [TERMINAL_PALETTE[1], TERMINAL_PALETTE[2], TERMINAL_PALETTE[4]],
  escaped: html(`plain <tag> ${esc}[31mred${esc}[0m & done`),
  bold: html(`${esc}[1mB`), dim: html(`${esc}[2mD`), under: html(`${esc}[4mU`),
  inverse: html(`${esc}[7mI`),
  truecolor: html(`${esc}[38;2;1;200;3mT`), colon: html(`${esc}[38:2::1:200:3mT`), c256: html(`${esc}[38;5;208mO`),
  plain: html("no colour"),
};""")
        for name, ratio in out["ratios"].items():
            self.assertGreaterEqual(ratio, 4.5, name)
        self.assertGreaterEqual(out["onWhite"], 4.5)
        self.assertEqual(out["palette"], ["#ff5555", "#50fa7b", "#bd93f9"])
        self.assertIn("plain &lt;tag&gt; ", out["escaped"])
        self.assertIn('<span style="color:#ff5555">red</span>', out["escaped"])
        self.assertIn("&amp; done", out["escaped"])
        self.assertIn("font-weight:700", out["bold"])
        self.assertIn("opacity:.62", out["dim"])
        self.assertIn("text-decoration:underline", out["under"])
        self.assertIn("background-color:#f8f8f2", out["inverse"])
        self.assertIn("color:#01c803", out["truecolor"])
        self.assertIn("color:#01c803", out["colon"])
        self.assertIn("color:#ff8700", out["c256"])
        self.assertEqual(out["plain"], "no colour")

if __name__ == "__main__":
    unittest.main(verbosity=2)
