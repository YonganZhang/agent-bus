#!/usr/bin/env python3
"""Background wake-up loop for one durable Secretary Bus leader session.

The daemon consumes only ``leader.tick_once`` compact deltas. On a meaningful
change it submits one bounded ``LEADER_EVENT`` prompt to the leader's frozen
tmux pane, so an idle Claude/Codex turn can resume judgment. State, PID/start
identity, dedupe digest, and logs live under ``AGENT_BUS_DIR/leader-daemons``.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import cli_bridge  # noqa: E402
import leader  # noqa: E402
import pane_detectors  # noqa: E402
import provider_state  # noqa: E402


DAEMONS = cli_bridge.BUS / "leader-daemons"
DEFAULT_INTERVAL = float(os.environ.get("SECRETARY_BUS_LEADERD_INTERVAL", "1.5"))
DEFAULT_PROBE_INTERVAL = float(os.environ.get("SECRETARY_BUS_LEADERD_PROBE_INTERVAL", "-1"))
MAX_NOTIFICATION_CHARS = int(os.environ.get("SECRETARY_BUS_LEADERD_MAX_NOTIFICATION", "3800"))
DEFAULT_REDRAW_COOLDOWN = float(os.environ.get("SECRETARY_BUS_LEADERD_REDRAW_COOLDOWN", "30"))
DEFAULT_DELIVERY_RETRY_SECONDS = float(os.environ.get("SECRETARY_BUS_LEADERD_RETRY_SECONDS", "5"))
DEFAULT_PROVIDER_POLL_SECONDS = float(os.environ.get("SECRETARY_BUS_LEADERD_PROVIDER_POLL_SECONDS", "5"))
DEFAULT_RECEIPT_TIMEOUT = float(os.environ.get("SECRETARY_BUS_LEADERD_RECEIPT_TIMEOUT", "120"))
DEFAULT_LOG_MAX_BYTES = int(os.environ.get("SECRETARY_BUS_LEADERD_LOG_MAX_BYTES", str(2 * 1024 * 1024)))
DEFAULT_LOG_BACKUPS = int(os.environ.get("SECRETARY_BUS_LEADERD_LOG_BACKUPS", "2"))
# Loop counters change every cycle but carry no recovery meaning; when they are
# the only change the state file is refreshed at most this often.
HEARTBEAT_SECONDS = float(os.environ.get("SECRETARY_BUS_LEADERD_HEARTBEAT_SECONDS", "30"))
HEARTBEAT_FIELDS = ("updated_at", "cycles", "last_tick_at", "last_tick_reason")
REDRAW_GLYPHS_RE = re.compile(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏◐◓◑◒▁▂▃▄▅▆▇█▓▒░|/\\—–-]+")
REDRAW_VOLATILE_RE = re.compile(
    r"\b(?:working|thinking|running)\b|\b\d+(?:\.\d+)?\s*(?:ms|s|sec|secs|seconds|min|mins|minutes|%)\b|"
    r"\b\d+\s*(?:tokens?|tok)\b|(?:esc|ctrl\+c)\s+to\s+interrupt",
    re.IGNORECASE,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def process_start_time(pid: int) -> str:
    return cli_bridge.process_start_time(pid)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def daemon_dir(leader_id: str) -> Path:
    return DAEMONS / leader.require_id(leader_id)


def daemon_state_path(leader_id: str) -> Path:
    return daemon_dir(leader_id) / "state.json"


def daemon_lock_path(leader_id: str) -> Path:
    return daemon_dir(leader_id) / "daemon.lock"


def daemon_log_path(leader_id: str) -> Path:
    return daemon_dir(leader_id) / "daemon.log"


def receipt_path(leader_id: str, delivery_id: str) -> Path:
    return daemon_dir(leader_id) / "receipts" / f"{leader.require_id(delivery_id, 'delivery id')}.json"


def rotate_log(
    leader_id: str,
    *,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    backups: int = DEFAULT_LOG_BACKUPS,
) -> bool:
    """Rotate one stopped daemon log before the next background start."""
    path = daemon_log_path(leader_id)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False
    if size <= max(0, int(max_bytes)):
        return False
    backups = max(1, int(backups))
    oldest = path.with_name(f"{path.name}.{backups}")
    oldest.unlink(missing_ok=True)
    for index in range(backups - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        if source.exists():
            source.replace(path.with_name(f"{path.name}.{index + 1}"))
    path.replace(path.with_name(f"{path.name}.1"))
    return True


def append_log(
    leader_id: str,
    item: dict[str, Any],
    *,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    backups: int = DEFAULT_LOG_BACKUPS,
) -> bool:
    """Append one compact JSON line and rotate between writes."""
    path = daemon_log_path(leader_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        rotate_log(leader_id, max_bytes=max_bytes, backups=backups)
        payload = {"ts": now_iso(), **item}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError:
        return False
    return True


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise SystemExit(f"corrupt leader daemon state: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"leader daemon state root is not an object: {path}")
    return value


_PERSISTED: dict[str, tuple[str, float]] = {}


def write_state(leader_id: str, state: dict[str, Any], *, force: bool = False) -> None:
    """Persist daemon state, skipping writes that would not change its content.

    Only this daemon process writes its state file (it holds the singleton
    lock), so an in-process record of the last persisted content is exact.
    """
    path = daemon_state_path(leader_id)
    core = json.dumps(
        {key: value for key, value in state.items() if key not in HEARTBEAT_FIELDS},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    now = time.time()
    previous = _PERSISTED.get(str(path))
    if not force and previous and previous[0] == core and now - previous[1] < HEARTBEAT_SECONDS:
        return
    state["updated_at"] = now_iso()
    leader.write_json_atomic(path, state)
    _PERSISTED[str(path)] = (core, now)


def acquire_lock(leader_id: str) -> Any:
    path = daemon_lock_path(leader_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise SystemExit(f"leader daemon already holds lock: {leader_id}")
    return handle


def daemon_status(leader_id: str) -> dict[str, Any]:
    state = read_json(daemon_state_path(leader_id))
    if not state:
        return {
            "leader_id": leader_id,
            "status": "stopped",
            "running": False,
            "reason": "no_state",
            "state_file": str(daemon_state_path(leader_id)),
            "log_file": str(daemon_log_path(leader_id)),
        }
    payload = dict(state)
    payload["state_file"] = str(daemon_state_path(leader_id))
    payload["log_file"] = str(daemon_log_path(leader_id))
    pid = int(state.get("pid") or 0)
    expected_token = str(state.get("pid_start_time") or "")
    if str(state.get("status") or "") not in {"starting", "running", "stopping"}:
        payload["running"] = False
        return payload
    if not pid_alive(pid):
        payload.update({"status": "stopped", "running": False, "reason": "process_missing"})
        return payload
    actual_token = process_start_time(pid)
    if not expected_token or actual_token != expected_token:
        payload.update(
            {
                "status": "stale",
                "running": False,
                "reason": "pid_reused",
                "actual_pid_start_time": actual_token,
            }
        )
        return payload
    payload["running"] = True
    return payload


def _compact_item(item: Any, text_limit: int = 320) -> Any:
    if not isinstance(item, dict):
        return str(item)[:text_limit]
    compact: dict[str, Any] = {}
    for key in (
        "id",
        "ts",
        "kind",
        "job_id",
        "target",
        "status",
        "worker",
        "from",
        "to",
        "excerpt",
    ):
        value = item.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, str) and len(value) > text_limit:
            value = value[: text_limit - 1].rstrip() + "…"
        compact[key] = value
    return compact


def compact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    workers = payload.get("workers") if isinstance(payload.get("workers"), dict) else {}
    return {
        "leader_id": payload.get("leader_id"),
        "delivery_id": payload.get("delivery_id"),
        "reason": payload.get("reason"),
        "status": payload.get("status"),
        "phase": payload.get("phase"),
        "cursor": payload.get("cursor"),
        "events": [_compact_item(item) for item in list(payload.get("events") or [])[-8:]],
        "changes": [_compact_item(item) for item in list(payload.get("changes") or [])[-8:]],
        "issues": [re.sub(r"\s+", " ", str(item)).strip()[:320] for item in list(payload.get("issues") or [])[-6:]],
        "workers": {str(key): str(value) for key, value in list(workers.items())[:32]},
    }


def notification_text(payload: dict[str, Any]) -> str:
    compact = compact_payload(payload)
    encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    delivery_id = str(compact.get("delivery_id") or "")
    leader_id = str(compact.get("leader_id") or "")
    ack = (
        f" First acknowledge receipt with `secretary-bus leaderd ack {leader_id} {delivery_id}`; "
        "a repeated delivery_id is the same event."
        if delivery_id and leader_id
        else ""
    )
    suffix = f"{ack} Review this change now; steer, collect, or verify as required, then keep monitoring."
    text = f"LEADER_EVENT {encoded}{suffix}"
    if len(text) <= MAX_NOTIFICATION_CHARS:
        return text
    fallback = {
        "leader_id": compact.get("leader_id"),
        "delivery_id": compact.get("delivery_id"),
        "reason": compact.get("reason"),
        "status": compact.get("status"),
        "phase": compact.get("phase"),
        "cursor": compact.get("cursor"),
        "event_count": len(compact.get("events") or []),
        "change_count": len(compact.get("changes") or []),
        "issue_count": len(compact.get("issues") or []),
        "workers": compact.get("workers"),
    }
    encoded = json.dumps(fallback, ensure_ascii=False, separators=(",", ":"))
    text = f"LEADER_EVENT {encoded}{suffix}"
    if len(text) > MAX_NOTIFICATION_CHARS:
        text = text[: MAX_NOTIFICATION_CHARS - 1].rstrip() + "…"
    return text


def notification_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(compact_payload(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def merge_notification_payload(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Merge compact deltas into one bounded outbox item."""
    left = compact_payload(current) if current else {}
    right = compact_payload(incoming)

    def unique(items: list[Any], limit: int) -> list[Any]:
        result: list[Any] = []
        seen: set[str] = set()
        for item in items:
            key = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result[-limit:]

    workers = dict(left.get("workers") or {})
    workers.update(right.get("workers") or {})
    return {
        "leader_id": right.get("leader_id") or left.get("leader_id"),
        "changed": True,
        "reason": right.get("reason") or left.get("reason"),
        "status": right.get("status") or left.get("status"),
        "phase": right.get("phase") or left.get("phase"),
        "cursor": max(int(left.get("cursor") or 0), int(right.get("cursor") or 0)),
        "events": unique(list(left.get("events") or []) + list(right.get("events") or []), 16),
        "changes": unique(list(left.get("changes") or []) + list(right.get("changes") or []), 16),
        "issues": unique(list(left.get("issues") or []) + list(right.get("issues") or []), 12),
        "workers": dict(list(workers.items())[-32:]),
    }


