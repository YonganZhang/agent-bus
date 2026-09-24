#!/usr/bin/env python3
"""Lightweight job/event ledger for Secretary Bus and Cards.

The ledger is intentionally file-based: append-only JSONL events plus one JSON
state file per job. It gives tmux/Codex/card-dashboard flows a shared status
view without introducing a new daemon or database.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def default_home() -> Path:
    script = Path(__file__).resolve()
    if ".claude" in script.parts:
        return Path.home() / ".claude"
    return Path.home() / ".codex"


BUS = Path(os.environ.get("AGENT_BUS_DIR", str(default_home() / "agent-bus")))
LEDGER = Path(os.environ.get("AGENT_EVENT_LEDGER_DIR", str(BUS / "event-ledger")))
EVENTS_FILE = LEDGER / "events.jsonl"
JOBS_DIR = LEDGER / "jobs"
COUNTER_FILE = LEDGER / "next-event-id.txt"
OFFSETS_FILE = LEDGER / "event-offsets.json"
LOCK_FILE = LEDGER / ".lock"
# event-offsets.json is no longer read by this module: readers locate a cursor
# by binary search over the monotonically increasing event ids in
# events.jsonl.  A short tail of the index is still written so processes that
# loaded an older version of this module (a long-running Cards server or leaderd)
# keep a correct committed head and fast recent-cursor seeks until they are
# restarted.  It used to hold 10000 entries (~226KB rewritten per event).
MAX_EVENT_OFFSETS = int(os.environ.get("AGENT_EVENT_LEDGER_MAX_OFFSETS", "256"))
SEARCH_LINEAR_BYTES = 4096

TERMINAL_STATUSES = {"completed", "blocked", "failed", "cancelled", "canceled", "interrupted"}
ACTIVE_STATUSES = {"created", "sent", "queued", "leased", "starting", "running", "waiting_user", "stale"}
COMPLETION_RE = re.compile(r"\bCOMPLETION_STATUS:\s*(COMPLETE|COMPLETED|BLOCKED|FAILED|CANCELLED|CANCELED)\b", re.I)
# Statuses `reap` may terminalize: the active set minus `stale` (a Cards
# warning state that is not part of the reap contract).
REAP_ACTIVE_STATUSES = ("created", "sent", "queued", "leased", "starting", "running", "waiting_user")
REAP_MAX_AGE_SECONDS = 24 * 3600
REAP_SENT_MAX_AGE_SECONDS = 3600
REAP_RECENT_SECONDS = 3600


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def compact(text: str, limit: int = 500) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def new_job_id(prefix: str = "job") -> str:
    return f"{prefix}-{datetime.now().astimezone().strftime('%Y%m%dT%H%M%S-%f')}"


def normalize_status(status: str) -> str:
    lowered = str(status or "").strip().lower().replace("-", "_")
    mapping = {
        "complete": "completed",
        "completed": "completed",
        "blocked": "blocked",
        "failed": "failed",
        "failure": "failed",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "interrupt": "interrupted",
        "interrupted": "interrupted",
        "inprogress": "running",
        "in_progress": "running",
        "active": "running",
        "terminal": "completed",
        "needs_attention": "waiting_user",
        "waiting": "waiting_user",
        "waiting_user": "waiting_user",
    }
    return mapping.get(lowered, lowered or "unknown")


def _visible_chars(text: str) -> tuple[str, list[int]]:
    """Drop whitespace and box-drawing glyphs, remembering original positions.

    TUIs re-wrap an echoed prompt at the terminal width and may frame it, so a
    prompt is located in captured pane text by its visible characters only.
    """
    chars: list[str] = []
    index: list[int] = []
    for position, char in enumerate(text):
        if char.isspace() or "\u2500" <= char <= "\u257f":
            continue
        chars.append(char)
        index.append(position)
    return "".join(chars), index


def prompt_echo_end(text: str, prompts: list[str] | tuple[str, ...]) -> int | None:
    """Return the offset just after the echoed prompts, or None if none is found.

    Prompts are located in send order, each at its first occurrence after the
    previous one: the echo is the first copy, a later copy is the worker
    quoting its task.
    """
    visible, index = _visible_chars(text)
    cursor = 0
    found = False
    for prompt in prompts:
        needle, _ = _visible_chars(prompt or "")
        if not needle:
            continue
        at = visible.find(needle, cursor)
        if at < 0:
            continue
        cursor = at + len(needle)
        found = True
    if not found:
        return None
    return index[cursor - 1] + 1


def completion_status_from_text(text: str, *, prompts: list[str] | tuple[str, ...] = ()) -> str:
    """Parse the worker's own COMPLETION_STATUS claim.

    Captured pane text includes the echoed prompt, and prompts routinely quote
    the marker they ask for.  Only text after the prompt echo counts, and the
    last marker wins.  When the echo cannot be located (scrolled away), a
    marker is trusted only if the capture holds more markers than the prompts
    themselves contributed.
    """
    text = text or ""
    prompts = [prompt for prompt in prompts if prompt]
    region = text
    if prompts:
        end = prompt_echo_end(text, prompts)
        if end is not None:
            region = text[end:]
        else:
            echoed = sum(len(COMPLETION_RE.findall(prompt)) for prompt in prompts)
            if echoed and len(COMPLETION_RE.findall(text)) <= echoed:
                return ""
    matches = list(COMPLETION_RE.finditer(region))
    if not matches:
        return ""
    return normalize_status(matches[-1].group(1))


def ensure_dirs() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def locked() -> Iterator[None]:
    ensure_dirs()
    with LOCK_FILE.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        return default


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _event_id_from_line(line: bytes) -> int:
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return 0
    if not isinstance(event, dict):
        return 0
    try:
        return int(event.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def _last_committed_event_id() -> int:
    """Id of the last complete (newline-terminated) event line in events.jsonl.

    A writer appends a whole line under the ledger lock; a trailing fragment
    without a newline is an append in progress and is not committed yet.
    """
    try:
        handle = EVENTS_FILE.open("rb")
    except FileNotFoundError:
        return 0
    with handle:
        handle.seek(0, os.SEEK_END)
        pos = handle.tell()
        carry = b""
        inside_trailing_fragment = True
        while pos > 0:
            step = min(8192, pos)
            pos -= step
            handle.seek(pos)
            data = handle.read(step) + carry
            if inside_trailing_fragment:
                if b"\n" not in data:
                    carry = data
                    continue
                data = data[: data.rindex(b"\n")]
                inside_trailing_fragment = False
            parts = data.split(b"\n")
            carry = parts.pop(0) if pos > 0 else b""
            for line in reversed(parts):
                event_id = _event_id_from_line(line)
                if event_id:
                    return event_id
    return 0


def _next_event_id_locked() -> int:
    try:
        current = int(COUNTER_FILE.read_text(encoding="utf-8").strip() or "0")
    except (FileNotFoundError, ValueError):
        current = 0
    current = max(current, _last_committed_event_id())
    next_id = current + 1
    COUNTER_FILE.write_text(str(next_id), encoding="utf-8")
    return next_id


def _offset_after_event_id(handle: Any, after: int, size: int) -> int:
    """Byte offset of a line start at or before the first event with id > after.

    Relies on event ids increasing with file position, which append_event()
    guarantees by allocating and appending under one lock (prune keeps order).
    The caller scans forward from the result and skips ids <= after.
    """
    lo, hi = 0, size
    while hi - lo > SEARCH_LINEAR_BYTES:
        mid = (lo + hi) // 2
        handle.seek(mid)
        handle.readline()
        pos = handle.tell()
        event_id = 0
        line = b""
        while pos < hi:
            line = handle.readline()
            if not line:
                break
            event_id = _event_id_from_line(line)
            if event_id:
                break
            pos += len(line)
        if not event_id or pos >= hi:
            hi = mid
        elif event_id <= after:
            lo = pos + len(line)
        else:
            hi = pos
    return lo


def _load_offsets_locked() -> dict[str, Any]:
    data = read_json(OFFSETS_FILE, {"offsets": {}, "last_id": 0})
    if not isinstance(data, dict):
        return {"offsets": {}, "last_id": 0}
    if not isinstance(data.get("offsets"), dict):
        data["offsets"] = {}
    return data


def _save_offsets_locked(data: dict[str, Any]) -> None:
    offsets = data.get("offsets") if isinstance(data.get("offsets"), dict) else {}
    if len(offsets) > MAX_EVENT_OFFSETS:
        keep = sorted((int(key), value) for key, value in offsets.items() if str(key).isdigit())[-MAX_EVENT_OFFSETS:]
        offsets = {str(key): value for key, value in keep}
    data["offsets"] = offsets
    write_json_atomic(OFFSETS_FILE, data)


def _record_event_offset_locked(event_id: int, end_offset: int) -> None:
    data = _load_offsets_locked()
    offsets = data.setdefault("offsets", {})
    offsets[str(event_id)] = end_offset
    data["last_id"] = max(int(data.get("last_id") or 0), event_id)
    _save_offsets_locked(data)


def append_event(
    kind: str,
    *,
    job_id: str = "",
    pane: str = "",
    target: str = "",
    source: str = "",
    status: str = "",
    message: str = "",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with locked():
        return _append_event_locked(
            kind,
            job_id=job_id,
            pane=pane,
            target=target,
            source=source,
            status=status,
            message=message,
            data=data,
        )


def _append_event_locked(
    kind: str,
    *,
    job_id: str = "",
    pane: str = "",
    target: str = "",
    source: str = "",
    status: str = "",
    message: str = "",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """append_event() for a caller that already holds locked().

    locked() is not reentrant (every call opens a new file description, so a
    nested flock in the same process blocks forever); callers that must write
    a job file and its event atomically call this inside their own lock.
    """
    event_status = normalize_status(status) if status else ""
    event_data = dict(data or {})
    if job_id and event_status and event_status not in TERMINAL_STATUSES:
        # Consumers (Cards) rebuild job state by replaying event status.
        # A non-terminal status on an event for an already terminal job
        # would revive it there, so keep the claim only as data.
        current = read_json(job_path(job_id), {})
        current_status = normalize_status(str(current.get("status") or "")) if isinstance(current, dict) else ""
        if current_status in TERMINAL_STATUSES:
            event_data["ignored_status"] = event_status
            event_data["job_status"] = current_status
            event_status = ""
    event = {
        "id": _next_event_id_locked(),
        "ts": now_iso(),
        "kind": kind,
        "source": source,
        "job_id": job_id,
        "pane": pane,
        "target": target,
        "status": event_status,
        "message": compact(message, 700),
        "data": event_data,
    }
    payload = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    with EVENTS_FILE.open("a+b") as fh:
        end = fh.seek(0, os.SEEK_END)
        if end:
            fh.seek(end - 1)
            if fh.read(1) != b"\n":
                # A writer that crashed mid-line left a fragment without a
                # newline; appending straight after it would glue this
                # event onto the fragment and lose it too.  Close the
                # fragment off as its own (unparseable, skipped) line.
                payload = b"\n" + payload
        fh.write(payload)
        fh.flush()
        _record_event_offset_locked(int(event["id"]), fh.seek(0, os.SEEK_END))
    return event


def job_path(job_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(job_id))
    return JOBS_DIR / f"{safe}.json"


def upsert_job(job_id: str, *, reopen: bool = False, **fields: Any) -> dict[str, Any]:
    """Create or update one job.

    A terminal job never moves back to an active status unless the caller
    passes ``reopen=True`` explicitly.  A refused update changes nothing,
    records a ``job_reopen_rejected`` audit event, and returns the unchanged
    job so the caller sees the real (terminal) status.
    """
    if not job_id:
        raise ValueError("job_id is required")
    rejected: dict[str, str] = {}
    with locked():
        path = job_path(job_id)
        job = read_json(path, {})
        if not isinstance(job, dict):
            job = {}
        created = not bool(job)
        now = now_iso()
        if created:
            job = {"id": job_id, "created_at": fields.get("created_at") or now}
        previous_status = normalize_status(str(job.get("status") or ""))
        requested_status = normalize_status(str(fields.get("status") or "")) if fields.get("status") else ""
        reopening = (
            previous_status in TERMINAL_STATUSES
            and bool(requested_status)
            and requested_status not in TERMINAL_STATUSES
        )
        if reopening and not reopen:
            rejected = {"previous_status": previous_status, "requested_status": requested_status}
        else:
            if reopening:
                job.pop("completed_at", None)
            for key, value in fields.items():
                if value is not None:
                    job[key] = value
            if "status" in job:
                job["status"] = normalize_status(str(job.get("status") or ""))
            job["updated_at"] = fields.get("updated_at") or now
            write_json_atomic(path, job)

        # The event goes out under the same lock as the job file: appended
        # after the lock is released, two concurrent upserts could leave the
        # job file at B while events.jsonl ends with A (replayed state wrong).
        if rejected:
            _append_event_locked(
                "job_reopen_rejected",
                job_id=job_id,
                pane=str(job.get("pane") or ""),
                target=str(job.get("target") or ""),
                source=str(fields.get("source") or ""),
                message=(
                    f"refused {rejected['previous_status']} -> {rejected['requested_status']}; "
                    "a terminal job only reopens with an explicit reopen"
                ),
                data={**rejected, "requested_message": compact(str(fields.get("message") or ""), 300)},
            )
            return job

        status = normalize_status(str(job.get("status") or ""))
        event_kind = "job_created" if created else ("job_status_changed" if status and status != previous_status else "")
        if event_kind:
            _append_event_locked(
                event_kind,
                job_id=job_id,
                pane=str(job.get("pane") or ""),
                target=str(job.get("target") or ""),
                source=str(job.get("source") or ""),
                status=status,
                message=str(job.get("message") or job.get("task_preview") or ""),
                data={"previous_status": previous_status} if previous_status else {},
            )
    return job


def get_job(job_id: str) -> dict[str, Any] | None:
    path = job_path(job_id)
    if not path.exists():
        return None
    data = read_json(path, {})
    return data if isinstance(data, dict) else None


def list_jobs(
    *,
    pane: str = "",
    status: str = "",
    limit: int = 50,
    include_terminal: bool = True,
) -> list[dict[str, Any]]:
    ensure_dirs()
    wanted_status = normalize_status(status) if status else ""
    jobs: list[dict[str, Any]] = []
    for path in JOBS_DIR.glob("*.json"):
        data = read_json(path, {})
        if not isinstance(data, dict):
            continue
        job_status = normalize_status(str(data.get("status") or ""))
        if pane and str(data.get("pane") or "") != pane:
            continue
        if wanted_status and job_status != wanted_status:
            continue
        if not include_terminal and job_status in TERMINAL_STATUSES:
            continue
        jobs.append(data)
    jobs.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    return jobs[: max(1, limit)]


def latest_job_for_pane(pane: str, *, include_terminal: bool = True) -> dict[str, Any] | None:
    jobs = list_jobs(pane=pane, limit=1, include_terminal=include_terminal)
    return jobs[0] if jobs else None


def latest_jobs_by_pane(*, include_terminal: bool = True) -> dict[str, dict[str, Any]]:
    ensure_dirs()
    latest: dict[str, dict[str, Any]] = {}
    for path in JOBS_DIR.glob("*.json"):
        data = read_json(path, {})
        if not isinstance(data, dict):
            continue
        pane = str(data.get("pane") or "")
        if not pane:
            continue
        if not include_terminal and normalize_status(str(data.get("status") or "")) in TERMINAL_STATUSES:
            continue
        previous = latest.get(pane)
        if previous is None or str(data.get("updated_at") or data.get("created_at") or "") > str(previous.get("updated_at") or previous.get("created_at") or ""):
            latest[pane] = data
    return latest


def read_events(
    *,
    after: int = 0,
    limit: int = 100,
    pane: str = "",
    job_id: str = "",
) -> tuple[list[dict[str, Any]], int]:
    ensure_dirs()
    events: list[dict[str, Any]] = []
    # Cursor resets must use the last event that is durably visible in the
    # JSONL + offsets index, not the allocation counter.  append_event()
    # reserves an id before it appends the line; exposing that reserved id as
    # a readable head lets a client advance past an event that has not landed
    # yet and then miss it permanently.
    global_last_id = current_committed_event_id()
    if global_last_id and after > global_last_id:
        return [], global_last_id
    last_id = after
    if not EVENTS_FILE.exists():
        return [], last_id
    max_events = max(1, min(limit, 1000))
    with EVENTS_FILE.open("rb") as fh:
        if after > 0:
            fh.seek(0, os.SEEK_END)
            fh.seek(_offset_after_event_id(fh, after, fh.tell()))
        for line in fh:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            event_id = int(event.get("id") or 0)
            last_id = max(last_id, event_id)
            if event_id <= after:
                continue
            if pane and str(event.get("pane") or "") != pane:
                continue
            if job_id and str(event.get("job_id") or "") != job_id:
                continue
            events.append(event)
            if len(events) >= max_events:
                break
    return events, last_id


def current_last_event_id() -> int:
    try:
        counter_last = int(COUNTER_FILE.read_text(encoding="utf-8").strip() or "0")
    except (FileNotFoundError, ValueError):
        counter_last = 0
    return max(current_committed_event_id(), counter_last)


def current_committed_event_id() -> int:
    """Return the highest event id durably committed to events.jsonl.

    The allocation counter may briefly lead this value while append_event()
    is between reserving an id and appending its line.  Consumer cursors and
    API ``head_id`` values must use this committed head.
    """
    return _last_committed_event_id()


def parse_ts(ts: str) -> float:
    if not ts:
        return 0.0
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def pruned_legacy_dir(day: str = "") -> Path:
    """Where prune moves old ledger records: <BUS>/_legacy/event-ledger-pruned/<date>/."""
    return LEDGER.parent / "_legacy" / "event-ledger-pruned" / (day or datetime.now().strftime("%Y-%m-%d"))


PRUNE_README = """# event-ledger prune 归档

