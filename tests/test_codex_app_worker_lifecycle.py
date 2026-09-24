#!/usr/bin/env python3
"""Regression tests for the `codex start/wait/doctor` worker lifecycle.

All app-server traffic is faked; no real codex process is started (the schema
contract test only runs the read-only `codex app-server generate-json-schema`).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import codex_app  # noqa: E402

REAL_ENSURE_WORKER_HOME = codex_app.ensure_worker_home
REAL_STREAM_UNTIL_COMPLETE = codex_app.AppServer.stream_until_complete  # tests patch AppServer


def start_args(**overrides: Any) -> Namespace:
    values: dict[str, Any] = dict(
        task="Reply OK", task_file="", name="员工", repo="", model="", reasoning_effort="low",
        approval="never", sandbox="read-only", timeout=1, wait=True, wait_timeout=5, idle_timeout=0,
        progress_interval=0, fail_on_idle=False, fail_on_error=False, shared_home=False,
        no_mcp=False, codex_config=[],
    )
    values.update(overrides)
    return Namespace(**values)


class FakeStartApp:
    """Stands in for AppServer inside cmd_start; records construction kwargs."""

    instances: list["FakeStartApp"] = []
    outcome: dict[str, Any] = {}

    def __init__(self, timeout: float = 1, config_args: list[str] | None = None, codex_home: str | None = None) -> None:
        self.codex_home = codex_home
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.stderr_lines: list[str] = []
        FakeStartApp.instances.append(self)

    def __enter__(self) -> "FakeStartApp":
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.requests.append((method, params or {}))
        if method == "thread/start":
            return {"thread": {"id": "t-life"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-life"}}
        return {}

    def stream_until_complete(self, thread_id: str, turn_id: str, run_dir: Path, **_kwargs: Any) -> dict[str, Any]:
        status_file = run_dir / "status.json"
        codex_app.write_json(status_file, {"thread_id": thread_id, "turn_id": turn_id, **self.outcome})
        return {"reply": "", "diff": "", "status_file": str(status_file), "warnings": [], **self.outcome}


class IsolatedBusTest(unittest.TestCase):
    def setUp(self) -> None:
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(patch.object(codex_app, "RUNS", self.root / "runs"))
        stack.enter_context(patch.object(codex_app, "RUN_INDEX", self.root / "index.json"))
        stack.enter_context(patch.object(codex_app, "CODEX_HOMES_ROOT", self.root / "homes"))
        self.worker_home = str(self.root / "homes" / "app-worker")
        stack.enter_context(patch.object(codex_app, "ensure_worker_home", return_value=self.worker_home))
        self.stdout = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        FakeStartApp.instances = []
        FakeStartApp.outcome = {"status": "completed", "diagnosis": "completed"}

    def only_run(self) -> tuple[str, dict[str, Any]]:
        runs = codex_app.load_index()["runs"]
        self.assertEqual(len(runs), 1)
        return next(iter(runs.items()))


class StartRequiresWaitTest(IsolatedBusTest):
    # Finding 1. Before: cmd_start without --wait started a turn, then the
    # `with AppServer` exit terminated app-server and killed it while printing
    # a "detached" hint; the call returned normally, so assertRaises failed.
    def test_start_without_wait_is_rejected_before_any_side_effect(self) -> None:
        with patch.object(codex_app, "AppServer", FakeStartApp):
            with self.assertRaises(SystemExit) as ctx:
                codex_app.cmd_start(start_args(wait=False))
        self.assertIn("--wait", str(ctx.exception.code))
        self.assertEqual(FakeStartApp.instances, [])
        self.assertFalse(codex_app.RUNS.exists())


class WaitTimeoutTest(IsolatedBusTest):
    # Finding 2. Before: timeout wrote the observed "inProgress" status, never
    # sent turn/interrupt, and cmd_start returned 0 unless --fail-on-error.
    def test_start_exits_nonzero_on_timeout_without_fail_on_error(self) -> None:
        FakeStartApp.outcome = {"status": "timed_out", "diagnosis": "timed_out"}
        with patch.object(codex_app, "AppServer", FakeStartApp):
            with self.assertRaises(SystemExit) as ctx:
                codex_app.cmd_start(start_args(fail_on_error=False))
        self.assertIn("timed out", str(ctx.exception.code))
        _run_id, run = self.only_run()
        self.assertEqual(run["status"], "timed_out")

    def test_completed_turn_with_command_warning_passes_fail_on_error(self) -> None:
        # Finding 7 at the CLI boundary. Before: the failed command set
        # diagnosis=command_failed and --fail-on-error raised SystemExit on a
        # turn that completed normally.
        class StreamingStartApp(FakeStartApp):
            def stream_until_complete(self, thread_id: str, turn_id: str, run_dir: Path, **kwargs: Any) -> dict[str, Any]:
                ids = {"threadId": thread_id, "turnId": turn_id}
                self._events = iter([
                    {"method": "item/completed", "params": {**ids, "item": {
                        "type": "commandExecution", "status": "failed", "exitCode": 1,
                        "command": "pytest -x", "aggregatedOutput": "1 failed"}}},
                    {"method": "item/agentMessage/delta", "params": {**ids, "delta": "fixed"}},
                    {"method": "turn/completed", "params": {**ids, "turn": {"status": "completed"}}},
                ])
                return REAL_STREAM_UNTIL_COMPLETE(self, thread_id, turn_id, run_dir, **kwargs)  # type: ignore[arg-type]

            def _read_line(self, timeout: float) -> dict[str, Any] | None:
                return next(self._events, None)

            def _read_turn_status(self, thread_id: str, turn_id: str) -> dict[str, Any]:
                return {}

        with patch.object(codex_app, "AppServer", StreamingStartApp):
            codex_app.cmd_start(start_args(fail_on_error=True))
        _run_id, run = self.only_run()
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["diagnosis"], "completed")
        self.assertEqual(run["warnings"][0]["command"], "pytest -x")


class StartFailureRecordTest(IsolatedBusTest):
    # Finding 4. Before: a failure before record_run left no index entry and no
    # stderr on disk, so `doctor --run-id` had nothing to report.
    def test_app_server_start_failure_is_indexed_with_stderr(self) -> None:
        def failing_app(**_kwargs: Any) -> None:
            raise codex_app.AppServerStartError(
                "app-server exited unexpectedly: bad config", ["Error: invalid config.toml at line 3"]
            )

        with patch.object(codex_app, "AppServer", side_effect=failing_app):
            with self.assertRaises(SystemExit):
                codex_app.cmd_start(start_args())
        run_id, run = self.only_run()
        self.assertEqual(run["status"], "start_failed")
        self.assertEqual(run["thread_id"], "")
        status = codex_app.read_json(Path(run["run_dir"]) / "status.json", {})
        self.assertEqual(status["status"], "start_failed")
        stderr_file = Path(status["app_server_stderr_file"])
        self.assertIn("invalid config.toml", stderr_file.read_text(encoding="utf-8"))

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            codex_app.cmd_doctor(Namespace(thread_id="", run_id=run_id, run_dir="", local=True, json=True,
                                           timeout=1, turns=20, no_mcp=False, codex_config=[]))
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["diagnosis"], "start_failed")
        self.assertEqual(payload["recommendation"], "check_app_server_startup_or_retry_with_no_mcp")
        self.assertIn("invalid config.toml", "\n".join(payload["app_server_stderr_tail"]))

    def doctor_run_dir(self, run_dir: Path, timeout: float = 1) -> dict[str, Any]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            codex_app.cmd_doctor(Namespace(thread_id="", run_id="", run_dir=str(run_dir), local=True, json=True,
                                           timeout=timeout, turns=20, no_mcp=False, codex_config=[]))
        return json.loads(buf.getvalue())

    def test_doctor_reports_stale_starting_run_as_start_failed(self) -> None:
        # Before: a run stuck in "starting" (starter killed) was reported as
        # starting_app_server forever.
        run_dir = self.root / "stale"
        run_dir.mkdir()
        status_file = run_dir / "status.json"
        status_file.write_text('{"status":"starting","diagnosis":"starting_app_server"}\n', encoding="utf-8")
        old = time.time() - 3600
        os.utime(status_file, (old, old))
        payload = self.doctor_run_dir(run_dir, timeout=20)
        self.assertEqual(payload["diagnosis"], "start_failed")
        self.assertEqual(payload["recommendation"], "check_app_server_startup_or_retry_with_no_mcp")

    def test_doctor_keeps_fresh_starting_run_as_starting(self) -> None:
        run_dir = self.root / "fresh"
        run_dir.mkdir()
        (run_dir / "status.json").write_text('{"status":"starting","diagnosis":"starting_app_server"}\n', encoding="utf-8")
        self.assertEqual(self.doctor_run_dir(run_dir, timeout=20)["diagnosis"], "starting_app_server")


class WorkerHomeTest(IsolatedBusTest):
    # Finding 3. Before: app-server inherited the caller's CODEX_HOME, sharing
    # state_5.sqlite with interactive TUIs; no codex_home was recorded.
    def test_start_uses_isolated_home_and_records_it(self) -> None:
        with patch.object(codex_app, "AppServer", FakeStartApp):
            codex_app.cmd_start(start_args())
        self.assertEqual(FakeStartApp.instances[0].codex_home, self.worker_home)
        _run_id, run = self.only_run()
        self.assertEqual(run["codex_home"], self.worker_home)
        self.assertEqual(codex_app.load_index()["threads"]["t-life"]["codex_home"], self.worker_home)
        self.assertEqual(codex_app.thread_codex_home(Namespace(), "t-life"), self.worker_home)

    def test_shared_home_flag_inherits_caller_home(self) -> None:
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root / "caller-home")}):
            with patch.object(codex_app, "AppServer", FakeStartApp):
                codex_app.cmd_start(start_args(shared_home=True))
        self.assertIsNone(FakeStartApp.instances[0].codex_home)
        _run_id, run = self.only_run()
        self.assertEqual(run["codex_home"], str(self.root / "caller-home"))

    def test_thread_commands_reuse_recorded_home(self) -> None:
        codex_app.save_index({"runs": {}, "threads": {"t-iso": {"codex_home": "/homes/app-worker"}, "t-old": {}}})
        seen: list[str | None] = []

        class HomeProbe(FakeStartApp):
            def __init__(self, **kwargs: Any) -> None:
                super().__init__(**kwargs)
                seen.append(kwargs.get("codex_home"))

            def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
                return {"thread": {"id": (params or {}).get("threadId")}, "data": []}

        with patch.object(codex_app, "AppServer", HomeProbe):
            codex_app.cmd_read(Namespace(thread_id="t-iso", turns=False, tail=3, json=True, timeout=1,
                                         no_mcp=False, codex_config=[]))
            codex_app.cmd_status(Namespace(thread_id="t-old", turns=5, json=True, timeout=1,
                                           no_mcp=False, codex_config=[]))
        self.assertEqual(seen, ["/homes/app-worker", None])

    def test_ensure_worker_home_runs_script_once_then_reuses(self) -> None:
        real_ensure = REAL_ENSURE_WORKER_HOME  # setUp patches the module attribute
        home = self.root / "homes" / "app-worker"

        def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            home.mkdir(parents=True, exist_ok=True)
            (home / "config.toml").write_text("", encoding="utf-8")
            (home / "auth.json").write_text("{}", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout=str(home), stderr="")

        # An explicit slug creates/reuses that home without claiming a pool slot
        # (slot allocation is covered by test_codex_app_worker_slots.py).
        with patch.object(codex_app.subprocess, "run", side_effect=fake_run) as run:
            self.assertEqual(real_ensure("app-worker"), str(home))
            self.assertEqual(real_ensure("app-worker"), str(home))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], ["bash", str(codex_app.CREATE_HOME_SCRIPT), "app-worker"])


class WaitProtocolTest(IsolatedBusTest):
    # Finding 6. Before: cmd_wait called thread/turns/items/list, which current
    # app-server rejects, so waiting on a finished turn exited with an error.
    def test_wait_reads_reply_via_thread_items_list(self) -> None:
        methods: list[str] = []

        class ProtocolApp(FakeStartApp):
            def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
                methods.append(method)
                if method == "thread/turns/list":
                    return {"data": [{"id": "turn-w", "status": "completed"}]}
                if method == "thread/items/list":
                    return {"data": [{"turnId": "turn-w", "item": {"type": "agentMessage", "id": "i1", "text": "DONE"}}]}
                raise SystemExit({"code": -32601, "message": f"unknown method {method}"})

        buf = io.StringIO()
        with patch.object(codex_app, "AppServer", ProtocolApp), contextlib.redirect_stdout(buf):
            codex_app.cmd_wait(Namespace(thread_id="t-w", turn_id="", interval=0, wait_timeout=1, items=10,
                                         verbose=False, json=True, timeout=1, no_mcp=False, codex_config=[]))
        self.assertIn("thread/items/list", methods)
        self.assertNotIn("thread/turns/items/list", methods)
        payload = json.loads(buf.getvalue().split("\n", 1)[1])
        self.assertEqual(payload["reply"], "DONE")


class AppServerSchemaContractTest(unittest.TestCase):
    """Every JSON-RPC method codex_app.py uses must exist in the local codex schema."""

    @staticmethod
    def schema_methods(path: Path) -> set[str]:
        data = json.loads(path.read_text(encoding="utf-8"))
        methods: set[str] = set()
        for variant in data.get("oneOf", []):
            methods.update(variant.get("properties", {}).get("method", {}).get("enum", []))
        return methods

    def test_methods_used_by_codex_app_exist_in_generated_schema(self) -> None:
        codex = shutil.which("codex")
        if codex is None:
            self.skipTest("codex binary not installed")
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run([codex, "app-server", "generate-json-schema", "--out", tmp],
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
            if proc.returncode != 0 or not (Path(tmp) / "ClientRequest.json").exists():
                self.skipTest(f"cannot generate app-server schema: {proc.stderr.strip()[:200]}")
            client_requests = self.schema_methods(Path(tmp) / "ClientRequest.json")
            client_notifications = self.schema_methods(Path(tmp) / "ClientNotification.json")
            server_notifications = self.schema_methods(Path(tmp) / "ServerNotification.json")
        source = (ROOT / "scripts" / "codex_app.py").read_text(encoding="utf-8")
        requested = set(re.findall(r'\.request\(\s*"([^"]+)"', source))
        requested |= set(re.findall(r'"(thread/(?:un)?archive)"', source))
        notified = set(re.findall(r'\.notify\(\s*"([^"]+)"', source))
        method_literals = set(re.findall(r'"([a-z][A-Za-z]*(?:/[A-Za-z_]+)+)"', source))
        self.assertIn("thread/items/list", requested)
        self.assertGreaterEqual(len(requested), 10)
        self.assertEqual(sorted(requested - client_requests), [])
        self.assertEqual(sorted(notified - client_notifications), [])
        known = client_requests | server_notifications
        self.assertEqual(sorted(method_literals - known), [])


if __name__ == "__main__":
    unittest.main()


class WorkerHomeCreationTest(unittest.TestCase):
    """Two concurrent `codex start` calls must not run the home installer twice."""

    def test_concurrent_first_use_runs_the_installer_once(self) -> None:
        import threading
        from pathlib import Path
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "homes"
            counter = Path(tmp) / "runs.txt"
            script = Path(tmp) / "fake-create.sh"
            script.write_text(
                "#!/usr/bin/env bash\nset -e\n"
                f'echo run >> "{counter}"\nsleep 0.3\n'
                f'mkdir -p "{root}/$1"\nprintf \'model = "x"\\n\' > "{root}/$1/config.toml"\n'
                f'echo {{}} > "{root}/$1/auth.json"\n',
                encoding="utf-8",
            )
            with mock.patch.object(codex_app, "CODEX_HOMES_ROOT", root), \
                 mock.patch.object(codex_app, "CREATE_HOME_SCRIPT", script):
                results: list[str] = []
                threads = [threading.Thread(target=lambda: results.append(codex_app.ensure_worker_home("w"))) for _ in range(3)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
            self.assertEqual(len(results), 3)
            self.assertEqual(counter.read_text().count("run"), 1)
