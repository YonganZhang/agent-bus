#!/usr/bin/env python3
"""Claude Code's own session status: ``claude agents --json``.

One call lists every interactive Claude session on this machine with ``pid``,
``sessionId``, ``cwd`` and ``status`` (``idle`` / ``busy`` / ``waiting``;
``waiting`` carries ``waitingFor`` such as ``"dialog open"``).  Verified on
this host: ``busy`` also covers a session whose main turn has ended while
background subagents still run.  This is the provider's own truth, so callers
use it before guessing from the screen.

Shared by the Cards dashboard and ``provider_state``.  A session is bound to a
tmux pane by pid inside the pane's process tree; a session id is only trusted
when a single record carries it (one session resumed in two windows shows up as
two pids with the same ``sessionId``).
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from typing import Any

COMMAND_TIMEOUT = float(os.environ.get("SECRETARY_BUS_PROVIDER_TIMEOUT", "4"))
CACHE_TTL = 3.0

_CACHE: dict[str, tuple[float, list[dict[str, Any]], str]] = {}
_LOCK = threading.Lock()


def _decode(stdout: str) -> tuple[list[dict[str, Any]], str]:
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        # Claude may truncate a long JSON array in the middle of its final
        # object.  Keep only fully decoded leading records.
        raw = stdout.lstrip()
        decoder = json.JSONDecoder()
        recovered: list[dict[str, Any]] = []
        if raw.startswith("["):
            index = 1
            while index < len(raw):
                while index < len(raw) and raw[index] in " \t\r\n,":
                    index += 1
                if index >= len(raw) or raw[index] == "]":
                    break
                try:
                    item, index = decoder.raw_decode(raw, index)
                except json.JSONDecodeError:
                    break
                if isinstance(item, dict):
                    recovered.append(item)
        if recovered:
            return recovered, f"truncated claude agents JSON; recovered {len(recovered)} complete records: {exc}"
        return [], f"invalid claude agents JSON: {exc}"
    if not isinstance(parsed, list):
        return [], "claude agents JSON root is not a list"
    return [item for item in parsed if isinstance(item, dict)], ""


def agent_records(cwd: str = "", *, ttl: float = CACHE_TTL) -> tuple[list[dict[str, Any]], str]:
    """``(records, error)``; cached per ``cwd`` for ``ttl`` seconds."""
    with _LOCK:
        now = time.time()
        cached = _CACHE.get(cwd)
        if cached and now - cached[0] < ttl:
            return list(cached[1]), cached[2]
        command = ["claude", "agents"]
        if cwd:
            command.extend(["--cwd", cwd])
        command.append("--json")
        try:
            cp = subprocess.run(
                command, text=True, capture_output=True, timeout=COMMAND_TIMEOUT,
                stdin=subprocess.DEVNULL, check=False, cwd="/",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            records, error = [], str(exc)[:240]
        else:
            if cp.returncode != 0:
                records, error = [], (cp.stderr or cp.stdout or f"exit={cp.returncode}")[:240]
            else:
                records, error = _decode(cp.stdout)
        _CACHE[cwd] = (now, records, error)
        return list(records), error


def record_for(records: list[dict[str, Any]], pids: set[int] | set[str], session_id: str = "") -> dict[str, Any] | None:
    """The record of the session running inside ``pids`` (exact), else the only
    record carrying ``session_id``."""
    wanted = {str(pid) for pid in pids}
    matches = [record for record in records if str(record.get("pid") or "") in wanted]
    if len(matches) == 1:
        return matches[0]
    if session_id and not matches:
        by_id = [record for record in records if str(record.get("sessionId") or "") == session_id]
        if len(by_id) == 1:
            return by_id[0]
    return None