- 来源：`{ledger}`（Agent Bus 事件账本：`jobs/*.json` 与 `events.jsonl`）。
- 原因：`event_ledger.py prune --days N --yes` 把最后更新早于 N 天的 job 文件，以及
  这些 job 的事件和同样过期的事件行移到这里，而不是直接删除。
- 内容：`jobs/` 是原样移走的 job 文件；`events-pruned-*.jsonl` 是移出的事件行（原文）。
- 恢复：把 `jobs/*.json` 移回账本 `jobs/`；把需要的事件行按 id 顺序合并回
  `events.jsonl`（事件 id 必须保持递增，读取端靠它二分定位），然后运行一次
  `event_ledger.py events --after 0 --limit 1` 确认可读。
"""


def _unique_destination(path: Path) -> Path:
    if not path.exists():
        return path
    stamp = datetime.now().strftime("%H%M%S-%f")
    return path.with_name(f"{path.stem}.{stamp}{path.suffix}")


def prune(days: float, *, yes: bool = False) -> dict[str, Any]:
    """Move jobs/events older than ``days`` to the ledger's _legacy area.

    Dry-run (the default) only counts.  Nothing is deleted: job files and
    event lines are moved under ``pruned_legacy_dir()`` with a README that
    says how to restore them.
    """
    cutoff = time.time() - days * 86400
    removed_jobs = 0
    kept_job_ids: set[str] = set()
    legacy = pruned_legacy_dir()
    with locked():
        old_job_paths: list[Path] = []
        for path in JOBS_DIR.glob("*.json"):
            data = read_json(path, {})
            if not isinstance(data, dict):
                continue
            ts = parse_ts(str(data.get("updated_at") or data.get("created_at") or ""))
            if ts and ts < cutoff:
                old_job_paths.append(path)
                removed_jobs += 1
            else:
                kept_job_ids.add(str(data.get("id") or path.stem))
        removed_events = 0
        kept_lines: list[str] = []
        pruned_lines: list[str] = []
        if EVENTS_FILE.exists():
            with EVENTS_FILE.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    record = line if line.endswith("\n") else line + "\n"
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        if yes:
                            removed_events += 1
                            pruned_lines.append(record)
                        continue
                    ts = parse_ts(str(event.get("ts") or ""))
                    job_id = str(event.get("job_id") or "")
                    keep = (not ts or ts >= cutoff) and (not job_id or job_id in kept_job_ids)
                    if keep:
                        kept_lines.append(record)
                    else:
                        removed_events += 1
                        pruned_lines.append(record)
        if yes and (old_job_paths or pruned_lines):
            legacy.mkdir(parents=True, exist_ok=True)
            readme = legacy / "README.md"
            if not readme.exists():
                readme.write_text(PRUNE_README.format(ledger=LEDGER), encoding="utf-8")
            if pruned_lines:
                segment = _unique_destination(legacy / f"events-pruned-{datetime.now().strftime('%H%M%S')}.jsonl")
                segment.write_text("".join(pruned_lines), encoding="utf-8")
            if old_job_paths:
                (legacy / "jobs").mkdir(parents=True, exist_ok=True)
                for path in old_job_paths:
                    shutil.move(str(path), str(_unique_destination(legacy / "jobs" / path.name)))
            if EVENTS_FILE.exists():
                tmp = EVENTS_FILE.with_suffix(".jsonl.tmp")
                tmp.write_text("".join(kept_lines), encoding="utf-8")
                os.replace(tmp, EVENTS_FILE)
                _rebuild_offsets_locked()
        return {"jobs": removed_jobs, "events": removed_events, "legacy_dir": str(legacy)}


def _rebuild_offsets_locked() -> None:
    data: dict[str, Any] = {"offsets": {}, "last_id": 0}
    if EVENTS_FILE.exists():
        with EVENTS_FILE.open("r", encoding="utf-8") as fh:
            while True:
                line = fh.readline()
                if not line:
                    break
                end = fh.tell()
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_id = int(event.get("id") or 0)
                if event_id:
                    data["offsets"][str(event_id)] = end
                    data["last_id"] = max(int(data.get("last_id") or 0), event_id)
    _save_offsets_locked(data)


class ReapAbort(Exception):
    """A whole-batch stop condition: the evidence source cannot be trusted."""


def tmux_pane_snapshot() -> dict[str, Any]:
    """Read-only view of the default tmux server used to prove panes are gone.

    Returns ``{"server_start": epoch, "panes": {pane_id: {"pane_pid", "pane_start_time"}}}``.
    """
    from cli_bridge import process_start_time  # same start-token format jobs record

    # Run from inside another tmux server (a test socket), `tmux` would talk
    # to that server via $TMUX, and every job would look "gone".  Pin the
    # server the Cards/secretary windows live on.
    env = {key: value for key, value in os.environ.items() if key != "TMUX"}
    socket = os.environ.get("SECRETARY_BUS_TMUX_SOCKET", "")
    command = ["tmux", *(["-S", socket] if socket else []), "list-panes", "-a", "-F",
               "#{start_time}\t#{pane_id}\t#{pane_pid}"]
    try:
        cp = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReapAbort(f"tmux query failed: {exc}") from exc
    if cp.returncode != 0:
        raise ReapAbort(f"tmux query failed: {cp.stderr.strip() or f'exit {cp.returncode}'}")
    starts: set[str] = set()
    panes: dict[str, dict[str, Any]] = {}
    for row in cp.stdout.splitlines():
        if not row.strip():
            continue
        try:
            start, pane_id, raw_pid = row.split("\t")
            pane_pid = int(raw_pid)
        except ValueError as exc:
            raise ReapAbort(f"unexpected tmux row: {row!r}") from exc
        starts.add(start)
        panes[pane_id] = {"pane_pid": pane_pid, "pane_start_time": process_start_time(pane_pid)}
    if len(starts) > 1:
        raise ReapAbort(f"tmux reported several server start times: {sorted(starts)}")
    return {"server_start": float(starts.pop()) if starts else 0.0, "panes": panes}


def _strict_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise ReapAbort(f"cannot read leader state {path}: {exc}") from exc


def leader_protected_jobs(now: float) -> dict[str, str]:
    """Map job id -> leader id for jobs owned by a running leader holding a live claim."""
    sessions = LEDGER.parent / "leader-sessions"
    claims = _strict_json(sessions / "claims.json") or {}
    if not isinstance(claims, dict):
        raise ReapAbort(f"leader claims are not an object: {sessions / 'claims.json'}")
    live = {
        str(pane_id): claim
        for pane_id, claim in claims.items()
        if isinstance(claim, dict) and float(claim.get("lease_until") or 0) > now
    }
    protected: dict[str, str] = {}
    for path in sorted(sessions.glob("*.json")) if sessions.exists() else []:
        if path.name == "claims.json":
            continue
        state = _strict_json(path)
        if not isinstance(state, dict) or str(state.get("status") or "") != "running":
            continue
        leader_id = str(state.get("id") or path.stem)
        owned = {pane_id for pane_id, claim in live.items() if claim.get("leader_id") == leader_id}
        if not owned:
            continue
        protected[f"leader:{leader_id}"] = leader_id
        for worker in (state.get("workers") or {}).values():
            if isinstance(worker, dict) and str(worker.get("pane_id") or "") in owned:
                for job_id in worker.get("job_ids") or []:
                    protected[str(job_id)] = leader_id
    return protected


def _recorded_identity(job: dict[str, Any]) -> tuple[int, str]:
    """pane_pid / pane_start_time frozen when the job was dispatched, if any."""
    pid = job.get("pane_pid")
    start = job.get("pane_start_time")
    secretary_dir = str(job.get("secretary_job_dir") or "")
    if not pid and secretary_dir:
        secretary = read_json(Path(secretary_dir) / "job.json", {})
        if isinstance(secretary, dict):
            pid = secretary.get("pane_pid")
            start = start or secretary.get("pane_start_time")
    try:
        return int(pid or 0), str(start or "")
    except (TypeError, ValueError):
        return 0, ""


def _job_pane(job: dict[str, Any]) -> str:
    pane = str(job.get("pane") or job.get("pane_id") or "")
    return pane if pane.startswith("%") else ""


def reap(
    *,
    yes: bool = False,
    now: float | None = None,
    tmux_query: Any = tmux_pane_snapshot,
    confirm_after: float = 60.0,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    """Terminalize active jobs whose process is provably gone (dry-run by default).

    A job is reaped only when all hold: it is active and not updated for 24h
    (1h for ``sent``); the process is proven dead (tmux server started after
    the job was created, or its pane id is absent from two queries of the same
    server ``confirm_after`` seconds apart, or the pane id now belongs to a
    different process); and no running leader with a live claim owns it.  The
    whole batch aborts when tmux cannot be queried, returns no panes, or none
    of the recently active jobs' panes can be seen (likely the wrong socket).
    Reaped jobs become ``interrupted``, never ``completed``.  A stale job whose
    pane identity still matches only gets a warning event.
    """
    now = time.time() if now is None else now
    result: dict[str, Any] = {"yes": yes, "aborted": "", "candidates": []}
    jobs = []
    for path in JOBS_DIR.glob("*.json") if JOBS_DIR.exists() else []:
        data = read_json(path, {})
        if isinstance(data, dict) and data.get("id"):
            jobs.append(data)

    def age_of(job: dict[str, Any]) -> float:
        ts = parse_ts(str(job.get("updated_at") or job.get("created_at") or ""))
        return now - ts if ts else float("inf")

    rows: list[dict[str, Any]] = []
    for job in jobs:
        status = normalize_status(str(job.get("status") or ""))
        if status not in REAP_ACTIVE_STATUSES:
            continue
        limit = REAP_SENT_MAX_AGE_SECONDS if status == "sent" else REAP_MAX_AGE_SECONDS
        age = age_of(job)
        if age < limit:
            continue
        rows.append(
            {
                "job_id": str(job["id"]),
                "source": str(job.get("source") or ""),
                "status": status,
                "age_hours": round(age / 3600, 1) if age != float("inf") else None,
                "pane": _job_pane(job),
                "updated_at": str(job.get("updated_at") or ""),
                "decision": "pending",
                "basis": "",
                "_job": job,
            }
        )
    rows.sort(key=lambda row: str(row["updated_at"]))
    result["candidates"] = rows
    if not rows:
        return _finish_reap(result)

    try:
        protected = leader_protected_jobs(now)
        first = tmux_query()
        if not first.get("panes"):
            raise ReapAbort("tmux query returned no panes")
        recent_panes = {
            _job_pane(job) for job in jobs if _job_pane(job) and age_of(job) < REAP_RECENT_SECONDS
        }
        if recent_panes and not recent_panes & set(first["panes"]):
            raise ReapAbort(
                f"none of the {len(recent_panes)} pane(s) of jobs updated in the last hour exist; "
                "probably the wrong tmux server/socket"
            )
    except ReapAbort as exc:
        result["aborted"] = str(exc)
        return _finish_reap(result)

    server_start = float(first.get("server_start") or 0)
    for row in rows:
        job = row["_job"]
        created = parse_ts(str(job.get("created_at") or ""))
        pane = row["pane"]
        if row["job_id"] in protected:
            row["decision"] = "protected"
            row["basis"] = f"owned by running leader {protected[row['job_id']]} with a live claim"
        elif created and server_start and server_start > created:
            row["decision"] = "reap"
            row["basis"] = (
                f"tmux server started {datetime.fromtimestamp(server_start).astimezone().isoformat(timespec='seconds')}"
                f", after the job was created {job.get('created_at')}"
            )
        elif not pane:
            row["decision"] = "skip"
            row["basis"] = "no pane recorded; cannot prove the process is gone"
        elif pane in first["panes"]:
            _judge_live_pane(row, first["panes"][pane])
        else:
            row["decision"] = "confirm"

    pending = [row for row in rows if row["decision"] == "confirm"]
    if pending:
        sleep(max(0.0, confirm_after))
        try:
            second = tmux_query()
            if not second.get("panes"):
                raise ReapAbort("second tmux query returned no panes")
            if float(second.get("server_start") or 0) != server_start:
                raise ReapAbort("tmux server changed between the two queries")
        except ReapAbort as exc:
            result["aborted"] = str(exc)
            return _finish_reap(result)
        for row in pending:
            if row["pane"] in second["panes"]:
                row["decision"] = "skip"
                row["basis"] = f"pane {row['pane']} reappeared between the two queries"
            else:
                row["decision"] = "reap"
                row["basis"] = (
                    f"pane {row['pane']} absent from two queries of the same tmux server "
                    f"{confirm_after:g}s apart"
                )

    if yes:
        for row in rows:
            if row["decision"] == "reap":
                current = get_job(row["job_id"]) or {}
                if (
                    normalize_status(str(current.get("status") or "")) not in REAP_ACTIVE_STATUSES
                    or str(current.get("updated_at") or "") != row["updated_at"]
                ):
                    row["decision"] = "skip"
                    row["basis"] = "job changed while reap was running; left untouched"
                    continue
                reaped_at = now_iso()
                upsert_job(
                    row["job_id"],
                    status="interrupted",
                    completed_at=reaped_at,
                    reaped_at=reaped_at,
                    reap_basis=row["basis"],
                    message=f"reaped: process gone ({row['basis']})",
                )
                append_event(
                    "job_reaped",
                    job_id=row["job_id"],
                    pane=row["pane"],
                    target=str(current.get("target") or ""),
                    source="event-ledger-reap",
                    message=row["basis"],
                    data={"previous_status": row["status"], "age_hours": row["age_hours"]},
                )
                row["decision"] = "reaped"
            elif row["decision"] == "warn":
                append_event(
                    "job_reap_warning",
                    job_id=row["job_id"],
                    pane=row["pane"],
                    target=str(row["_job"].get("target") or ""),
                    source="event-ledger-reap",
                    message=f"{row['status']} for {row['age_hours']}h but {row['basis']}; not reaped",
                    data={"status": row["status"], "age_hours": row["age_hours"]},
                )
    return _finish_reap(result)


def _judge_live_pane(row: dict[str, Any], current: dict[str, Any]) -> None:
    recorded_pid, recorded_start = _recorded_identity(row["_job"])
    current_pid = int(current.get("pane_pid") or 0)
    current_start = str(current.get("pane_start_time") or "")
    if recorded_pid and current_pid and recorded_pid != current_pid:
        row["decision"] = "reap"
        row["basis"] = f"pane id {row['pane']} reused: recorded pane_pid {recorded_pid}, now {current_pid}"
    elif recorded_pid and recorded_start and current_start and recorded_start != current_start:
        row["decision"] = "reap"
        row["basis"] = f"pane id {row['pane']} reused: pane process start token changed"
    elif recorded_pid:
        row["decision"] = "warn"
        row["basis"] = f"pane {row['pane']} still exists with the recorded process identity"
    else:
        row["decision"] = "warn"
        row["basis"] = f"pane {row['pane']} still exists and the job recorded no process identity to compare"


def _finish_reap(result: dict[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for row in result["candidates"]:
        row.pop("_job", None)
        if result["aborted"] and row["decision"] == "pending":
            row["decision"] = "not_evaluated"
        counts[row["decision"]] = counts.get(row["decision"], 0) + 1
    result["counts"] = counts
    return result


def mark_completion_for_pane(pane: str, text: str, *, source: str, target: str = "") -> dict[str, Any] | None:
    status = completion_status_from_text(text)
    if not status:
        return None
    job = latest_job_for_pane(pane, include_terminal=False)
    if not job:
        return None
    job_id = str(job.get("id") or "")
    if not job_id:
        return None
    if normalize_status(str(job.get("status") or "")) in TERMINAL_STATUSES:
        return job
    updated = upsert_job(
        job_id,
        status=status,
        pane=pane,
        target=target or str(job.get("target") or ""),
        source=str(job.get("source") or source),
        completed_at=now_iso() if status in TERMINAL_STATUSES else "",
        message=f"Completion marker: {status}",
    )
    append_event(
        "completion_marker",
        job_id=job_id,
        pane=pane,
        target=target or str(job.get("target") or ""),
        source=source,
        status=status,
        message=f"COMPLETION_STATUS parsed as {status}",
    )
    return updated


def cmd_jobs(args: argparse.Namespace) -> None:
    jobs = list_jobs(
        pane=args.pane,
        status=args.status,
        limit=args.limit,
        include_terminal=not args.active_only,
    )
    if args.json:
        print(json.dumps({"jobs": jobs}, ensure_ascii=False, indent=2))
        return
    if not jobs:
        print("(no jobs)")
        return
    for job in jobs:
        print(
            f"{job.get('id')}\tstatus={job.get('status','')}\tpane={job.get('pane','')}\t"
            f"target={job.get('target','')}\tupdated={job.get('updated_at','')}\t{job.get('task_preview','')}"
        )


def cmd_status(args: argparse.Namespace) -> None:
    job = get_job(args.job_id)
    if not job:
        raise SystemExit(f"job not found: {args.job_id}")
    print(json.dumps(job, ensure_ascii=False, indent=2))


def cmd_events(args: argparse.Namespace) -> None:
    events, last_id = read_events(after=args.after, limit=args.limit, pane=args.pane, job_id=args.job_id)
    if args.json:
        print(json.dumps({"events": events, "last_id": last_id}, ensure_ascii=False, indent=2))
        return
    for event in events:
        print(
            f"{event.get('id')}\t{event.get('ts')}\t{event.get('kind')}\t"
            f"job={event.get('job_id','')}\tstatus={event.get('status','')}\t{event.get('message','')}"
        )


def cmd_watch(args: argparse.Namespace) -> None:
    after = args.after
    deadline = time.time() + args.timeout if args.timeout else 0
    # An idle observation only wakes this waiter if it happened after the
    # point the caller asked about (--after), or, without --after, after the
    # watch started — an old idle from a previous turn must not return at once.
    idle_floor = args.after if args.after else current_committed_event_id()
    while True:
        events, last_id = read_events(after=after, limit=args.limit, pane=args.pane, job_id=args.job_id)
        after = max(after, last_id)
        for event in events:
            print(json.dumps(event, ensure_ascii=False), flush=True)
            if args.until_terminal and normalize_status(str(event.get("status") or "")) in TERMINAL_STATUSES:
                return
            # Cards no longer finishes leader/supervisor jobs from an idle
            # screen; it records pane_idle_observed instead.  Wake the waiter
            # so it can collect/verify — idle itself proves nothing.
            if (
                args.until_terminal
                and event.get("kind") == "pane_idle_observed"
                and int(event.get("id") or 0) > idle_floor
            ):
                print(
                    "watch: worker pane went idle (not proof of completion); "
                    "collect or verify with independent evidence next",
                    file=sys.stderr,
                    flush=True,
                )
                return
        if deadline and time.time() >= deadline:
            return
        time.sleep(args.interval)


def cmd_post(args: argparse.Namespace) -> None:
    """Append a small non-job coordination event from another local producer."""
    data: dict[str, Any] = {}
    if args.path:
        data["path"] = args.path
    if args.sha256:
        data["sha256"] = args.sha256
    event = append_event(
        args.kind,
        source=args.source,
        target=args.target,
        status=args.status,
        message=args.message,
        data=data,
    )
    print(json.dumps(event, ensure_ascii=False))


def cmd_prune(args: argparse.Namespace) -> None:
    result = prune(args.days, yes=args.yes)
    if args.yes:
        print(f"moved {result['jobs']} job(s) and {result['events']} event(s) to {result['legacy_dir']}")
    else:
        print(
            f"dry-run only; add --yes to move {result['jobs']} job(s) and {result['events']} event(s) "
            f"to {result['legacy_dir']}"
        )


def cmd_reap(args: argparse.Namespace) -> int:
    result = reap(yes=args.yes, confirm_after=max(0.0, args.confirm_after))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for row in result["candidates"]:
            print(
                f"{row['job_id']}\tsource={row['source'] or '-'}\tstatus={row['status']}\t"
                f"age_h={row['age_hours']}\tdecision={row['decision']}\t{row['basis']}"
            )
        summary = " ".join(f"{key}={value}" for key, value in sorted(result["counts"].items())) or "no candidates"
        if result["aborted"]:
            print(f"ABORTED (nothing written): {result['aborted']}")
        mode = "written" if args.yes else "dry-run only; add --yes to write"
        print(f"reap {mode}: {summary}")
    return 2 if result["aborted"] else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Secretary Bus job/event ledger")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("jobs")
    p.add_argument("--pane", default="")
    p.add_argument("--status", default="")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--active-only", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_jobs)

    p = sub.add_parser("status")
    p.add_argument("job_id")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("events")
    p.add_argument("--after", type=int, default=0)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--pane", default="")
    p.add_argument("--job-id", default="")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_events)

    p = sub.add_parser("watch")
    p.add_argument("--after", type=int, default=0)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--pane", default="")
    p.add_argument("--job-id", default="")
    p.add_argument("--interval", type=float, default=1.5)
    p.add_argument("--timeout", type=float, default=0)
    p.add_argument("--until-terminal", action="store_true")
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("post")
    p.add_argument("--kind", required=True)
    p.add_argument("--source", default="")
    p.add_argument("--target", default="")
    p.add_argument("--status", default="")
    p.add_argument("--message", default="")
    p.add_argument("--path", default="")
    p.add_argument("--sha256", default="")
    p.set_defaults(fn=cmd_post)

    p = sub.add_parser("prune", help="Move jobs/events older than --days to <bus>/_legacy/event-ledger-pruned/<date>/")
    p.add_argument("--days", type=float, default=30)
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_prune)

    p = sub.add_parser("reap", help="Mark active jobs whose tmux process is provably gone as interrupted (dry-run by default)")
    p.add_argument("--yes", action="store_true", help="Write the interrupted status / warning events")
    p.add_argument(
        "--confirm-after",
        type=float,
        default=60.0,
        help="Seconds between the two tmux queries that must both miss a pane (default 60)",
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_reap)

    args = parser.parse_args()
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
