"""Test helpers for the optional plan / archive integration.

The dashboard only talks to a plan CLI configured by CARDS_TOP_CLI.  Tests use
``fixtures/fake_plan_cli.py`` (a minimal implementation of that contract) so
they run without share-top installed.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import server

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FAKE_PLAN_CLI = FIXTURES / "fake_plan_cli.py"
ARCHIVE_PROMPT_TEXT = "【归档】把这个项目的改动分拣、提交，并更新计划。"


def enable_plan_integration(case: unittest.TestCase, archive_prompt: Path | None = None) -> None:
    """Point server.PLAN_CLI (and optionally ARCHIVE_PROMPT) at the fake CLI for one test."""
    for name, value in (("PLAN_CLI", FAKE_PLAN_CLI), ("ARCHIVE_PROMPT", archive_prompt)):
        if name == "ARCHIVE_PROMPT" and value is None:
            continue
        patcher = mock.patch.object(server, name, value)
        patcher.start()
        case.addCleanup(patcher.stop)


def write_archive_prompt(directory: Path) -> Path:
    path = directory / "archive-prompt.md"
    path.write_text(ARCHIVE_PROMPT_TEXT + "\n", encoding="utf-8")
    return path


def wait_plan_index(timeout: float = 10.0) -> None:
    """Wait until no plan summary refresh (plan list on the git thread pool) is pending."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with server._GIT_LOCK:
            if not server._TOP_PLAN_PENDING:
                return
        time.sleep(0.01)
    raise AssertionError("plan index refresh never finished")


def plan_list_items(text: str) -> list[dict[str, object]]:
    """What ``plan list`` of the fake CLI returns for this plan text."""
    with tempfile.TemporaryDirectory() as tmp:
        plan = Path(tmp) / "_wiki-methodology" / "_top" / "_task_plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text(text, encoding="utf-8")
        result = subprocess.run([sys.executable, str(FAKE_PLAN_CLI), "plan", "list", tmp], capture_output=True,
                                text=True, check=True, stdin=subprocess.DEVNULL, timeout=30)
    return json.loads(result.stdout)["items"]
