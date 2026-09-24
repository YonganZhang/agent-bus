#!/usr/bin/env python3
"""Manual browser E2E (not collected by pytest): mobile composer send behavior.

Run against a live dashboard: ``python3 tests/e2e/mobile_send_e2e.py``
(needs Playwright + Chromium, a running dashboard at TMUX_CARD_TEST_URL, and
Basic Auth credentials). It opens a temporary window in TMUX_CARD_SESSION.

Creates a temporary tmux pane that echoes submitted lines, opens the dashboard
in a mobile Chromium viewport, and verifies the three send paths used by phone
keyboards.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "dashboard"
WEBTERM_ENV = Path(os.environ.get("WEBTERM_ENV", str(Path(os.environ.get("AGENT_BUS_DIR", str(Path.home() / ".codex" / "agent-bus"))) / "webterm.env")))
BASE_URL = os.environ.get("TMUX_CARD_TEST_URL", "http://127.0.0.1:7795/cards")
SESSION = os.environ.get("TMUX_CARD_SESSION", "secretary_web")


def run_tmux(args: list[str]) -> str:
    cp = subprocess.run(["tmux", *args], text=True, capture_output=True, timeout=5, check=False)
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.strip() or f"tmux failed: {' '.join(args)}")
    return cp.stdout


def load_auth() -> tuple[str, str]:
    user = os.environ.get("TMUX_CARD_USER", "")
    password = os.environ.get("TMUX_CARD_PASS", "")
    if WEBTERM_ENV.exists():
        for line in WEBTERM_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if key == "WEBTERM_USER" and not user:
                user = value
            elif key == "WEBTERM_PASS" and not password:
                password = value
    if not user or not password:
        raise RuntimeError("missing dashboard Basic Auth in env/webterm.env")
    return user, password


def capture(pane: str) -> str:
    return run_tmux(["capture-pane", "-p", "-t", pane, "-S", "-160"])


def wait_got(pane: str, text: str, timeout: float = 6) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        captured = capture(pane)
        if f"GOT:{text}" in captured or f"GOT:{text}" in captured.replace("\n", ""):
            return True
        time.sleep(0.2)
    return False


def new_echo_window() -> tuple[str, str]:
    name = f"cards-mobile-send-test-{int(time.time())}"
    command = "bash -lc 'while IFS= read -r line; do printf \"GOT:%s\\n\" \"$line\"; done'"
    out = run_tmux(["new-window", "-d", "-P", "-F", "#{pane_id}\t#{window_id}", "-t", SESSION, "-n", name, command])
    pane, window = out.strip().split("\t", 1)
    deadline = time.time() + 3
    while time.time() < deadline:
        if pane in run_tmux(["list-panes", "-a", "-F", "#{pane_id}"]):
            break
        time.sleep(0.1)
    return pane, window


def main() -> int:
    from playwright.sync_api import sync_playwright

    user, password = load_auth()
    pane, window = new_echo_window()
    stamp = int(time.time())
    texts = {
        "keydown": f"MOBILE_KEYDOWN_SEND_{stamp}",
        "beforeinput": f"MOBILE_BEFOREINPUT_SEND_{stamp}",
        "beforeinput_trailing": f"MOBILE_BEFOREINPUT_TRAILING_SEND_{stamp}",
        "fallback": f"MOBILE_INPUT_FALLBACK_SEND_{stamp}",
        "late_restore": f"MOBILE_LATE_RESTORE_SEND_{stamp}",
        "copy_mode": f"MOBILE_COPY_MODE_SEND_{stamp}",
    }
    results: dict[str, bool] = {}
    sent_payloads: list[dict[str, object]] = []
    header: dict[str, object] = {}
    drawer_drag: dict[str, object] = {}
    composer_button: dict[str, object] = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 390, "height": 844},
                is_mobile=True,
                has_touch=True,
                http_credentials={"username": user, "password": password},
            )
            page = context.new_page()
            page.on(
                "request",
                lambda request: sent_payloads.append(json.loads(request.post_data or "{}"))
                if request.method == "POST" and request.url.endswith("/api/send")
                else None,
            )
            page.goto(BASE_URL, wait_until="domcontentloaded")
            page.wait_for_selector("#cards .card", timeout=8000)
            page.wait_for_function(
                "pane => Array.from(document.querySelectorAll('[data-pane]')).some(el => el.dataset.pane === pane)",
                arg=pane,
                timeout=8000,
            )
            page.evaluate(
                "pane => Array.from(document.querySelectorAll('[data-pane]')).find(el => el.dataset.pane === pane).click()",
                arg=pane,
            )
            page.wait_for_selector(".card.active", timeout=8000)
            page.wait_for_function("() => document.body.classList.contains('mobile-composer-collapsed')", timeout=5000)

            composer_box = page.locator("#mobileComposerFloat").bounding_box()
            if composer_box is None:
                raise RuntimeError("mobile composer toggle is not visible")
            page.locator("#mobileComposerFloat").click()
            page.wait_for_function(
                "() => document.body.classList.contains('mobile-composer-expanded') && document.activeElement?.id === 'composerText'",
                timeout=5000,
            )
            page.locator("#mobileComposerFloat").click()
            page.wait_for_function("() => document.body.classList.contains('mobile-composer-collapsed')", timeout=5000)

            def open_composer() -> None:
                page.locator("#mobileComposerFloat").click()
                page.wait_for_function(
                    "() => document.body.classList.contains('mobile-composer-expanded') && document.activeElement?.id === 'composerText'",
                    timeout=5000,
                )

            open_composer()
            page.fill("#composerText", texts["keydown"])
            page.locator("#composerText").press("Enter")
            page.wait_for_function("() => document.querySelector('#composerText').value === ''", timeout=5000)
            page.wait_for_function("() => document.body.classList.contains('mobile-composer-collapsed')", timeout=5000)
            results["keydown"] = wait_got(pane, texts["keydown"])

            open_composer()
            page.fill("#composerText", texts["beforeinput"])
            page.evaluate(
                """() => {
                    const textarea = document.querySelector('#composerText');
                    const event = new InputEvent('beforeinput', {
                      inputType: 'insertLineBreak',
                      bubbles: true,
                      cancelable: true
                    });
                    textarea.dispatchEvent(event);
                }"""
            )
            page.wait_for_function("() => document.querySelector('#composerText').value === ''", timeout=5000)
            page.wait_for_function("() => document.body.classList.contains('mobile-composer-collapsed')", timeout=5000)
            results["beforeinput"] = wait_got(pane, texts["beforeinput"])

            open_composer()
            page.evaluate(
                """text => {
                    const textarea = document.querySelector('#composerText');
                    textarea.value = text + String.fromCharCode(10);
                    const event = new InputEvent('beforeinput', {
                      inputType: 'insertLineBreak',
                      bubbles: true,
                      cancelable: true
                    });
                    textarea.dispatchEvent(event);
                }""",
                arg=texts["beforeinput_trailing"],
            )
            page.wait_for_function("() => document.querySelector('#composerText').value === ''", timeout=5000)
            page.wait_for_function("() => document.body.classList.contains('mobile-composer-collapsed')", timeout=5000)
            results["beforeinput_trailing"] = wait_got(pane, texts["beforeinput_trailing"])

            open_composer()
            page.evaluate(
                """text => {
                    const textarea = document.querySelector('#composerText');
                    textarea.value = text + String.fromCharCode(10);
                    textarea.dispatchEvent(new Event('input', {bubbles: true}));
                }""",
                arg=texts["fallback"],
            )
            page.wait_for_function("() => document.querySelector('#composerText').value === ''", timeout=5000)
            page.wait_for_function("() => document.body.classList.contains('mobile-composer-collapsed')", timeout=5000)
            results["fallback"] = wait_got(pane, texts["fallback"])

            open_composer()
            page.fill("#composerText", texts["late_restore"])
            page.locator("#composerText").press("Enter")
            page.wait_for_timeout(80)
            page.evaluate(
                """text => {
                    const textarea = document.querySelector('#composerText');
                    textarea.value = text;
                    textarea.dispatchEvent(new Event('input', {bubbles: true}));
                }""",
                arg=texts["late_restore"],
            )
            page.wait_for_function("() => document.querySelector('#composerText').value === ''", timeout=5000)
            page.wait_for_function("() => document.body.classList.contains('mobile-composer-collapsed')", timeout=5000)
            results["late_restore"] = wait_got(pane, texts["late_restore"])

            run_tmux(["copy-mode", "-t", pane])
            open_composer()
            page.fill("#composerText", texts["copy_mode"])
            page.locator("#composerText").press("Enter")
            page.wait_for_function("() => document.querySelector('#composerText').value === ''", timeout=5000)
            page.wait_for_function("() => document.body.classList.contains('mobile-composer-collapsed')", timeout=5000)
            results["copy_mode"] = wait_got(pane, texts["copy_mode"])

            page.wait_for_timeout(300)
            header = page.evaluate(
                """() => ({
                    sublineDisplay: getComputedStyle(document.querySelector('.detail-subline')).display,
                    settingsDisplay: getComputedStyle(document.querySelector('#settingsSummary')).display,
                    title: document.querySelector('#detailTitle').textContent.trim(),
                    composerHeight: Math.round(document.querySelector('.composer').getBoundingClientRect().height),
                    shellHeight: Math.round(document.querySelector('.composer-shell').getBoundingClientRect().height),
                    textareaHeight: Math.round(document.querySelector('#composerText').getBoundingClientRect().height),
                    composerCollapsed: document.body.classList.contains('mobile-composer-collapsed')
                })"""
            )
            box = page.locator("#mobileDrawerToggle").bounding_box()
            if box is None:
                raise RuntimeError("mobile drawer toggle is not visible")
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.mouse.down()
            page.mouse.move(74, 142, steps=6)
            page.mouse.up()
            drawer_drag = page.evaluate(
                """() => {
                    const button = document.querySelector('#mobileDrawerToggle');
                    const rect = button.getBoundingClientRect();
                    return {
                      left: Math.round(rect.left),
                      top: Math.round(rect.top),
                      stored: Boolean(localStorage.getItem('tmuxCardDrawerTogglePos')),
                      drawerOpen: document.body.classList.contains('mobile-drawer-open')
                    };
                }"""
            )
            composer_box = page.locator("#mobileComposerFloat").bounding_box()
            if composer_box is None:
                raise RuntimeError("mobile composer toggle disappeared")
            page.mouse.move(composer_box["x"] + composer_box["width"] / 2, composer_box["y"] + composer_box["height"] / 2)
            page.mouse.down()
            page.mouse.move(268, 118, steps=6)
            page.mouse.up()
            composer_button = page.evaluate(
                """() => {
                    const button = document.querySelector('#mobileComposerFloat');
                    const rect = button.getBoundingClientRect();
                    return {
                      left: Math.round(rect.left),
                      top: Math.round(rect.top),
                      stored: Boolean(localStorage.getItem('tmuxCardComposerTogglePos')),
                      expanded: document.body.classList.contains('mobile-composer-expanded'),
                      label: button.textContent.trim()
                    };
                }"""
            )
            browser.close()
    finally:
        run_tmux(["kill-window", "-t", window])

    no_trailing_newline = all(not str(payload.get("text", "")).endswith("\n") for payload in sent_payloads)
    drawer_drag_ok = bool(drawer_drag.get("stored")) and not bool(drawer_drag.get("drawerOpen"))
    composer_button_ok = bool(composer_button.get("stored")) and composer_button.get("label") in {"对话框", "收起对话框"}
    line_state_ok = header.get("composerCollapsed") and header.get("composerHeight") <= 10 and header.get("textareaHeight") == 0
    ok = all(results.values()) and no_trailing_newline and drawer_drag_ok and composer_button_ok and line_state_ok
    print(
        json.dumps(
            {
                "ok": ok,
                "results": results,
                "payloads": len(sent_payloads),
                "no_trailing_newline": no_trailing_newline,
                "drawer_drag": drawer_drag,
                "composer_button": composer_button,
                "line_state_ok": line_state_ok,
                "header": header,
            },
            ensure_ascii=False,
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
