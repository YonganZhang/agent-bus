#!/usr/bin/env python3
"""Safe Agent Bus control surface for AI Session Cards organization metadata."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = os.environ.get("CARDS_BASE_URL", "http://127.0.0.1:7795/cards").rstrip("/")
# Basic Auth credentials file shared with dashboard/server.py (keys
# WEBTERM_USER / WEBTERM_PASS; TMUX_CARD_USER / TMUX_CARD_PASS env vars win).
WEBTERM_ENV = Path(
    os.environ.get(
        "WEBTERM_ENV",
        str(Path(os.environ.get("AGENT_BUS_DIR", str(Path.home() / ".codex" / "agent-bus"))) / "webterm.env"),
    )
).expanduser()
REQUEST_TIMEOUT = float(os.environ.get("CARDS_CONTROL_TIMEOUT", "8"))


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
        raise SystemExit(f"Cards credentials are unavailable: {WEBTERM_ENV}")
    return user, password


class CardsClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")
        user, password = load_auth()
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        self.headers = {"Authorization": f"Basic {token}"}

    def request(self, method: str, path: str, payload: dict[str, object] | None = None) -> dict[str, Any]:
        body = None
        headers = dict(self.headers)
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                error = json.loads(exc.read().decode("utf-8")).get("error")
            except Exception:
                error = None
            raise SystemExit(f"Cards API {exc.code}: {error or exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SystemExit(f"Cards API unavailable at {self.base_url}: {exc}") from exc
        if not isinstance(data, dict):
            raise SystemExit("Cards API returned a non-object response")
        if data.get("error"):
            raise SystemExit(f"Cards API error: {data['error']}")
        return data

    def snapshot(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        pane_data = self.request("GET", "/api/panes?light=1")
        pref_data = self.request("GET", "/api/prefs")
        panes = pane_data.get("panes")
        prefs = pref_data.get("prefs")
        return (
            list(panes) if isinstance(panes, list) else [],
            dict(prefs) if isinstance(prefs, dict) else {},
        )


def pane_preference_key(pane: dict[str, Any]) -> str:
    target = f"{pane.get('session', '')}:{pane.get('window_index', '')}.{pane.get('pane_index', '')}"
    cwd = str(pane.get("cwd") or "").strip()
    return f"{target}|{cwd}" if cwd else target


def pane_alias(pane: dict[str, Any], prefs: dict[str, Any]) -> str:
    aliases = prefs.get("paneAliases") if isinstance(prefs.get("paneAliases"), dict) else {}
    return str(
        aliases.get(pane_preference_key(pane))
        or aliases.get(str(pane.get("target") or ""))
        or aliases.get(str(pane.get("pane_id") or ""))
        or ""
    )


def pane_display_name(pane: dict[str, Any], prefs: dict[str, Any]) -> str:
    alias = pane_alias(pane, prefs)
    raw = alias or pane.get("project") or pane.get("window_name") or pane.get("title") or pane.get("target")
    return str(raw or "").strip() or str(pane.get("pane_id") or "")


def candidate_line(pane: dict[str, Any], prefs: dict[str, Any]) -> str:
    return (
        f"{pane.get('pane_id')}\t#{pane.get('window_index')}\t{pane.get('target')}\t"
        f"{pane_display_name(pane, prefs)}\t{pane.get('kind', '')}"
    )


def resolve_pane(selector: str, panes: list[dict[str, Any]], prefs: dict[str, Any]) -> dict[str, Any]:
    selector = str(selector or "").strip()
    if not selector:
        raise SystemExit("pane selector is required")

    def choose(matches: list[dict[str, Any]], reason: str) -> dict[str, Any] | None:
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            options = "\n".join(f"  {candidate_line(pane, prefs)}" for pane in matches)
            raise SystemExit(f"ambiguous Cards pane selector ({reason}): {selector}\n{options}")
        return None

    exact_identity = [
        pane for pane in panes
        if selector in {str(pane.get("pane_id") or ""), str(pane.get("target") or "")}
    ]
    hit = choose(exact_identity, "identity")
    if hit:
        return hit

    number_match = re.fullmatch(r"#?(\d+)(?:\.(\d+))?", selector)
    if number_match:
        window_index = int(number_match.group(1))
        pane_index = int(number_match.group(2)) if number_match.group(2) is not None else None
        numbered = [
            pane for pane in panes
            if int(pane.get("window_index", -1)) == window_index
            and (pane_index is None or int(pane.get("pane_index", -1)) == pane_index)
        ]
        hit = choose(numbered, "window number")
        if hit:
            return hit

    needle = selector.casefold()
    fields_by_pane = {
        str(pane.get("pane_id") or ""): [
            pane_display_name(pane, prefs),
            str(pane.get("project") or ""),
            str(pane.get("window_name") or ""),
            str(pane.get("title") or ""),
            str(pane.get("cwd") or ""),
        ]
        for pane in panes
    }
    exact_name = [
        pane for pane in panes
        if any(value.casefold() == needle for value in fields_by_pane[str(pane.get("pane_id") or "")] if value)
    ]
    hit = choose(exact_name, "exact name")
    if hit:
        return hit

    partial = [
        pane for pane in panes
        if any(needle in value.casefold() for value in fields_by_pane[str(pane.get("pane_id") or "")] if value)
    ]
    hit = choose(partial, "partial name")
    if hit:
        return hit
    raise SystemExit(f"Cards pane not found: {selector}")


def pane_state(pane: dict[str, Any], prefs: dict[str, Any]) -> dict[str, Any]:
    key = pane_preference_key(pane)
    favorites = prefs.get("paneFavorites") if isinstance(prefs.get("paneFavorites"), dict) else {}
    categories = prefs.get("paneCategories") if isinstance(prefs.get("paneCategories"), dict) else {}
    return {
        "pane": pane.get("pane_id"),
        "target": pane.get("target"),
        "window": pane.get("window_index"),
        "name": pane_display_name(pane, prefs),
        "alias": pane_alias(pane, prefs),
        "kind": pane.get("kind"),
        "favorite": bool(favorites.get(key) or favorites.get(str(pane.get("pane_id") or ""))),
        "category": categories.get(key) or categories.get(str(pane.get("pane_id") or "")) or "",
        "key": key,
    }


def mutation_payload(pane: dict[str, Any]) -> dict[str, object]:
    pane_id = str(pane.get("pane_id") or "")
    pane_pid = str(pane.get("pane_pid") or "")
    pane_start_time = str(pane.get("pane_start_time") or "")
    if not pane_id or not pane_pid or not pane_start_time:
        raise SystemExit("Cards pane identity is incomplete; refresh/retry before mutating")
    return {"pane": pane_id, "pane_pid": pane_pid, "pane_start_time": pane_start_time}


def print_result(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    favorite = "yes" if result.get("favorite") else "no"
    category = result.get("category") or "未分"
    print(
        f"updated {result.get('target')} ({result.get('name')}): "
        f"favorite={favorite} category={category} alias={result.get('alias') or '-'}"
    )


def cmd_list(args: argparse.Namespace, client: CardsClient) -> None:
    panes, prefs = client.snapshot()
    rows = [pane_state(pane, prefs) for pane in panes]
    rows.sort(key=lambda item: (str(item["name"]).casefold(), int(item["window"] or 0)))
    if args.json:
        print(json.dumps({"panes": rows, "categories": prefs.get("categories", [])}, ensure_ascii=False, indent=2))
        return
    print("pane\twindow\ttarget\tname\tkind\tfavorite\tcategory")
    for row in rows:
        print(
            f"{row['pane']}\t#{row['window']}\t{row['target']}\t{row['name']}\t{row['kind']}\t"
            f"{'yes' if row['favorite'] else 'no'}\t{row['category'] or '未分'}"
        )
    categories = [str(item) for item in prefs.get("categories", []) if str(item) not in {"最近", "全部"}]
    print(f"categories: {', '.join(categories) if categories else '(none)'}")


def cmd_mutate(args: argparse.Namespace, client: CardsClient) -> None:
    panes, prefs = client.snapshot()
    pane = resolve_pane(args.selector, panes, prefs)
    payload = mutation_payload(pane)
    if args.command == "favorite":
        payload["favorite"] = True
    elif args.command == "unfavorite":
        payload["favorite"] = False
    elif args.command == "category":
        payload["category"] = args.category
        payload["create_category"] = bool(args.create)
    elif args.command == "uncategorize":
        payload["category"] = ""
    elif args.command == "alias":
        payload["alias"] = args.alias
    response = client.request("POST", "/api/prefs/pane", payload)
    result = pane_state(pane, response.get("prefs") if isinstance(response.get("prefs"), dict) else {})
    print_result(result, args.json)


def assign_group(
    client: CardsClient,
    category: str,
    selectors: list[str],
    *,
    create: bool = False,
) -> dict[str, Any]:
    """Resolve one live pane set and assign it through one atomic API call."""
    panes, prefs = client.snapshot()
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for selector in selectors:
        pane = resolve_pane(selector, panes, prefs)
        pane_id = str(pane.get("pane_id") or "")
        if pane_id in seen:
            continue
        seen.add(pane_id)
        selected.append(pane)
    if not selected:
        raise SystemExit("at least one distinct Cards pane is required")
    payload: dict[str, object] = {
        "category": category,
        "create_category": create,
        "members": [mutation_payload(pane) for pane in selected],
    }
    response = client.request("POST", "/api/prefs/group", payload)
    result = {key: value for key, value in response.items() if key != "prefs"}
    result["resolved"] = [
        {
            "pane": pane.get("pane_id"),
            "target": pane.get("target"),
            "name": pane_display_name(pane, prefs),
        }
        for pane in selected
    ]
    return result


def assign_uncategorized_group(
    client: CardsClient,
    category: str,
    selectors: list[str],
    *,
    create: bool = False,
) -> dict[str, Any]:
    """Preserve categorized panes and assign only currently uncategorized panes.

    If all already-categorized members share one category, that stable category
    wins for any uncategorized members.  This prevents a short-lived leader
    phase name from moving an established project group.
    """
    panes, prefs = client.snapshot()
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for selector in selectors:
        pane = resolve_pane(selector, panes, prefs)
        pane_id = str(pane.get("pane_id") or "")
        if pane_id in seen:
            continue
        seen.add(pane_id)
        selected.append(pane)
    if not selected:
        raise SystemExit("at least one distinct Cards pane is required")

    states = {str(pane.get("pane_id") or ""): pane_state(pane, prefs) for pane in selected}
    preserved = [pane for pane in selected if str(states[str(pane.get("pane_id") or "")]["category"])]
    uncategorized = [pane for pane in selected if not str(states[str(pane.get("pane_id") or "")]["category"])]
    existing_categories = sorted(
        {
            str(states[str(pane.get("pane_id") or "")]["category"])
            for pane in preserved
            if str(states[str(pane.get("pane_id") or "")]["category"])
        }
    )
    requested_category = str(category or "").strip()
    if len(existing_categories) == 1:
        effective_category = existing_categories[0]
    elif uncategorized:
        effective_category = requested_category
    else:
        effective_category = ""
    if uncategorized and not effective_category:
        raise SystemExit("uncategorized Cards panes require a non-empty category")

    response: dict[str, Any] = {"ok": True}
    if uncategorized:
        known_categories = {
            str(item)
            for item in prefs.get("categories", [])
            if str(item) not in {"最近", "全部"}
        }
        payload: dict[str, object] = {
            "category": effective_category,
            "create_category": bool(
                create
                and effective_category not in known_categories
                and effective_category not in existing_categories
            ),
            "members": [mutation_payload(pane) for pane in uncategorized],
        }
        response = client.request("POST", "/api/prefs/group", payload)

    categories = {
        pane_id: str(state["category"])
        for pane_id, state in states.items()
        if str(state["category"])
    }
    for pane in uncategorized:
        categories[str(pane.get("pane_id") or "")] = effective_category
    result = {key: value for key, value in response.items() if key != "prefs"}
    result.update(
        {
            "category": effective_category,
            "requested_category": requested_category,
            "categories": categories,
            "assigned": [
                {
                    "pane": pane.get("pane_id"),
                    "target": pane.get("target"),
                    "name": pane_display_name(pane, prefs),
                }
                for pane in uncategorized
            ],
            "preserved": [
                {
                    "pane": pane.get("pane_id"),
                    "target": pane.get("target"),
                    "name": pane_display_name(pane, prefs),
                    "category": states[str(pane.get("pane_id") or "")]["category"],
                }
                for pane in preserved
            ],
        }
    )
    return result


def cmd_group(args: argparse.Namespace, client: CardsClient) -> None:
    result = assign_group(client, args.category, args.selector, create=args.create)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    names = ", ".join(str(item.get("name") or item.get("target")) for item in result["resolved"])
    print(f"grouped {len(result['resolved'])} panes into {result.get('category')}: {names}")


def cmd_delete_category(args: argparse.Namespace, client: CardsClient) -> None:
    response = client.request("POST", "/api/prefs/category/delete", {"category": args.category})
    result = {key: value for key, value in response.items() if key != "prefs"}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(f"deleted category {result.get('category')}; unassigned={result.get('unassigned', 0)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Organize live AI Session Cards panes through the identity-safe Cards API"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="List live Cards panes, favorites, and categories")
    list_parser.add_argument("--json", action="store_true")

    for command, help_text in (
        ("favorite", "Add one live pane to Cards favorites"),
        ("unfavorite", "Remove one live pane from Cards favorites"),
        ("uncategorize", "Clear one live pane's category"),
    ):
        child = sub.add_parser(command, help=help_text)
        child.add_argument("selector", help="%%pane, tmux target, #window, display name, project, or unique substring")
        child.add_argument("--json", action="store_true")

    category = sub.add_parser("category", help="Assign one live pane to a Cards category")
    category.add_argument("selector", help="%%pane, tmux target, #window, display name, project, or unique substring")
    category.add_argument("category")
    category.add_argument("--create", action="store_true", help="Create the category if it does not exist")
    category.add_argument("--json", action="store_true")

    alias = sub.add_parser("alias", help="Set or clear one live pane's Cards display alias")
    alias.add_argument("selector", help="%%pane, tmux target, #window, display name, project, or unique substring")
    alias.add_argument("alias", help="new Cards alias; pass an empty string to clear")
    alias.add_argument("--json", action="store_true")

    group = sub.add_parser("group", help="Atomically assign multiple live panes to one category")
    group.add_argument("category")
    group.add_argument(
        "selector",
        nargs="+",
        help="one or more %%pane, tmux target, #window, display name, project, or unique substring selectors",
    )
    group.add_argument("--create", action="store_true", help="Create the category if it does not exist")
    group.add_argument("--json", action="store_true")

    category_delete = sub.add_parser("category-delete", help="Delete one custom Cards category")
    category_delete.add_argument("category")
    category_delete.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    client = CardsClient()
    if args.command == "list":
        cmd_list(args, client)
    elif args.command == "group":
        cmd_group(args, client)
    elif args.command == "category-delete":
        cmd_delete_category(args, client)
    else:
        cmd_mutate(args, client)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
