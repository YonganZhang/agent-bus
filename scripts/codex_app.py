#!/usr/bin/env python3
"""Secretary Bus Codex app-server controller.

This backend talks to `codex app-server --stdio` over JSON-RPC. It is the
preferred control plane for Codex workers because it exposes thread naming,
turn steering, interruption, archive, and streamed diff updates without tmux
key injection.

`codex start` runs each worker under one CODEX_HOME from a slot pool
(~/.codex-homes/app-worker-1..N). A slot only isolates user-level state
(sessions, state_5.sqlite, config.toml); project files, the project's
AGENTS.md and project rules are still read from the worker's --repo cwd.
"""

from __future__ import annotations

import argparse
import html
import fcntl
import json
import os
import re
import select
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import cli_bridge  # noqa: E402
import supervisor  # noqa: E402


BUS = cli_bridge.BUS
RUNS = BUS / "codex-runs"
RUN_INDEX = BUS / "codex-runs.json"
DEFAULT_TIMEOUT = float(os.environ.get("SECRETARY_BUS_CODEX_TIMEOUT", "20"))
DEFAULT_AGENT_MODEL = "gpt-5.6-luna"
DEFAULT_AGENT_REASONING = "low"
AGENT_REASONING_CHOICES = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
TERMINAL_TURN_STATUSES = {"completed", "failed", "cancelled", "canceled", "interrupted"}
# Workers run under their own CODEX_HOME so app-server never shares
# state_5.sqlite with interactive TUIs or with each other: each concurrent
# worker holds one slot home <slug>-1..N; see create-isolated-codex-home.sh.
WORKER_HOME_SLUG = os.environ.get("SECRETARY_BUS_CODEX_WORKER_HOME_SLUG", "app-worker")
WORKER_SLOTS = int(os.environ.get("SECRETARY_BUS_CODEX_WORKER_SLOTS", "6"))
SLOT_WAIT_POLL = 0.5
# Slot locks held by this process, keyed by home path; flock is released on
# close or process exit, so a crashed worker never leaves a slot stuck.
_HELD_SLOT_LOCKS: dict[str, Any] = {}
CODEX_HOMES_ROOT = Path(os.environ.get("AGENT_BUS_CODEX_HOMES_ROOT", str(Path.home() / ".codex-homes")))
CREATE_HOME_SCRIPT = SCRIPT_DIR / "create-isolated-codex-home.sh"
# Startup issues initialize, thread/start, thread/name/set and turn/start, each
# bounded by --timeout; a run still "starting" after that budget never started.
START_RPC_COUNT = 4
PROBLEM_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("auth_required", ("authrequired", "oauth", "permission_denied", "api key invalid", "unauthorized")),
    ("rate_limited", ("rate limit", "rate_limit", "too many requests", "quota exceeded", "resource_exhausted")),
    ("network", ("connection refused", "connection reset", "tls handshake", "tls error", "websocket", "connection timed out")),
    ("filesystem", ("read-only file system", "no space left on device", "disk quota exceeded")),
    ("model_rejected", ("model is not supported", "model_not_supported", "unknown model")),
]
HTTP_CODE_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("auth_required", ("401", "403")),
    ("rate_limited", ("402", "429")),
]


def now() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    if hasattr(os, "O_DIRECTORY"):
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def compact(text: str, width: int = 90) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def problem_kind(text: str) -> str:
    lower = (text or "").lower()
    for kind, needles in PROBLEM_PATTERNS:
        if any(needle in lower for needle in needles):
            return kind
    for kind, codes in HTTP_CODE_PATTERNS:
        if any(re.search(rf"(?<![0-9a-z]){re.escape(code)}(?![0-9a-z])", lower) for code in codes):
            return kind
    return ""


def file_age_seconds(path: Path) -> float | None:
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except FileNotFoundError:
        return None


def latest_run_for_thread(index: dict[str, Any], thread_id: str) -> tuple[str, dict[str, Any]]:
    thread_meta = index.get("threads", {}).get(thread_id, {})
    run_id = thread_meta.get("last_run_id", "")
    if run_id and run_id in index.get("runs", {}):
        return str(run_id), index["runs"][run_id]
    matches = [(rid, run) for rid, run in index.get("runs", {}).items() if run.get("thread_id") == thread_id]
    if not matches:
        return "", {}
    matches.sort(key=lambda item: str(item[1].get("updated_at", "")))
    return str(matches[-1][0]), matches[-1][1]


def configured_mcp_servers(config_path: Path | None = None) -> list[str]:
    path = config_path or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
    if not path.exists():
        return []
    names: list[str] = []
    pattern = re.compile(r'^\s*\[mcp_servers\.([^\].]+)\]\s*$')
    for line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            names.append(match.group(1))
    return names


def app_config_args(args: argparse.Namespace, codex_home: str | None = None) -> list[str]:
    values = list(getattr(args, "codex_config", []) or [])
    if getattr(args, "no_mcp", False):
        servers = configured_mcp_servers(Path(codex_home) / "config.toml" if codex_home else None)
        if not servers:
            values.append("mcp_servers={}")
        for name in servers:
            values.append(f"mcp_servers.{name}.enabled=false")
    for value in values:
        if "=" not in value:
            raise SystemExit(f"--codex-config must be key=value, got: {value}")
    return values


def agent_model_policy(model: str | None = None, reasoning_effort: str | None = None) -> dict[str, str]:
    """Freeze inexpensive automation defaults without inheriting interactive settings.

    Unsupported models/efforts are rejected by app-server; never retry on a
    more expensive model. Explicit caller settings always win.
    """
    effort = (reasoning_effort or DEFAULT_AGENT_REASONING).strip()
    if effort not in AGENT_REASONING_CHOICES:
        raise SystemExit(f"unsupported reasoning effort: {effort}")
    return {"model": (model or DEFAULT_AGENT_MODEL).strip() or DEFAULT_AGENT_MODEL,
            "reasoning_effort": effort}


def add_agent_model_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", default=DEFAULT_AGENT_MODEL,
                        help=f"Automation model (default: {DEFAULT_AGENT_MODEL}); overrides interactive config")
    parser.add_argument("--reasoning-effort", default=DEFAULT_AGENT_REASONING,
                        choices=AGENT_REASONING_CHOICES,
                        help="Automation reasoning effort (default: low); unsupported model combinations fail")


