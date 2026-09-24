#!/usr/bin/env python3
"""Durable Secretary Bus operator preferences.

The defaults are deliberately independent of environment variables: every
temporary ``AGENT_BUS_DIR`` starts non-trusted until its own config says
otherwise, while the real bus can opt in once and retain that choice.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any, Iterator

import cli_bridge


CONFIG_FILE = cli_bridge.BUS / "config.json"
LOCK_FILE = CONFIG_FILE.with_suffix(".json.lock")
# ``auto_approve_permissions``: opt-in standing authorization that permission-type
# dialogs on AI panes driven by the bus are answered by the policy in dialogs.py;
# questions about the work always go to the user. Off by default; the
# AGENT_BUS_AUTO_APPROVE=1|0 environment variable overrides it (cli_bridge).
DEFAULT_CONFIG: dict[str, Any] = {"version": 1, "trusted_owner": False, "auto_approve_permissions": False}
BOOL_KEYS = ("trusted_owner", "auto_approve_permissions")


@contextlib.contextmanager
def locked() -> Iterator[None]:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_config() -> dict[str, Any]:
    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict(DEFAULT_CONFIG)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"corrupt Secretary Bus config; refusing to guess: {CONFIG_FILE}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SystemExit(f"invalid Secretary Bus config: {CONFIG_FILE}")
    for key in BOOL_KEYS:
        if key in raw and not isinstance(raw[key], bool):
            raise SystemExit(f"invalid Secretary Bus config: {CONFIG_FILE}: {key} must be true or false")
    return {**DEFAULT_CONFIG, **raw}


def save_config(config: dict[str, Any]) -> dict[str, Any]:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {**DEFAULT_CONFIG, **config}
    tmp = CONFIG_FILE.with_name(f"{CONFIG_FILE.name}.{os.getpid()}.{time.time_ns()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, CONFIG_FILE)
    return payload


def update_config(
    *, trusted_owner: bool | None = None, auto_approve_permissions: bool | None = None
) -> dict[str, Any]:
    with locked():
        config = load_config()
        if trusted_owner is not None:
            config["trusted_owner"] = trusted_owner
        if auto_approve_permissions is not None:
            config["auto_approve_permissions"] = auto_approve_permissions
        return save_config(config)


def trusted_owner() -> bool:
    return bool(load_config()["trusted_owner"])
