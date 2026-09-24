#!/usr/bin/env python3
"""Provider-neutral leader sessions over Secretary Bus primitives.

This is intentionally a thin control plane. It owns compact coordination
state, worker claims, event cursors, and bounded probes; execution remains in
the existing tmux supervisor / Codex adapters and lifecycle truth remains in
event_ledger.py.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import cli_bridge  # noqa: E402
import bus_config  # noqa: E402
import cards_control  # noqa: E402
import dialogs  # noqa: E402
import event_ledger  # noqa: E402
import provider_state  # noqa: E402
import supervisor  # noqa: E402


BUS = cli_bridge.BUS
SESSIONS = BUS / "leader-sessions"
CLAIMS_FILE = SESSIONS / "claims.json"
LOCK_FILE = SESSIONS / ".lock"
ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
FINAL_STATES = {"completed", "blocked", "failed", "cancelled", "superseded"}
DEFAULT_LEASE_SECONDS = int(os.environ.get("SECRETARY_BUS_LEADER_LEASE_SECONDS", "7200"))
DEFAULT_PROBE_LINES = int(os.environ.get("SECRETARY_BUS_LEADER_PROBE_LINES", "80"))
# The same dialog (pane + question) is not retried within this many seconds.
PROMPT_RETRY_SECONDS = 30.0
LEADER_SOURCE = "secretary-bus-leader"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def require_id(value: str, label: str = "leader id") -> str:
    if not ID_RE.fullmatch(value):
        raise SystemExit(f"invalid {label}: use only letters, digits, dot, underscore, or hyphen")
    if value in {"claims", ".lock"}:
        raise SystemExit(f"reserved {label}: {value}")
    return value


def default_cards_category(objective: str, leader_id: str) -> str:
    """Build a readable, collision-resistant fallback for visible leaders."""
    stem = re.sub(r"\s+", " ", str(objective or "")).strip()
    stem = re.sub(r"[\x00-\x1f]", "", stem).strip(" ·|/\\") or "未命名任务"
    suffix = re.sub(r"[^A-Za-z0-9_.-]+", "", leader_id)[-6:] or "leader"
    budget = max(1, 32 - len("任务··") - len(suffix))
    return f"任务·{stem[:budget]}·{suffix}"


def state_path(leader_id: str) -> Path:
    return SESSIONS / f"{require_id(leader_id)}.json"


def read_json_strict(path: Path, *, missing: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return missing
    except json.JSONDecodeError as exc:
        raise SystemExit(f"corrupt leader state; refusing to guess: {path}: {exc}") from exc


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


@contextlib.contextmanager
def locked() -> Iterator[None]:
    SESSIONS.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_state(leader_id: str) -> dict[str, Any]:
    state = read_json_strict(state_path(leader_id))
    if not isinstance(state, dict):
        raise SystemExit(f"leader session not found: {leader_id}")
    return state


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    state["revision"] = int(state.get("revision") or 0) + 1
    write_json_atomic(state_path(str(state["id"])), state)


def load_claims() -> dict[str, dict[str, Any]]:
    claims = read_json_strict(CLAIMS_FILE, missing={})
    if not isinstance(claims, dict):
        raise SystemExit(f"corrupt leader claims; refusing to guess: {CLAIMS_FILE}")
    return {str(key): value for key, value in claims.items() if isinstance(value, dict)}


def active_claims(claims: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    now = time.time()
    return {key: value for key, value in claims.items() if float(value.get("lease_until") or 0) > now}


def durable_claims(claims: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The claims to write back.  An expired claim stays while its leader is
    still running: a paused leader re-acquires its own expired claim on its
    next tick (renew_claims), which another leader's write must not erase.
    Only claims of leaders that are gone or finished are dropped."""
    now = time.time()
    kept: dict[str, dict[str, Any]] = {}
    for key, value in claims.items():
        if float(value.get("lease_until") or 0) <= now:
            try:
                owner = read_json_strict(state_path(str(value.get("leader_id") or "")), missing={})
            except SystemExit:
                owner = {}
            if not isinstance(owner, dict) or owner.get("status") != "running":
                continue
        kept[key] = value
    return kept


def target_or_die(name: str) -> cli_bridge.Target:
    targets = cli_bridge.load_targets()
    if name not in targets:
        raise SystemExit(f"unknown registered target: {name}")
    return targets[name]


def runtime_snapshot(name: str) -> dict[str, Any]:
    target = target_or_die(name)
    info = cli_bridge.target_info(target)
    if str(info["command"]) != target.expected_command:
        raise SystemExit(
            f"target command mismatch: {name} expected={target.expected_command} actual={info['command']}"
        )
    fingerprint_source = "\0".join(
        [
            str(info["pane_id"]),
            str(info["pane_pid"]),
            str(info["pane_start_time"]),
            str(info["foreground_pid"]),
            str(info["foreground_start_time"]),
            str(info["command"]),
            str(info["cwd"]),
        ]
    )
    return {
        "name": name,
        "pane": str(info["pane"]),
        "pane_id": str(info["pane_id"]),
        "pane_pid": int(info["pane_pid"]),
        "pane_start_time": str(info["pane_start_time"]),
        "foreground_pid": int(info["foreground_pid"]),
        "foreground_start_time": str(info["foreground_start_time"]),
        "expected_command": target.expected_command,
        "cwd": str(info["cwd"]),
        "runtime_fingerprint": hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:24],
    }


def runtime_fingerprint(info: dict[str, Any]) -> str:
    source = "\0".join(
        str(info.get(key, ""))
        for key in (
            "pane_id",
            "pane_pid",
            "pane_start_time",
            "foreground_pid",
            "foreground_start_time",
            "command",
            "cwd",
        )
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]


def snapshot_from_info(name: str, info: dict[str, Any], expected_command: str) -> dict[str, Any]:
    return {
        "name": name,
        "pane": str(info["pane"]),
        "pane_id": str(info["pane_id"]),
        "pane_pid": int(info["pane_pid"]),
        "pane_start_time": str(info["pane_start_time"]),
        "foreground_pid": int(info["foreground_pid"]),
        "foreground_start_time": str(info["foreground_start_time"]),
        "expected_command": expected_command,
        "cwd": str(info["cwd"]),
        "runtime_fingerprint": runtime_fingerprint(info),
    }


def capture_digest(member: dict[str, Any], lines: int = DEFAULT_PROBE_LINES) -> tuple[str, str]:
    cp = cli_bridge.tmux(
        "capture-pane",
        "-p",
        "-t",
        str(member["pane_id"]),
        "-S",
        f"-{max(10, min(lines, 300))}",
        check=False,
    )
    if cp.returncode != 0:
        raise SystemExit(cp.stderr.strip() or f"capture-pane failed: {member['pane_id']}")
    digest = hashlib.sha256(cp.stdout.encode("utf-8")).hexdigest()[:24]
    nonblank = [re.sub(r"\s+", " ", line).strip() for line in cp.stdout.splitlines() if line.strip()]
    excerpt = " | ".join(nonblank[-4:])
    if len(excerpt) > 700:
        excerpt = excerpt[-699:] + "…"
    return digest, excerpt


def public_state(state: dict[str, Any]) -> dict[str, Any]:
    # The compact state intentionally contains no full task prompt or transcript.
    return state