def input_text(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": text}]


def text_from_item(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for content in item.get("content", []) or []:
        if isinstance(content, dict) and content.get("type") == "text":
            parts.append(str(content.get("text", "")))
    for key in ["text", "message", "content"]:
        value = item.get(key)
        if isinstance(value, str):
            parts.append(value)
    return "".join(parts)


def command_failure_summary(item: dict[str, Any]) -> dict[str, Any]:
    if item.get("type") != "commandExecution":
        return {}
    status = str(item.get("status", "")).lower()
    exit_code = item.get("exitCode", item.get("exit_code"))
    failed = status in {"failed", "error", "cancelled", "canceled"}
    if exit_code is not None:
        try:
            failed = failed or int(exit_code) != 0
        except (TypeError, ValueError):
            failed = failed or bool(exit_code)
    if not failed:
        return {}
    command = item.get("command") or item.get("cmd") or item.get("text") or ""
    output = item.get("aggregatedOutput") or item.get("output") or item.get("stderr") or item.get("stdout") or ""
    output_text = str(output)
    summary: dict[str, Any] = {
        "kind": "command_failed",
        "status": status or "unknown",
        "exit_code": exit_code,
        "command": compact(str(command), 240),
    }
    if output_text:
        tail = output_text[-2000:]
        summary["output_tail"] = compact(tail, 1000)
        problem = problem_kind(tail)
        if problem:
            summary["problem_kind"] = problem
    return summary


class AppServerStartError(SystemExit):
    """app-server failed before initialize completed; carries its stderr."""

    def __init__(self, message: str, stderr_lines: list[str]) -> None:
        super().__init__(message)
        self.stderr_lines = stderr_lines


class AppServer:
    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT,
        config_args: list[str] | None = None,
        codex_home: str | None = None,
    ) -> None:
        self.timeout = timeout
        self.next_id = 1
        self.stderr_lines: list[str] = []
        command = ["codex", "app-server"]
        for value in config_args or []:
            command.extend(["-c", value])
        command.append("--stdio")
        env = os.environ.copy()
        if codex_home:
            env["CODEX_HOME"] = codex_home
        self.codex_home = env.get("CODEX_HOME", str(Path.home() / ".codex"))
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        # Read pipe bytes ourselves: TextIOWrapper.readline() can read ahead,
        # hiding complete JSON lines from select() after the kernel pipe empties.
        self._pipe_buffers: dict[Any, bytes] = {}
        self._pipe_eof: set[Any] = set()
        self._pending_notifications: deque[dict[str, Any]] = deque()
        self._preserve_notifications = False
        try:
            self._initialize()
        except SystemExit as exc:
            self.close()
            raise AppServerStartError(str(exc), list(self.stderr_lines)) from exc

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def __enter__(self) -> "AppServer":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()

    def _send(self, payload: dict[str, Any]) -> None:
        if self.proc.stdin is None:
            raise SystemExit("app-server stdin is closed")
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()

    def _request_payload(self, method: str, params: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        rid = self.next_id
        self.next_id += 1
        return rid, {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        rid, payload = self._request_payload(method, params)
        self._send(payload)
        return self._read_response(rid, timeout=self.timeout)

    def _read_line(self, timeout: float) -> dict[str, Any] | None:
        if self._pending_notifications:
            return self._pending_notifications.popleft()
        return self._read_pipe_message(timeout)

    def _read_pipe_message(self, timeout: float) -> dict[str, Any] | None:
        end = time.monotonic() + timeout
        streams = [stream for stream in (self.proc.stdout, self.proc.stderr) if stream is not None]
        while True:
            # Always drain complete user-space lines before consulting select.
            # Decode only complete lines so a split UTF-8 character is retained.
            for stream in streams:
                pending = self._pipe_buffers.get(stream, b"")
                while b"\n" in pending or (stream in self._pipe_eof and pending):
                    if b"\n" in pending:
                        raw, pending = pending.split(b"\n", 1)
                    else:
                        raw, pending = pending, b""
                    self._pipe_buffers[stream] = pending
                    if stream is self.proc.stderr:
                        self.stderr_lines.append(raw.decode("utf-8", errors="replace").strip())
                        continue
                    try:
                        return json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        raise SystemExit(f"invalid app-server JSON: {raw[:200]!r}") from exc
            active = [stream for stream in streams if stream not in self._pipe_eof]
            if not active:
                details = "\n".join(self.stderr_lines).strip()
                raise SystemExit(f"app-server exited unexpectedly: {details}")
            remaining = end - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select(active, [], [], min(0.2, remaining))
            for stream in ready:
                chunk = os.read(stream.fileno(), 65536)
                if chunk:
                    self._pipe_buffers[stream] = self._pipe_buffers.get(stream, b"") + chunk
                else:
                    self._pipe_eof.add(stream)

    def _read_response(self, rid: int, timeout: float) -> dict[str, Any]:
        end = time.time() + timeout
        while time.time() < end:
            msg = self._read_pipe_message(timeout=max(0.1, end - time.time()))
            if msg is None:
                break
            if msg.get("id") == rid:
                if "error" in msg:
                    raise SystemExit(f"{msg['error']}")
                return msg.get("result", {})
            if self._preserve_notifications and "method" in msg:
                self._pending_notifications.append(msg)
        raise SystemExit(f"timed out waiting for app-server response id={rid}")

    def stream_until_complete(
        self,
        thread_id: str,
        turn_id: str,
        run_dir: Path,
        timeout: float,
        idle_timeout: float = 120,
        progress_interval: float = 30,
        fail_on_idle: bool = False,
    ) -> dict[str, Any]:
        transcript_file = run_dir / "stream.jsonl"
        reply_file = run_dir / "reply.txt"
        diff_file = run_dir / "diff.patch"
        status_file = run_dir / "status.json"
        latest_diff = ""
        reply_parts: list[str] = []
        errors: list[Any] = []
        # Failed shell commands inside a turn are routine (tests, probes); they
        # are surfaced as warnings and never override the turn's terminal status.
        warnings: list[dict[str, Any]] = []
        last_event_ts = time.time()
        last_event_at = now()
        last_event_method = "turn/start"
        event_count = 0
        status: dict[str, Any] = {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "status": "running",
            "diagnosis": "running",
            "started_at": now(),
            "updated_at": now(),
            "last_event_at": last_event_at,
            "last_event_method": last_event_method,
            "idle_seconds": 0,
            "event_count": 0,
        }

        def write_status(current: str = "running") -> None:
            idle_seconds = int(max(0.0, time.time() - last_event_ts))
            diagnostic_text = "\n".join([json.dumps(e, ensure_ascii=False) for e in errors] + self.stderr_lines[-5:])
            detected_problem = problem_kind(diagnostic_text)
            if current in {"completed", "timed_out"}:
                diagnosis = current
            elif detected_problem:
                diagnosis = detected_problem
            elif idle_timeout and idle_seconds >= idle_timeout:
                diagnosis = "idle_no_events"
            else:
                diagnosis = current
            status.update({
                "status": current,
                "diagnosis": diagnosis,
                "updated_at": now(),
                "last_event_at": last_event_at,
                "last_event_method": last_event_method,
                "idle_seconds": idle_seconds,
                "event_count": event_count,
            })
            if errors:
                status["errors"] = errors
            if warnings:
                status["warnings"] = warnings
            if self.stderr_lines:
                stderr_file = run_dir / "app-server-stderr.log"
                stderr_file.write_text("\n".join(self.stderr_lines) + "\n", encoding="utf-8")
                status["app_server_stderr_file"] = str(stderr_file)
                status["app_server_stderr_tail"] = self.stderr_lines[-5:]
            status_file.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        write_status("running")
        end = time.time() + timeout
        next_progress = time.time() + max(1.0, progress_interval)
        idle_reported = False
        while time.time() < end:
            remaining = max(0.0, end - time.time())
            try:
                msg = self._read_line(timeout=min(1.0, remaining))
            except SystemExit as exc:
                errors.append({"app_server_error": str(exc)})
                status["app_server_error"] = str(exc)
                write_status("app_server_died")
                raise
            if msg is None:
                write_status("running")
                idle_seconds = int(time.time() - last_event_ts)
                if idle_timeout and idle_seconds >= idle_timeout and not idle_reported:
                    print(f"watchdog: no app-server events for {idle_seconds}s; status={status.get('diagnosis')}", flush=True)
                    idle_reported = True
                    if fail_on_idle:
                        interrupt_error = self.interrupt_turn(thread_id, turn_id)
                        status.update({"status": "idle_timeout", "completed_at": now(), "interrupt_requested": True})
                        if interrupt_error:
                            status["interrupt_error"] = interrupt_error
                        write_status("idle_timeout")
                        raise SystemExit(f"idle timeout after {idle_seconds}s without app-server events")
                if progress_interval and time.time() >= next_progress:
                    print(
                        f"waiting: status={status.get('status')} diagnosis={status.get('diagnosis')} idle={status.get('idle_seconds')}s last={status.get('last_event_method')}",
                        flush=True,
                    )
                    next_progress = time.time() + max(1.0, progress_interval)
                continue
            transcript_file.parent.mkdir(parents=True, exist_ok=True)
            with transcript_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
            method = msg.get("method")
            params = msg.get("params", {})
            if params.get("threadId") != thread_id:
                continue
            if "turnId" in params and params.get("turnId") != turn_id:
                continue
            event_count += 1
            last_event_ts = time.time()
            last_event_at = now()
            last_event_method = str(method or "unknown")
            idle_reported = False
            if method == "item/agentMessage/delta":
                reply_parts.append(params.get("delta", ""))
                reply_file.write_text("".join(reply_parts), encoding="utf-8")
            elif method == "item/completed":
                item = params.get("item", {})
                command_failure = command_failure_summary(item)
                if command_failure:
                    warnings.append(command_failure)
                if item.get("type") in {"agentMessage", "assistantMessage"}:
                    text = text_from_item(item)
                    current = "".join(reply_parts)
                    if text and text.strip() != current.strip():
                        reply_parts.append(text)
                        reply_file.write_text("".join(reply_parts), encoding="utf-8")
            elif method == "turn/diff/updated":
                latest_diff = params.get("diff", "")
                diff_file.write_text(latest_diff, encoding="utf-8")
            elif method == "thread/status/changed":
                thread_status = params.get("status", {})
                if isinstance(thread_status, dict) and thread_status.get("type") == "idle":
                    observed = self._read_turn_status(thread_id, turn_id)
                    if observed and observed.get("completed"):
                        observed_status = observed.get("status", "completed")
                        if observed.get("error"):
                            errors.append(observed["error"])
                        status.update({"completed_at": now()})
                        write_status(observed_status)
                        return {
                            "status": observed_status,
                            "diagnosis": status.get("diagnosis", observed_status),
                            "reply": "".join(reply_parts),
                            "diff": latest_diff,
                            "transcript_file": str(transcript_file),
                            "reply_file": str(reply_file),
                            "diff_file": str(diff_file),
                            "status_file": str(status_file),
                            "warnings": warnings,
                        }
            elif method == "error":
                errors.append(params.get("error", params))
                (run_dir / "errors.json").write_text(json.dumps(errors, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            elif method == "turn/completed":
                turn = params.get("turn", {})
                completed_status = turn.get("status", "completed")
                if turn.get("error"):
                    errors.append(turn["error"])
                status.update({"completed_at": now()})
                write_status(completed_status)
                return {
                    "status": completed_status,
                    "diagnosis": status.get("diagnosis", completed_status),
                    "reply": "".join(reply_parts),
                    "diff": latest_diff,
                    "transcript_file": str(transcript_file),
                    "reply_file": str(reply_file),
                    "diff_file": str(diff_file),
                    "status_file": str(status_file),
                    "warnings": warnings,
                }
            write_status("running")
        observed = self._read_turn_status(thread_id, turn_id)
        if observed and observed.get("completed"):
            # The turn finished right at the deadline; report its real status.
            status["completed_at"] = now()
            if observed.get("error"):
                errors.append(observed["error"])
            write_status(str(observed.get("status") or "completed"))
        else:
            # This process owns the app-server; leaving the turn running would
            # let close() kill it silently. Interrupt explicitly and say so.
            interrupt_error = self.interrupt_turn(thread_id, turn_id)
            status.update({
                "completed_at": now(),
                "interrupt_requested": True,
                "turn_status_at_timeout": (observed or {}).get("status", "unknown"),
            })
            if interrupt_error:
                status["interrupt_error"] = interrupt_error
            write_status("timed_out")
        return {
            "status": status["status"],
            "diagnosis": status.get("diagnosis", status["status"]),
            "reply": "".join(reply_parts),
            "diff": latest_diff,
            "transcript_file": str(transcript_file),
            "reply_file": str(reply_file),
            "diff_file": str(diff_file),
            "status_file": str(status_file),
            "warnings": warnings,
        }

    def interrupt_turn(self, thread_id: str, turn_id: str) -> str:
        """Request turn/interrupt; return the error text ('' on success)."""
        try:
            self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        except SystemExit as exc:
            return str(exc) or "turn/interrupt failed"
        return ""

    def _read_turn_status(self, thread_id: str, turn_id: str) -> dict[str, Any] | None:
        # An idle event can arrive just before turn/completed. Preserve any
        # notifications received while the diagnostic RPC awaits its response.
        # Only streaming diagnostics retain these; polling-only daemons do not
        # accumulate an unused notification queue.
        self._preserve_notifications = True
        try:
            result = self.request("thread/read", {"threadId": thread_id, "includeTurns": True})
        except SystemExit:
            return None
        finally:
            self._preserve_notifications = False
        for turn in result.get("thread", {}).get("turns", []):
            if turn.get("id") == turn_id:
                return {
                    "status": turn.get("status", ""),
                    "completed": turn.get("status") in {"completed", "failed", "cancelled"},
                    "error": turn.get("error"),
                }
        return None

    def _initialize(self) -> None:
        result = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "secretary_bus",
                    "title": "Secretary Bus",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        self.notify("initialized", {})
        self.user_agent = result.get("userAgent", "")


def load_index() -> dict[str, Any]:
    return read_json(RUN_INDEX, {"threads": {}, "runs": {}})


def save_index(index: dict[str, Any]) -> None:
    write_json(RUN_INDEX, index)


def update_thread_index(thread_id: str, **fields: Any) -> None:
    index = load_index()
    thread = index.setdefault("threads", {}).setdefault(thread_id, {})
    thread.update(fields)
    thread["updated_at"] = now()
    save_index(index)


def record_run(run_id: str, thread_id: str, **fields: Any) -> None:
    """Upsert a run; thread_id may be empty while the run is still starting."""
    index = load_index()
    run = index.setdefault("runs", {}).setdefault(run_id, {})
    run.update({"thread_id": thread_id or run.get("thread_id", ""), **fields, "updated_at": now()})
    if thread_id:
        index.setdefault("threads", {}).setdefault(thread_id, {})["last_run_id"] = run_id
        index["threads"][thread_id]["updated_at"] = now()
    save_index(index)


def _toml_parser() -> Any:
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ModuleNotFoundError:
            return None
    return tomllib


def _worker_home_ready(home: Path) -> bool:
    config = home / "config.toml"
    if not config.exists() or not (home / "auth.json").exists():
        return False
    parser = _toml_parser()
    if parser is None:
        return True
    try:
        parser.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False  # half-written or duplicated tables: rebuild instead of failing every start
    return True


def _project_trusted(home: Path, project_dir: str) -> bool:
    """True when config.toml has the trusted table create-isolated-codex-home.sh writes."""
    header = "[projects." + json.dumps(str(Path(project_dir).resolve()), ensure_ascii=False) + "]"
    try:
        lines = (home / "config.toml").read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    in_table = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            in_table = stripped == header
        elif in_table and re.match(r'^trust_level\s*=\s*"trusted"\s*(#.*)?$', stripped):
            return True
    return False


def _home_ok(home: Path, project_dir: str | None) -> bool:
    return _worker_home_ready(home) and (not project_dir or _project_trusted(home, project_dir))


def _create_or_trust_home(slug: str, project_dir: str | None) -> str:
    """Create the home once and pre-trust project_dir, serialized by a per-home lock.

    Two concurrent `codex start` calls used to run the install script at the
    same time, which shares one temp config path and appends tables after a
    grep, and could leave a broken config.toml that then failed every later start.
    """
    home = CODEX_HOMES_ROOT / slug
    if _home_ok(home, project_dir):
        return str(home)
    CODEX_HOMES_ROOT.mkdir(parents=True, exist_ok=True)
    with open(CODEX_HOMES_ROOT / f".{slug}.create.lock", "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if _home_ok(home, project_dir):
            return str(home)
        cmd = ["bash", str(CREATE_HOME_SCRIPT), slug]
        if project_dir:
            cmd.append(str(Path(project_dir).resolve()))
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 or not _home_ok(home, project_dir):
        raise SystemExit(
            f"failed to prepare isolated worker CODEX_HOME {home} (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()}; rerun with --shared-home to use the caller's CODEX_HOME"
        )
    return str(home)


def _slot_owner(lock_path: Path) -> str:
    try:
        owner = json.loads(lock_path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return "owner unknown"
    if not owner:
        return "owner unknown"
    return f"run={owner.get('run_id', '?')} name={owner.get('display_name', '?')} pid={owner.get('pid', '?')}"


def _claim_worker_slot(wait_slot: float, owner: dict[str, Any]) -> tuple[str, Any]:
    """Lock the first free slot <slug>-1..N; the returned file keeps the lock."""
    if WORKER_SLOTS < 1:
        raise SystemExit(f"SECRETARY_BUS_CODEX_WORKER_SLOTS must be >= 1, got {WORKER_SLOTS}")
    CODEX_HOMES_ROOT.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.0, wait_slot)
    announced = False
    while True:
        for index in range(1, WORKER_SLOTS + 1):
            slug = f"{WORKER_HOME_SLUG}-{index}"
            # "a+" so a busy slot's owner record is not truncated before flock.
            handle = open(CODEX_HOMES_ROOT / f".{slug}.slot.lock", "a+", encoding="utf-8")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                continue
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps({**owner, "pid": os.getpid(), "slot": slug, "claimed_at": now()}, ensure_ascii=False))
            handle.flush()
            return slug, handle
        if time.monotonic() >= deadline:
            busy = "; ".join(
                f"{WORKER_HOME_SLUG}-{i}: {_slot_owner(CODEX_HOMES_ROOT / f'.{WORKER_HOME_SLUG}-{i}.slot.lock')}"
                for i in range(1, WORKER_SLOTS + 1)
            )
            raise SystemExit(
                f"all {WORKER_SLOTS} Codex worker slots are busy ({busy}); retry later, pass --wait-slot SECONDS, "
                "raise SECRETARY_BUS_CODEX_WORKER_SLOTS, or use --shared-home"
            )
        if not announced:
            print(f"all {WORKER_SLOTS} worker slots busy; waiting up to {wait_slot:g}s for a free slot", flush=True)
            announced = True
        time.sleep(min(SLOT_WAIT_POLL, max(0.0, deadline - time.monotonic())))


def ensure_worker_home(
    slug: str | None = None,
    project_dir: str | None = None,
    *,
    wait_slot: float = 0,
    owner: dict[str, Any] | None = None,
) -> str:
    """Return an isolated worker CODEX_HOME, pre-trusting project_dir.

    With no slug, claim a free slot from the pool and keep its lock until
    release_worker_slot() or process exit, so concurrent workers never share
    a home. An explicit slug only creates/reuses that home (no slot lock).
    """
    if slug is not None:
        return _create_or_trust_home(slug, project_dir)
    slug, handle = _claim_worker_slot(wait_slot, owner or {})
    try:
        home = _create_or_trust_home(slug, project_dir)
    except BaseException:
        handle.close()
        raise
    _HELD_SLOT_LOCKS[home] = handle
    return home


def release_worker_slot(home: str | None) -> None:
    handle = _HELD_SLOT_LOCKS.pop(home, None) if home else None
    if handle is not None:
        handle.close()


def thread_codex_home(args: argparse.Namespace, thread_id: str) -> str | None:
    """CODEX_HOME that owns a thread: the one recorded at start, else inherited.

    Runs created before isolated worker homes have no record and keep using the
    caller's CODEX_HOME, which is where they were created.
    """
    if not thread_id:
        return None
    index = load_index()
    home = index.get("threads", {}).get(thread_id, {}).get("codex_home")
    if not home:
        _run_id, run = latest_run_for_thread(index, thread_id)
        home = run.get("codex_home")
    return str(home) if home else None


def thread_app(args: argparse.Namespace, thread_id: str) -> "AppServer":
    home = thread_codex_home(args, thread_id)
    return AppServer(timeout=args.timeout, config_args=app_config_args(args, home), codex_home=home)


def thread_summary(thread: dict[str, Any]) -> str:
    tid = thread.get("id") or thread.get("sessionId") or ""
    name = thread.get("threadName") or thread.get("name") or ""
    cwd = thread.get("cwd") or thread.get("session", {}).get("cwd") or ""
    preview = thread.get("preview") or thread.get("title") or ""
    updated = thread.get("updatedAt") or thread.get("updated_at") or thread.get("createdAt") or ""
    return f"{tid}\t{name or '-'}\t{updated or '-'}\t{cwd or '-'}\t{compact(preview, 110)}"


def thread_public_view(thread: dict[str, Any], *, preview_width: int = 220) -> dict[str, Any]:
    """Small dashboard view; app-server raw thread objects can contain huge previews."""
    return {
        "id": thread.get("id") or thread.get("sessionId") or "",
        "name": thread.get("threadName") or thread.get("name") or "",
        "cwd": thread.get("cwd") or thread.get("session", {}).get("cwd") or "",
        "updatedAt": thread.get("updatedAt") or thread.get("updated_at") or thread.get("createdAt") or "",
        "status": thread.get("status", {}),
        "source": thread.get("source") or thread.get("threadSource") or "",
        "preview": compact(thread.get("preview") or thread.get("title") or "", preview_width),
    }


def is_active_turn(turn: dict[str, Any]) -> bool:
    status = str(turn.get("status", "")).lower()
    return bool(status) and status not in TERMINAL_TURN_STATUSES


def turns_list(app: AppServer, thread_id: str, limit: int = 20, items_view: str = "summary") -> list[dict[str, Any]]:
    result = app.request(
        "thread/turns/list",
        {
            "threadId": thread_id,
            "limit": limit,
            "sortDirection": "desc",
            "itemsView": items_view,
        },
    )
    return result.get("data", [])


def latest_turn(turns: list[dict[str, Any]]) -> dict[str, Any] | None:
    return turns[0] if turns else None


def active_turn(turns: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((turn for turn in turns if is_active_turn(turn)), None)


def turn_items(app: AppServer, thread_id: str, turn_id: str, limit: int = 100) -> list[dict[str, Any]]:
    try:
        result = app.request(
            "thread/items/list",
            {"threadId": thread_id, "turnId": turn_id, "limit": limit, "sortDirection": "asc"},
        )
    except SystemExit as exc:
        if "not supported yet" in str(exc):
            return []
        raise
    # Entries are {"item": ThreadItem, "turnId": ...}.
    return [entry.get("item", entry) if isinstance(entry, dict) else entry for entry in result.get("data", [])]


def reply_from_items(items: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in items:
        if item.get("type") in {"agentMessage", "assistantMessage"}:
            text = text_from_item(item)
            if text:
                parts.append(text)
    return "\n".join(parts)


def resolve_turn_id(app: AppServer, thread_id: str, explicit: str = "", allow_terminal: bool = False) -> tuple[str, dict[str, Any] | None]:
    if explicit:
        turns = turns_list(app, thread_id, limit=50)
        turn = next((item for item in turns if item.get("id") == explicit), None)
        return explicit, turn
    turns = turns_list(app, thread_id, limit=50)
    turn = active_turn(turns)
    if turn:
        return str(turn["id"]), turn
    if allow_terminal and turns:
        return str(turns[0]["id"]), turns[0]
    run_id, run = latest_run_for_thread(load_index(), thread_id)
    run_turn_id = str(run.get("turn_id") or run.get("last_turn_id") or "")
    run_status = str(run.get("status") or "")
    run_dir = Path(str(run.get("run_dir", ""))) if run.get("run_dir") else None
    if run_dir:
        local_status = read_json(run_dir / "status.json", {})
        run_status = str(local_status.get("status") or run_status)
        run_turn_id = str(local_status.get("turn_id") or run_turn_id)
    if run_turn_id and run_status in {"running", "starting"}:
        return run_turn_id, None
    latest = latest_turn(turns)
    hint = f"; latest turn={latest.get('id')} status={latest.get('status')}" if latest else ""
    if run_id and run_turn_id:
        hint += f"; local last turn={run_turn_id} status={run_status or '?'}"
    raise SystemExit(f"no active turn found for thread {thread_id}{hint}")


def git_diff_snapshot(repo: Path, out_dir: Path | None = None, baseline_head: str | None = None) -> dict[str, str]:
    target = out_dir or Path(os.environ.get("TMPDIR", "/tmp")) / f"secretary-bus-diff-{os.getpid()}"
    target.mkdir(parents=True, exist_ok=True)
    git = supervisor.git_capture(repo, baseline_head, target)
    diff = ""
    for key in ("git-diff-worktree-since-baseline.patch", "git-diff-uncommitted.patch"):
        path = git.get(key)
        if path and Path(str(path)).exists():
            text = Path(str(path)).read_text(encoding="utf-8")
            if text.strip():
                diff += text if text.endswith("\n") else text + "\n"
                if key == "git-diff-worktree-since-baseline.patch":
                    break
    combined = target / "git-diff-current.patch"
    combined.write_text(diff, encoding="utf-8")
    git["git-diff-current.patch"] = str(combined)
    return {k: str(v) for k, v in git.items()}


def cmd_list(args: argparse.Namespace) -> None:
    params: dict[str, Any] = {
        "limit": args.limit,
        "archived": args.archived,
        "sortKey": "updated_at",
        "sortDirection": "desc",
    }
    if args.cwd:
        params["cwd"] = args.cwd
    if args.search:
        params["searchTerm"] = args.search
    if args.all_sources:
        params["sourceKinds"] = ["cli", "vscode", "exec", "appServer", "subAgent", "unknown"]
    with AppServer(timeout=args.timeout, config_args=app_config_args(args)) as app:
        result = app.request("thread/list", params)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print("thread_id\tname\tupdated\tcwd\tpreview")
    for thread in result.get("data", []):
        print(thread_summary(thread))


def cmd_read(args: argparse.Namespace) -> None:
    with thread_app(args, args.thread_id) as app:
        result = app.request("thread/read", {"threadId": args.thread_id, "includeTurns": args.turns})
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    thread = result.get("thread", {})
    print(thread_summary(thread))
    if args.turns:
        for turn in thread.get("turns", [])[-args.tail:]:
            print(f"\n## turn {turn.get('id', '')} status={turn.get('status', '')}")
            for item in turn.get("items", [])[-20:]:
                text = item.get("text") or item.get("content") or item.get("message") or ""
                if text:
                    print(compact(str(text), 160))


def cmd_status(args: argparse.Namespace) -> None:
    index = load_index()
    local_thread = index.get("threads", {}).get(args.thread_id, {})
    with thread_app(args, args.thread_id) as app:
        thread = app.request("thread/read", {"threadId": args.thread_id, "includeTurns": False}).get("thread", {})
        turns = turns_list(app, args.thread_id, limit=args.turns, items_view="summary")
    latest = latest_turn(turns)
    active = active_turn(turns)
    result = {
        "thread": {
            "id": args.thread_id,
            "name": thread.get("threadName") or thread.get("name") or local_thread.get("display_name", ""),
            "cwd": thread.get("cwd") or local_thread.get("repo", ""),
            "status": thread.get("status"),
            "preview": thread.get("preview", ""),
        },
        "active_turn": active,
        "latest_turn": latest,
        "local": local_thread,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    t = result["thread"]
    print(f"thread={t['id']}")
    print(f"name={t.get('name') or '-'}")
    print(f"cwd={t.get('cwd') or '-'}")
    print(f"thread_status={t.get('status')}")
    if active:
        print(f"active_turn={active.get('id')} status={active.get('status')}")
    else:
        print("active_turn=(none)")
    if latest:
        print(f"latest_turn={latest.get('id')} status={latest.get('status')} started={latest.get('startedAt')} completed={latest.get('completedAt')}")


def cmd_wait(args: argparse.Namespace) -> None:
    with thread_app(args, args.thread_id) as app:
        turn_id, turn = resolve_turn_id(app, args.thread_id, args.turn_id, allow_terminal=True)
        deadline = time.time() + args.wait_timeout
        last_status = ""
        while time.time() < deadline:
            turns = turns_list(app, args.thread_id, limit=50, items_view="summary")
            turn = next((item for item in turns if item.get("id") == turn_id), turn)
            status = str((turn or {}).get("status", "unknown"))
            if status != last_status or args.verbose:
                print(f"{now()}\tturn={turn_id}\tstatus={status}")
                last_status = status
            if status.lower() in TERMINAL_TURN_STATUSES:
                items = turn_items(app, args.thread_id, turn_id, limit=args.items)
                reply = reply_from_items(items)
                if args.json:
                    print(json.dumps({"thread_id": args.thread_id, "turn_id": turn_id, "status": status, "reply": reply}, ensure_ascii=False, indent=2))
                elif reply:
                    print("\nreply:")
                    print(reply.strip())
                update_thread_index(args.thread_id, last_turn_id=turn_id, status=status)
                return
            time.sleep(args.interval)
    raise SystemExit(f"timed out waiting for turn {turn_id}")


def cmd_watch(args: argparse.Namespace) -> None:
    deadline = time.time() + args.duration if args.duration > 0 else float("inf")
    with thread_app(args, args.thread_id) as app:
        while time.time() < deadline:
            turns = turns_list(app, args.thread_id, limit=args.turns, items_view="summary")
            active = active_turn(turns)
            latest = latest_turn(turns)
            print(f"{now()}\tactive={active.get('id') if active else '-'}\tlatest={latest.get('id') if latest else '-'}\tstatus={latest.get('status') if latest else '-'}")
            if args.once:
                return
            time.sleep(args.interval)


def cmd_name(args: argparse.Namespace) -> None:
    with thread_app(args, args.thread_id) as app:
        app.request("thread/name/set", {"threadId": args.thread_id, "name": args.name})
    update_thread_index(args.thread_id, display_name=args.name)
    print(f"renamed {args.thread_id}: {args.name}")


def cmd_archive(args: argparse.Namespace) -> None:
    method = "thread/archive" if not args.unarchive else "thread/unarchive"
    with thread_app(args, args.thread_id) as app:
        app.request(method, {"threadId": args.thread_id})
    update_thread_index(args.thread_id, archived=not args.unarchive)
    print(("unarchived" if args.unarchive else "archived") + f" {args.thread_id}")


def read_task(args: argparse.Namespace) -> str:
    if args.task_file:
        return Path(args.task_file).read_text(encoding="utf-8")
    if args.task:
        return args.task
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("--task or --task-file is required")


NO_WAIT_MESSAGE = (
    "codex start requires --wait: the worker's app-server is owned by this command "
    "and is terminated when it exits, so a turn started without --wait would be killed "
    "immediately. Re-run with --wait (tune --wait-timeout), or use `secretary-bus leader` "
    "for long-lived supervision."
)


def record_start_failure(
    run_id: str,
    run_dir: Path,
    thread_id: str,
    turn_id: str,
    exc: BaseException,
    stderr_lines: list[str],
) -> None:
    """Persist why a start/wait run failed so doctor has something actionable."""
    status_file = run_dir / "status.json"
    try:
        current = read_json(status_file, {})
    except json.JSONDecodeError as decode_exc:
        current = {"status_file_error": str(decode_exc)}
    error_text = str(exc) or exc.__class__.__name__
    extra: dict[str, Any] = {}
    if stderr_lines:
        stderr_file = run_dir / "app-server-stderr.log"
        stderr_file.write_text("\n".join(stderr_lines) + "\n", encoding="utf-8")
        extra = {"app_server_stderr_file": str(stderr_file), "app_server_stderr_tail": stderr_lines[-5:]}
    prior = str(current.get("status", ""))
    if prior in {"", "starting", "running"}:
        new_status = "failed" if turn_id else "start_failed"
        detected = problem_kind(error_text + "\n" + "\n".join(stderr_lines[-5:]))
        diagnosis = detected or ("controller_error" if turn_id else "start_failed")
        current.update({"status": new_status, "diagnosis": diagnosis, "failed_at": now()})
    else:
        # stream_until_complete already wrote a terminal status (timed_out,
        # idle_timeout, app_server_died); keep it and only add evidence.
        new_status = prior
        diagnosis = str(current.get("diagnosis") or prior)
    if thread_id:
        current.setdefault("thread_id", thread_id)
    if turn_id:
        current.setdefault("turn_id", turn_id)
    current.update({"error": compact(error_text, 2000), "updated_at": now(), **extra})
    write_json(status_file, current)
    fields: dict[str, Any] = {"status": new_status, "diagnosis": diagnosis, "error": compact(error_text, 500),
                              "status_file": str(status_file)}
    if turn_id:
        fields["turn_id"] = turn_id
    record_run(run_id, thread_id, **fields)


def cmd_start(args: argparse.Namespace) -> None:
    if not args.wait:
        raise SystemExit(NO_WAIT_MESSAGE)
    task = read_task(args).strip()
    if not task:
        raise SystemExit("empty task")
    policy = agent_model_policy(args.model, getattr(args, "reasoning_effort", None))
    display_name = args.name or "Codex员工"
    repo = Path(args.repo).resolve() if args.repo else None
    run_id = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S-%f")
    codex_home = None if args.shared_home else ensure_worker_home(
        project_dir=str(repo) if repo else None,
        wait_slot=getattr(args, "wait_slot", 0),
        owner={"run_id": run_id, "display_name": display_name, "repo": str(repo) if repo else ""},
    )
    try:
        _start_worker(args, task, policy, display_name, repo, run_id, codex_home)
    finally:
        release_worker_slot(codex_home)


def _start_worker(
    args: argparse.Namespace,
    task: str,
    policy: dict[str, str],
    display_name: str,
    repo: Path | None,
    run_id: str,
    codex_home: str | None,
) -> None:
    effective_home = codex_home or os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    RUNS.mkdir(parents=True, exist_ok=True)
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "task.txt").write_text(task + "\n", encoding="utf-8")
    baseline = supervisor.baseline_repo(repo, run_dir) if repo else {}
    params = {
        "cwd": str(repo) if repo else None,
        "model": policy["model"],
        "config": {"model_reasoning_effort": policy["reasoning_effort"]},
        "approvalPolicy": args.approval,
        "sandbox": args.sandbox,
        "threadSource": "user",
    }
    params = {k: v for k, v in params.items() if v not in (None, "")}
    run_fields = {
        "run_dir": str(run_dir),
        "display_name": display_name,
        "repo": str(repo) if repo else "",
        "baseline_head": baseline.get("head", ""),
        "codex_home": effective_home,
        **policy,
        "task_file": str(run_dir / "task.txt"),
    }
    write_json(run_dir / "status.json", {
        "status": "starting",
        "diagnosis": "starting_app_server",
        **policy,
        "display_name": display_name,
        "repo": str(repo) if repo else "",
        "run_dir": str(run_dir),
        "codex_home": effective_home,
        "started_at": now(),
        "updated_at": now(),
    })
    # Index the run before app-server starts so a startup failure is findable.
    record_run(run_id, "", status="starting", diagnosis="starting_app_server", started_at=now(),
               status_file=str(run_dir / "status.json"), **run_fields)
    print(f"name={display_name}", flush=True)
    print(f"run dir={run_dir}", flush=True)
    print(f"codex home={effective_home}", flush=True)
    app: AppServer | None = None
    thread_id = ""
    turn_id = ""
    try:
        with AppServer(timeout=args.timeout, config_args=app_config_args(args, codex_home), codex_home=codex_home) as app:
            started = app.request("thread/start", params)
            thread = started.get("thread", {})
            thread_id = thread.get("id") or thread.get("sessionId") or ""
            if not thread_id:
                raise SystemExit("thread/start did not return a thread id")
            app.request("thread/name/set", {"threadId": thread_id, "name": display_name})
            turn_result = app.request("turn/start", {"threadId": thread_id, "input": input_text(task), "effort": policy["reasoning_effort"]})
            turn = turn_result.get("turn", {})
            turn_id = turn.get("id") or ""
            if not turn_id:
                raise SystemExit("turn/start did not return a turn id")
            record_run(run_id, thread_id, **run_fields, turn_id=turn_id, status="running", diagnosis="running")
            update_thread_index(
                thread_id,
                display_name=display_name,
                repo=str(repo) if repo else "",
                baseline_head=baseline.get("head", ""),
                codex_home=effective_home,
                last_turn_id=turn_id,
                archived=False,
            )
            write_json(run_dir / "status.json", {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "status": "running",
                "diagnosis": "running",
                **policy,
                "display_name": display_name,
                "repo": str(repo) if repo else "",
                "run_dir": str(run_dir),
                "codex_home": effective_home,
                "started_at": now(),
                "updated_at": now(),
            })
            print(f"thread id={thread_id}", flush=True)
            print(f"turn id={turn_id}", flush=True)
            outcome = app.stream_until_complete(
                thread_id,
                turn_id,
                run_dir,
                timeout=args.wait_timeout,
                idle_timeout=args.idle_timeout,
                progress_interval=args.progress_interval,
                fail_on_idle=args.fail_on_idle,
            )
    except BaseException as exc:
        stderr_lines = getattr(exc, "stderr_lines", None) or (list(app.stderr_lines) if app is not None else [])
        record_start_failure(run_id, run_dir, thread_id, turn_id, exc, stderr_lines)
        raise
    record_run(run_id, thread_id, **outcome)
    update_thread_index(thread_id, last_turn_id=turn_id, status=outcome["status"])
    print(f"status={outcome['status']} diagnosis={outcome.get('diagnosis', outcome['status'])}", flush=True)
    warnings = outcome.get("warnings") or []
    if warnings:
        print(f"warnings={len(warnings)} failed command(s) inside the turn; see {outcome.get('status_file')}", flush=True)
    if outcome["reply"]:
        print("\nreply:", flush=True)
        print(outcome["reply"].strip(), flush=True)
    if outcome["diff"]:
        print(f"\ndiff={run_dir / 'diff.patch'}", flush=True)
    if outcome["status"] == "timed_out":
        raise SystemExit(
            f"worker turn timed out after {args.wait_timeout:g}s and turn/interrupt was requested; "
            f"see {outcome.get('status_file')} (raise --wait-timeout for longer tasks)"
        )
    if args.fail_on_error:
        status_ok = str(outcome.get("status", "")).lower() == "completed"
        diagnosis_ok = outcome.get("diagnosis") in {"completed", ""}
        if not (status_ok and diagnosis_ok):
            raise SystemExit(f"worker turn ended with status={outcome.get('status')} diagnosis={outcome.get('diagnosis')}")


def cmd_steer(args: argparse.Namespace) -> None:
    text = read_task(args).strip()
    with thread_app(args, args.thread_id) as app:
        turn_id, _turn = resolve_turn_id(app, args.thread_id, args.turn_id)
        result = app.request("turn/steer", {"threadId": args.thread_id, "expectedTurnId": turn_id, "input": input_text(text)})
    update_thread_index(args.thread_id, last_turn_id=result.get("turnId", turn_id))
    print(f"steered thread={args.thread_id} turn={result.get('turnId', turn_id)}")


def cmd_interrupt(args: argparse.Namespace) -> None:
    with thread_app(args, args.thread_id) as app:
        turn_id, _turn = resolve_turn_id(app, args.thread_id, args.turn_id)
        app.request("turn/interrupt", {"threadId": args.thread_id, "turnId": turn_id})
    update_thread_index(args.thread_id, last_turn_id=turn_id, status="interrupted")
    print(f"interrupted thread={args.thread_id} turn={turn_id}")


def cmd_diff(args: argparse.Namespace) -> None:
    index = load_index()
    thread_meta = index.get("threads", {}).get(args.thread_id, {})

    if args.repo:
        repo = Path(args.repo).resolve()
        run_id = args.run_id or thread_meta.get("last_run_id")
        run = index.get("runs", {}).get(run_id, {}) if run_id else {}
        out_dir = Path(args.out_dir).resolve() if args.out_dir else None
        if out_dir is None and args.save and run.get("run_dir"):
            out_dir = Path(str(run["run_dir"])) / "git-snapshot"
        baseline_head = run.get("baseline_head") or thread_meta.get("baseline_head") or None
        snapshot = git_diff_snapshot(repo, out_dir=out_dir, baseline_head=baseline_head)
        diff_file = snapshot.get("git-diff-current.patch", "")
        print(Path(diff_file).read_text(encoding="utf-8"), end="")
        if args.save:
            run_id = args.run_id or thread_meta.get("last_run_id") or datetime.now().astimezone().strftime("%Y%m%dT%H%M%S-%f")
            run = index.setdefault("runs", {}).setdefault(run_id, {})
            run.update({
                "thread_id": args.thread_id,
                "repo": str(repo),
                "diff_file": diff_file,
                "git_status_file": snapshot.get("git-status.txt", ""),
                "baseline_head": baseline_head or run.get("baseline_head", ""),
                "updated_at": now(),
            })
            index.setdefault("threads", {}).setdefault(args.thread_id, {})["last_run_id"] = run_id
            save_index(index)
        return

    run_id = args.run_id or thread_meta.get("last_run_id")
    if run_id:
        run = index.get("runs", {}).get(run_id, {})
        diff_file = run.get("diff_file")
        if diff_file and Path(diff_file).exists():
            print(Path(diff_file).read_text(encoding="utf-8"), end="")
            return
    raise SystemExit("no saved diff for this thread; pass --repo to show current git diff")


def cmd_doctor(args: argparse.Namespace) -> None:
    index = load_index()
    thread_id = args.thread_id or ""
    run_id = args.run_id or ""
    run: dict[str, Any] = {}
    if args.run_dir:
        run = {"run_dir": str(Path(args.run_dir).resolve())}
    elif run_id:
        run = index.get("runs", {}).get(run_id, {})
        thread_id = thread_id or str(run.get("thread_id", ""))
    elif thread_id:
        run_id, run = latest_run_for_thread(index, thread_id)
    else:
        raise SystemExit("doctor needs <thread-id>, --run-id, or --run-dir")
    data: dict[str, Any] = {"thread_id": thread_id or "", "run_id": run_id or ""}
    status: dict[str, Any] = {}
    if run:
        data["run"] = run
        run_dir = Path(str(run.get("run_dir", ""))) if run.get("run_dir") else None
        if run_dir:
            data["run_dir"] = str(run_dir)
            status_file = run_dir / "status.json"
            transcript_file = run_dir / "stream.jsonl"
            reply_file = run_dir / "reply.txt"
            diff_file = run_dir / "diff.patch"
            status = read_json(status_file, {})
            data["local_status"] = status
            thread_id = thread_id or str(status.get("thread_id", ""))
            data["thread_id"] = thread_id
            if status.get("turn_id"):
                data["turn_id"] = str(status.get("turn_id"))
            data["file_ages_seconds"] = {
                "status": file_age_seconds(status_file),
                "transcript": file_age_seconds(transcript_file),
                "reply": file_age_seconds(reply_file),
                "diff": file_age_seconds(diff_file),
            }
            stderr_file = status.get("app_server_stderr_file")
            if stderr_file and Path(str(stderr_file)).exists():
                lines = Path(str(stderr_file)).read_text(encoding="utf-8", errors="replace").splitlines()
                data["app_server_stderr_tail"] = lines[-5:]
    diagnostic_text = json.dumps(status, ensure_ascii=False) + "\n" + "\n".join(data.get("app_server_stderr_tail", []))
    data["diagnosis"] = status.get("diagnosis") or problem_kind(diagnostic_text) or (status.get("status") if status else "no_local_run")
    status_age = (data.get("file_ages_seconds") or {}).get("status")
    start_budget = START_RPC_COUNT * float(args.timeout)
    if status.get("status") == "starting" and status_age is not None and status_age > start_budget:
        # The starter never reached turn/start within its RPC budget and did not
        # record why (killed, or a pre-fix version): report a startup failure.
        data["diagnosis"] = problem_kind(diagnostic_text) or "start_failed"
        data["start_stale_seconds"] = int(status_age)

    if not args.local and thread_id:
        try:
            home = str(run.get("codex_home") or status.get("codex_home") or "") or thread_codex_home(args, thread_id)
            with AppServer(timeout=args.timeout, config_args=app_config_args(args, home), codex_home=home) as app:
                thread = app.request("thread/read", {"threadId": thread_id}).get("thread", {})
                turns = turns_list(app, thread_id, limit=args.turns)
                active = active_turn(turns)
                latest = latest_turn(turns)
                data["app_thread"] = thread_public_view(thread)
                data["active_turn"] = active or {}
                data["latest_turn"] = latest or {}
                if active:
                    data["app_state"] = "active"
                elif latest and not is_active_turn(latest):
                    data["app_state"] = "terminal"
                    if data["diagnosis"] in {"no_local_run", "running", "idle_no_events", ""}:
                        data["diagnosis"] = str(latest.get("status") or data["diagnosis"])
                else:
                    data["app_state"] = "unknown"
        except SystemExit as exc:
            data["app_server_error"] = str(exc)
            data["app_server_problem"] = problem_kind(str(exc)) or "app_server_error"
            if data["diagnosis"] in {"no_local_run", "running", ""}:
                data["diagnosis"] = data["app_server_problem"]

    recommendation = "observe"
    if data.get("diagnosis") in {"auth_required", "rate_limited", "network", "model_rejected"}:
        recommendation = "fix_upstream_or_retry_later"
    elif data.get("diagnosis") == "command_failed":
        recommendation = "inspect_failed_command"
    elif data.get("diagnosis") == "idle_no_events":
        recommendation = "inspect_then_interrupt_or_continue"
    elif data.get("diagnosis") == "detached_uncontrolled":
        recommendation = "restart_with_wait"
    elif data.get("diagnosis") in {"starting_app_server", "start_failed", "app_server_died"}:
        recommendation = "check_app_server_startup_or_retry_with_no_mcp"
    elif data.get("diagnosis") in {"timeout", "timed_out", "idle_timeout"}:
        recommendation = "inspect_status_then_retry_or_interrupt"
    elif data.get("diagnosis") in TERMINAL_TURN_STATUSES or data.get("app_state") == "terminal":
        recommendation = "collect_diff_or_archive"
    data["recommendation"] = recommendation

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    print(f"thread={thread_id or '-'}")
    print(f"run_id={data.get('run_id') or '-'}")
    if data.get("run_dir"):
        print(f"run_dir={data['run_dir']}")
    print(f"diagnosis={data.get('diagnosis')}")
    print(f"recommendation={recommendation}")
    local = data.get("local_status", {})
    if local:
        print(
            f"local_status={local.get('status')} idle={local.get('idle_seconds', '-')}s last={local.get('last_event_method', '-')} updated={local.get('updated_at', '-')}"
        )
    if data.get("app_state"):
        latest = data.get("latest_turn", {})
        print(f"app_state={data.get('app_state')} latest_turn={latest.get('id', '-')} status={latest.get('status', '-')}")
    if data.get("app_server_error"):
        print(f"app_server_error={compact(data['app_server_error'], 220)}")
    for line in data.get("app_server_stderr_tail", []):
        print(f"stderr_tail={compact(line, 220)}")


def local_dashboard() -> dict[str, Any]:
    index = load_index()
    jobs: list[dict[str, Any]] = []
    if supervisor.JOBS.exists():
        for path in sorted(supervisor.JOBS.iterdir(), reverse=True):
            if path.is_dir() and (path / "job.json").exists():
                job = read_json(path / "job.json", {})
                job["job_dir"] = str(path)
                jobs.append(job)
    targets = []
    for name, target in cli_bridge.load_targets().items():
        try:
            info = cli_bridge.target_info(target)
            status = "ok" if info["command"] == target.expected_command else f"command-mismatch:{info['command']}"
        except SystemExit:
            info = {"pane": target.pane, "command": "", "cwd": "", "title": ""}
            status = "missing"
        targets.append({"name": name, "target": target.__dict__, "status": status, "pane": info})
    return {"threads": index.get("threads", {}), "runs": index.get("runs", {}), "jobs": jobs[:50], "targets": targets}


def cmd_dashboard(args: argparse.Namespace) -> None:
    data = local_dashboard()
    with AppServer(timeout=args.timeout, config_args=app_config_args(args)) as app:
        try:
            threads = app.request(
                "thread/list",
                {"limit": args.limit, "archived": False, "sourceKinds": ["cli", "vscode", "exec", "appServer"]},
            ).get("data", [])
            data["codex_threads"] = threads if args.full else [thread_public_view(t) for t in threads]
        except SystemExit as exc:
            data["codex_error"] = str(exc)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    if args.html:
        out = Path(args.out or str(BUS / "dashboard.html"))
        write_dashboard_html(out, data)
        print(f"dashboard={out}")
        return
    print("Secretary Bus Dashboard")
    print("\nCodex threads:")
    for thread in data.get("codex_threads", [])[: args.limit]:
        print("  " + thread_summary(thread))
    print("\nTracked runs:")
    for run_id, run in list(data.get("runs", {}).items())[-20:]:
        print(f"  {run_id}\t{run.get('status','')}\t{run.get('display_name','')}\t{run.get('thread_id','')}")
    print("\nTmux targets:")
    for target in data.get("targets", []):
        print(f"  {target['name']}\t{target['status']}\t{target['pane'].get('cwd','')}")
    print("\nSupervisor jobs:")
    for job in data.get("jobs", [])[:20]:
        print(f"  {job.get('id')}\t{job.get('target')}\t{job.get('repo')}")


def remove_tree(path: Path) -> None:
    resolved = path.resolve()
    runs_root = RUNS.resolve()
    if resolved != runs_root and runs_root not in resolved.parents:
        raise SystemExit(f"refusing to remove path outside codex run dir: {resolved}")
    if not resolved.exists():
        return
    for child in sorted(resolved.rglob("*"), reverse=True):
        if child.is_file() or child.is_symlink():
            child.unlink()
        elif child.is_dir():
            child.rmdir()
    resolved.rmdir()


def cmd_prune_runs(args: argparse.Namespace) -> None:
    index = load_index()
    runs = index.get("runs", {})
    threads = index.get("threads", {})
    run_candidates: list[str] = []
    for run_id, run in runs.items():
        if args.status and run.get("status") != args.status:
            continue
        if args.name_prefix and not str(run.get("display_name", "")).startswith(args.name_prefix):
            continue
        run_candidates.append(run_id)

    thread_candidates: list[str] = []
    if args.name_prefix:
        for thread_id, thread in threads.items():
            if str(thread.get("display_name", "")).startswith(args.name_prefix):
                thread_candidates.append(thread_id)

    for run_id in run_candidates:
        run = runs.get(run_id, {})
        print(f"prune run: {run_id}\t{run.get('status','')}\t{run.get('display_name','')}\t{run.get('run_dir','')}")
    for thread_id in thread_candidates:
        thread = threads.get(thread_id, {})
        print(f"prune thread-index: {thread_id}\t{thread.get('display_name','')}\t{thread.get('repo','')}")

    total = len(run_candidates) + len(thread_candidates)
    if not args.yes:
        print(f"dry-run only; add --yes to remove {len(run_candidates)} run(s) and {len(thread_candidates)} thread index entry(s)")
        return

    for run_id in run_candidates:
        run = runs.pop(run_id, {})
        run_dir = run.get("run_dir")
        if run_dir:
            remove_tree(Path(run_dir))
        thread_id = run.get("thread_id")
        if thread_id and threads.get(thread_id, {}).get("last_run_id") == run_id:
            threads[thread_id].pop("last_run_id", None)
    for thread_id in thread_candidates:
        threads.pop(thread_id, None)
    save_index(index)
    print(f"removed {len(run_candidates)} run(s) and {len(thread_candidates)} thread index entry(s); total={total}")


def write_dashboard_html(out: Path, data: dict[str, Any]) -> None:
    rows = []
    for thread in data.get("codex_threads", []):
        rows.append(
            "<tr>"
            f"<td>{html.escape(thread.get('id',''))}</td>"
            f"<td>{html.escape(thread.get('threadName') or thread.get('name') or '')}</td>"
            f"<td>{html.escape(thread.get('cwd') or '')}</td>"
            f"<td>{html.escape(compact(thread.get('preview') or '', 160))}</td>"
            "</tr>"
        )
    body = "\n".join(rows) or "<tr><td colspan='4'>No active Codex threads</td></tr>"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        """<!doctype html>
<meta charset="utf-8">
<title>Secretary Bus Dashboard</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:24px;color:#1f2937;background:#f8fafc}
h1{font-size:24px;margin:0 0 18px}
table{border-collapse:collapse;width:100%;background:white;border:1px solid #e5e7eb}
th,td{border-bottom:1px solid #e5e7eb;text-align:left;padding:8px 10px;font-size:13px;vertical-align:top}
th{background:#f1f5f9}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
</style>
<h1>Secretary Bus Dashboard</h1>
<table>
<thead><tr><th>Thread</th><th>Name</th><th>CWD</th><th>Preview</th></tr></thead>
<tbody>
""" + body + """
</tbody>
</table>
""",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Secretary Bus Codex controller: app-server threads, naming, status, wait/watch, steering, interrupts, diff, dashboard."
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--no-mcp", action="store_true", help="Start the app-server with configured MCP servers disabled for isolated worker runs")
    parser.add_argument("--codex-config", action="append", default=[], metavar="KEY=VALUE", help="Forward a Codex -c config override to the app-server; repeatable")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="List Codex threads")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--archived", action="store_true")
    p.add_argument("--cwd", default="")
    p.add_argument("--search", default="")
    p.add_argument("--all-sources", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("read", help="Read a Codex thread")
    p.add_argument("thread_id")
    p.add_argument("--turns", action="store_true")
    p.add_argument("--tail", type=int, default=3)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_read)

    p = sub.add_parser("status", help="Show thread and active-turn status")
    p.add_argument("thread_id")
    p.add_argument("--turns", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("name", help="Set a Chinese display name for a Codex thread")
    p.add_argument("thread_id")
    p.add_argument("name")
    p.set_defaults(fn=cmd_name)

    p = sub.add_parser("archive", help="Archive or unarchive a Codex thread")
    p.add_argument("thread_id")
    p.add_argument("--unarchive", action="store_true")
    p.set_defaults(fn=cmd_archive)

    p = sub.add_parser(
        "start",
        help="Start a Codex app-server worker thread and wait for its turn (--wait is required)",
        description=(
            "Start a Codex app-server worker and wait for its turn. Each concurrent worker gets its own "
            f"CODEX_HOME slot ({CODEX_HOMES_ROOT}/{WORKER_HOME_SLUG}-1..N, N from SECRETARY_BUS_CODEX_WORKER_SLOTS, "
            "default 6). A slot only isolates user-level state (sessions, state db, config.toml); project files, "
            "the project's AGENTS.md and project rules are still read from --repo, which is pre-trusted in the slot."
        ),
    )
    p.add_argument("--name", default="")
    p.add_argument("--repo", default="")
    p.add_argument("--task", default="")
    p.add_argument("--task-file", default="")
    add_agent_model_arguments(p)
    p.add_argument("--approval", default="never")
    p.add_argument("--sandbox", default="workspace-write")
    p.add_argument("--wait", action="store_true",
                   help="Required: stream the turn until it finishes; this command owns the app-server, so it cannot detach")
    p.add_argument("--wait-timeout", type=float, default=600,
                   help="Seconds to wait; on expiry the turn is interrupted, recorded as timed_out, and the command exits nonzero")
    p.add_argument("--shared-home", action="store_true",
                   help=f"Use the caller's CODEX_HOME instead of an isolated worker slot ({CODEX_HOMES_ROOT}/{WORKER_HOME_SLUG}-N)")
    p.add_argument("--wait-slot", type=float, default=0, metavar="SECONDS",
                   help="When every worker slot is busy, wait up to this many seconds for one instead of failing")
    p.add_argument("--idle-timeout", type=float, default=120, help="Warn when no app-server events arrive for this many seconds; 0 disables")
    p.add_argument("--progress-interval", type=float, default=30, help="Print wait progress every N seconds; 0 disables")
    p.add_argument("--fail-on-idle", action="store_true", help="Exit nonzero when idle-timeout is reached")
    p.add_argument("--fail-on-error", action="store_true", help="Exit nonzero when a waited turn ends without completed status (failed commands inside a completed turn are warnings only)")
    p.set_defaults(fn=cmd_start)

    p = sub.add_parser("steer", help="Steer an active Codex turn")
    p.add_argument("thread_id")
    p.add_argument("--turn-id", default="")
    p.add_argument("--task", default="")
    p.add_argument("--task-file", default="")
    p.set_defaults(fn=cmd_steer)

    p = sub.add_parser("interrupt", help="Interrupt an active Codex turn")
    p.add_argument("thread_id")
    p.add_argument("--turn-id", default="")
    p.set_defaults(fn=cmd_interrupt)

    p = sub.add_parser("wait", help="Poll until a Codex turn reaches a terminal status")
    p.add_argument("thread_id")
    p.add_argument("--turn-id", default="")
    p.add_argument("--interval", type=float, default=5)
    p.add_argument("--wait-timeout", type=float, default=600)
    p.add_argument("--items", type=int, default=100)
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_wait)

    p = sub.add_parser("watch", help="Poll live thread status repeatedly")
    p.add_argument("thread_id")
    p.add_argument("--turns", type=int, default=10)
    p.add_argument("--interval", type=float, default=5)
    p.add_argument("--duration", type=float, default=0)
    p.add_argument("--once", action="store_true")
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("diff", help="Show latest saved Codex turn diff or repo diff")
    p.add_argument("thread_id")
    p.add_argument("--run-id", default="")
    p.add_argument("--repo", default="")
    p.add_argument("--out-dir", default="")
    p.add_argument("--save", action="store_true")
    p.set_defaults(fn=cmd_diff)

    p = sub.add_parser("doctor", help="Diagnose a Codex worker wait/run state")
    p.add_argument("thread_id", nargs="?")
    p.add_argument("--run-id", default="", help="Inspect a saved Secretary Bus run id")
    p.add_argument("--run-dir", default="", help="Inspect a run directory directly")
    p.add_argument("--turns", type=int, default=20)
    p.add_argument("--local", action="store_true", help="Only inspect local Secretary Bus run files")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("dashboard", help="Show Secretary Bus dashboard")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.add_argument("--html", action="store_true")
    p.add_argument("--full", action="store_true", help="Include raw app-server thread objects; default JSON uses compact dashboard views")
    p.add_argument("--out", default="")
    p.set_defaults(fn=cmd_dashboard)

    p = sub.add_parser("prune-runs", help="Remove saved Codex run records and artifacts")
    p.add_argument("--status", default="")
    p.add_argument("--name-prefix", default="")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_prune_runs)

    args = parser.parse_args()
    args.fn(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
