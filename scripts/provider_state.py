#!/usr/bin/env python3
"""Compact, identity-safe state for one registered Claude/Codex tmux target.

Tmux runtime identity is always checked through ``cli_bridge``. Provider
history is called exact only when it is joined by process identity:

* Claude: ``claude agents --json`` record PID belongs to the pane process tree.
* Codex: the pane process tree has the rollout JSONL open as a real file
  descriptor; its session metadata supplies the thread id.

When those joins are unavailable, this module reports a bounded live-pane
inference and deliberately leaves the session id empty. It never selects a
history file by cwd, filename recency, or mtime.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import claude_sessions  # noqa: E402
import claude_subagents  # noqa: E402
import cli_bridge  # noqa: E402
import codex_subagents  # noqa: E402
import pane_detectors  # noqa: E402


BUS = cli_bridge.BUS
CODEX_RUN_INDEX = BUS / "codex-runs.json"
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
COMMAND_TIMEOUT = float(os.environ.get("SECRETARY_BUS_PROVIDER_TIMEOUT", "4"))
MAX_TAIL_BYTES = int(os.environ.get("SECRETARY_BUS_PROVIDER_TAIL_BYTES", str(128 * 1024)))
MAX_HEAD_BYTES = int(os.environ.get("SECRETARY_BUS_PROVIDER_HEAD_BYTES", str(64 * 1024)))
DEFAULT_MAX_CHARS = int(os.environ.get("SECRETARY_BUS_PROVIDER_MAX_CHARS", "1200"))
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")

PERMISSION_PATTERNS = (
    "do you want to proceed",
    "would you like to run",
    "allow this command",
    "allow command",
    "yes, and don't ask again",
    "press enter to confirm",
    "esc to cancel",
    "permission required",
    "approval required",
)
BUSY_PATTERNS = (
    "esc to interrupt",
    "ctrl+c to interrupt",
    "working…",
    "working...",
    "thinking…",
    "thinking...",
    "running…",
    "running...",
)
IDLE_PATTERNS = (
    "› ask codex",
    "› describe a task",
    "❯ how can claude",
    "❯ try ",
)


def compact_text(text: str, limit: int = DEFAULT_MAX_CHARS) -> str:
    clean = ANSI_RE.sub("", text or "").replace("\x00", "")
    clean = re.sub(r"[ \t]+", " ", clean)
    clean = re.sub(r"\s*\n\s*", " | ", clean).strip(" |")
    limit = max(40, int(limit))
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def process_tree_pids(root_pid: int, limit: int = 64) -> list[int]:
    """Return an exact Linux descendant set anchored at the foreground PID."""
    if root_pid <= 0:
        return []
    found: list[int] = []
    pending = [root_pid]
    seen: set[int] = set()
    while pending and len(found) < limit:
        pid = pending.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        if not Path(f"/proc/{pid}").exists():
            continue
        found.append(pid)
        try:
            raw = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="utf-8").strip()
        except (FileNotFoundError, PermissionError, OSError):
            raw = ""
        for value in raw.split():
            try:
                child = int(value)
            except ValueError:
                continue
            if child not in seen:
                pending.append(child)
    return found


def process_cmdline(pids: list[int]) -> str:
    values: list[str] = []
    for pid in pids:
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ")
            values.append(raw.decode("utf-8", errors="replace"))
        except (FileNotFoundError, PermissionError, OSError):
            continue
    return " ".join(values).lower()


def _provider_of(evidence: str) -> str:
    if re.search(r"(^|[ /])claude(?:\s|$)", evidence):
        return "claude"
    if re.search(r"(^|[ /])codex(?:\s|$)", evidence) or "codex-cli" in evidence:
        return "codex"
    return "unknown"


def detect_provider(target: cli_bridge.Target, info: dict[str, Any], pids: list[int]) -> str:
    # pids are breadth-first from the pane's foreground process: the first AI
    # process met is the pane's own; a `claude -p` a Codex tool spawned sits deeper.
    for pid in pids:
        provider = _provider_of(process_cmdline([pid]))
        if provider != "unknown":
            return provider
    return _provider_of(" ".join([str(target.expected_command), str(info.get("command") or "")]).lower())


def capture_pane(pane_id: str, lines: int = 80) -> str:
    cp = cli_bridge.tmux(
        "capture-pane", "-p", "-t", pane_id, "-S", f"-{max(10, min(int(lines), 250))}", check=False
    )
    if cp.returncode != 0:
        raise SystemExit(cp.stderr.strip() or f"capture-pane failed: {pane_id}")
    return cp.stdout[-65536:]


def claude_agents_json(cwd: str = "") -> tuple[list[dict[str, Any]], str]:
    """Claude's own session list (shared with the Cards dashboard, see claude_sessions)."""
    return claude_sessions.agent_records(cwd)


