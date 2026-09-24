#!/usr/bin/env python3
"""Regression tests for the per-worker CODEX_HOME slot pool used by `codex start`.

Before: every `codex start` got the single home ~/.codex-homes/app-worker, so
concurrent workers still shared one state_5.sqlite and fought over its locks,
and a new --repo was never pre-trusted in that home. These tests drive the real
create-isolated-codex-home.sh against a fake default home and a fake installer;
no codex or app-server process is started.
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import codex_app  # noqa: E402


def start_args(**overrides: Any) -> Namespace:
    values: dict[str, Any] = dict(
        task="Reply OK", task_file="", name="员工", repo="", model="", reasoning_effort="low",
        approval="never", sandbox="read-only", timeout=1, wait=True, wait_timeout=5, idle_timeout=0,
        progress_interval=0, fail_on_idle=False, fail_on_error=False, shared_home=False,
        wait_slot=0, no_mcp=False, codex_config=[],
    )
    values.update(overrides)
    return Namespace(**values)


class SlotApp:
    """Fake AppServer; stream_until_complete can block until the test releases it."""

    instances: list["SlotApp"] = []
    gates: dict[str, threading.Event] = {}
    entered: dict[str, threading.Event] = {}
    fail_on_start = False

    def __init__(self, timeout: float = 1, config_args: list[str] | None = None, codex_home: str | None = None) -> None:
        self.codex_home = codex_home
        self.stderr_lines: list[str] = []
        SlotApp.instances.append(self)

    def __enter__(self) -> "SlotApp":
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if method == "thread/start":
            if SlotApp.fail_on_start:
                raise RuntimeError("fake app-server start failure")
            return {"thread": {"id": f"t-{Path(self.codex_home or 'shared').name}"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        return {}

    def stream_until_complete(self, thread_id: str, turn_id: str, run_dir: Path, **_kwargs: Any) -> dict[str, Any]:
        name = Path(self.codex_home or "shared").name
        if name in SlotApp.entered:
            SlotApp.entered[name].set()
        if name in SlotApp.gates:
            assert SlotApp.gates[name].wait(10), "test never released the fake worker"
        status_file = run_dir / "status.json"
        current = json.loads(status_file.read_text(encoding="utf-8"))
        current.update({"status": "completed", "diagnosis": "completed"})
        codex_app.write_json(status_file, current)
        return {"status": "completed", "diagnosis": "completed", "reply": "", "diff": "",
                "status_file": str(status_file), "warnings": []}


class SlotPoolTest(unittest.TestCase):
    def setUp(self) -> None:
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.homes = self.root / "homes"
        self.counter = self.root / "installer-runs.txt"
        default_home = self.root / "default-home"
        (default_home / "scripts").mkdir(parents=True)
        (default_home / "rules").mkdir()
        (default_home / "skills").mkdir()
        (default_home / "auth.json").write_text("{}", encoding="utf-8")
        installer = default_home / "scripts" / "installer.sh"
        installer.write_text(
            "#!/usr/bin/env bash\nset -e\n"
            f'echo "$CODEX_HOME" >> "{self.counter}"\n'
            '[ -s "$CODEX_HOME/config.toml" ] || printf \'model = "x"\\n\' > "$CODEX_HOME/config.toml"\n',
            encoding="utf-8",
        )
        installer.chmod(0o755)
        stack.enter_context(patch.dict(os.environ, {
            "AGENT_BUS_DEFAULT_CODEX_HOME": str(default_home),
            "AGENT_BUS_CODEX_HOMES_ROOT": str(self.homes),
            "AGENT_BUS_CODEX_HOME_INSTALLER": str(installer),
        }))
        stack.enter_context(patch.object(codex_app, "RUNS", self.root / "runs"))
        stack.enter_context(patch.object(codex_app, "RUN_INDEX", self.root / "index.json"))
        stack.enter_context(patch.object(codex_app, "CODEX_HOMES_ROOT", self.homes))
        stack.enter_context(patch.object(codex_app, "WORKER_HOME_SLUG", "app-worker"))
        stack.enter_context(patch.object(codex_app, "WORKER_SLOTS", 6))
        stack.enter_context(patch.object(codex_app, "SLOT_WAIT_POLL", 0.05))
        stack.enter_context(patch.object(codex_app, "AppServer", SlotApp))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.repo = self.root / "repo"
        self.repo.mkdir()
        SlotApp.instances = []
        SlotApp.gates = {}
        SlotApp.entered = {}
        SlotApp.fail_on_start = False
        self.addCleanup(self.assert_no_slot_leak)

    def assert_no_slot_leak(self) -> None:
        self.assertEqual(codex_app._HELD_SLOT_LOCKS, {})

    def installer_runs(self) -> list[str]:
        return self.counter.read_text(encoding="utf-8").split() if self.counter.exists() else []

    def slot(self, index: int) -> str:
        return str(self.homes / f"app-worker-{index}")

    def hold_slot(self, index: int, run_id: str) -> Any:
        self.homes.mkdir(parents=True, exist_ok=True)
        handle = open(self.homes / f".app-worker-{index}.slot.lock", "a+", encoding="utf-8")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.truncate(0)
        handle.write(json.dumps({"run_id": run_id, "display_name": "占用者", "pid": 4242}))
        handle.flush()
        self.addCleanup(handle.close)
        return handle

    def test_concurrent_starts_get_different_slots(self) -> None:
        # Before: both calls got ~/.codex-homes/app-worker (one shared home).
        SlotApp.gates = {"app-worker-1": threading.Event(), "app-worker-2": threading.Event()}
        SlotApp.entered = {"app-worker-1": threading.Event(), "app-worker-2": threading.Event()}
        errors: list[BaseException] = []

        def run(name: str) -> None:
            try:
                codex_app.cmd_start(start_args(name=name, repo=str(self.repo)))
            except BaseException as exc:  # surfaced by the assertion below
                errors.append(exc)

        first = threading.Thread(target=run, args=("员工A",))
        first.start()
        self.assertTrue(SlotApp.entered["app-worker-1"].wait(10))
        second = threading.Thread(target=run, args=("员工B",))
        second.start()
        self.assertTrue(SlotApp.entered["app-worker-2"].wait(10))
        SlotApp.gates["app-worker-1"].set()
        first.join(10)
        SlotApp.gates["app-worker-2"].set()
        second.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(app.codex_home for app in SlotApp.instances), [self.slot(1), self.slot(2)])
        homes = {run["display_name"]: run["codex_home"] for run in codex_app.load_index()["runs"].values()}
        self.assertEqual(homes, {"员工A": self.slot(1), "员工B": self.slot(2)})

    def test_record_status_and_thread_index_use_slot_path(self) -> None:
        codex_app.cmd_start(start_args(repo=str(self.repo)))
        run = next(iter(codex_app.load_index()["runs"].values()))
        self.assertEqual(run["codex_home"], self.slot(1))
        status = json.loads((Path(run["run_dir"]) / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["codex_home"], self.slot(1))
        self.assertEqual(codex_app.load_index()["threads"]["t-app-worker-1"]["codex_home"], self.slot(1))
        self.assertEqual(codex_app.thread_codex_home(Namespace(), "t-app-worker-1"), self.slot(1))

    def test_all_slots_busy_fails_with_occupants_and_no_side_effects(self) -> None:
        with patch.object(codex_app, "WORKER_SLOTS", 2):
            self.hold_slot(1, "run-busy-1")
            self.hold_slot(2, "run-busy-2")
            with self.assertRaises(SystemExit) as ctx:
                codex_app.cmd_start(start_args(repo=str(self.repo)))
        message = str(ctx.exception.code)
        self.assertIn("all 2 Codex worker slots are busy", message)
        self.assertIn("run=run-busy-1", message)
        self.assertIn("run=run-busy-2", message)
        self.assertIn("--wait-slot", message)
        self.assertEqual(SlotApp.instances, [])
        self.assertFalse(codex_app.RUNS.exists())
        # The busy slot's owner record must survive a failed claim attempt.
        owner = json.loads((self.homes / ".app-worker-1.slot.lock").read_text(encoding="utf-8"))
        self.assertEqual(owner["run_id"], "run-busy-1")

    def test_wait_slot_takes_the_slot_freed_while_waiting(self) -> None:
        with patch.object(codex_app, "WORKER_SLOTS", 2):
            self.hold_slot(1, "run-busy-1")
            busy_two = self.hold_slot(2, "run-busy-2")
            threading.Timer(0.3, busy_two.close).start()
            began = time.monotonic()
            codex_app.cmd_start(start_args(repo=str(self.repo), wait_slot=5))
        self.assertGreaterEqual(time.monotonic() - began, 0.25)
        self.assertEqual(SlotApp.instances[0].codex_home, self.slot(2))

    def test_first_use_runs_installer_once_and_slot_is_released(self) -> None:
        codex_app.cmd_start(start_args(repo=str(self.repo)))
        codex_app.cmd_start(start_args(repo=str(self.repo)))
        # Sequential starts reuse slot 1 (lock released on exit) and install once.
        self.assertEqual([app.codex_home for app in SlotApp.instances], [self.slot(1), self.slot(1)])
        self.assertEqual(self.installer_runs(), [self.slot(1)])

    def test_repo_is_pre_trusted_in_the_slot_config(self) -> None:
        other = self.root / "other repo"
        other.mkdir()
        codex_app.cmd_start(start_args(repo=str(self.repo)))
        codex_app.cmd_start(start_args(repo=str(other)))
        config = (self.homes / "app-worker-1" / "config.toml").read_text(encoding="utf-8")
        for repo in (self.repo, other):
            header = "[projects." + json.dumps(str(repo.resolve()), ensure_ascii=False) + "]"
            self.assertEqual(config.count(header), 1)
            self.assertIn(header + '\ntrust_level = "trusted"', config)
        parser = codex_app._toml_parser()
        if parser is not None:
            projects = parser.loads(config)["projects"]
            self.assertEqual(projects[str(other.resolve())]["trust_level"], "trusted")
        # A second repo re-runs the idempotent script once to add its trust table.
        self.assertEqual(len(self.installer_runs()), 2)

    def test_slot_is_released_when_worker_start_fails(self) -> None:
        SlotApp.fail_on_start = True
        with self.assertRaises(RuntimeError):
            codex_app.cmd_start(start_args(repo=str(self.repo)))
        SlotApp.fail_on_start = False
        codex_app.cmd_start(start_args(repo=str(self.repo)))
        self.assertEqual([app.codex_home for app in SlotApp.instances], [self.slot(1), self.slot(1)])

    def test_legacy_single_home_is_left_alone_and_not_allocated(self) -> None:
        legacy = self.homes / "app-worker"
        legacy.mkdir(parents=True)
        (legacy / "config.toml").write_text('model = "old"\n', encoding="utf-8")
        codex_app.cmd_start(start_args(repo=str(self.repo)))
        self.assertEqual(SlotApp.instances[0].codex_home, self.slot(1))
        self.assertEqual((legacy / "config.toml").read_text(encoding="utf-8"), 'model = "old"\n')

    def test_shared_home_skips_the_pool(self) -> None:
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root / "caller-home")}):
            codex_app.cmd_start(start_args(shared_home=True, repo=str(self.repo)))
        self.assertIsNone(SlotApp.instances[0].codex_home)
        self.assertFalse(self.homes.exists())


if __name__ == "__main__":
    unittest.main()