def _pane_only(payload: dict[str, Any]) -> bool:
    changes = list(payload.get("changes") or [])
    return bool(changes) and not payload.get("events") and not payload.get("issues") and all(
        isinstance(item, dict) and item.get("kind") == "pane_changed" for item in changes
    )


def _redraw_signature(payload: dict[str, Any]) -> str:
    excerpts = [
        str(item.get("excerpt") or "")
        for item in list(payload.get("changes") or [])
        if isinstance(item, dict) and item.get("kind") == "pane_changed"
    ]
    clean = " ".join(excerpts).lower()
    clean = REDRAW_VOLATILE_RE.sub(" ", clean)
    clean = REDRAW_GLYPHS_RE.sub(" ", clean)
    clean = re.sub(r"[\d\W_]+", " ", clean, flags=re.UNICODE)
    clean = re.sub(r"\s+", " ", clean).strip()
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()[:24] if clean else ""


def notification_candidate(
    state: dict[str, Any],
    payload: dict[str, Any],
    *,
    redraw_cooldown: float = DEFAULT_REDRAW_COOLDOWN,
) -> dict[str, Any] | None:
    """Drop spinner/progress redraws while retaining semantic pane changes."""
    if not _pane_only(payload):
        return payload
    signature = _redraw_signature(payload)
    previous = str(state.get("last_redraw_signature") or "")
    now = time.time()
    last_at = float(state.get("last_redraw_signature_at") or 0)
    if not signature or (signature == previous and now - last_at < max(0, redraw_cooldown)):
        state["filtered_redraws"] = int(state.get("filtered_redraws") or 0) + 1
        return None
    state["last_redraw_signature"] = signature
    state["last_redraw_signature_at"] = now
    return payload