def claude_transcript_path(session_id: str) -> Path | None:
    """Find a transcript only by the exact authoritative session id."""
    if not SESSION_ID_RE.fullmatch(session_id or ""):
        return None
    root = CLAUDE_HOME / "projects"
    candidates = [path for path in root.glob(f"*/{session_id}.jsonl") if path.is_file()]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _bounded_lines(path: Path) -> tuple[list[str], list[str]]:
    """Read bounded head and tail records without loading a full history."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            head = handle.read(MAX_HEAD_BYTES)
            if size <= MAX_TAIL_BYTES:
                handle.seek(0)
            else:
                handle.seek(max(0, size - MAX_TAIL_BYTES))
                handle.readline()  # discard a possibly partial first record
            tail = handle.read(MAX_TAIL_BYTES)
    except (FileNotFoundError, PermissionError, OSError):
        return [], []
    head_lines = head.decode("utf-8", errors="replace").splitlines()
    tail_lines = tail.decode("utf-8", errors="replace").splitlines()
    return head_lines[:300], tail_lines[-600:]


LIFECYCLE_SCAN_BYTES = int(os.environ.get("SECRETARY_BUS_LIFECYCLE_SCAN_BYTES", str(32 * 1024 * 1024)))
_LIFECYCLE_MARKERS = (b"task_started", b"task_complete", b"turn_started", b"turn_complete", b"turn_aborted")
_BUSY_EVENTS = {"task_started", "turn_started"}
_IDLE_EVENTS = {"task_complete", "turn_complete", "turn_completed", "turn_aborted"}


def last_lifecycle_event(path: Path, limit_bytes: int = LIFECYCLE_SCAN_BYTES) -> str:
    """Most recent turn start/end event, scanning backwards past the tail window.

    A long Codex turn writes megabytes of tool output after its task_started;
    a fixed tail window then holds no turn boundary at all and the state
    degrades to guessing from the screen (observed: panes showing "Working
    (5m…)" reported idle).  Only small event_msg lines are parsed.
    """
    try:
        size = path.stat().st_size
        handle = path.open("rb")
    except OSError:
        return ""
    with handle:
        position, carry, scanned = size, b"", 0
        while position > 0 and scanned < limit_bytes:
            step = min(1024 * 1024, position)
            position -= step
            scanned += step
            handle.seek(position)
            chunk = handle.read(step) + carry
            lines = chunk.split(b"\n")
            carry = lines[0] if position > 0 else b""
            for raw in reversed(lines[1:] if position > 0 else lines):
                if b'"event_msg"' not in raw or not any(marker in raw for marker in _LIFECYCLE_MARKERS):
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                payload = record.get("payload") if isinstance(record, dict) else None
                event = str(payload.get("type") or "") if isinstance(payload, dict) else ""
                if record.get("type") == "event_msg" and event in _BUSY_EVENTS | _IDLE_EVENTS:
                    return event
    return ""


def _json_records(lines: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_content_text(item) for item in value]
        return "".join(part for part in parts if part)
    if not isinstance(value, dict):
        return ""
    value_type = str(value.get("type") or "")
    if value_type in {"text", "output_text", "input_text"} and isinstance(value.get("text"), str):
        return str(value["text"])
    for key in ("content", "message", "text"):
        text = _content_text(value.get(key))
        if text:
            return text
    return ""


def claude_last_assistant(path: Path, max_chars: int) -> str:
    _head, tail = _bounded_lines(path)
    latest = ""
    for item in _json_records(tail):
        message = item.get("message")
        if item.get("type") == "assistant" or (isinstance(message, dict) and message.get("role") == "assistant"):
            text = _content_text(message if message is not None else item)
            if text:
                latest = text
    return compact_text(latest, max_chars)


def rollout_paths_for_pids(pids: list[int]) -> list[Path]:
    """Return rollout files actually open by the exact pane process tree."""
    paths: dict[str, Path] = {}
    for pid in pids:
        fd_root = Path(f"/proc/{pid}/fd")
        try:
            fds = list(fd_root.iterdir())
        except (FileNotFoundError, PermissionError, OSError):
            continue
        for fd in fds[:512]:
            try:
                raw = os.readlink(fd)
            except (FileNotFoundError, PermissionError, OSError):
                continue
            raw = raw.removesuffix(" (deleted)")
            if not raw.endswith(".jsonl"):
                continue
            if "rollout-" not in Path(raw).name and "/sessions/" not in raw:
                continue
            path = Path(raw)
            if path.is_file():
                paths[str(path)] = path
    return [paths[key] for key in sorted(paths)]


def rollout_snapshot(path: Path, max_chars: int) -> dict[str, Any]:
    head_lines, tail_lines = _bounded_lines(path)
    head_records = _json_records(head_lines)
    tail_records = _json_records(tail_lines)
    thread_id = ""
    source_kind = ""
    parent_thread_id = ""
    for item in head_records:
        if item.get("type") != "session_meta":
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        thread_id = str(payload.get("id") or payload.get("thread_id") or "")
        raw_source = payload.get("source")
        if isinstance(raw_source, str):
            source_kind = raw_source
        elif isinstance(raw_source, dict):
            subagent = raw_source.get("subagent") if isinstance(raw_source.get("subagent"), dict) else {}
            spawn = subagent.get("thread_spawn") if isinstance(subagent.get("thread_spawn"), dict) else {}
            parent_thread_id = str(spawn.get("parent_thread_id") or "")
            if parent_thread_id:
                source_kind = "subagent"
        if thread_id:
            break

    state = "unknown"
    last_event = ""
    assistant = ""
    for item in tail_records:
        record_type = str(item.get("type") or "")
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        payload_type = str(payload.get("type") or "")
        if payload_type in {"task_started", "turn_started"} or record_type == "turn_context":
            state, last_event = "busy", payload_type or record_type
        elif payload_type in {"task_complete", "turn_complete", "turn_completed", "turn_aborted"}:
            state, last_event = "idle", payload_type
        if record_type == "event_msg" and payload_type == "agent_message":
            text = _content_text(payload.get("message"))
            if text:
                assistant = text
        elif record_type == "response_item" and str(payload.get("role") or "") == "assistant":
            text = _content_text(payload.get("content") or payload)
            if text:
                assistant = text
    if state == "unknown":
        last_event = last_lifecycle_event(path)
        if last_event:
            state = "busy" if last_event in _BUSY_EVENTS else "idle"
    return {
        "thread_id": thread_id,
        "source_kind": source_kind,
        "parent_thread_id": parent_thread_id,
        "state": state,
        "last_event": last_event,
        "last_assistant": compact_text(assistant, max_chars),
        "path_name": path.name,
    }


def select_exact_rollout(snapshots: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Select one exact Codex root when every other open rollout is its descendant."""
    exact = [item for item in snapshots if item.get("thread_id")]
    if len(exact) == 1:
        only = exact[0]
        # A lone child FD is not proof of the tmux window's root thread. The
        # root rollout can be momentarily closed while a subagent keeps its own
        # file open, so claiming the child would splice the wrong history into
        # the leader view.
        if only.get("source_kind") == "subagent" or only.get("parent_thread_id"):
            return None
        return only
    by_id = {str(item["thread_id"]): item for item in exact}
    roots = [item for item in exact if item.get("source_kind") == "cli"]
    if len(roots) != 1:
        return None
    root = roots[0]
    root_id = str(root["thread_id"])
    for item in exact:
        current_id = str(item["thread_id"])
        if current_id == root_id:
            continue
        seen: set[str] = set()
        while current_id and current_id not in seen:
            seen.add(current_id)
            current = by_id.get(current_id)
            if current is None:
                return None
            parent = str(current.get("parent_thread_id") or "")
            if parent == root_id:
                break
            current_id = parent
        else:
            return None
    return root


def _read_json(path: Path) -> dict[str, Any]:
    try:
        item = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, PermissionError, OSError):
        return {}
    return item if isinstance(item, dict) else {}