def worker_map(values: list[str], workers: list[str], label: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        name, separator, text = value.partition("=")
        if not separator or not name.strip() or not text.strip():
            raise SystemExit(f"invalid {label}: expected WORKER=VALUE")
        name = name.strip()
        if name not in workers:
            raise SystemExit(f"{label} references a worker outside membership: {name}")
        parsed[name] = event_ledger.compact(text, 500)
    return parsed


def emit(payload: Any, *, as_json: bool, text_line: str = "") -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif text_line:
        print(text_line)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def execution_options(args: argparse.Namespace) -> tuple[bool, bool]:
    """Resolve explicit flags over the durable trusted-owner default."""
    trusted = bus_config.trusted_owner()
    send = bool(getattr(args, "yes", False)) or (trusted and not bool(getattr(args, "dry_run", False)))
    allow_newline = bool(getattr(args, "allow_newline", False)) or trusted
    return send, allow_newline


def abandon_hint(leader_id: str, worker: str) -> str:
    return (
        f"if worker {worker}'s pane is gone or now runs another process, end its attempt with: "
        f"secretary-bus leader abandon {leader_id} --worker {worker} --reason \"<why>\""
    )


def cancel_active_child_jobs(state: dict[str, Any], *, reason: str) -> list[str]:
    """Terminalize this leader's still-active attempts as cancelled (ledger only).

    Called when the leader stops owning its workers (close or takeover), so
    no child job is left active with nobody able to steer, collect or close
    it.  The worker panes are not interrupted.
    """
    cancelled: list[str] = []
    for worker in (state.get("workers") or {}).values():
        job_ids = [str(value) for value in worker.get("job_ids") or []]
        for job_id in job_ids:
            job = event_ledger.get_job(job_id)
            if not job:
                continue
            if event_ledger.normalize_status(str(job.get("status") or "")) in event_ledger.TERMINAL_STATUSES:
                continue
            event_ledger.upsert_job(
                job_id,
                source="secretary-bus-leader",
                status="cancelled",
                completed_at=now_iso(),
                message=reason,
            )
            cancelled.append(job_id)
            if job_ids and job_id == job_ids[-1] and not worker.get("retired"):
                worker["status"] = "cancelled"
    return cancelled


def mark_superseded(leader_id: str, by: str) -> None:
    path = state_path(leader_id)
    state = read_json_strict(path)
    if not isinstance(state, dict) or str(state.get("status")) in FINAL_STATES:
        return
    state["status"] = "superseded"
    state["phase"] = "closed"
    state["superseded_by"] = by
    state["cancelled_jobs"] = cancel_active_child_jobs(
        state,
        reason=f"leader {leader_id} superseded by {by}; attempt cancelled (worker pane was not interrupted)",
    )
    save_state(state)
    event_ledger.upsert_job(
        f"leader:{leader_id}",
        source="secretary-bus-leader",
        status="interrupted",
        message=f"leader session superseded by {by}",
    )


def cmd_create(args: argparse.Namespace) -> int:
    leader_id = require_id(args.id or event_ledger.new_job_id("lead"))
    if len(args.objective) > 1000:
        raise SystemExit("objective too long; store a spec path and keep the objective under 1000 characters")
    lease_seconds = max(30, args.lease_seconds)
    names = [args.leader, *args.worker]
    if len(set(names)) != len(names):
        raise SystemExit("leader and worker target names must be distinct")
    snapshots = {name: runtime_snapshot(name) for name in names}
    pane_ids = [str(item["pane_id"]) for item in snapshots.values()]
    if len(set(pane_ids)) != len(pane_ids):
        raise SystemExit("leader and workers must resolve to distinct tmux panes")
    hidden = bool(getattr(args, "hidden", False))
    requested_category = str(getattr(args, "cards_category", "") or "").strip()
    if hidden and requested_category:
        raise SystemExit("--hidden and --cards-category are mutually exclusive")
    display_mode = "hidden" if hidden else "visible"
    cards_category = "" if hidden else (requested_category or default_cards_category(args.objective, leader_id))
    cards_membership: dict[str, str] = {}
    cards_assignment: dict[str, Any] = {}

    workers: dict[str, dict[str, Any]] = {}
    task_map = worker_map(args.scope, args.worker, "--scope")
    write_ownership = worker_map(args.write_owner, args.worker, "--write-owner")
    for name in args.worker:
        item = dict(snapshots[name])
        digest, _excerpt = capture_digest(item)
        item.update(
            {
                "job_ids": [],
                "status": "unassigned",
                "last_digest": digest,
                "last_probe_at": time.time(),
                "last_progress_at": time.time(),
                "last_event": 0,
            }
        )
        workers[name] = item

    with locked():
        if state_path(leader_id).exists():
            raise SystemExit(f"leader session already exists: {leader_id}")
        raw_claims = load_claims()
        active = active_claims(raw_claims)
        claims = durable_claims(raw_claims)
        conflicting_leaders: set[str] = set()
        for worker in workers.values():
            owner = active.get(str(worker["pane_id"]), {}).get("leader_id")
            if owner and owner != leader_id:
                if not args.takeover:
                    raise SystemExit(
                        f"worker {worker['name']} is already claimed by leader {owner}; "
                        "wait for expiry or pass --takeover only with explicit authority"
                    )
                conflicting_leaders.add(str(owner))
        if display_mode == "visible":
            # Existing Cards categories are project/user organization metadata,
            # not leader-owned state.  Preserve them and only categorize panes
            # that are currently uncategorized.
            cards_assignment = cards_control.assign_uncategorized_group(
                cards_control.CardsClient(),
                cards_category,
                pane_ids,
                create=True,
            )
            cards_category = str(cards_assignment.get("category") or "")
            cards_membership = {
                str(pane_id): str(category)
                for pane_id, category in (cards_assignment.get("categories") or {}).items()
                if str(category)
            }
        for old_leader in conflicting_leaders:
            mark_superseded(old_leader, leader_id)
            claims = {key: value for key, value in claims.items() if value.get("leader_id") != old_leader}

        lease_until = time.time() + lease_seconds
        for worker in workers.values():
            claims[str(worker["pane_id"])] = {
                "leader_id": leader_id,
                "worker": worker["name"],
                "runtime_fingerprint": worker["runtime_fingerprint"],
                "lease_until": lease_until,
            }
        write_json_atomic(CLAIMS_FILE, claims)
        state = {
            "version": 1,
            "id": leader_id,
            "leader": args.leader,
            "leader_runtime": snapshots[args.leader],
            "display_mode": display_mode,
            "cards_category": cards_category,
            "cards_membership": cards_membership,
            "cards_assignment": {
                "requested_category": str(cards_assignment.get("requested_category") or ""),
                "assigned": cards_assignment.get("assigned") or [],
                "preserved": cards_assignment.get("preserved") or [],
            },
            "objective": re.sub(r"\s+", " ", args.objective).strip(),
            "acceptance": [re.sub(r"\s+", " ", value).strip() for value in args.acceptance],
            "authority": [
                re.sub(r"\s+", " ", value).strip()
                for value in (args.authority or ["dispatch", "steer", "collect", "read", "verify"])
            ],
            "task_map": task_map,
            "write_ownership": write_ownership,
            "monitor_policy": {
                "progress_deadline_seconds": max(0, args.progress_deadline),
                "hard_deadline_seconds": max(0, args.hard_deadline),
                "max_attempts_per_worker": max(1, args.max_attempts),
                "probe_interval_seconds": max(1, args.probe_interval),
            },
            "status": "running",
            "phase": "monitor",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "revision": 1,
            "event_cursor": event_ledger.current_last_event_id(),
            "lease_seconds": lease_seconds,
            "lease_until": lease_until,
            "workers": workers,
            "evidence": [],
            "last_action": {},
        }
        write_json_atomic(state_path(leader_id), state)

    event_ledger.upsert_job(
        f"leader:{leader_id}",
        source="secretary-bus-leader",
        status="running",
        target=args.leader,
        pane=str(snapshots[args.leader]["pane_id"]),
        owner_id=leader_id,
        task_preview=event_ledger.compact(args.objective, 220),
        message=(
            f"leader session created with {len(workers)} worker(s); "
            f"display={display_mode}; cards_category={cards_category or '-'}"
        ),
    )
    event_ledger.append_event(
        "leader_created",
        job_id=f"leader:{leader_id}",
        pane=str(snapshots[args.leader]["pane_id"]),
        target=args.leader,
        source="secretary-bus-leader",
        status="running",
        message=f"workers={','.join(workers)};display={display_mode};cards_category={cards_category or '-'}",
    )
    emit(public_state(state), as_json=args.json, text_line=f"leader {leader_id} created; workers={','.join(workers)}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    with locked():
        state = load_state(args.id)
    emit({**public_state(state), "deadlines": deadline_summary(state)}, as_json=args.json)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    SESSIONS.mkdir(parents=True, exist_ok=True)
    states: list[dict[str, Any]] = []
    for path in sorted(SESSIONS.glob("*.json")):
        if path.name == CLAIMS_FILE.name:
            continue
        state = read_json_strict(path)
        if isinstance(state, dict):
            states.append(
                {
                    "id": state.get("id"),
                    "status": state.get("status"),
                    "phase": state.get("phase"),
                    "leader": state.get("leader"),
                    "workers": list((state.get("workers") or {}).keys()),
                    "updated_at": state.get("updated_at"),
                }
            )
    emit({"leaders": states}, as_json=args.json)
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    """Enumerate all panes in this Unix user's tmux server without registering them."""
    fmt = "#{session_name}\t#{window_index}\t#{window_name}\t#{pane_index}\t#{pane_id}"
    cp = cli_bridge.tmux("list-panes", "-a", "-F", fmt, check=False)
    if cp.returncode != 0:
        raise SystemExit(cp.stderr.strip() or "tmux list-panes failed")
    names_by_pane: dict[str, list[str]] = {}
    for name, target in cli_bridge.load_targets().items():
        pane_id = target.pane_id
        if pane_id:
            names_by_pane.setdefault(pane_id, []).append(name)
    panes: list[dict[str, Any]] = []
    for line in cp.stdout.splitlines():
        if not line:
            continue
        try:
            session, window_index, window_name, pane_index, pane_id = line.split("\t", 4)
        except ValueError as exc:
            raise SystemExit(f"unexpected tmux discovery row: {line!r}") from exc
        info = cli_bridge.pane_info(pane_id)
        panes.append(
            {
                "session": session,
                "window_index": int(window_index),
                "window_name": window_name,
                "pane_index": int(pane_index),
                "pane": str(info["pane"]),
                "pane_id": str(info["pane_id"]),
                "pane_pid": int(info["pane_pid"]),
                "foreground_pid": int(info["foreground_pid"]),
                "command": str(info["command"]),
                "cwd": str(info["cwd"]),
                "title": str(info["title"]),
                "runtime_fingerprint": runtime_fingerprint(info),
                "registered_names": sorted(names_by_pane.get(str(info["pane_id"]), [])),
            }
        )
    payload = {"panes": panes, "count": len(panes)}
    emit(payload, as_json=args.json)
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    trusted = (args.trusted_owner == "true") if args.trusted_owner else None
    auto_approve = (args.auto_approve_permissions == "true") if args.auto_approve_permissions else None
    if trusted is None and auto_approve is None:
        config = bus_config.load_config()
    else:
        config = bus_config.update_config(trusted_owner=trusted, auto_approve_permissions=auto_approve)
    emit(config, as_json=args.json)
    return 0


def ensure_running_and_owned(state: dict[str, Any], worker_name: str) -> dict[str, Any]:
    if str(state.get("status")) != "running":
        raise SystemExit(f"leader session is not running: {state.get('status')}")
    workers = state.get("workers") or {}
    if worker_name not in workers:
        raise SystemExit(f"worker is outside leader membership: {worker_name}")
    worker = workers[worker_name]
    if worker.get("retired"):
        raise SystemExit(f"worker is retired and cannot receive further controls: {worker_name}")
    claims = active_claims(load_claims())
    claim = claims.get(str(worker["pane_id"]))
    if not claim or claim.get("leader_id") != state["id"]:
        raise SystemExit(
            f"worker claim lost or expired: {worker_name}; run `secretary-bus leader doctor {state['id']}` "
            f"before sending; {abandon_hint(str(state['id']), worker_name)}"
        )
    try:
        current = runtime_snapshot(worker_name)
    except SystemExit as exc:
        raise SystemExit(f"{exc}\n{abandon_hint(str(state['id']), worker_name)}") from None
    if (
        current["pane_id"] != worker["pane_id"]
        or current["runtime_fingerprint"] != worker["runtime_fingerprint"]
    ):
        raise SystemExit(
            f"worker runtime changed since leader activation: {worker_name}; "
            f"{abandon_hint(str(state['id']), worker_name)}"
        )
    return worker


def prepare_action(
    state: dict[str, Any],
    *,
    worker: dict[str, Any],
    kind: str,
    action_key: str,
    payload_hash: str,
) -> tuple[dict[str, Any], bool]:
    """Persist a recovery-safe action claim before touching tmux."""
    key = require_id(action_key, "action key")
    actions = state.setdefault("actions", {})
    previous = actions.get(key)
    if isinstance(previous, dict) and previous.get("status") in {"prepared", "sent", "sent_unverified"}:
        contract = (previous.get("kind"), previous.get("worker"), previous.get("payload_hash"))
        wanted = (kind, worker["name"], payload_hash)
        if contract != wanted:
            raise SystemExit(f"action key collision with a different control contract: {key}")
        return previous, True
    action = {
        "key": key,
        "kind": kind,
        "worker": worker["name"],
        "pane_id": worker["pane_id"],
        "runtime_fingerprint": worker["runtime_fingerprint"],
        "payload_hash": payload_hash,
        "status": "prepared",
        "at": now_iso(),
    }
    actions[key] = action
    state["last_action"] = action
    save_state(state)
    return action, False


def finish_action(state: dict[str, Any], action_key: str, status: str) -> dict[str, Any]:
    action = (state.get("actions") or {}).get(action_key)
    if not isinstance(action, dict):
        raise SystemExit(f"prepared action disappeared: {action_key}")
    action["status"] = status
    action["finished_at"] = now_iso()
    state["last_action"] = action
    save_state(state)
    return action


def control_keys(
    args: argparse.Namespace,
    *,
    kind: str,
    keys: list[str],
) -> int:
    if not keys or len(keys) > 32 or any(not value or len(value) > 100 or "\x00" in value for value in keys):
        raise SystemExit("keys must contain 1-32 non-empty tmux key names of at most 100 characters")
    send, _allow_newline = execution_options(args)
    payload_hash = hashlib.sha256("\0".join(keys).encode("utf-8")).hexdigest()[:16]
    action_key = args.action_key or event_ledger.new_job_id(f"{kind}-{args.worker}")
    with locked():
        state = load_state(args.id)
        worker = ensure_running_and_owned(state, args.worker)
        action, duplicate = prepare_action(
            state,
            worker=worker,
            kind=kind,
            action_key=action_key,
            payload_hash=payload_hash,
        )
        if duplicate:
            payload = {
                "leader_id": args.id,
                "worker": args.worker,
                "kind": kind,
                "keys": keys,
                "action_key": action_key,
                "sent": action.get("status") == "sent",
                "duplicate": True,
            }
            emit(payload, as_json=args.json)
            return 0
        try:
            if send:
                cli_bridge.tmux("send-keys", "-t", str(worker["pane_id"]), *keys)
            finish_action(state, action_key, "sent" if send else "dry_run")
        except BaseException:
            finish_action(state, action_key, "failed")
            raise
    event_ledger.append_event(
        "leader_control",
        job_id=str(worker.get("job_ids", [""])[-1]) if worker.get("job_ids") else f"leader:{args.id}",
        pane=str(worker["pane_id"]),
        target=args.worker,
        source="secretary-bus-leader",
        message=f"{kind} {'sent' if send else 'dry-run'}",
        data={"leader_id": args.id, "action_key": action_key, "keys": keys},
    )
    payload = {
        "leader_id": args.id,
        "worker": args.worker,
        "kind": kind,
        "keys": keys,
        "action_key": action_key,
        "sent": send,
        "duplicate": False,
    }
    emit(payload, as_json=args.json)
    return 0


def cmd_keys(args: argparse.Namespace) -> int:
    return control_keys(args, kind="keys", keys=args.key)


def cmd_approve(args: argparse.Namespace) -> int:
    if args.key:
        return control_keys(args, kind="approve", keys=args.key)
    return approve_by_policy(args)


def pane_capture(pane_id: str) -> str:
    """The visible screen of one pane (what a dialog is drawn on)."""
    cp = cli_bridge.tmux("capture-pane", "-p", "-t", pane_id, check=False)
    if cp.returncode != 0:
        raise SystemExit(cp.stderr.strip() or f"capture-pane failed: {pane_id}")
    return cp.stdout


def pane_sender(pane_id: str) -> Any:
    return lambda key: cli_bridge.tmux("send-keys", "-t", pane_id, key)


def first_line(text: str) -> str:
    return next((line.strip() for line in str(text or "").splitlines() if line.strip()), "")


def approve_by_policy(args: argparse.Namespace) -> int:
    """Answer the worker's open dialog by the standing policy, verifying each key.

    Only permission-type dialogs are answered (dialogs.auto_answer_index); a
    question about the work is refused so the user decides it.  Nothing is
    sent when no dialog can be read or the dialog changes while driving.
    """
    send, _allow_newline = execution_options(args)
    with locked():
        state = load_state(args.id)
        worker = ensure_running_and_owned(state, args.worker)
        pane_id = str(worker["pane_id"])
        previous = (state.get("actions") or {}).get(args.action_key) if args.action_key else None
        if isinstance(previous, dict) and previous.get("status") in {"prepared", "sent", "sent_unverified"}:
            if (previous.get("kind"), previous.get("worker")) != ("approve", args.worker):
                raise SystemExit(f"action key collision with a different control contract: {args.action_key}")
            emit(
                {
                    "leader_id": args.id,
                    "worker": args.worker,
                    "kind": "approve",
                    "action_key": args.action_key,
                    "sent": previous.get("status") == "sent",
                    "duplicate": True,
                },
                as_json=args.json,
            )
            return 0
        dialog = dialogs.read_dialog(pane_capture(pane_id))
        if dialog is None:
            raise SystemExit(
                f"no dialog can be read on worker {args.worker} ({pane_id}); nothing sent. "
                "To press raw keys deliberately use `leader approve --key KEY` or `leader keys`."
            )
        index = dialogs.auto_answer_index(dialog)
        if index is None:
            options = " | ".join(dialog.options)
            if dialog.kind == "question":
                raise SystemExit(
                    f"the dialog on worker {args.worker} is a question about the work; hand it to the user, "
                    f"nothing sent.\nquestion: {dialog.question}\noptions: {options}"
                )
            raise SystemExit(
                f"no option of this {dialog.kind} dialog matches the standing policy; nothing sent.\n"
                f"question: {dialog.question}\noptions: {options}"
            )
        answer = dialog.options[index]
        payload_hash = hashlib.sha256(f"policy\0{dialog.question}\0{answer}".encode("utf-8")).hexdigest()[:16]
        action_key = args.action_key or event_ledger.new_job_id(f"approve-{args.worker}")
        prepare_action(state, worker=worker, kind="approve", action_key=action_key, payload_hash=payload_hash)
        try:
            if send:
                dialogs.drive(
                    answer,
                    capture=lambda: pane_capture(pane_id),
                    send=pane_sender(pane_id),
                    pane_id=pane_id,
                    tmux=cli_bridge.tmux_query,
                )
            finish_action(state, action_key, "sent" if send else "dry_run")
        except (ValueError, RuntimeError) as exc:
            finish_action(state, action_key, "failed")
            raise SystemExit(f"approve stopped without confirming the dialog: {exc}") from None
        except BaseException:
            finish_action(state, action_key, "failed")
            raise
    detail = {"dialog_kind": dialog.kind, "question": dialog.headline, "prompt": dialog.question, "answer": answer}
    event_ledger.append_event(
        "leader_control",
        job_id=str(worker.get("job_ids", [""])[-1]) if worker.get("job_ids") else f"leader:{args.id}",
        pane=pane_id,
        target=args.worker,
        source=LEADER_SOURCE,
        message=f"approve {'sent' if send else 'dry-run'} by policy: {dialog.kind} -> {answer}",
        data={"leader_id": args.id, "action_key": action_key, **detail},
    )
    emit(
        {
            "leader_id": args.id,
            "worker": args.worker,
            "kind": "approve",
            "action_key": action_key,
            "sent": send,
            "duplicate": False,
            **detail,
        },
        as_json=args.json,
    )
    return 0


def interrupt_observation(worker: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Observe a bounded post-Ctrl-C idle/stopped signal without guessing history."""
    name = str(worker["name"])
    try:
        snapshot = provider_state.snapshot_target(name, max_chars=320)
        provider_value = str((snapshot.get("state") or {}).get("value") or "unknown")
        if provider_value == "idle":
            return True, {
                "source": str((snapshot.get("state") or {}).get("source") or "provider_state"),
                "state": provider_value,
                "confidence": str((snapshot.get("state") or {}).get("confidence") or ""),
            }
    except SystemExit as exc:
        provider_value = "unknown"
        provider_error = event_ledger.compact(str(exc), 240)
    else:
        provider_error = ""

    try:
        info = cli_bridge.pane_info(str(worker["pane_id"]))
    except SystemExit as exc:
        return True, {
            "source": "tmux_runtime",
            "state": "pane_missing",
            "confidence": "high",
            "detail": event_ledger.compact(str(exc), 240),
        }
    if str(info.get("command") or "") != str(worker.get("expected_command") or ""):
        return True, {
            "source": "tmux_runtime",
            "state": "original_command_exited",
            "confidence": "high",
            "detail": f"expected={worker.get('expected_command')} actual={info.get('command')}",
        }
    cp = cli_bridge.tmux(
        "capture-pane", "-p", "-t", str(worker["pane_id"]), "-S", "-20", check=False
    )
    if cp.returncode == 0:
        lines = [line.rstrip() for line in cp.stdout.splitlines() if line.strip()]
        shell_prompt = re.compile(r"(?:^|\s)(?:bash-[^\s]+|[A-Za-z0-9_.@:/~+-]+)?[$#>]\s*$")
        if any(shell_prompt.search(line) for line in lines[-4:]):
            return True, {
                "source": "tmux_live_tail",
                "state": "shell_prompt",
                "confidence": "medium",
            }
    observation = {
        "source": "provider_state" if provider_value != "unknown" else "tmux_runtime",
        "state": provider_value,
        "confidence": "low",
    }
    if provider_error:
        observation["detail"] = provider_error
    return False, observation


def wait_for_interrupt(worker: dict[str, Any], *, timeout: float, interval: float) -> tuple[bool, dict[str, Any]]:
    deadline = time.time() + max(0.0, timeout)
    while True:
        verified, observation = interrupt_observation(worker)
        if verified or time.time() >= deadline:
            return verified, observation
        time.sleep(max(0.05, interval))


def cmd_interrupt(args: argparse.Namespace) -> int:
    send, _allow_newline = execution_options(args)
    action_key = args.action_key or event_ledger.new_job_id(f"interrupt-{args.worker}")
    payload_hash = hashlib.sha256(b"C-c").hexdigest()[:16]
    with locked():
        state = load_state(args.id)
        worker = ensure_running_and_owned(state, args.worker)
        action, duplicate = prepare_action(
            state,
            worker=worker,
            kind="interrupt",
            action_key=action_key,
            payload_hash=payload_hash,
        )
        job_id = str(worker.get("job_ids", [])[-1]) if worker.get("job_ids") else ""
        if duplicate:
            verified = action.get("status") == "sent"
            payload = {
                "leader_id": args.id,
                "worker": args.worker,
                "job_id": job_id,
                "action_key": action_key,
                "sent": action.get("status") in {"sent", "sent_unverified"},
                "verified": verified,
                "duplicate": True,
            }
            emit(payload, as_json=args.json)
            return 0 if verified else 1
        verified = False
        observation: dict[str, Any] = {"source": "dry_run", "state": "not_sent", "confidence": "high"}
        try:
            if send:
                cli_bridge.tmux("send-keys", "-t", str(worker["pane_id"]), "C-c")
                verified, observation = wait_for_interrupt(
                    worker,
                    timeout=max(0.0, args.wait),
                    interval=max(0.05, args.interval),
                )
                if verified and job_id:
                    event_ledger.upsert_job(
                        job_id,
                        source="secretary-bus-leader",
                        status="interrupted",
                        completed_at=now_iso(),
                        message=f"interrupted by leader {args.id}",
                    )
                    worker["status"] = "interrupted"
            finish_action(
                state,
                action_key,
                "sent" if send and verified else ("sent_unverified" if send else "dry_run"),
            )
        except BaseException:
            finish_action(state, action_key, "failed")
            raise
    event_ledger.append_event(
        "leader_interrupt",
        job_id=job_id or f"leader:{args.id}",
        pane=str(worker["pane_id"]),
        target=args.worker,
        source="secretary-bus-leader",
        message=(
            "Ctrl-C verified"
            if send and verified
            else ("Ctrl-C sent but stop was not verified" if send else "interrupt dry-run")
        ),
        data={"leader_id": args.id, "action_key": action_key, "verified": verified, "observation": observation},
    )
    emit(
        {
            "leader_id": args.id,
            "worker": args.worker,
            "job_id": job_id,
            "action_key": action_key,
            "sent": send,
            "verified": verified,
            "observation": observation,
            "duplicate": False,
        },
        as_json=args.json,
    )
    return 1 if send and not verified else 0


def refresh_registered_target(name: str, info: dict[str, Any], expected_command: str) -> None:
    with cli_bridge.targets_locked():
        targets = cli_bridge.load_targets()
        if name not in targets:
            raise SystemExit(f"registered target disappeared during restart: {name}")
        old = targets[name]
        targets[name] = cli_bridge.Target(
            name=name,
            pane=str(info["pane"]),
            expected_command=expected_command,
            note=old.note,
            pane_id=str(info["pane_id"]),
            pane_pid=int(info["pane_pid"]),
            pane_start_time=str(info["pane_start_time"]),
            foreground_pid=int(info["foreground_pid"]),
            foreground_start_time=str(info["foreground_start_time"]),
            shell=old.shell,
        )
        cli_bridge.save_targets(targets)


def require_owned_claim_without_runtime(state: dict[str, Any], worker_name: str) -> dict[str, Any]:
    """Validate authority while allowing a prepared restart to rotate runtime."""
    if str(state.get("status")) != "running":
        raise SystemExit(f"leader session is not running: {state.get('status')}")
    worker = (state.get("workers") or {}).get(worker_name)
    if not isinstance(worker, dict):
        raise SystemExit(f"worker is outside leader membership: {worker_name}")
    if worker.get("retired"):
        raise SystemExit(f"worker is retired and cannot receive further controls: {worker_name}")
    claim = active_claims(load_claims()).get(str(worker["pane_id"]))
    if not claim or claim.get("leader_id") != state["id"]:
        raise SystemExit(
            f"worker claim lost or expired: {worker_name}; run `secretary-bus leader doctor {state['id']}` "
            f"before sending; {abandon_hint(str(state['id']), worker_name)}"
        )
    if str(claim.get("runtime_fingerprint") or "") != str(worker.get("runtime_fingerprint") or ""):
        raise SystemExit(f"worker claim fingerprint changed before restart recovery: {worker_name}")
    return worker


def adopt_restarted_runtime(
    state: dict[str, Any],
    worker: dict[str, Any],
    *,
    info: dict[str, Any],
    expected_command: str,
    job_id: str,
    message: str,
) -> None:
    if str(info.get("pane_id") or "") != str(worker.get("pane_id") or ""):
        raise SystemExit("restart recovery resolved a different pane id; refusing to adopt it")
    if str(info.get("command") or "") != expected_command:
        raise SystemExit(
            f"restart recovery found unexpected command: expected={expected_command} actual={info.get('command')}"
        )
    refresh_registered_target(str(worker["name"]), info, expected_command)
    fresh = snapshot_from_info(str(worker["name"]), info, expected_command)
    worker.update(fresh)
    worker["status"] = "restarted"
    worker["restart_count"] = int(worker.get("restart_count") or 0) + 1
    if job_id:
        event_ledger.upsert_job(
            job_id,
            source="secretary-bus-leader",
            status="interrupted",
            completed_at=now_iso(),
            message=message,
        )
    claims = load_claims()
    claim = claims.get(str(worker["pane_id"]), {})
    if claim.get("leader_id") not in {None, "", state["id"]}:
        raise SystemExit(f"worker claim changed during restart recovery: {worker['name']}")
    claim.update(
        {
            "leader_id": state["id"],
            "worker": worker["name"],
            "runtime_fingerprint": worker["runtime_fingerprint"],
            "lease_until": state.get("lease_until", time.time() + DEFAULT_LEASE_SECONDS),
        }
    )
    claims[str(worker["pane_id"])] = claim
    write_json_atomic(CLAIMS_FILE, claims)


def cmd_restart(args: argparse.Namespace) -> int:
    send, _allow_newline = execution_options(args)
    action_key = require_id(
        args.action_key or event_ledger.new_job_id(f"restart-{args.worker}"), "action key"
    )
    reconciled = False
    duplicate = False
    with locked():
        state = load_state(args.id)
        worker = require_owned_claim_without_runtime(state, args.worker)
        target = target_or_die(args.worker)
        command = args.command or target.expected_command
        expected_command = args.expected_command or target.expected_command
        cwd = str(Path(args.cwd or str(worker["cwd"])).expanduser().resolve())
        payload_hash = hashlib.sha256(
            f"{command}\0{expected_command}\0{cwd}".encode("utf-8")
        ).hexdigest()[:16]
        job_id = str(worker.get("job_ids", [])[-1]) if worker.get("job_ids") else ""
        previous = (state.get("actions") or {}).get(action_key)
        if isinstance(previous, dict) and previous.get("status") in {"prepared", "sent"}:
            contract = (previous.get("kind"), previous.get("worker"), previous.get("payload_hash"))
            if contract != ("restart", args.worker, payload_hash):
                raise SystemExit(f"action key collision with a different control contract: {action_key}")
            if previous.get("status") == "sent":
                ensure_running_and_owned(state, args.worker)
                duplicate = True
            else:
                info = cli_bridge.pane_info(str(worker["pane_id"]))
                current_fingerprint = runtime_fingerprint(info)
                if current_fingerprint != str(worker["runtime_fingerprint"]):
                    if not send:
                        raise SystemExit(
                            "prepared restart already changed runtime; omit --dry-run to reconcile durable state"
                        )
                    adopt_restarted_runtime(
                        state,
                        worker,
                        info=info,
                        expected_command=expected_command,
                        job_id=job_id,
                        message=f"runtime restart reconciled by leader {args.id}",
                    )
                    finish_action(state, action_key, "sent")
                    reconciled = True
                    duplicate = True
                else:
                    # The prepared record landed but the side effect did not.
                    # Reuse that durable action claim and perform one respawn.
                    ensure_running_and_owned(state, args.worker)
        else:
            worker = ensure_running_and_owned(state, args.worker)
            _action, duplicate = prepare_action(
                state,
                worker=worker,
                kind="restart",
                action_key=action_key,
                payload_hash=payload_hash,
            )
        if duplicate and not reconciled:
            emit(
                {
                    "leader_id": args.id,
                    "worker": args.worker,
                    "job_id": job_id,
                    "action_key": action_key,
                    "sent": True,
                    "duplicate": True,
                    "reconciled": False,
                },
                as_json=args.json,
            )
            return 0
        respawned = reconciled
        try:
            if send and not reconciled:
                cli_bridge.tmux(
                    "respawn-pane",
                    "-k",
                    "-t",
                    str(worker["pane_id"]),
                    "-c",
                    cwd,
                    command,
                )
                respawned = True
                if job_id:
                    event_ledger.upsert_job(
                        job_id,
                        source="secretary-bus-leader",
                        status="interrupted",
                        completed_at=now_iso(),
                        message=f"runtime restarted by leader {args.id}",
                    )
                deadline = time.time() + max(0.2, args.wait)
                info: dict[str, Any] | None = None
                while time.time() <= deadline:
                    try:
                        candidate = cli_bridge.pane_info(str(worker["pane_id"]))
                    except SystemExit:
                        time.sleep(0.05)
                        continue
                    if str(candidate["command"]) == expected_command:
                        info = candidate
                        break
                    time.sleep(0.05)
                if info is None:
                    raise SystemExit(
                        f"restarted pane did not reach expected command within {args.wait}s: {expected_command}"
                    )
                adopt_restarted_runtime(
                    state,
                    worker,
                    info=info,
                    expected_command=expected_command,
                    job_id=job_id,
                    message=f"runtime restarted by leader {args.id}",
                )
            if not reconciled:
                finish_action(state, action_key, "sent" if send else "dry_run")
        except BaseException:
            if respawned and job_id:
                event_ledger.upsert_job(
                    job_id,
                    source="secretary-bus-leader",
                    status="interrupted",
                    completed_at=now_iso(),
                    message=f"runtime restart did not complete cleanly under leader {args.id}",
                )
            finish_action(state, action_key, "failed")
            raise
    event_ledger.append_event(
        "leader_restart",
        job_id=job_id or f"leader:{args.id}",
        pane=str(worker["pane_id"]),
        target=args.worker,
        source="secretary-bus-leader",
        message=("worker runtime restart reconciled" if reconciled else ("worker runtime restarted" if send else "restart dry-run")),
        data={
            "leader_id": args.id,
            "action_key": action_key,
            "command": command,
            "reconciled": reconciled,
        },
    )
    emit(
        {
            "leader_id": args.id,
            "worker": args.worker,
            "job_id": job_id,
            "action_key": action_key,
            "sent": send,
            "duplicate": duplicate,
            "reconciled": reconciled,
            "runtime_fingerprint": worker["runtime_fingerprint"] if send else "",
        },
        as_json=args.json,
    )
    return 0


def cmd_kill(args: argparse.Namespace) -> int:
    send, _allow_newline = execution_options(args)
    action_key = args.action_key or event_ledger.new_job_id(f"kill-{args.worker}")
    payload_hash = hashlib.sha256(b"kill-pane").hexdigest()[:16]
    with locked():
        state = load_state(args.id)
        if str(state.get("status")) != "running":
            raise SystemExit(f"leader session is not running: {state.get('status')}")
        worker = (state.get("workers") or {}).get(args.worker)
        if not isinstance(worker, dict):
            raise SystemExit(f"worker is outside leader membership: {args.worker}")
        previous = (state.get("actions") or {}).get(action_key)
        if isinstance(previous, dict) and previous.get("status") in {"prepared", "sent"}:
            contract = (previous.get("kind"), previous.get("worker"), previous.get("payload_hash"))
            if contract != ("kill", args.worker, payload_hash):
                raise SystemExit(f"action key collision with a different control contract: {action_key}")
            emit(
                {
                    "leader_id": args.id,
                    "worker": args.worker,
                    "job_id": str(worker.get("job_ids", [])[-1]) if worker.get("job_ids") else "",
                    "action_key": action_key,
                    "sent": previous.get("status") == "sent",
                    "duplicate": True,
                },
                as_json=args.json,
            )
            return 0
        worker = ensure_running_and_owned(state, args.worker)
        _action, _duplicate = prepare_action(
            state,
            worker=worker,
            kind="kill",
            action_key=action_key,
            payload_hash=payload_hash,
        )
        job_id = str(worker.get("job_ids", [])[-1]) if worker.get("job_ids") else ""
        killed = False
        try:
            if send:
                cli_bridge.tmux("kill-pane", "-t", str(worker["pane_id"]))
                killed = True
                if job_id:
                    event_ledger.upsert_job(
                        job_id,
                        source="secretary-bus-leader",
                        status="interrupted",
                        completed_at=now_iso(),
                        message=f"pane killed by leader {args.id}",
                    )
                worker["status"] = "killed"
                worker["retired"] = True
                worker["retired_at"] = now_iso()
                claims = load_claims()
                claims.pop(str(worker["pane_id"]), None)
                write_json_atomic(CLAIMS_FILE, claims)
                with cli_bridge.targets_locked():
                    targets = cli_bridge.load_targets()
                    targets.pop(args.worker, None)
                    cli_bridge.save_targets(targets)
            finish_action(state, action_key, "sent" if send else "dry_run")
        except BaseException:
            if killed:
                worker["status"] = "killed"
                worker["retired"] = True
                worker["retired_at"] = now_iso()
                if job_id:
                    event_ledger.upsert_job(
                        job_id,
                        source="secretary-bus-leader",
                        status="interrupted",
                        completed_at=now_iso(),
                        message=f"pane killed by leader {args.id}; cleanup incomplete",
                    )
            finish_action(state, action_key, "failed")
            raise
    event_ledger.append_event(
        "leader_kill",
        job_id=job_id or f"leader:{args.id}",
        pane=str(worker["pane_id"]),
        target=args.worker,
        source="secretary-bus-leader",
        message="worker pane killed" if send else "kill dry-run",
        data={"leader_id": args.id, "action_key": action_key},
    )
    emit(
        {
            "leader_id": args.id,
            "worker": args.worker,
            "job_id": job_id,
            "action_key": action_key,
            "sent": send,
            "duplicate": False,
        },
        as_json=args.json,
    )
    return 0


def cmd_assign(args: argparse.Namespace) -> int:
    leader_id = require_id(args.id)
    send, allow_newline = execution_options(args)
    task = supervisor.read_task_source(args.task, getattr(args, "task_file", None))
    with locked():
        state = load_state(leader_id)
        worker = ensure_running_and_owned(state, args.worker)
        if not send and bool(getattr(args, "dry_run", False)):
            repo = Path(args.repo).resolve() if args.repo else Path(str(worker["cwd"])).resolve()
            pane_cwd = Path(str(worker["cwd"])).resolve()
            if args.repo and not args.allow_pane_cwd_mismatch and not supervisor.is_within(pane_cwd, repo):
                raise SystemExit(
                    "refusing to preview dispatch: target pane cwd is not inside requested repo\n"
                    f"  target={args.worker} pane={worker['pane']} command={worker['expected_command']}\n"
                    f"  pane_cwd={pane_cwd}\n"
                    f"  repo={repo}\n"
                    "Use a pane/worker already in the target repo, or pass "
                    "--allow-pane-cwd-mismatch only after explicitly verifying the receiver context."
                )
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                # No job exists yet, so a long task previews through the
                # shared spill directory instead of a job directory.
                cli_bridge.send_to_target(
                    target_or_die(args.worker),
                    cli_bridge.inline_or_spill(task, label="task"),
                    enter=True,
                    yes=False,
                    allow_newline=allow_newline,
                )
            payload = {
                "leader_id": leader_id,
                "worker": args.worker,
                "job_id": "",
                "sent": False,
                "preview": True,
            }
            if args.json:
                emit(payload, as_json=True)
            else:
                print(captured.getvalue().rstrip())
                print("leader assignment previewed; no job or attempt was created")
            return 0
        for old_id in worker.get("job_ids", []):
            old_job = event_ledger.get_job(str(old_id))
            if old_job and event_ledger.normalize_status(str(old_job.get("status") or "")) not in event_ledger.TERMINAL_STATUSES:
                raise SystemExit(
                    f"worker {args.worker} already has active job {old_id}; steer it or close that attempt first"
                )
        max_attempts = int((state.get("monitor_policy") or {}).get("max_attempts_per_worker") or 3)
        if len(worker.get("job_ids", [])) >= max_attempts:
            raise SystemExit(
                f"worker {args.worker} reached max attempts ({max_attempts}); revise the leader contract before retrying"
            )
        suffix = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S-%f")
        safe_worker = re.sub(r"[^A-Za-z0-9_.-]+", "_", args.worker).strip("._-") or "worker"
        job_id = f"lead-{leader_id}-{safe_worker}-{suffix}"
        worker.setdefault("job_ids", []).append(job_id)
        worker["status"] = "dispatching"
        worker["last_progress_at"] = time.time()
        worker.pop("stall_notified_for", None)
        worker["task_hash"] = hashlib.sha256(task.encode("utf-8")).hexdigest()[:16]
        save_state(state)

    # supervisor.cmd_start validates the text it will type (and spills a long
    # task into the job directory) before it creates the ledger job; a
    # rejected task therefore leaves no job and the rollback below drops the
    # attempt, so it never counts against max_attempts.
    supervisor_args = argparse.Namespace(
        target=args.worker,
        task=task,
        task_file=None,
        repo=args.repo,
        id=job_id,
        wait=0,
        history=args.history,
        allow_newline=allow_newline,
        allow_pane_cwd_mismatch=args.allow_pane_cwd_mismatch,
        yes=send,
    )
    captured = io.StringIO()
    try:
        # Reacquire and hold the leader/claim lock across the actual send. This
        # serializes takeover against dispatch: either this leader sends while
        # it still owns the worker, or takeover wins first and this check fails.
        with locked():
            state = load_state(leader_id)
            worker = ensure_running_and_owned(state, args.worker)
            if job_id not in worker.get("job_ids", []):
                raise SystemExit(f"prepared assignment disappeared before send: {job_id}")
            with contextlib.redirect_stdout(captured):
                supervisor.cmd_start(supervisor_args)
            dispatched_job = event_ledger.get_job(job_id)
            worker["status"] = (
                event_ledger.normalize_status(str((dispatched_job or {}).get("status") or "sent"))
                if send
                else "created"
            )
            save_state(state)
    except BaseException:
        with locked():
            state = load_state(leader_id)
            worker = (state.get("workers") or {}).get(args.worker, {})
            failed_job = event_ledger.get_job(job_id)
            if failed_job:
                # The supervisor reached durable job creation; preserve the
                # failed attempt so the leader can audit it and choose a new
                # successor instead of silently orphaning the failure.
                worker["status"] = event_ledger.normalize_status(str(failed_job.get("status") or "failed"))
            else:
                worker["job_ids"] = [value for value in worker.get("job_ids", []) if value != job_id]
                worker["status"] = "unassigned" if not worker["job_ids"] else worker.get("status", "running")
            save_state(state)
        raise

    payload = {"leader_id": leader_id, "worker": args.worker, "job_id": job_id, "sent": send}
    if args.json:
        emit(payload, as_json=True)
    else:
        print(captured.getvalue().rstrip())
        print(f"leader assignment recorded: {job_id}")
    return 0


def compact_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        key: event.get(key)
        for key in ("id", "ts", "kind", "job_id", "target", "status")
        if event.get(key) not in (None, "")
    }


def renew_claims(state: dict[str, Any]) -> list[str]:
    raw_claims = load_claims()
    active = active_claims(raw_claims)
    # Copies: raw_claims must stay as read so the change check below sees a renewal.
    claims = {key: dict(value) for key, value in durable_claims(raw_claims).items()}
    issues: list[str] = []
    lease_seconds = int(state.get("lease_seconds") or DEFAULT_LEASE_SECONDS)
    lease_until = time.time() + lease_seconds
    # A lease is only extended once it has aged by the slack.  Renewing on
    # every 1.5s leaderd tick rewrote claims.json and the leader state each
    # time for no change in ownership.
    renew_slack = min(60.0, lease_seconds / 4)
    renewed = False
    for worker in (state.get("workers") or {}).values():
        pane_id = str(worker["pane_id"])
        if worker.get("retired"):
            if claims.get(pane_id, {}).get("leader_id") == state["id"]:
                claims.pop(pane_id, None)
            continue
        claim = active.get(pane_id)
        if claim and claim.get("leader_id") != state["id"]:
            issues.append(f"claim_lost:{worker['name']}")
            continue
        if claim and float(claim.get("lease_until") or 0) >= lease_until - renew_slack:
            continue
        # A paused monitor may wake after its lease expires. If no newer leader
        # claimed the pane, atomically reacquire it from the durable session
        # state instead of stranding the original leader. A real takeover has
        # already marked the old session superseded, so it never reaches here.
        if not claim:
            previous = raw_claims.get(pane_id)
            if (
                not previous
                or previous.get("leader_id") != state["id"]
                or previous.get("runtime_fingerprint") != worker["runtime_fingerprint"]
            ):
                issues.append(f"claim_lost:{worker['name']}")
                continue
            claim = {
                "leader_id": state["id"],
                "worker": worker["name"],
                "runtime_fingerprint": worker["runtime_fingerprint"],
            }
        claim = {**claim, "lease_until": lease_until}
        claims[pane_id] = claim
        renewed = True
    if renewed and not issues:
        state["lease_until"] = lease_until
    if claims != raw_claims:
        write_json_atomic(CLAIMS_FILE, claims)
    return issues


def stable_issue_key(issue: str) -> str:
    """Issue identity for change detection: volatile numbers (pids, pane ids) masked."""
    kind, _, rest = issue.partition(":")
    if kind != "runtime":
        return issue
    name, _, detail = rest.partition(":")
    return f"runtime:{name}:{re.sub(r'[0-9]+', '#', detail)[:160]}"


def iso_epoch(value: Any) -> float:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return 0.0


def epoch_iso(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).astimezone().isoformat(timespec="seconds")


def deadline_summary(state: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Deadline view for status/doctor; detection and events happen in tick."""
    now = time.time() if now is None else now
    policy = state.get("monitor_policy") or {}
    hard = int(policy.get("hard_deadline_seconds") or 0)
    progress = int(policy.get("progress_deadline_seconds") or 0)
    summary: dict[str, Any] = {"hard_deadline_seconds": hard, "progress_deadline_seconds": progress}
    created = iso_epoch(state.get("created_at"))
    if hard > 0 and created:
        summary["hard_deadline_at"] = epoch_iso(created + hard)
        summary["hard_deadline_exceeded"] = now >= created + hard
    if state.get("deadline_exceeded_at"):
        summary["deadline_exceeded_event_at"] = state["deadline_exceeded_at"]
    stalled: dict[str, Any] = {}
    for name, worker in (state.get("workers") or {}).items():
        last = worker.get("stall_notified_for")
        if worker.get("retired") or last is None or last != worker.get("last_progress_at"):
            continue
        stalled[name] = {"last_progress_at": epoch_iso(float(last)), "seconds_without_progress": int(now - float(last))}
    summary["stalled_workers"] = stalled
    return summary


def check_hard_deadline(state: dict[str, Any], now: float) -> dict[str, Any] | None:
    """Emit ``leader_deadline_exceeded`` once per leader; never acts on workers."""
    hard = int((state.get("monitor_policy") or {}).get("hard_deadline_seconds") or 0)
    created = iso_epoch(state.get("created_at"))
    if hard <= 0 or not created or now < created + hard or state.get("deadline_exceeded_at"):
        return None
    state["deadline_exceeded_at"] = now_iso()
    message = (
        f"hard deadline of {hard}s since {state.get('created_at')} passed; "
        "the leader decides: interrupt/restart/kill a stuck worker, or close blocked/failed"
    )
    event_ledger.append_event(
        "leader_deadline_exceeded",
        job_id=f"leader:{state['id']}",
        target=str(state.get("leader") or ""),
        source=LEADER_SOURCE,
        message=message,
        data={"leader_id": state["id"], "hard_deadline_seconds": hard, "deadline_at": epoch_iso(created + hard)},
    )
    return {"kind": "leader_deadline_exceeded", "excerpt": message}


def check_progress_stall(
    state: dict[str, Any], name: str, worker: dict[str, Any], job_id: str, now: float
) -> dict[str, Any] | None:
    """Emit ``leader_progress_stalled`` once each time a worker crosses the progress deadline."""
    limit = int((state.get("monitor_policy") or {}).get("progress_deadline_seconds") or 0)
    last = float(worker.get("last_progress_at") or now)
    if limit <= 0 or now - last < limit or worker.get("stall_notified_for") == worker.get("last_progress_at"):
        return None
    worker["stall_notified_for"] = worker.get("last_progress_at")
    message = f"no progress from {name} for {int(now - last)}s (progress deadline {limit}s) on {job_id}"
    event_ledger.append_event(
        "leader_progress_stalled",
        job_id=f"leader:{state['id']}",
        pane=str(worker.get("pane_id") or ""),
        target=name,
        source=LEADER_SOURCE,
        message=message,
        data={"leader_id": state["id"], "worker_job_id": job_id, "last_progress_at": epoch_iso(last)},
    )
    return {"worker": name, "kind": "leader_progress_stalled", "excerpt": message}


def answer_worker_prompt(state: dict[str, Any], name: str, worker: dict[str, Any], job_id: str) -> dict[str, Any] | None:
    """Auto-answer a permission-type dialog on a worker that is needs_input.

    The caller has already verified the worker's frozen identity and claim.
    Fail closed: an unreadable dialog, a question about the work, or a drive
    error sends nothing further and is recorded.  One dialog (pane + question)
    is handled at most once per PROMPT_RETRY_SECONDS; a dialog left to the
    user is reported once.
    """
    pane_id = str(worker["pane_id"])
    records = state.get("prompt_attempts") or {}
    try:
        observed = provider_state.snapshot_target(name, max_chars=240)
    except SystemExit:
        return None
    if str((observed.get("state") or {}).get("value") or "") != "needs_input":
        if pane_id in records:
            records.pop(pane_id)
            if not records:
                state.pop("prompt_attempts", None)
        return None
    reason = ""
    try:
        dialog = dialogs.read_dialog(pane_capture(pane_id))
    except SystemExit as exc:
        dialog, reason = None, f"capture failed: {exc}"
    question = dialog.question if dialog else ""
    key = hashlib.sha256(f"{pane_id}\0{question}".encode("utf-8")).hexdigest()[:16]
    previous = records.get(pane_id) or {}
    now = time.time()
    if previous.get("key") == key and (
        previous.get("outcome") == "needs_user" or now - float(previous.get("at") or 0) < PROMPT_RETRY_SECONDS
    ):
        return None
    detail: dict[str, Any] = {"kind": dialog.kind if dialog else "", "question": dialog.headline if dialog else ""}
    if dialog is None:
        outcome, detail["reason"] = "needs_user", reason or "provider needs_input but no dialog could be read"
    elif dialogs.auto_answer_index(dialog) is None:
        outcome = "needs_user"
        detail["reason"] = "question for the user" if dialog.kind == "question" else f"no {dialog.kind} option matches the policy"
        detail["options"] = dialog.options
    else:
        try:
            answered = dialogs.auto_approve(
                capture=lambda: pane_capture(pane_id),
                send=pane_sender(pane_id),
                pane_id=pane_id,
                tmux=cli_bridge.tmux_query,
            )
        except dialogs.PaneBusy:
            return None  # someone else is on this pane; look again next tick
        except (ValueError, RuntimeError, SystemExit) as exc:
            answered, detail["reason"] = None, "drive stopped: " + re.sub(r"\s+", " ", str(exc)).strip()
        else:
            if not answered:
                detail["reason"] = "dialog disappeared or changed before it was answered"
        outcome = "answered" if answered else "failed"
        if answered:
            detail.update(answered)
    records[pane_id] = {"key": key, "at": now, "outcome": outcome, "kind": detail["kind"], "question": detail["question"]}
    state["prompt_attempts"] = records
    kind = {
        "answered": "worker_prompt_auto_answered",
        "needs_user": "worker_prompt_needs_user",
        "failed": "worker_prompt_auto_answer_failed",
    }[outcome]
    if outcome == "answered":
        message = f"{detail['kind']}: {detail['question']} -> {detail['answer']}"
    else:
        message = f"{detail['kind'] or 'unreadable'}: {detail['question'] or '-'}; not answered: {detail['reason']}"
    event_ledger.append_event(
        kind,
        job_id=f"leader:{state['id']}",
        pane=pane_id,
        target=name,
        source=LEADER_SOURCE,
        message=message,
        data={"leader_id": state["id"], "worker_job_id": job_id, **detail},
    )
    return {"worker": name, "kind": kind, "excerpt": message}


def tick_once(
    leader_id: str,
    *,
    probe_interval: float,
    force_probe: bool,
    before_commit: Any = None,
) -> dict[str, Any]:
    """One event-first reconcile step.

    The leader state is written only when it actually changed.  When the step
    produces a change worth reporting, ``before_commit(old_cursor)`` runs
    under the leader lock just before that write, so a caller (leaderd) can
    durably mark the in-flight notification first.
    """
    with locked():
        state = load_state(leader_id)
        loaded = json.dumps(state, sort_keys=True, ensure_ascii=False, default=str)
        if str(state.get("status")) != "running":
            return {
                "leader_id": leader_id,
                "changed": True,
                "reason": "leader_terminal",
                "status": state.get("status"),
                "phase": state.get("phase"),
                "events": [],
                "workers": {},
            }
        issues = renew_claims(state)
        effective_probe_interval = (
            probe_interval
            if probe_interval >= 0
            else float((state.get("monitor_policy") or {}).get("probe_interval_seconds") or 30)
        )
        job_ids = {
            str(job_id)
            for worker in (state.get("workers") or {}).values()
            for job_id in worker.get("job_ids", [])
        }
        events, last_id = event_ledger.read_events(after=int(state.get("event_cursor") or 0), limit=1000)
        old_cursor = int(state.get("event_cursor") or 0)
        # Ledger prune/rebuild may move the global cursor backwards. Follow the
        # ledger reset contract or this leader would remain permanently ahead
        # and never observe future child events.
        state["event_cursor"] = last_id if last_id < old_cursor else max(old_cursor, last_id)
        relevant = [compact_event(event) for event in events if str(event.get("job_id") or "") in job_ids]
        job_owner = {
            str(job_id): str(name)
            for name, worker in (state.get("workers") or {}).items()
            for job_id in worker.get("job_ids", [])
        }
        # Progress means a durable signal from the worker's attempt: a ledger
        # event not written by the leader itself, or a status change.  Screen
        # redraws (spinners, timers) are not progress.
        progressed = {
            job_owner[str(event.get("job_id") or "")]
            for event in events
            if str(event.get("job_id") or "") in job_owner and event.get("source") != LEADER_SOURCE
        }
        policy = state.get("monitor_policy") or {}
        auto_approve: bool | None = None
        changed = bool(relevant)
        changes: list[dict[str, Any]] = []
        statuses: dict[str, str] = {}
        all_terminal = bool(state.get("workers"))
        all_assigned = True
        now = time.time()
        for name, worker in (state.get("workers") or {}).items():
            if worker.get("retired"):
                statuses[name] = str(worker.get("status") or "retired")
                continue
            latest_id = str(worker.get("job_ids", [])[-1]) if worker.get("job_ids") else ""
            if not latest_id:
                all_assigned = False
                all_terminal = False
                status = "unassigned"
            else:
                job = event_ledger.get_job(latest_id) or {}
                status = event_ledger.normalize_status(str(job.get("status") or "unknown"))
                if status in event_ledger.TERMINAL_STATUSES:
                    # Terminal job truth is durable and does not depend on its
                    # CLI still being alive. Interrupt/kill/normal CLI exit can
                    # invalidate the pane runtime after the terminal event.
                    if status != worker.get("status"):
                        changes.append(
                            {"worker": name, "kind": "status", "from": worker.get("status"), "to": status}
                        )
                        worker["status"] = status
                        changed = True
                    statuses[name] = status
                    continue
                all_terminal = False
                # A bounded post-send observation can miss a real provider
                # transition (notably when Codex is wrapped by bash).  Recheck
                # only this pending worker during the normal event-first tick;
                # upgrade evidence when present, but never resend or invent a
                # terminal result when it remains inconclusive.
                delivery = job.get("delivery") if isinstance(job.get("delivery"), dict) else {}
                pending_due = force_probe or now - float(worker.get("last_probe_at") or 0) >= effective_probe_interval
                if str(delivery.get("state") or "") == "pending_confirmation" and pending_due:
                    # Reuse the normal probe cadence: a pending delivery must
                    # not turn leaderd's short tick interval into hot polling.
                    try:
                        observed = provider_state.snapshot_target(name, max_chars=240)
                    except SystemExit:
                        observed = {}
                    observed_state = str((observed.get("state") or {}).get("value") or "unknown")
                    observed_status = {"busy": "running", "needs_input": "waiting_user"}.get(observed_state, "")
                    probes = int(delivery.get("pending_probes") or 0) + 1
                    updated_delivery = {**delivery, "pending_probes": probes, "observed_state": observed_state}
                    if observed_status:
                        status = observed_status
                        message = f"pending dispatch observed as {observed_state}"
                        kind = "pending_dispatch_observed"
                    elif probes >= 2:
                        # Do not free or resend an ambiguous task automatically:
                        # make the required operator decision explicit instead.
                        status = "waiting_user"
                        message = "pending dispatch remained unconfirmed after bounded probes"
                        kind = "pending_dispatch_requires_decision"
                    else:
                        message = "pending dispatch remains unconfirmed; bounded recheck scheduled"
                        kind = "pending_dispatch_recheck"
                    event_ledger.upsert_job(
                        latest_id,
                        source="secretary-bus-leader",
                        status=status,
                        target=name,
                        pane=str(worker["pane_id"]),
                        message=message,
                        delivery=updated_delivery,
                    )
                    event_ledger.append_event(
                        kind,
                        job_id=latest_id,
                        target=name,
                        pane=str(worker["pane_id"]),
                        source="secretary-bus-leader",
                        status=status,
                        message=message,
                    )
            runtime_error = ""
            try:
                current = runtime_snapshot(name)
                if current["runtime_fingerprint"] != worker["runtime_fingerprint"]:
                    runtime_error = "process_changed"
            except SystemExit as exc:
                runtime_error = re.sub(r"\s+", " ", str(exc)).strip()
            if runtime_error:
                issues.append(f"runtime:{name}:{runtime_error}")
                statuses[name] = "identity_error"
                all_terminal = False
                continue
            worker.setdefault("last_progress_at", now)
            if name in progressed:
                worker["last_progress_at"] = now
            if status != worker.get("status"):
                changes.append({"worker": name, "kind": "status", "from": worker.get("status"), "to": status})
                worker["status"] = status
                worker["last_progress_at"] = now
                changed = True
            statuses[name] = status
            if latest_id and int(policy.get("progress_deadline_seconds") or 0) > 0:
                stalled = check_progress_stall(state, name, worker, latest_id, now)
                if stalled:
                    changes.append(stalled)
                    changed = True

            due = force_probe or now - float(worker.get("last_probe_at") or 0) >= effective_probe_interval
            if due:
                digest, excerpt = capture_digest(worker)
                worker["last_probe_at"] = now
                if digest != worker.get("last_digest"):
                    worker["last_digest"] = digest
                    changes.append({"worker": name, "kind": "pane_changed", "excerpt": excerpt})
                    changed = True
                if f"claim_lost:{name}" not in issues:
                    if auto_approve is None:
                        # Owner's standing authorization; unreadable config reads as off.
                        auto_approve = cli_bridge.auto_approve_enabled()
                    if auto_approve:
                        prompt_change = answer_worker_prompt(state, name, worker, latest_id)
                        if prompt_change:
                            changes.append(prompt_change)
                            changed = True

        deadline_change = check_hard_deadline(state, now)
        if deadline_change:
            changes.append(deadline_change)
            changed = True

        if all_assigned and all_terminal:
            if state.get("phase") != "verifying":
                changes.append({"kind": "verification_required"})
                changed = True
            state["phase"] = "verifying"
        elif state.get("phase") == "verifying":
            state["phase"] = "monitor"

        # A persisting identity/claim problem is reported in every payload
        # but only counts as a change when the set of problems changes;
        # otherwise `leader watch` returned immediately on every poll.
        issue_keys = sorted({stable_issue_key(issue) for issue in issues})
        if issue_keys != list(state.get("issue_keys") or []):
            changed = True
            if issue_keys:
                state["issue_keys"] = issue_keys
            else:
                state.pop("issue_keys", None)

        if json.dumps(state, sort_keys=True, ensure_ascii=False, default=str) != loaded:
            if changed and before_commit is not None:
                before_commit(old_cursor)
            save_state(state)
        reason = "event_or_state_change" if changed else "no_change"
        if issues:
            reason = "identity_or_claim_error"
        elif all_assigned and all_terminal:
            reason = "verification_required"
        return {
            "leader_id": leader_id,
            "changed": changed,
            "reason": reason,
            "status": state["status"],
            "phase": state["phase"],
            "cursor": state["event_cursor"],
            "events": relevant,
            "changes": changes,
            "issues": issues,
            "workers": statuses,
        }


def cmd_tick(args: argparse.Namespace) -> int:
    payload = tick_once(args.id, probe_interval=args.probe_interval, force_probe=args.force_probe)
    emit(payload, as_json=args.json)
    return 1 if payload.get("issues") else 0


def cmd_watch(args: argparse.Namespace) -> int:
    deadline = time.time() + args.timeout if args.timeout > 0 else 0
    while True:
        payload = tick_once(args.id, probe_interval=args.probe_interval, force_probe=False)
        if payload.get("changed"):
            emit(payload, as_json=args.json)
            return 1 if payload.get("issues") else 0
        if deadline and time.time() >= deadline:
            payload["reason"] = "timeout"
            emit(payload, as_json=args.json)
            return 1 if payload.get("issues") else 0
        time.sleep(max(0.05, args.interval))


def cmd_steer(args: argparse.Namespace) -> int:
    send, allow_newline = execution_options(args)
    text = supervisor.read_task_source(args.text, getattr(args, "text_file", None))
    with locked():
        state = load_state(args.id)
        worker = ensure_running_and_owned(state, args.worker)
        if not worker.get("job_ids"):
            raise SystemExit(f"worker has no leader job to steer: {args.worker}")
        job_id = str(worker["job_ids"][-1])
        action_key = args.action_key or (
            "steer-" + hashlib.sha256(f"{args.id}\0{job_id}\0{text}".encode("utf-8")).hexdigest()[:20]
        )
        payload_hash = hashlib.sha256(f"{job_id}\0{text}".encode("utf-8")).hexdigest()[:16]
        action, duplicate = prepare_action(
            state,
            worker=worker,
            kind="steer",
            action_key=action_key,
            payload_hash=payload_hash,
        )
        if duplicate:
            payload = {
                "leader_id": args.id,
                "worker": args.worker,
                "job_id": job_id,
                "sent": action.get("status") == "sent",
                "duplicate": True,
                "action_key": action_key,
            }
            emit(payload, as_json=args.json)
            return 0
        action["job_id"] = job_id
        action["text_hash"] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        save_state(state)
    captured = io.StringIO()
    try:
        with locked():
            state = load_state(args.id)
            ensure_running_and_owned(state, args.worker)
            if ((state.get("actions") or {}).get(action_key) or {}).get("status") != "prepared":
                raise SystemExit(f"steer action changed before send: {action_key}")
            with contextlib.redirect_stdout(captured):
                supervisor.cmd_continue(
                    argparse.Namespace(id=job_id, text=text, text_file=None, allow_newline=allow_newline, yes=send)
                )
            finish_action(state, action_key, "sent" if send else "dry_run")
    except BaseException:
        with locked():
            state = load_state(args.id)
            if action_key in (state.get("actions") or {}):
                finish_action(state, action_key, "failed")
        raise
    payload = {
        "leader_id": args.id,
        "worker": args.worker,
        "job_id": job_id,
        "sent": send,
        "duplicate": False,
        "action_key": action_key,
    }
    if args.json:
        emit(payload, as_json=True)
    else:
        print(captured.getvalue().rstrip())
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    captured = io.StringIO()
    with locked():
        state = load_state(args.id)
        worker = ensure_running_and_owned(state, args.worker)
        job_id = args.job or (str(worker.get("job_ids", [])[-1]) if worker.get("job_ids") else "")
        if not job_id or job_id not in worker.get("job_ids", []):
            raise SystemExit(f"job is not owned by worker {args.worker}: {job_id or '(none)'}")
        with contextlib.redirect_stdout(captured):
            supervisor.cmd_collect(argparse.Namespace(id=job_id, history=args.history))
    payload = {"leader_id": args.id, "worker": args.worker, "job_id": job_id, "collected": True}
    if args.json:
        emit(payload, as_json=True)
    else:
        print(captured.getvalue().rstrip())
    return 0


def all_workers_terminal(state: dict[str, Any]) -> bool:
    workers = state.get("workers") or {}
    if not workers:
        return False
    for worker in workers.values():
        if worker.get("retired"):
            continue
        job_ids = worker.get("job_ids") or []
        if not job_ids:
            return False
        job = event_ledger.get_job(str(job_ids[-1])) or {}
        status = event_ledger.normalize_status(str(job.get("status") or "unknown"))
        if status not in event_ledger.TERMINAL_STATUSES:
            return False
    return True


def cmd_verify(args: argparse.Namespace) -> int:
    evidence = [event_ledger.compact(value, 300) for value in args.evidence if str(value).strip()]
    if not evidence:
        raise SystemExit("worker verification requires at least one --evidence from an independent check")
    with locked():
        state = load_state(args.id)
        worker = ensure_running_and_owned(state, args.worker)
        job_ids = worker.get("job_ids") or []
        if not job_ids:
            raise SystemExit(f"worker has no latest job to verify: {args.worker}")
        if not args.force:
            # Accepting work the worker is still changing makes the evidence stale.
            try:
                observed = provider_state.snapshot_target(args.worker, max_chars=240)
            except SystemExit:
                observed = {}
            value = str((observed.get("state") or {}).get("value") or "")
            if value in {"busy", "needs_input"}:
                raise SystemExit(
                    f"worker {args.worker} is still {value}; verify once it has stopped (or interrupt it first), "
                    "or pass --force to accept anyway"
                )
        job_id = str(job_ids[-1])
        payload_hash = hashlib.sha256(
            f"{job_id}\0".encode("utf-8") + "\0".join(evidence).encode("utf-8")
        ).hexdigest()[:16]
        action_key = require_id(
            args.action_key
            or "verify-"
            + hashlib.sha256(f"{args.id}\0{args.worker}\0{job_id}\0{payload_hash}".encode("utf-8")).hexdigest()[:20],
            "action key",
        )
        previous = (state.get("actions") or {}).get(action_key)
        duplicate = False
        if isinstance(previous, dict) and previous.get("status") in {"prepared", "sent"}:
            contract = (previous.get("kind"), previous.get("worker"), previous.get("payload_hash"))
            if contract != ("verify", args.worker, payload_hash):
                raise SystemExit(f"action key collision with a different control contract: {action_key}")
            duplicate = previous.get("status") == "sent"
        else:
            prepare_action(
                state,
                worker=worker,
                kind="verify",
                action_key=action_key,
                payload_hash=payload_hash,
            )
        if duplicate:
            emit(
                {
                    "leader_id": args.id,
                    "worker": args.worker,
                    "job_id": job_id,
                    "action_key": action_key,
                    "verified": True,
                    "duplicate": True,
                    "phase": state.get("phase"),
                },
                as_json=args.json,
            )
            return 0

        existing = event_ledger.get_job(job_id) or {}
        already_recorded = str(existing.get("verification_action_key") or "") == action_key
        event_ledger.upsert_job(
            job_id,
            source="secretary-bus-leader",
            status="completed",
            completed_at=now_iso(),
            message=f"independently verified by leader {args.id}",
            verified_by_leader=args.id,
            verification_action_key=action_key,
            verification_evidence=evidence,
        )
        worker["status"] = "completed"
        worker["verified_at"] = now_iso()
        worker["verification_evidence"] = evidence
        if all_workers_terminal(state):
            state["phase"] = "verifying"
        if not already_recorded:
            event_ledger.append_event(
                "leader_worker_verified",
                job_id=job_id,
                pane=str(worker["pane_id"]),
                target=args.worker,
                source="secretary-bus-leader",
                status="completed",
                message=f"worker independently verified by leader {args.id}",
                data={
                    "leader_id": args.id,
                    "action_key": action_key,
                    "evidence_count": len(evidence),
                },
            )
        finish_action(state, action_key, "sent")
        payload = {
            "leader_id": args.id,
            "worker": args.worker,
            "job_id": job_id,
            "action_key": action_key,
            "verified": True,
            "duplicate": False,
            "phase": state.get("phase"),
        }
    emit(payload, as_json=args.json)
    return 0


def diagnose_member(member: dict[str, Any]) -> dict[str, Any]:
    result = {"name": member["name"], "pane_id": member["pane_id"], "status": "ok"}
    if member.get("retired"):
        result["status"] = "retired"
        return result
    try:
        target = target_or_die(str(member["name"]))
        info = cli_bridge.target_info(target)
        if str(info["command"]) != str(member["expected_command"]):
            result["status"] = "command_mismatch"
        elif (
            str(info["pane_id"]) != str(member["pane_id"])
            or str(info["pane_start_time"]) != str(member["pane_start_time"])
        ):
            result["status"] = "process_changed"
    except SystemExit as exc:
        message = str(exc)
        result["status"] = "process_changed" if "runtime changed" in message else "missing"
        result["detail"] = re.sub(r"\s+", " ", message).strip()
    return result


def cmd_doctor(args: argparse.Namespace) -> int:
    with locked():
        state = load_state(args.id)
        members = [state["leader_runtime"], *(state.get("workers") or {}).values()]
        diagnosed = [diagnose_member(member) for member in members]
        claims = active_claims(load_claims())
        for member in diagnosed[1:]:
            claim = claims.get(str(member["pane_id"]))
            if member["status"] == "ok" and (not claim or claim.get("leader_id") != state["id"]):
                member["status"] = "claim_lost"
            if member["status"] in {"missing", "process_changed", "command_mismatch"}:
                member["next"] = (
                    f"secretary-bus leader abandon {state['id']} --worker {member['name']} --reason \"<why>\""
                )
        healthy = all(item["status"] in {"ok", "retired"} for item in diagnosed)
    cards: dict[str, Any] = {"mode": state.get("display_mode", "hidden"), "status": "not_required"}
    if state.get("display_mode") == "visible":
        expected_category = str(state.get("cards_category") or "")
        try:
            panes, prefs = cards_control.CardsClient().snapshot()
            by_id = {str(pane.get("pane_id") or ""): pane for pane in panes}
            expected_ids = [str(member.get("pane_id") or "") for member in members if not member.get("retired")]
            expected_membership = {
                str(pane_id): str(category)
                for pane_id, category in (state.get("cards_membership") or {}).items()
                if str(category)
            }
            if not expected_membership:
                expected_membership = {pane_id: expected_category for pane_id in expected_ids}
            missing = [pane_id for pane_id in expected_ids if pane_id not in by_id]
            misplaced = [
                pane_id
                for pane_id in expected_ids
                if pane_id in by_id
                and cards_control.pane_state(by_id[pane_id], prefs).get("category")
                != expected_membership.get(pane_id, "")
            ]
            cards = {
                "mode": "visible",
                "status": "ok" if not missing and not misplaced else "category_mismatch",
                "category": expected_category,
                "membership": expected_membership,
                "missing": missing,
                "misplaced": misplaced,
            }
            healthy = healthy and cards["status"] == "ok"
        except SystemExit as exc:
            cards = {"mode": "visible", "status": "unavailable", "detail": str(exc)}
            healthy = False
    payload = {
        "leader_id": args.id,
        "healthy": healthy,
        "members": diagnosed,
        "cards": cards,
        # Informational: a passed deadline is the leader's decision, not an identity fault.
        "deadlines": deadline_summary(state),
    }
    emit(payload, as_json=args.json)
    return 0 if healthy else 1


PROCESS_IDENTITY_FIELDS = ("pane_pid", "pane_start_time", "foreground_pid", "foreground_start_time")


def pane_gone_evidence(worker: dict[str, Any]) -> str:
    """Why the worker's frozen runtime provably no longer exists; '' if it is alive.

    Fail closed: a failed or empty tmux query is never treated as proof.
    """
    cp = cli_bridge.tmux("list-panes", "-a", "-F", "#{pane_id}", check=False)
    if cp.returncode != 0:
        raise SystemExit(
            f"cannot query tmux to prove the worker pane is gone; refusing: {cp.stderr.strip() or cp.returncode}"
        )
    pane_ids = {line.strip() for line in cp.stdout.splitlines() if line.strip()}
    if not pane_ids:
        raise SystemExit("tmux returned no panes (wrong server/socket?); refusing to treat that as proof")
    pane_id = str(worker["pane_id"])
    if pane_id not in pane_ids:
        return f"pane {pane_id} no longer exists on the tmux server"
    info = cli_bridge.pane_info(pane_id)
    changed = [field for field in PROCESS_IDENTITY_FIELDS if str(info.get(field)) != str(worker.get(field))]
    if str(info.get("command") or "") != str(worker.get("expected_command") or ""):
        changed.append("command")
    if changed:
        return f"pane {pane_id} now runs a different process ({','.join(changed)} changed)"
    return ""


def cmd_abandon(args: argparse.Namespace) -> int:
    """End a worker whose pane vanished or was reused, without touching tmux."""
    reason = re.sub(r"\s+", " ", args.reason or "").strip()
    if not reason:
        raise SystemExit("abandon requires a non-empty --reason")
    with locked():
        state = load_state(args.id)
        if str(state.get("status")) != "running":
            raise SystemExit(
                f"leader session is not running: {state.get('status')}; its attempts were terminalized when it closed"
            )
        worker = (state.get("workers") or {}).get(args.worker)
        if not isinstance(worker, dict):
            raise SystemExit(f"worker is outside leader membership: {args.worker}")
        if worker.get("retired"):
            if worker.get("status") == "abandoned":
                emit(
                    {"leader_id": args.id, "worker": args.worker, "abandoned": True, "duplicate": True},
                    as_json=args.json,
                    text_line=f"worker {args.worker} was already abandoned",
                )
                return 0
            if worker.get("status") != "killed":
                raise SystemExit(f"worker is already retired: {args.worker} status={worker.get('status')}")
            # This leader killed the worker itself; recording the reason lets a
            # completed close account for it instead of dead-ending.
            worker.update({"status": "abandoned", "abandon_reason": reason, "abandon_evidence": "killed by this leader"})
            state["last_action"] = {"kind": "abandon", "worker": args.worker, "reason": reason, "at": now_iso()}
            save_state(state)
            event_ledger.append_event(
                "leader_worker_abandoned",
                job_id=f"leader:{args.id}",
                pane=str(worker.get("pane_id") or ""),
                target=args.worker,
                source="secretary-bus-leader",
                message=f"{reason}; killed by this leader",
                data={"leader_id": args.id, "terminalized_jobs": []},
            )
            emit(
                {"leader_id": args.id, "worker": args.worker, "abandoned": True, "after_kill": True},
                as_json=args.json,
                text_line=f"worker {args.worker} (killed earlier) recorded as abandoned: {reason}",
            )
            return 0
        pane_id = str(worker["pane_id"])
        claim = active_claims(load_claims()).get(pane_id)
        if claim and claim.get("leader_id") != state["id"]:
            raise SystemExit(f"pane {pane_id} is claimed by leader {claim.get('leader_id')}; refusing to abandon")
        evidence = pane_gone_evidence(worker)
        if not evidence:
            raise SystemExit(
                f"worker {args.worker} pane {pane_id} is still alive with its frozen identity; "
                "use interrupt, kill or verify instead of abandon"
            )
        message = f"worker abandoned by leader {args.id}: {reason}; {evidence}"
        terminalized: list[str] = []
        for job_id in [str(value) for value in worker.get("job_ids") or []]:
            job = event_ledger.get_job(job_id)
            if job and event_ledger.normalize_status(str(job.get("status") or "")) not in event_ledger.TERMINAL_STATUSES:
                event_ledger.upsert_job(
                    job_id,
                    source="secretary-bus-leader",
                    status="interrupted",
                    completed_at=now_iso(),
                    message=message,
                )
                terminalized.append(job_id)
        worker.update(
            {
                "status": "abandoned",
                "retired": True,
                "retired_at": now_iso(),
                "abandon_reason": reason,
                "abandon_evidence": evidence,
            }
        )
        claims = load_claims()
        if claims.get(pane_id, {}).get("leader_id") == state["id"]:
            claims.pop(pane_id, None)
            write_json_atomic(CLAIMS_FILE, claims)
        state["last_action"] = {"kind": "abandon", "worker": args.worker, "reason": reason, "at": now_iso()}
        save_state(state)
    event_ledger.append_event(
        "leader_worker_abandoned",
        job_id=f"leader:{args.id}",
        pane=pane_id,
        target=args.worker,
        source="secretary-bus-leader",
        message=f"{reason}; {evidence}",
        data={"leader_id": args.id, "terminalized_jobs": terminalized},
    )
    emit(
        {
            "leader_id": args.id,
            "worker": args.worker,
            "abandoned": True,
            "duplicate": False,
            "evidence": evidence,
            "terminalized_jobs": terminalized,
        },
        as_json=args.json,
        text_line=f"worker {args.worker} abandoned ({evidence}); interrupted {len(terminalized)} attempt(s)",
    )
    return 0


def unaccepted_workers(state: dict[str, Any]) -> list[str]:
    """Workers whose latest attempt blocks a completed close, each with the next step."""
    leader_id = str(state["id"])
    blocked: list[str] = []
    for name, worker in (state.get("workers") or {}).items():
        if worker.get("status") == "abandoned" and str(worker.get("abandon_reason") or "").strip():
            continue
        job_ids = [str(value) for value in worker.get("job_ids") or []]
        verify = f'secretary-bus leader verify {leader_id} --worker {name} --evidence "<independent check>"'
        if not job_ids:
            blocked.append(f"{name}: no attempt was ever assigned")
            continue
        job = event_ledger.get_job(job_ids[-1]) or {}
        job_status = event_ledger.normalize_status(str(job.get("status") or "unknown"))
        if str(job.get("verified_by_leader") or "") == leader_id and job.get("verification_evidence"):
            continue
        if job_status == "cancelled" and str(job.get("message") or "").strip():
            continue
        if worker.get("retired"):
            blocked.append(
                f"{name}: retired ({worker.get('status')}) with unverified attempt {job_ids[-1]} status={job_status}; "
                "it can no longer be verified; record why with "
                f'secretary-bus leader abandon {leader_id} --worker {name} --reason "<why>" '
                "or close with --status failed|blocked|cancelled"
            )
        else:
            blocked.append(f"{name}: attempt {job_ids[-1]} status={job_status} not verified -> {verify}")
    return blocked


def cmd_close(args: argparse.Namespace) -> int:
    status = event_ledger.normalize_status(args.status)
    if status not in {"completed", "blocked", "failed", "cancelled"}:
        raise SystemExit("close status must be completed, blocked, failed, or cancelled")
    if status == "completed" and not args.evidence:
        raise SystemExit("leader completion requires at least one --evidence from an independent acceptance check")
    with locked():
        state = load_state(args.id)
        if str(state.get("status")) in FINAL_STATES:
            raise SystemExit(f"leader session is already terminal: {state.get('status')}")
        if status == "completed" and state.get("phase") != "verifying":
            raise SystemExit(
                "workers have not all reached terminal claims; keep monitoring or close with a non-completed status"
            )
        if status == "completed":
            unaccepted = unaccepted_workers(state)
            if unaccepted:
                raise SystemExit(
                    "leader completion requires every worker's latest attempt to be verified by `leader verify`, "
                    "or the worker abandoned / its attempt cancelled with a reason; not accepted:\n"
                    + "\n".join(f"  {line}" for line in unaccepted)
                )
        state["status"] = status
        state["phase"] = "closed"
        state["evidence"] = [event_ledger.compact(value, 300) for value in args.evidence]
        state["closed_at"] = now_iso()
        claims = load_claims()
        claims = {key: value for key, value in claims.items() if value.get("leader_id") != state["id"]}
        write_json_atomic(CLAIMS_FILE, claims)
        state["cancelled_jobs"] = cancel_active_child_jobs(
            state,
            reason=f"leader {args.id} closed as {status}; attempt cancelled (worker pane was not interrupted)",
        )
        save_state(state)
    event_ledger.upsert_job(
        f"leader:{args.id}",
        source="secretary-bus-leader",
        status=status,
        completed_at=now_iso(),
        message=f"leader closed after {len(args.evidence)} acceptance evidence item(s)",
    )
    event_ledger.append_event(
        "leader_closed",
        job_id=f"leader:{args.id}",
        source="secretary-bus-leader",
        status=status,
        message="leader session closed",
    )
    emit(public_state(state), as_json=args.json, text_line=f"leader {args.id} closed: {status}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Secretary Bus provider-neutral leader sessions")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create", help="Create a scoped leader session and claim workers")
    p.add_argument("--id", default="")
    p.add_argument("--leader", required=True, help="Registered target acting as leader")
    p.add_argument("--worker", action="append", required=True, help="Registered worker target; repeat as needed")
    p.add_argument("--objective", required=True)
    p.add_argument("--acceptance", action="append", default=[])
    p.add_argument("--authority", action="append", default=[], help="Durable authority item; repeat as needed")
    p.add_argument("--scope", action="append", default=[], help="WORKER=short task scope")
    p.add_argument("--write-owner", action="append", default=[], help="WORKER=owned file/module scope")
    p.add_argument("--progress-deadline", type=int, default=1800)
    p.add_argument("--hard-deadline", type=int, default=7200)
    p.add_argument("--max-attempts", type=int, default=3)
    p.add_argument("--probe-interval", type=int, default=30)
    p.add_argument("--lease-seconds", type=int, default=DEFAULT_LEASE_SECONDS)
    p.add_argument("--takeover", action="store_true", help="Explicitly supersede another active leader claim")
    display = p.add_mutually_exclusive_group()
    display.add_argument(
        "--cards-category",
        default="",
        help=(
            "Visible-mode fallback for uncategorized panes only; existing pane categories are preserved. "
            "Omitted means a readable fallback is generated only when needed"
        ),
    )
    display.add_argument(
        "--hidden",
        action="store_true",
        help="Explicitly keep this one-off leader session out of AI Session Cards",
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_create)

    p = sub.add_parser("list", help="List compact leader sessions")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("discover", help="Enumerate every pane in the current user's tmux server")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_discover)

    p = sub.add_parser("config", help="Read or update durable Secretary Bus owner preferences")
    p.add_argument("--trusted-owner", choices=("true", "false"), default="")
    p.add_argument(
        "--auto-approve-permissions",
        choices=("true", "false"),
        default="",
        help="Auto-answer permission-type dialogs on driven AI panes (default false; AGENT_BUS_AUTO_APPROVE=1|0 overrides); questions about the work always go to the user",
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_config)

    p = sub.add_parser("status", help="Read the durable compact snapshot")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("assign", help="Dispatch one auditable worker attempt")
    p.add_argument("id")
    p.add_argument("--worker", required=True)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--task")
    source.add_argument(
        "--task-file", help="UTF-8 task file, or - for stdin; long/multi-line tasks are sent as a file pointer"
    )
    p.add_argument("--repo", default="")
    p.add_argument("--history", type=int, default=5000)
    p.add_argument("--allow-newline", action="store_true")
    p.add_argument("--allow-pane-cwd-mismatch", action="store_true")
    send_group = p.add_mutually_exclusive_group()
    send_group.add_argument("--yes", action="store_true")
    send_group.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_assign)

    p = sub.add_parser("tick", help="Perform one event-first reconcile step")
    p.add_argument("id")
    p.add_argument("--probe-interval", type=float, default=-1, help="Override session policy; default uses create value")
    p.add_argument("--force-probe", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_tick)

    p = sub.add_parser("watch", help="Block until relevant change or timeout")
    p.add_argument("id")
    p.add_argument("--timeout", type=float, default=120)
    p.add_argument("--interval", type=float, default=1.5)
    p.add_argument("--probe-interval", type=float, default=-1, help="Override session policy; default uses create value")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("steer", help="Send a bounded correction to a worker attempt")
    p.add_argument("id")
    p.add_argument("--worker", required=True)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--text")
    source.add_argument(
        "--text-file", help="UTF-8 file, or - for stdin; long/multi-line text is sent as a file pointer"
    )
    p.add_argument("--allow-newline", action="store_true")
    send_group = p.add_mutually_exclusive_group()
    send_group.add_argument("--yes", action="store_true")
    send_group.add_argument("--dry-run", action="store_true")
    p.add_argument("--action-key", default="", help="Stable idempotency key for recovery-safe steer")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_steer)

    p = sub.add_parser("keys", help="Send raw tmux key names to a claimed worker")
    p.add_argument("id")
    p.add_argument("--worker", required=True)
    p.add_argument("--key", action="append", required=True, help="tmux key name; repeat in order")
    p.add_argument("--action-key", default="", help="Stable idempotency key")
    send_group = p.add_mutually_exclusive_group()
    send_group.add_argument("--yes", action="store_true")
    send_group.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_keys)

    p = sub.add_parser(
        "approve",
        help="Answer the worker's permission dialog by the standing policy (verified key by key); "
        "refuses questions about the work",
    )
    p.add_argument("id")
    p.add_argument("--worker", required=True)
    p.add_argument(
        "--key", action="append", default=[], help="send these raw tmux keys instead of the policy answer"
    )
    p.add_argument("--action-key", default="", help="Stable idempotency key")
    send_group = p.add_mutually_exclusive_group()
    send_group.add_argument("--yes", action="store_true")
    send_group.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_approve)

    p = sub.add_parser("interrupt", help="Send Ctrl-C and terminalize the current worker attempt")
    p.add_argument("id")
    p.add_argument("--worker", required=True)
    p.add_argument("--wait", type=float, default=2.0, help="Seconds to verify idle/stopped after Ctrl-C")
    p.add_argument("--interval", type=float, default=0.1, help="Post-interrupt observation interval")
    p.add_argument("--action-key", default="", help="Stable idempotency key")
    send_group = p.add_mutually_exclusive_group()
    send_group.add_argument("--yes", action="store_true")
    send_group.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_interrupt)

    p = sub.add_parser("restart", help="Respawn a claimed worker pane and rotate its frozen identity")
    p.add_argument("id")
    p.add_argument("--worker", required=True)
    p.add_argument("--command", default="", help="tmux shell-command; default is registered expected command")
    p.add_argument("--expected-command", default="", help="expected pane_current_command after restart")
    p.add_argument("--cwd", default="", help="restart cwd; default is the frozen worker cwd")
    p.add_argument("--wait", type=float, default=5.0)
    p.add_argument("--action-key", default="", help="Stable idempotency key")
    send_group = p.add_mutually_exclusive_group()
    send_group.add_argument("--yes", action="store_true")
    send_group.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_restart)

    p = sub.add_parser("kill", help="Kill and retire a claimed worker pane")
    p.add_argument("id")
    p.add_argument("--worker", required=True)
    p.add_argument("--action-key", default="", help="Stable idempotency key")
    send_group = p.add_mutually_exclusive_group()
    send_group.add_argument("--yes", action="store_true")
    send_group.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_kill)

    p = sub.add_parser("collect", help="Collect one owned worker attempt and its diff")
    p.add_argument("id")
    p.add_argument("--worker", required=True)
    p.add_argument("--job", default="")
    p.add_argument("--history", type=int, default=5000)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_collect)

    p = sub.add_parser("verify", help="Independently verify one worker's latest job without interrupting it")
    p.add_argument("id")
    p.add_argument("--worker", required=True)
    p.add_argument("--evidence", action="append", default=[], help="Independent acceptance evidence; repeatable")
    p.add_argument("--action-key", default="", help="Stable idempotency key")
    p.add_argument("--force", action="store_true", help="Accept even though the worker is still busy")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("doctor", help="Verify member identity and claims")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser(
        "abandon",
        help="End a worker whose pane is gone or now runs another process (ledger only; refuses a live pane)",
    )
    p.add_argument("id")
    p.add_argument("--worker", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_abandon)

    p = sub.add_parser("close", help="Close, release claims and cancel still-active attempts; completion requires evidence")
    p.add_argument("id")
    p.add_argument("--status", required=True)
    p.add_argument("--evidence", action="append", default=[])
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_close)

    args = parser.parse_args()
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