def queue_notification(state: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    pending = state.get("pending_notification") if isinstance(state.get("pending_notification"), dict) else {}
    incoming = merge_notification_payload({}, payload)
    incoming.pop("delivery_id", None)
    incoming_digest = notification_digest(incoming)
    if pending.get("awaiting_receipt") and incoming_digest == str(pending.get("digest") or ""):
        return pending
    destination = "deferred_notification" if pending.get("awaiting_receipt") else "pending_notification"
    existing = state.get(destination) if isinstance(state.get(destination), dict) else {}
    merged = merge_notification_payload(existing.get("payload") or {}, payload)
    merged.pop("delivery_id", None)
    digest = notification_digest(merged)
    delivery_id = f"leaderd-{leader.require_id(str(merged.get('leader_id') or state.get('leader_id') or 'leader'))}-{digest}"
    merged["delivery_id"] = delivery_id
    queued = {
        "digest": digest,
        "delivery_id": delivery_id,
        "payload": merged,
        "created_at": existing.get("created_at") or now_iso(),
        "updated_at": now_iso(),
        "attempts": int(existing.get("attempts") or 0),
        "last_error": str(existing.get("last_error") or ""),
        "next_attempt_at": float(existing.get("next_attempt_at") or 0),
        "next_gate_at": float(existing.get("next_gate_at") or 0),
    }
    state[destination] = queued
    return queued


def record_receipt(leader_id: str, delivery_id: str) -> dict[str, Any]:
    """Record an idempotent leader-side receipt without taking the daemon lock."""
    leader_id = leader.require_id(leader_id)
    delivery_id = leader.require_id(delivery_id, "delivery id")
    state = read_json(daemon_state_path(leader_id))
    pending = state.get("pending_notification") if isinstance(state.get("pending_notification"), dict) else {}
    last = state.get("last_notification") if isinstance(state.get("last_notification"), dict) else {}
    if delivery_id not in {str(pending.get("delivery_id") or ""), str(last.get("delivery_id") or "")}:
        raise SystemExit(f"delivery is not pending for leader {leader_id}: {delivery_id}")
    receipt = {"leader_id": leader_id, "delivery_id": delivery_id, "received_at": now_iso()}
    leader.write_json_atomic(receipt_path(leader_id, delivery_id), receipt)
    return receipt


def consume_receipt(leader_id: str, state: dict[str, Any]) -> bool:
    pending = state.get("pending_notification")
    if not isinstance(pending, dict) or not pending.get("awaiting_receipt"):
        return False
    delivery_id = str(pending.get("delivery_id") or "")
    path = receipt_path(leader_id, delivery_id)
    receipt = read_json(path)
    if str(receipt.get("delivery_id") or "") != delivery_id:
        return False
    last = dict(state.get("last_notification") or {})
    last.update({"delivery_id": delivery_id, "processed_ack": True, "receipt_at": receipt.get("received_at")})
    state["last_notification"] = last
    state["last_notification_digest"] = str(pending.get("digest") or "")
    state["last_receipt"] = receipt
    deferred = state.get("deferred_notification") if isinstance(state.get("deferred_notification"), dict) else {}
    state["pending_notification"] = deferred
    state["deferred_notification"] = {}
    # Commit the acknowledgement and deferred promotion before deleting the
    # receipt. A crash after this write can leave a harmless stale receipt, but
    # it cannot lose the ack and trigger another transport injection.
    write_state(leader_id, state)
    path.unlink(missing_ok=True)
    append_log(leader_id, {"event": "receipt", "delivery_id": delivery_id})
    return True


def recover_inflight_tick(leader_id: str, state: dict[str, Any]) -> bool:
    """Recover a crash after tick advanced its cursor but before outbox commit."""
    inflight = state.get("tick_inflight")
    if not isinstance(inflight, dict) or not inflight:
        return False
    contract = leader.load_state(leader_id)
    old_cursor = int(inflight.get("cursor") or 0)
    current_cursor = int(contract.get("event_cursor") or old_cursor)
    job_ids = {
        str(job_id)
        for worker in (contract.get("workers") or {}).values()
        for job_id in worker.get("job_ids", [])
    }
    events, _last_id = leader.event_ledger.read_events(after=old_cursor, limit=1000)
    relevant = [leader.compact_event(event) for event in events if str(event.get("job_id") or "") in job_ids]
    workers = {
        str(name): str(worker.get("status") or "unknown")
        for name, worker in (contract.get("workers") or {}).items()
        if not worker.get("retired")
    }
    changes: list[dict[str, Any]] = []
    issues: list[str] = []
    if not relevant:
        # Pane-only changes cannot be reconstructed after a process crash, so
        # request one bounded reconcile instead of silently losing the wake-up.
        changes.append({"kind": "reconcile_required", "from_cursor": old_cursor, "to_cursor": current_cursor})
        issues.append("leaderd_recovered_inflight_tick")
    queue_notification(
        state,
        {
            "leader_id": leader_id,
            "changed": True,
            "reason": "daemon_recovered_inflight_tick",
            "status": contract.get("status"),
            "phase": contract.get("phase"),
            "cursor": current_cursor,
            "events": relevant,
            "changes": changes,
            "issues": issues,
            "workers": workers,
        },
    )
    state["tick_inflight"] = {}
    write_state(leader_id, state)
    append_log(leader_id, {"event": "tick_recovered", "from_cursor": old_cursor, "to_cursor": current_cursor})
    return True


# Claude draws a dimmed suggestion in an empty input box on some versions;
# without colours it reads like text, but nobody typed it.
CLAUDE_PLACEHOLDER_RE = re.compile(r'^Try ".*"$')
CODEX_DRAFT_RE = re.compile(r"^\s*›\s+(?!\d+[.)]\s)(\S.*)$")


def composer_draft(screen: str) -> str:
    """Text a person typed into the provider's input box but has not sent ('' when empty).

    "idle" from the provider only means no turn is running: the user may be
    halfway through a message, and a wake-up typed + submitted now would be
    glued onto that draft and sent as one prompt.
    """
    lines = pane_detectors.strip_agent_panel(screen.splitlines())
    while lines and not lines[-1].strip():
        lines.pop()
    split = pane_detectors.split_screen(lines)
    if split.provider == "codex":
        # Codex's anchor row is its placeholder; it is only drawn while the composer is empty.
        return ""
    if split.provider == "claude":
        rows = list(split.input_lines)
        if rows and (pane_detectors.PROMPT_BOX_TOP_RE.match(rows[0]) or pane_detectors.PROMPT_BOX_RULE_RE.match(rows[0])):
            rows = rows[1:]
        text: list[str] = []
        for row in rows:
            if pane_detectors.PROMPT_BOX_RULE_RE.match(row):
                break
            text.append(row.strip())
        if text:
            text[0] = re.sub(r"^❯\s?", "", text[0])
        draft = "\n".join(row for row in text if row).strip()
        return "" if CLAUDE_PLACEHOLDER_RE.match(draft) else draft
    # No input region: Codex with a draft has lost its placeholder anchor, so
    # look for its `› …` composer row near the bottom of the screen.
    tail = [line for line in lines if line.strip()][-pane_detectors.MAX_INPUT_BOX_HEIGHT:]
    for line in reversed(tail):
        match = CODEX_DRAFT_RE.match(line)
        if match:
            return match.group(1).strip()
    return ""


def provider_gate(leader_id: str) -> dict[str, Any]:
    """Read the exact/inferred provider state for the frozen leader target.

    ``draft_chars`` > 0 means the leader's input box holds unsent text; only
    its length is kept (the draft itself never goes into state or logs)."""
    contract = leader.load_state(leader_id)
    target_name = str(contract.get("leader") or "")
    snapshot = provider_state.snapshot_target(target_name, max_chars=240)
    provider_status = snapshot.get("state") if isinstance(snapshot.get("state"), dict) else {}
    session = snapshot.get("session") if isinstance(snapshot.get("session"), dict) else {}
    runtime = snapshot.get("runtime") if isinstance(snapshot.get("runtime"), dict) else {}
    pane_id = str(runtime.get("pane_id") or "")
    if not pane_id:
        raise SystemExit(f"leader target {target_name} has no pane id; cannot check its input box")
    return {
        "value": str(provider_status.get("value") or "unknown"),
        "source": str(provider_status.get("source") or "unknown"),
        "confidence": str(provider_status.get("confidence") or "low"),
        "provider": str(snapshot.get("provider") or "unknown"),
        "session_exact": bool(session.get("exact")),
        "draft_chars": len(composer_draft(provider_state.capture_pane(pane_id))),
    }


def attempt_pending_delivery(
    leader_id: str,
    state: dict[str, Any],
    *,
    dry_run: bool,
    redraw_cooldown: float,
    delivery_retry_seconds: float,
    provider_poll_seconds: float,
    receipt_timeout: float,
) -> None:
    if consume_receipt(leader_id, state):
        write_state(leader_id, state)
    pending = state.get("pending_notification")
    if not isinstance(pending, dict) or not isinstance(pending.get("payload"), dict):
        return
    now = time.time()
    if now < float(pending.get("next_gate_at") or 0):
        return
    if now < float(pending.get("next_attempt_at") or 0):
        return
    try:
        gate = provider_gate(leader_id)
    except (Exception, SystemExit) as exc:
        pending["attempts"] = int(pending.get("attempts") or 0) + 1
        pending["last_error"] = re.sub(r"\s+", " ", str(exc)).strip()[:1000]
        pending["next_attempt_at"] = now + max(0, delivery_retry_seconds)
        state["notification_failures"] = int(state.get("notification_failures") or 0) + 1
        state["last_provider_gate"] = {"value": "error", "error": pending["last_error"]}
        write_state(leader_id, state)
        return
    state["last_provider_gate"] = gate
    pending["gate"] = gate
    if str(gate.get("value") or "") != "idle" or str(gate.get("confidence") or "") == "low":
        pending["next_gate_at"] = now + max(0, provider_poll_seconds)
        write_state(leader_id, state)
        return
    draft_chars = int(gate.get("draft_chars") or 0)
    if draft_chars:
        # Someone is typing in the leader window: submitting now would send
        # their half-written message glued to this event.  Wait it out.
        reason = f"leader input box holds an unsent draft ({draft_chars} chars); not typing over it"
        if pending.get("deferred_reason") != reason:
            append_log(leader_id, {"event": "notification_deferred", "delivery_id": pending.get("delivery_id"), "reason": reason})
        pending["deferred_reason"] = reason
        pending["next_gate_at"] = now + max(0, provider_poll_seconds)
        write_state(leader_id, state)
        return
    pending.pop("deferred_reason", None)
    pending.pop("next_gate_at", None)

    payload = pending["payload"]
    if _pane_only(payload):
        elapsed = now - float(state.get("last_pane_notification_epoch") or 0)
        if elapsed < max(0, redraw_cooldown):
            pending["cooldown_remaining"] = max(0, redraw_cooldown - elapsed)
            write_state(leader_id, state)
            return
        pending.pop("cooldown_remaining", None)

    if dry_run:
        if str(state.get("last_preview_digest") or "") != str(pending.get("digest") or ""):
            delivery = notify_leader(leader_id, payload, dry_run=True)
            state["last_preview_digest"] = str(pending.get("digest") or "")
            state["last_notification"] = {
                "at": now_iso(),
                "digest": pending.get("digest"),
                "sent": False,
                "dry_run": True,
                "transport_acked": False,
                "processed_ack": False,
                "reason": payload.get("reason"),
                "status": payload.get("status"),
                "phase": payload.get("phase"),
                "chars": delivery.get("chars"),
            }
        write_state(leader_id, state)
        return

    try:
        delivery = notify_leader(leader_id, payload, dry_run=False)
        if not delivery.get("sent"):
            raise RuntimeError("notification returned without delivery acknowledgement")
    except (Exception, SystemExit) as exc:
        pending["attempts"] = int(pending.get("attempts") or 0) + 1
        pending["last_error"] = re.sub(r"\s+", " ", str(exc)).strip()[:1000]
        pending["next_attempt_at"] = now + max(0, delivery_retry_seconds)
        state["notification_failures"] = int(state.get("notification_failures") or 0) + 1
        append_log(
            leader_id,
            {"event": "notification_error", "delivery_id": pending.get("delivery_id"), "error": pending["last_error"]},
        )
        write_state(leader_id, state)
        return

    digest = str(pending.get("digest") or notification_digest(payload))
    injection_count = int(pending.get("injection_count") or 0)
    if injection_count == 0:
        state["notifications"] = int(state.get("notifications") or 0) + 1
    state["transport_injections"] = int(state.get("transport_injections") or 0) + 1
    pending["injection_count"] = injection_count + 1
    pending["awaiting_receipt"] = True
    pending["injected_at"] = now_iso()
    pending["next_attempt_at"] = now + max(0, receipt_timeout)
    state["last_notification"] = {
        "at": now_iso(),
        "digest": digest,
        "delivery_id": pending.get("delivery_id"),
        "sent": True,
        "dry_run": False,
        "transport_acked": True,
        "processed_ack": False,
        "reason": payload.get("reason"),
        "status": payload.get("status"),
        "phase": payload.get("phase"),
    }
    if _pane_only(payload):
        state["last_pane_notification_epoch"] = now
    append_log(
        leader_id,
        {"event": "notification_injected", "delivery_id": pending.get("delivery_id"), "attempt": pending["injection_count"]},
    )
    write_state(leader_id, state)


def notify_leader(leader_id: str, payload: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    """Wake the exact frozen leader pane with one bounded compact event."""
    leader_state = leader.load_state(leader_id)
    target = leader.target_or_die(str(leader_state.get("leader") or ""))
    current = leader.runtime_snapshot(str(leader_state.get("leader") or ""))
    frozen = leader_state.get("leader_runtime") or {}
    if (
        str(current.get("pane_id") or "") != str(frozen.get("pane_id") or "")
        or str(current.get("runtime_fingerprint") or "") != str(frozen.get("runtime_fingerprint") or "")
    ):
        raise SystemExit("leader runtime changed; refusing daemon wake-up until a new leader contract is created")
    text = notification_text(payload)
    result = {
        "sent": False,
        "dry_run": bool(dry_run),
        "target": target.name,
        "pane_id": str(current.get("pane_id") or ""),
        "chars": len(text),
        "digest": notification_digest(payload),
    }
    if dry_run:
        return result
    cli_bridge.send_to_target(
        target,
        text,
        enter=True,
        yes=True,
        allow_newline=False,
        show_text=False,
        delivery_mode="followup",
    )
    result["sent"] = True
    return result


def run_loop(
    leader_id: str,
    *,
    interval: float = DEFAULT_INTERVAL,
    probe_interval: float = DEFAULT_PROBE_INTERVAL,
    dry_run: bool = False,
    max_cycles: int = 0,
    redraw_cooldown: float = DEFAULT_REDRAW_COOLDOWN,
    delivery_retry_seconds: float = DEFAULT_DELIVERY_RETRY_SECONDS,
    provider_poll_seconds: float = DEFAULT_PROVIDER_POLL_SECONDS,
    receipt_timeout: float = DEFAULT_RECEIPT_TIMEOUT,
) -> dict[str, Any]:
    """Run one leader's event-first monitor until stopped or terminal."""
    leader_id = leader.require_id(leader_id)
    lock = acquire_lock(leader_id)
    previous = read_json(daemon_state_path(leader_id))
    state: dict[str, Any] = {
        "version": 2,
        "leader_id": leader_id,
        "status": "running",
        "reason": "monitoring",
        "pid": os.getpid(),
        "pid_start_time": process_start_time(os.getpid()),
        "started_at": now_iso(),
        "dry_run": bool(dry_run),
        "cycles": 0,
        "notifications": int(previous.get("notifications") or 0),
        "last_notification_digest": str(previous.get("last_notification_digest") or ""),
        "last_notification": previous.get("last_notification") or {},
        "pending_notification": previous.get("pending_notification") or {},
        "deferred_notification": previous.get("deferred_notification") or {},
        "last_preview_digest": str(previous.get("last_preview_digest") or ""),
        "last_redraw_signature": str(previous.get("last_redraw_signature") or ""),
        "last_redraw_signature_at": float(previous.get("last_redraw_signature_at") or 0),
        "last_pane_notification_epoch": float(previous.get("last_pane_notification_epoch") or 0),
        "filtered_redraws": int(previous.get("filtered_redraws") or 0),
        "notification_failures": int(previous.get("notification_failures") or 0),
        "transport_injections": int(previous.get("transport_injections") or 0),
        "last_provider_gate": previous.get("last_provider_gate") or {},
        "tick_inflight": previous.get("tick_inflight") or {},
    }
    stop_requested = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    old_term = signal.signal(signal.SIGTERM, request_stop)
    old_int = signal.signal(signal.SIGINT, request_stop)
    write_state(leader_id, state, force=True)
    recover_inflight_tick(leader_id, state)
    consume_receipt(leader_id, state)
    write_state(leader_id, state)
    append_log(leader_id, {"event": "daemon_started", "pid": state["pid"], "dry_run": bool(dry_run)})
    try:
        while not stop_requested:
            state["cycles"] = int(state.get("cycles") or 0) + 1

            def mark_inflight(old_cursor: int) -> None:
                # tick_once calls this under the leader lock right before it
                # commits a reportable change (cursor past relevant events,
                # pane/identity change).  A crash between that commit and the
                # outbox write below is then recovered on restart.  Idle ticks
                # commit nothing reportable and skip both marker writes.
                state["tick_inflight"] = {"cursor": int(old_cursor or 0), "at": now_iso()}
                write_state(leader_id, state)

            try:
                payload = leader.tick_once(
                    leader_id, probe_interval=probe_interval, force_probe=False, before_commit=mark_inflight
                )
                state["last_tick_at"] = now_iso()
                state["last_tick_reason"] = str(payload.get("reason") or "")
                if payload.get("reason") == "leader_terminal" or str(payload.get("status") or "") in leader.FINAL_STATES:
                    state.update(
                        {
                            "status": "stopped",
                            "reason": "leader_terminal",
                            "leader_status": payload.get("status"),
                            "pending_notification": {},
                            "deferred_notification": {},
                            "tick_inflight": {},
                        }
                    )
                    append_log(leader_id, {"event": "daemon_stopped", "reason": "leader_terminal"})
                    write_state(leader_id, state)
                    return state
                if payload.get("changed"):
                    candidate = notification_candidate(state, payload, redraw_cooldown=redraw_cooldown)
                    if candidate is not None:
                        digest = notification_digest(candidate)
                        if digest != state.get("last_notification_digest"):
                            queue_notification(state, candidate)
                            # tick_once already advanced the leader event cursor;
                            # persist the outbox before any fallible provider/tmux call.
                            write_state(leader_id, state)
                state["tick_inflight"] = {}
                write_state(leader_id, state)
                attempt_pending_delivery(
                    leader_id,
                    state,
                    dry_run=dry_run,
                    redraw_cooldown=redraw_cooldown,
                    delivery_retry_seconds=delivery_retry_seconds,
                    provider_poll_seconds=provider_poll_seconds,
                    receipt_timeout=receipt_timeout,
                )
            except (Exception, SystemExit) as exc:
                state.update(
                    {
                        "status": "failed",
                        "reason": "monitor_error",
                        "error": re.sub(r"\s+", " ", str(exc)).strip()[:1000],
                    }
                )
                write_state(leader_id, state)
                append_log(leader_id, {"event": "daemon_failed", "error": state["error"]})
                return state

            write_state(leader_id, state)
            if max_cycles and int(state["cycles"]) >= max_cycles:
                state.update({"status": "stopped", "reason": "max_cycles"})
                append_log(leader_id, {"event": "daemon_stopped", "reason": "max_cycles"})
                write_state(leader_id, state)
                return state
            if interval > 0:
                time.sleep(max(0.05, interval))

        state.update({"status": "stopped", "reason": "stop_requested", "stopped_at": now_iso()})
        append_log(leader_id, {"event": "daemon_stopped", "reason": "stop_requested"})
        write_state(leader_id, state)
        return state
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def cmd_run(args: argparse.Namespace) -> int:
    result = run_loop(
        args.id,
        interval=args.interval,
        probe_interval=args.probe_interval,
        dry_run=args.dry_run,
        max_cycles=args.max_cycles,
        redraw_cooldown=args.redraw_cooldown,
        delivery_retry_seconds=args.delivery_retry_seconds,
        provider_poll_seconds=args.provider_poll_seconds,
        receipt_timeout=args.receipt_timeout,
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 1 if result.get("status") == "failed" else 0


def cmd_start(args: argparse.Namespace) -> int:
    current = daemon_status(args.id)
    if current.get("running"):
        print(
            f"leader daemon already running id={args.id} pid={current.get('pid')} log={daemon_log_path(args.id)}"
        )
        return 0
    # Probe the singleton lock before spawning. The child acquires it for the
    # full lifetime; no PID-file race can create two active loops.
    probe = acquire_lock(args.id)
    fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
    probe.close()
    directory = daemon_dir(args.id)
    directory.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "run",
        args.id,
        "--interval",
        str(args.interval),
        "--probe-interval",
        str(args.probe_interval),
    ]
    if args.dry_run:
        command.append("--dry-run")
    if args.max_cycles:
        command.extend(["--max-cycles", str(args.max_cycles)])
    command.extend(["--redraw-cooldown", str(args.redraw_cooldown)])
    command.extend(["--delivery-retry-seconds", str(args.delivery_retry_seconds)])
    command.extend(["--provider-poll-seconds", str(args.provider_poll_seconds)])
    command.extend(["--receipt-timeout", str(args.receipt_timeout)])
    rotate_log(args.id)
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.time() + args.start_timeout
    while time.time() < deadline:
        status = daemon_status(args.id)
        if status.get("running") and int(status.get("pid") or 0) == proc.pid:
            print(f"leader daemon id={args.id} pid={proc.pid} log={daemon_log_path(args.id)}")
            return 0
        if proc.poll() is not None:
            status = daemon_status(args.id)
            if status.get("reason") == "leader_terminal" and proc.returncode == 0:
                print(f"leader daemon id={args.id} exited: leader terminal; log={daemon_log_path(args.id)}")
                return 0
            raise SystemExit(
                f"leader daemon exited early code={proc.returncode} status={status.get('status')} "
                f"reason={status.get('reason')} log={daemon_log_path(args.id)}"
            )
        time.sleep(0.05)
    raise SystemExit(
        f"leader daemon did not become ready within {args.start_timeout}s; pid={proc.pid} log={daemon_log_path(args.id)}"
    )


def cmd_status(args: argparse.Namespace) -> int:
    status = daemon_status(args.id)
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
    else:
        print(
            f"leaderd id={args.id} status={status.get('status')} running={str(bool(status.get('running'))).lower()} "
            f"pid={status.get('pid') or '-'} reason={status.get('reason') or '-'} log={status.get('log_file')}"
        )
    return 0 if status.get("running") else 1


def cmd_stop(args: argparse.Namespace) -> int:
    status = daemon_status(args.id)
    if not status.get("running"):
        print(f"leader daemon is not running: {args.id} status={status.get('status')} reason={status.get('reason')}")
        return 0
    pid = int(status["pid"])
    # daemon_status already compared Linux start tokens, so this signal cannot
    # be redirected to a recycled PID.
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + args.stop_timeout
    while time.time() < deadline:
        current = daemon_status(args.id)
        if not current.get("running"):
            print(f"leader daemon stopped: {args.id} pid={pid}")
            return 0
        time.sleep(0.05)
    raise SystemExit(f"leader daemon did not stop within {args.stop_timeout}s: id={args.id} pid={pid}")


def cmd_ack(args: argparse.Namespace) -> int:
    receipt = record_receipt(args.id, args.delivery_id)
    if args.json:
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
    else:
        print(f"leader delivery acknowledged id={args.id} delivery_id={args.delivery_id}")
    return 0


def add_loop_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("id")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    parser.add_argument("--probe-interval", type=float, default=DEFAULT_PROBE_INTERVAL)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-cycles", type=int, default=0, help="Testing/one-shot bound; 0 runs until stopped")
    parser.add_argument("--redraw-cooldown", type=float, default=DEFAULT_REDRAW_COOLDOWN)
    parser.add_argument("--delivery-retry-seconds", type=float, default=DEFAULT_DELIVERY_RETRY_SECONDS)
    parser.add_argument("--provider-poll-seconds", type=float, default=DEFAULT_PROVIDER_POLL_SECONDS)
    parser.add_argument("--receipt-timeout", type=float, default=DEFAULT_RECEIPT_TIMEOUT)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Persistent compact wake-up loop for one Secretary Bus leader")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("start", help="Start one leader daemon in the background")
    add_loop_args(p)
    p.add_argument("--start-timeout", type=float, default=5)
    p.set_defaults(fn=cmd_start)

    p = sub.add_parser("run", help="Run one leader daemon in the foreground")
    add_loop_args(p)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("status", help="Show PID/start-token verified daemon state")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("stop", help="Stop the exact PID/start-token daemon")
    p.add_argument("id")
    p.add_argument("--stop-timeout", type=float, default=10)
    p.set_defaults(fn=cmd_stop)

    p = sub.add_parser("ack", help="Acknowledge one idempotent LEADER_EVENT delivery")
    p.add_argument("id")
    p.add_argument("delivery_id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_ack)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