def _run_local_state(run: dict[str, Any]) -> dict[str, Any]:
    status_path = Path(str(run.get("status_file") or "")) if run.get("status_file") else None
    if status_path is None and run.get("run_dir"):
        status_path = Path(str(run["run_dir"])) / "status.json"
    return _read_json(status_path) if status_path else {}


def _controller_pid(run: dict[str, Any], local: dict[str, Any], pids: set[int]) -> int:
    for source in (local, run):
        for key in ("daemon_pid", "controller_pid", "app_server_pid", "pid"):
            try:
                pid = int(source.get(key) or 0)
            except (TypeError, ValueError):
                continue
            if pid <= 0 or pid not in pids:
                continue
            token = str(
                source.get(f"{key}_start_time")
                or source.get(f"{key}_start_token")
                or (source.get("pid_start_time") if key == "pid" else "")
                or ""
            )
            if token and cli_bridge.process_start_time(pid) != token:
                continue
            return pid
    return 0


def codex_exact_run_state(thread_id: str, *, pids: list[int]) -> dict[str, Any]:
    """Join run state only when its controller belongs to this pane tree."""
    if not thread_id or not pids:
        return {}
    index = _read_json(CODEX_RUN_INDEX)
    thread = (index.get("threads") or {}).get(thread_id)
    if not isinstance(thread, dict):
        return {}
    pid_set = {int(value) for value in pids if int(value) > 0}
    candidates: list[tuple[str, str, dict[str, Any], dict[str, Any], int]] = []
    for key, value in (index.get("runs") or {}).items():
        if not isinstance(value, dict) or str(value.get("thread_id") or "") != thread_id:
            continue
        local = _run_local_state(value)
        owner_pid = _controller_pid(value, local, pid_set)
        if not owner_pid:
            continue
        updated_at = str(local.get("updated_at") or value.get("updated_at") or "")
        candidates.append((updated_at, str(key), value, local, owner_pid))
    if not candidates:
        return {}
    candidates.sort(key=lambda item: (item[0], item[1]))
    _updated_at, run_id, run, local, owner_pid = candidates[-1]
    status = str(local.get("status") or local.get("diagnosis") or run.get("status") or "")
    turn_id = str(local.get("turn_id") or run.get("turn_id") or "")
    reply = ""
    reply_path = Path(str(local.get("reply_file") or run.get("reply_file") or "")) if (
        local.get("reply_file") or run.get("reply_file")
    ) else None
    if reply_path:
        try:
            with reply_path.open("rb") as handle:
                size = reply_path.stat().st_size
                handle.seek(max(0, size - MAX_TAIL_BYTES))
                reply = handle.read(MAX_TAIL_BYTES).decode("utf-8", errors="replace")
        except (FileNotFoundError, PermissionError, OSError):
            reply = ""
    return {
        "run_id": run_id,
        "turn_id": turn_id,
        "status": status,
        "reply": reply,
        "controller_pid": owner_pid,
    }


