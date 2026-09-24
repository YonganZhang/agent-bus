#!/usr/bin/env python3
"""Manual browser E2E (not collected by pytest): Cards close/unfavorite actions
and favorite ordering.

Run against a live dashboard: ``python3 tests/e2e/cards_actions_e2e.py``
(needs Playwright + Chromium, a running dashboard at TMUX_CARD_TEST_URL, and
Basic Auth credentials). It opens temporary windows in TMUX_CARD_SESSION.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Page, Route


WEBTERM_ENV = Path(os.environ.get("WEBTERM_ENV", str(Path(os.environ.get("AGENT_BUS_DIR", str(Path.home() / ".codex" / "agent-bus"))) / "webterm.env")))
BASE_URL = os.environ.get("TMUX_CARD_TEST_URL", "http://127.0.0.1:7795/cards")
SESSION = os.environ.get("TMUX_CARD_SESSION", "secretary_web")


def run_tmux(args: list[str], *, check: bool = True) -> str:
    result = subprocess.run(
        ["tmux", *args],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"tmux failed: {' '.join(args)}")
    return result.stdout


def load_auth() -> tuple[str, str]:
    user = os.environ.get("TMUX_CARD_USER", "")
    password = os.environ.get("TMUX_CARD_PASS", "")
    if WEBTERM_ENV.is_file():
        for raw in WEBTERM_ENV.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
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


def new_test_window(label: str) -> tuple[str, str]:
    name = f"cards-actions-{label}-{time.time_ns()}"
    command = "bash -lc 'while :; do sleep 60; done'"
    output = run_tmux(
        [
            "new-window",
            "-d",
            "-P",
            "-F",
            "#{pane_id}\t#{window_id}",
            "-t",
            SESSION,
            "-n",
            name,
            command,
        ]
    )
    return tuple(output.strip().split("\t", 1))  # type: ignore[return-value]


def pane_exists(pane_id: str) -> bool:
    panes = run_tmux(["list-panes", "-a", "-F", "#{pane_id}"], check=False).splitlines()
    return pane_id in panes


def isolate_prefs(route: Route) -> None:
    if route.request.url.endswith("/api/prefs/favorite"):
        route.fulfill(status=503, content_type="application/json", body='{"error":"test offline"}')
        return
    if route.request.url.endswith("/api/prefs/category"):
        payload = json.loads(route.request.post_data or "{}")
        key = str(payload.get("key") or "")
        category = str(payload.get("category") or "")
        mapping = {key: category} if key and category else {}
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "ok": True,
                "prefs": {
                    "categories": ["最近", "全部", "开发", "论文"],
                    "paneCategories": mapping,
                },
            }, ensure_ascii=False),
        )
        return
    route.fulfill(status=200, content_type="application/json", body='{"ok":true,"prefs":{}}')


def wait_for_test_cards(page: Page, pane_ids: list[str]) -> None:
    page.goto(BASE_URL, wait_until="domcontentloaded")
    page.wait_for_selector("#cards .card", timeout=10_000)
    for pane_id in pane_ids:
        page.wait_for_function(
            "pane => Boolean(document.querySelector(`[data-pane=\"${pane}\"]`))",
            arg=pane_id,
            timeout=10_000,
        )


def install_favorites(page: Page, aliases: dict[str, str]) -> None:
    page.evaluate(
        """aliases => {
          for (const [paneId, name] of Object.entries(aliases)) {
            const pane = state.panes.find((item) => item.pane_id === paneId);
            if (!pane) throw new Error(`missing pane ${paneId}`);
            state.paneAliases[paneStableKey(pane)] = name;
            state.paneFavorites[paneStableKey(pane)] = true;
          }
          state._lastCardsSig = '';
          state.lastFavoritesSignature = '';
          renderCards();
          renderFavoritesBar();
        }""",
        aliases,
    )


def favorite_names(page: Page) -> list[str]:
    return page.locator("#favoritesBar .fav-pill-name").all_text_contents()


def close_card(page: Page, pane_id: str) -> None:
    page.once("dialog", lambda dialog: dialog.accept())
    with page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/api/pane/close"),
        timeout=10_000,
    ) as response_info:
        page.locator(f'[data-pane="{pane_id}"] [data-card-close="{pane_id}"]').click()
    response = response_info.value
    if not response.ok:
        raise AssertionError(f"close API failed: {response.status} {response.text()}")
    page.wait_for_function(
        "pane => !document.querySelector(`[data-pane=\"${pane}\"]`)",
        arg=pane_id,
        timeout=10_000,
    )


def desktop_check(browser: Browser, auth: tuple[str, str], panes: dict[str, str]) -> dict[str, object]:
    context = browser.new_context(
        viewport={"width": 1440, "height": 900},
        http_credentials={"username": auth[0], "password": auth[1]},
    )
    page = context.new_page()
    page.route("**/api/prefs", isolate_prefs)
    page.route("**/api/prefs/favorite", isolate_prefs)
    page.route("**/api/prefs/category", isolate_prefs)
    wait_for_test_cards(page, list(panes.values()))
    aliases = {
        panes["a10"]: "A10",
        panes["a2"]: "a2",
        panes["b1"]: "B1",
    }
    install_favorites(page, aliases)
    page.wait_for_function("() => document.querySelectorAll('#favoritesBar .fav-pill').length === 3")
    ordered = favorite_names(page)
    if ordered != ["a2", "A10", "B1"]:
        raise AssertionError(f"unexpected desktop favorite order: {ordered}")
    if page.locator("[data-card-files]").count() != 0:
        raise AssertionError("per-card attachment action still exists")
    with page.expect_request(
        lambda request: request.method == "POST" and request.url.endswith("/api/prefs/category"),
        timeout=10_000,
    ) as category_request_info:
        page.locator(f'[data-pane="{panes["a2"]}"] [data-card-category]').select_option("开发")
    category_payload = json.loads(category_request_info.value.post_data or "{}")
    if category_payload.get("category") != "开发":
        raise AssertionError(f"category did not use item mutation endpoint: {category_payload}")
    close_menu = page.locator(f'[data-pane="{panes["desktop_close"]}"] .card-menu')
    menu_classes = close_menu.locator(":scope > *").evaluate_all(
        "nodes => nodes.map((node) => node.className || node.tagName)"
    )
    if "card-close" not in str(menu_classes[1]):
        raise AssertionError(f"close did not replace attachment position: {menu_classes}")
    page.locator(f'[data-fav-remove="{panes["a10"]}"]').click()
    page.wait_for_function("() => document.querySelectorAll('#favoritesBar .fav-pill').length === 2")
    if not pane_exists(panes["a10"]):
        raise AssertionError("favorite remove closed the tmux pane")
    close_card(page, panes["desktop_close"])
    if pane_exists(panes["desktop_close"]):
        raise AssertionError("desktop card close did not close tmux pane")
    result = {
        "order": ordered,
        "after_unfavorite": favorite_names(page),
        "file_header_present": page.locator("#sharedFilesToggle").count() == 1,
        "close_position": menu_classes,
        "category_payload": category_payload,
    }
    context.close()
    return result


def mobile_check(browser: Browser, auth: tuple[str, str], panes: dict[str, str]) -> dict[str, object]:
    context = browser.new_context(
        viewport={"width": 390, "height": 844},
        is_mobile=True,
        has_touch=True,
        http_credentials={"username": auth[0], "password": auth[1]},
    )
    page = context.new_page()
    page.route("**/api/prefs", isolate_prefs)
    page.route("**/api/prefs/favorite", isolate_prefs)
    page.route("**/api/prefs/category", isolate_prefs)
    live_panes = [pane for pane in panes.values() if pane_exists(pane)]
    wait_for_test_cards(page, live_panes)
    aliases = {
        panes["a10"]: "A10",
        panes["a2"]: "a2",
        panes["b1"]: "B1",
    }
    install_favorites(page, aliases)
    page.wait_for_function("() => document.querySelectorAll('#favoritesBar .fav-pill').length === 3")
    ordered = favorite_names(page)
    if ordered != ["a2", "A10", "B1"]:
        raise AssertionError(f"unexpected mobile favorite order: {ordered}")
    page.locator(f'[data-fav-remove="{panes["b1"]}"]').click()
    page.wait_for_function("() => document.querySelectorAll('#favoritesBar .fav-pill').length === 2")
    if not pane_exists(panes["b1"]):
        raise AssertionError("mobile favorite remove closed the tmux pane")
    page.locator("#mobileDrawerToggle").click()
    page.wait_for_function("() => document.body.classList.contains('mobile-drawer-open')")
    close_button = page.locator(
        f'[data-pane="{panes["mobile_close"]}"] [data-card-close="{panes["mobile_close"]}"]'
    )
    if not close_button.is_visible():
        raise AssertionError("mobile card close button is not visible")
    close_card(page, panes["mobile_close"])
    if pane_exists(panes["mobile_close"]):
        raise AssertionError("mobile card close did not close tmux pane")
    result = {
        "order": ordered,
        "after_unfavorite": favorite_names(page),
        "close_visible": True,
    }
    context.close()
    return result


def main() -> int:
    from playwright.sync_api import sync_playwright

    auth = load_auth()
    windows: list[str] = []
    panes: dict[str, str] = {}
    try:
        for label in ("a10", "a2", "b1", "desktop_close", "mobile_close"):
            pane_id, window_id = new_test_window(label)
            panes[label] = pane_id
            windows.append(window_id)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            result = {
                "desktop": desktop_check(browser, auth, panes),
                "mobile": mobile_check(browser, auth, panes),
            }
            browser.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        for window_id in windows:
            run_tmux(["kill-window", "-t", window_id], check=False)


if __name__ == "__main__":
    raise SystemExit(main())
