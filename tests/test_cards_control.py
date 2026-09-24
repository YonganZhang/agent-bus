#!/usr/bin/env python3
"""Unit tests for Agent Bus AI Session Cards organization control."""

from __future__ import annotations

import sys
import unittest
import contextlib
import io
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cards_control  # noqa: E402


def pane(
    pane_id: str,
    window: int,
    name: str,
    cwd: str,
    *,
    pane_index: int = 0,
) -> dict[str, object]:
    return {
        "pane_id": pane_id,
        "target": f"secretary_web:{window}.{pane_index}",
        "session": "secretary_web",
        "window_index": window,
        "pane_index": pane_index,
        "window_name": name,
        "project": name,
        "title": name,
        "cwd": cwd,
        "kind": "Codex",
        "pane_pid": str(1000 + window),
        "pane_start_time": f"{1000 + window}:9",
    }


class CardsControlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.panes = [
            pane("%10", 2, "论文主线", "/work/paper"),
            pane("%11", 10, "开发面板", "/work/dashboard"),
            pane("%12", 12, "开发后端", "/work/backend"),
        ]
        self.prefs = {
            "paneAliases": {"secretary_web:2.0|/work/paper": "A2 论文"},
            "paneFavorites": {"secretary_web:2.0|/work/paper": True},
            "paneCategories": {"secretary_web:2.0|/work/paper": "论文"},
        }

    def test_resolves_exact_pane_target_window_and_alias(self) -> None:
        self.assertEqual(cards_control.resolve_pane("%10", self.panes, self.prefs)["pane_id"], "%10")
        self.assertEqual(cards_control.resolve_pane("secretary_web:10.0", self.panes, self.prefs)["pane_id"], "%11")
        self.assertEqual(cards_control.resolve_pane("#12", self.panes, self.prefs)["pane_id"], "%12")
        self.assertEqual(cards_control.resolve_pane("A2 论文", self.panes, self.prefs)["pane_id"], "%10")

    def test_ambiguous_name_fails_closed_with_candidates(self) -> None:
        with self.assertRaisesRegex(SystemExit, "ambiguous Cards pane selector"):
            cards_control.resolve_pane("开发", self.panes, self.prefs)

    def test_state_uses_stable_key_alias_favorite_and_category(self) -> None:
        state = cards_control.pane_state(self.panes[0], self.prefs)
        self.assertEqual(state["name"], "A2 论文")
        self.assertEqual(state["alias"], "A2 论文")
        self.assertTrue(state["favorite"])
        self.assertEqual(state["category"], "论文")
        self.assertEqual(state["key"], "secretary_web:2.0|/work/paper")

    def test_state_reads_legacy_target_alias_for_snapshot_migration(self) -> None:
        prefs = {**self.prefs, "paneAliases": {"secretary_web:2.0": "旧别名"}}
        state = cards_control.pane_state(self.panes[0], prefs)
        self.assertEqual(state["alias"], "旧别名")
        self.assertEqual(state["name"], "旧别名")

    def test_mutation_payload_requires_frozen_process_identity(self) -> None:
        payload = cards_control.mutation_payload(self.panes[0])
        self.assertEqual(payload["pane"], "%10")
        broken = dict(self.panes[0])
        broken["pane_start_time"] = ""
        with self.assertRaisesRegex(SystemExit, "identity is incomplete"):
            cards_control.mutation_payload(broken)

    def test_alias_uses_identity_verified_atomic_pane_endpoint(self) -> None:
        class FakeClient:
            def __init__(self, panes, prefs):
                self.panes = panes
                self.prefs = prefs
                self.calls = []

            def snapshot(self):
                return self.panes, self.prefs

            def request(self, method, path, payload):
                self.calls.append((method, path, payload))
                return {
                    "ok": True,
                    "prefs": {
                        **self.prefs,
                        "paneAliases": {
                            "secretary_web:2.0|/work/paper": payload["alias"],
                        },
                    },
                }

        client = FakeClient(self.panes, self.prefs)
        args = cards_control.argparse.Namespace(
            command="alias",
            selector="%10",
            alias="论文恢复名",
            json=True,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            cards_control.cmd_mutate(args, client)
        method, path, payload = client.calls[0]
        self.assertEqual((method, path), ("POST", "/api/prefs/pane"))
        self.assertEqual(payload["alias"], "论文恢复名")
        self.assertEqual(payload["pane"], "%10")

    def test_assign_group_resolves_and_deduplicates_before_one_request(self) -> None:
        class FakeClient:
            def __init__(self, panes, prefs):
                self.panes = panes
                self.prefs = prefs
                self.calls = []

            def snapshot(self):
                return self.panes, self.prefs

            def request(self, method, path, payload):
                self.calls.append((method, path, payload))
                return {"ok": True, "category": payload["category"], "members": payload["members"], "prefs": {}}

        client = FakeClient(self.panes, self.prefs)
        result = cards_control.assign_group(client, "任务·论文", ["%10", "A2 论文", "#10"], create=True)
        self.assertEqual(len(client.calls), 1)
        method, path, payload = client.calls[0]
        self.assertEqual((method, path), ("POST", "/api/prefs/group"))
        self.assertEqual(payload["category"], "任务·论文")
        self.assertTrue(payload["create_category"])
        self.assertEqual([item["pane"] for item in payload["members"]], ["%10", "%11"])
        self.assertEqual(len(result["resolved"]), 2)

    def test_leader_group_preserves_existing_category_and_only_assigns_uncategorized(self) -> None:
        class FakeClient:
            def __init__(self, panes, prefs):
                self.panes = panes
                self.prefs = prefs
                self.calls = []

            def snapshot(self):
                return self.panes, self.prefs

            def request(self, method, path, payload):
                self.calls.append((method, path, payload))
                return {"ok": True, "prefs": {}}

        client = FakeClient(self.panes, self.prefs)
        result = cards_control.assign_uncategorized_group(
            client,
            "一次性阶段分类",
            ["%10", "%11", "%12"],
            create=True,
        )
        self.assertEqual(len(client.calls), 1)
        method, path, payload = client.calls[0]
        self.assertEqual((method, path), ("POST", "/api/prefs/group"))
        self.assertEqual(payload["category"], "论文")
        self.assertFalse(payload["create_category"])
        self.assertEqual([item["pane"] for item in payload["members"]], ["%11", "%12"])
        self.assertEqual([item["pane"] for item in result["preserved"]], ["%10"])
        self.assertEqual(result["categories"], {"%10": "论文", "%11": "论文", "%12": "论文"})

    def test_leader_group_assigns_requested_category_when_every_pane_is_uncategorized(self) -> None:
        class FakeClient:
            def __init__(self, panes, prefs):
                self.panes = panes
                self.prefs = prefs
                self.calls = []

            def snapshot(self):
                return self.panes, self.prefs

            def request(self, method, path, payload):
                self.calls.append((method, path, payload))
                return {"ok": True, "prefs": {}}

        prefs = dict(self.prefs)
        prefs["paneCategories"] = {}
        client = FakeClient(self.panes[1:], prefs)
        result = cards_control.assign_uncategorized_group(
            client,
            "稳定项目名",
            ["%11", "%12"],
            create=True,
        )
        self.assertEqual(len(client.calls), 1)
        _method, _path, payload = client.calls[0]
        self.assertEqual(payload["category"], "稳定项目名")
        self.assertTrue(payload["create_category"])
        self.assertEqual([item["pane"] for item in payload["members"]], ["%11", "%12"])
        self.assertEqual(result["preserved"], [])
        self.assertEqual(result["categories"], {"%11": "稳定项目名", "%12": "稳定项目名"})

    def test_leader_group_does_not_mutate_when_every_pane_is_already_categorized(self) -> None:
        class FakeClient:
            def __init__(self, panes, prefs):
                self.panes = panes
                self.prefs = prefs
                self.calls = []

            def snapshot(self):
                return self.panes, self.prefs

            def request(self, method, path, payload):
                self.calls.append((method, path, payload))
                return {"ok": True, "prefs": {}}

        prefs = dict(self.prefs)
        prefs["paneCategories"] = {
            "secretary_web:2.0|/work/paper": "论文",
            "secretary_web:10.0|/work/dashboard": "开发",
        }
        client = FakeClient(self.panes[:2], prefs)
        result = cards_control.assign_uncategorized_group(
            client,
            "一次性阶段分类",
            ["%10", "%11"],
            create=True,
        )
        self.assertEqual(client.calls, [])
        self.assertEqual(result["assigned"], [])
        self.assertEqual(result["categories"], {"%10": "论文", "%11": "开发"})

    def test_every_subcommand_help_formats_literal_pane_selector(self) -> None:
        parser = cards_control.build_parser()
        subparsers = next(
            action
            for action in parser._actions
            if isinstance(action, cards_control.argparse._SubParsersAction)
        )
        for command in ("favorite", "alias", "category", "group"):
            help_text = subparsers.choices[command].format_help()
            self.assertIn("%pane", help_text)

    def test_category_delete_uses_atomic_category_endpoint(self) -> None:
        class FakeClient:
            def __init__(self):
                self.calls = []

            def request(self, method, path, payload):
                self.calls.append((method, path, payload))
                return {"ok": True, "category": payload["category"], "unassigned": 2, "prefs": {}}

        client = FakeClient()
        args = cards_control.argparse.Namespace(category="任务·临时", json=True)
        with contextlib.redirect_stdout(io.StringIO()):
            cards_control.cmd_delete_category(args, client)
        self.assertEqual(
            client.calls,
            [("POST", "/api/prefs/category/delete", {"category": "任务·临时"})],
        )


class OrderReplaceTest(unittest.TestCase):
    """A restarted pane takes its predecessor's slot in the card order."""

    class Client:
        def __init__(self, order: list[str]) -> None:
            self.order = order
            self.posted: list[dict[str, object]] = []

        def request(self, method: str, path: str, payload: dict[str, object] | None = None) -> dict:
            if method == "GET":
                return {"prefs": {"paneOrder": list(self.order)}}
            self.posted.append(payload or {})
            return {"ok": True}

    def test_new_pane_takes_the_old_slot(self) -> None:
        client = self.Client(["%1", "%2", "%3", "%9"])  # %9: the new pane already appended by a browser tab
        result = cards_control.replace_pane_in_order(client, "%2", "%9")
        self.assertEqual(client.posted, [{"paneOrder": ["%1", "%9", "%3"]}])
        self.assertEqual(result["position"], 1)

    def test_unknown_old_pane_changes_nothing(self) -> None:
        client = self.Client(["%1"])
        self.assertFalse(cards_control.replace_pane_in_order(client, "%5", "%6")["replaced"])
        self.assertEqual(client.posted, [])


if __name__ == "__main__":
    unittest.main()