def state_from_status(status: str) -> str:
    normalized = str(status or "").strip().lower().replace("-", "_")
    if normalized in {"busy", "active", "running", "starting", "working", "leased", "queued"}:
        return "busy"
    # `claude agents --json` reports an open dialog (permission prompt, picker,
    # settings question) as status "waiting" with waitingFor "dialog open".
    if normalized in {"needs_input", "waiting", "waiting_user", "waiting_for_approval", "approval_required", "blocked"}:
        return "needs_input"
    if normalized in {
        "idle",
        "completed",
        "complete",
        "failed",
        "cancelled",
        "canceled",
        "interrupted",
        "terminal",
    }:
        return "idle"
    return "unknown"


def live_tail_state(text: str) -> tuple[str, str, list[dict[str, str]]]:
    clean = ANSI_RE.sub("", text or "")
    # A real permission prompt/picker replaces the provider's input box.  While
    # the normal input box is drawn, prompt-like words above it are conversation
    # (an answer quoting "Do you want to proceed?", a numbered list), not a
    # question waiting for a key press.  The subagent panel Claude draws below
    # the footer is chrome and must not push status lines out of the window.
    raw_lines = pane_detectors.strip_agent_panel(clean.splitlines())
    split = pane_detectors.split_screen(raw_lines)
    prompt_can_be_live = not split.has_input_region
    lines = [line.strip().lower() for line in raw_lines if line.strip()][-16:]
    hits: list[tuple[int, int, str, str, str]] = []
    for index, line in enumerate(lines):
        for pattern in PERMISSION_PATTERNS:
            if prompt_can_be_live and pattern in line:
                hits.append((index, 3, "needs_input", "permission_prompt", pattern))
        for pattern in BUSY_PATTERNS:
            if pattern in line:
                hits.append((index, 2, "busy", "busy_marker", pattern))
        idle_detail = next((pattern for pattern in IDLE_PATTERNS if pattern in line), "")
        if idle_detail or re.match(r"^[›❯]\s", line):
            hits.append((index, 1, "idle", "input_prompt", idle_detail or "interactive prompt visible"))
    if hits:
        # A busy marker above the input box outranks the input box itself (the
        # box is always drawn below "Working…"); position only breaks ties.
        _index, _priority, value, kind, detail = max(hits, key=lambda hit: (hit[1], hit[0]))
        confidence = "high" if value == "needs_input" else "medium"
        return value, confidence, [{"kind": kind, "source": "tmux_live_tail", "detail": detail}]
    return "unknown", "low", [
        {"kind": "no_stable_marker", "source": "tmux_live_tail", "detail": "bounded pane tail only"}
    ]


