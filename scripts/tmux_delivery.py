#!/usr/bin/env python3
"""One tmux text-delivery primitive shared by Cards and Secretary Bus."""

from __future__ import annotations

import os
import secrets
import subprocess
import threading
import time
from collections.abc import Callable


CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]
InputRunner = Callable[[list[str], str], subprocess.CompletedProcess[str]]


def _command_runner(timeout: float) -> CommandRunner:
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", *args],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    return run


def _input_runner(timeout: float) -> InputRunner:
    def run(args: list[str], text: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", *args],
            input=text,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    return run


def _require_ok(result: subprocess.CompletedProcess[str], message: str) -> None:
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or message)


def paste_and_submit(
    pane_id: str,
    text: str,
    enter: bool,
    *,
    timeout: float = 10.0,
    submit_delay: float = 0.18,
    run_command: CommandRunner | None = None,
    run_input: InputRunner | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Bracket-paste text, end the paste, then submit outside the paste burst."""
    pane_id = str(pane_id or "").strip()
    if not pane_id:
        raise ValueError("pane id is required")
    command = run_command or _command_runner(timeout)
    input_command = run_input or _input_runner(timeout)

    mode = command(["display-message", "-p", "-t", pane_id, "#{pane_in_mode}"])
    _require_ok(mode, "tmux pane mode check failed")
    if mode.stdout.strip() == "1":
        _require_ok(command(["send-keys", "-t", pane_id, "-X", "cancel"]), "tmux copy-mode cancel failed")

    buffer_name = f"agent-bus-input-{os.getpid()}-{threading.get_ident()}-{secrets.token_hex(4)}"
    loaded = input_command(["load-buffer", "-b", buffer_name, "-"], text)
    _require_ok(loaded, "tmux load-buffer failed")
    try:
        pasted = command(["paste-buffer", "-p", "-t", pane_id, "-b", buffer_name])
        _require_ok(pasted, "tmux paste-buffer failed")
    finally:
        command(["delete-buffer", "-b", buffer_name])
    if enter:
        if submit_delay > 0:
            sleep(min(float(submit_delay), 1.0))
        _require_ok(command(["send-keys", "-t", pane_id, "C-m"]), "tmux send submit failed")