def snapshot_target(name: str, max_chars: int = DEFAULT_MAX_CHARS) -> dict[str, Any]:
    targets = cli_bridge.load_targets()
    if name not in targets:
        raise SystemExit(f"unknown registered target: {name}")
    target = targets[name]
    info = cli_bridge.target_info(target)
    if str(info.get("command") or "") != target.expected_command:
        raise SystemExit(
            f"target command mismatch: {name} expected={target.expected_command} actual={info.get('command')}"
        )
    pids = process_tree_pids(int(info.get("foreground_pid") or 0))
    provider = detect_provider(target, info, pids)
    pane_tail = capture_pane(str(info["pane_id"]))
    live_state, live_confidence, live_evidence = live_tail_state(pane_tail)
    state = "unknown"
    confidence = "low"
    source = "tmux_live_tail"
    evidence: list[dict[str, str]] = []
    session = {"id": "", "source": "", "exact": False}
    last_assistant = ""

    if provider == "claude":
        agents, agents_error = claude_agents_json(str(info.get("cwd") or ""))
        pid_set = set(pids)
        exact_matches = [item for item in agents if int(item.get("pid") or 0) in pid_set]
        if len(exact_matches) == 1:
            record = exact_matches[0]
            state = state_from_status(str(record.get("status") or ""))
            confidence = "authoritative" if state != "unknown" else "high"
            source = "claude_agents"
            session_id = str(record.get("sessionId") or record.get("session_id") or "")
            if session_id:
                session = {"id": session_id, "source": "claude_agents", "exact": True}
                transcript = claude_transcript_path(session_id)
                if transcript:
                    last_assistant = claude_last_assistant(transcript, max_chars)
            evidence.append(
                {
                    "kind": "provider_status",
                    "source": "claude_agents",
                    "detail": f"pid={record.get('pid')} status={record.get('status', '')}"
                    + (f" waitingFor={record.get('waitingFor')}" if record.get("waitingFor") else ""),
                }
            )
        elif len(exact_matches) > 1:
            evidence.append(
                {"kind": "ambiguous_provider_identity", "source": "claude_agents", "detail": "multiple PID matches"}
            )
        elif agents_error:
            evidence.append({"kind": "provider_unavailable", "source": "claude_agents", "detail": agents_error})

    elif provider == "codex":
        rollouts = rollout_paths_for_pids(pids)
        snapshots = [rollout_snapshot(path, max_chars) for path in rollouts]
        exact = [item for item in snapshots if item.get("thread_id")]
        selected = select_exact_rollout(snapshots)
        if selected is not None:
            rollout = selected
            session = {"id": rollout["thread_id"], "source": "codex_rollout_fd", "exact": True}
            state = str(rollout.get("state") or "unknown")
            confidence = "high" if state != "unknown" else "medium"
            source = "codex_rollout_fd"
            last_assistant = str(rollout.get("last_assistant") or "")
            evidence.append(
                {
                    "kind": "rollout_fd",
                    "source": "codex_rollout_fd",
                    "detail": f"{rollout.get('path_name')} event={rollout.get('last_event') or 'unknown'}",
                }
            )
            run = codex_exact_run_state(str(rollout["thread_id"]), pids=pids)
            run_state = state_from_status(str(run.get("status") or ""))
            if run_state != "unknown":
                state, confidence, source = run_state, "authoritative", "codex_exact_run"
                evidence.append(
                    {
                        "kind": "exact_run_status",
                        "source": "codex_exact_run",
                        "detail": f"run={run.get('run_id', '')} turn={run.get('turn_id', '')} status={run.get('status', '')}",
                    }
                )
            if run.get("reply"):
                last_assistant = compact_text(str(run["reply"]), max_chars)
            # Codex runs spawned agents in-process; the root turn can finish while
            # they keep working.  Their open rollouts say so; the work is not done.
            busy_children = [
                item for item in snapshots
                if item.get("source_kind") == "subagent" and item.get("state") == "busy"
            ]
            if state == "idle" and busy_children:
                state, source = "busy", "codex_subagent_rollout_fd"
                evidence.append(
                    {
                        "kind": "subagents_busy",
                        "source": "codex_rollout_fd",
                        "detail": f"{len(busy_children)} subagent rollout(s) mid-turn",
                    }
                )
        elif len(exact) > 1:
            evidence.append(
                {
                    "kind": "ambiguous_rollout_fd",
                    "source": "codex_rollout_fd",
                    "detail": f"{len(exact)} open rollout sessions",
                }
            )

    # A visible approval/input prompt is the most actionable live fact and
    # overrides a provider's generic "busy" status without changing identity —
    # except for an exact Claude record: Claude reports open dialogs itself
    # (status "waiting"), so screen words cannot override it.
    if live_state == "needs_input" and source != "claude_agents":
        state, confidence, source = live_state, live_confidence, "tmux_live_tail"
        evidence = [*live_evidence, *evidence]
    elif state == "unknown":
        state, confidence, source = live_state, live_confidence, "tmux_live_tail"
        evidence = [*evidence, *live_evidence]

    subagents: list[dict[str, Any]] = []
    if provider == "claude" and session.get("exact"):
        transcript = claude_transcript_path(str(session.get("id") or ""))
        if transcript:
            states = claude_subagents.load_subagents(transcript, frozenset(), claude_subagents.parent_notifications(transcript))
            subagents = [item.to_dict() for item in claude_subagents.live_batch(states)]
    elif provider == "codex":
        now_states = [codex_subagents.child_state(path) for path in rollout_paths_for_pids(pids)]
        subagents = [item.to_dict() for item in claude_subagents.live_batch([st for st in now_states if st])]
    if subagents:
        running = sum(1 for item in subagents if item.get("status") == "running")
        evidence.append({"kind": "subagents", "source": f"{provider}_subagent_logs",
                         "detail": f"{running} running / {len(subagents)} in current batch"})

    runtime = {
        key: info.get(key)
        for key in (
            "pane",
            "pane_id",
            "pane_pid",
            "pane_start_time",
            "foreground_pid",
            "foreground_start_time",
            "command",
            "cwd",
        )
    }
    return {
        "target": name,
        "provider": provider,
        "runtime": runtime,
        "state": {
            "value": state,
            "confidence": confidence,
            "source": source,
            "evidence": evidence[:6],
        },
        "session": session,
        "subagents": [
            {key: item.get(key) for key in ("type", "description", "status", "activity", "tool_calls", "started_at", "updated_at")}
            for item in subagents
        ],
        "fidelity": "exact" if session["exact"] else "inferred",
        "last_assistant": compact_text(last_assistant, max_chars),
        "last_output": compact_text(pane_tail, max_chars),
        "bounds": {"max_chars": int(max_chars), "pane_lines": 80, "history_tail_bytes": MAX_TAIL_BYTES},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compact exact/inferred provider state for a registered target")
    parser.add_argument("target")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    payload = snapshot_target(args.target, max_chars=max(80, min(args.max_chars, 4000)))
    if args.pretty:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
