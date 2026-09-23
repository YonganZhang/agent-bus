#!/usr/bin/env python3
"""Tmux card dashboard API for AI sessions.

The service exposes tmux metadata, parsed scrollback/transcripts, preference
storage, image uploads, and narrowly-scoped pane input/key endpoints for the
local cards dashboard.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import functools
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse


ROOT = Path(__file__).resolve().parent
AGENT_BUS_SCRIPTS = ROOT.parent / "scripts"
if AGENT_BUS_SCRIPTS.exists():
    sys.path.insert(0, str(AGENT_BUS_SCRIPTS))

# 读取 Claude/Codex 自身状态的模块与 Agent Bus 共用一份（仓库的 scripts/）。
import claude_sessions  # noqa: E402
import cli_bridge  # noqa: E402
import claude_subagents  # noqa: E402
import codex_subagents  # noqa: E402
import dialogs  # noqa: E402
import event_ledger  # noqa: E402
import pane_detectors  # noqa: E402
import provider_state  # noqa: E402
import tmux_delivery  # noqa: E402
import trace_data  # noqa: E402

INDEX = ROOT / "index.html"
TRACE_VIEW_JS = ROOT / "trace_view.js"
TRACE_VIEW_CSS = ROOT / "trace_view.css"


def asset_version() -> str:
    """Cheap fingerprint of the served frontend, read fresh off disk each call.

    index.html is served straight from disk with no server restart required
    (see do_GET), so a browser tab can silently keep running JS from before a
    fix landed with no signal that anything changed. Comparing this against
    what the tab saw on its own last poll lets the frontend prompt a reload
    instead of a stale tab silently keeping the old behavior after a fix.
    """
    try:
        stat = INDEX.stat()
    except OSError:
        return ""
    return hashlib.sha1(f"{stat.st_mtime_ns}:{stat.st_size}".encode("utf-8")).hexdigest()[:12]
# Runtime state lives outside the code tree (default ~/.codex/agent-bus/card-dashboard,
# override with AGENT_BUS_DASHBOARD_STATE_DIR); see cli_bridge.DASHBOARD_STATE_DIR.
STATE_DIR = cli_bridge.DASHBOARD_STATE_DIR
UPLOAD_DIR = STATE_DIR / "uploads"
SHARED_FILES_DIR = Path(
    os.environ.get("TMUX_CARD_SHARED_FILES_DIR", str(STATE_DIR / "shared_files"))
).expanduser()
SHARED_CHUNKS_DIR = SHARED_FILES_DIR / ".chunks"
# Workspace whose deliverables (PDF, images, archives, ...) mentioned in AI
# replies may be downloaded through the authenticated /local-files/ route.
LOCAL_ARTIFACT_ROOT = Path(
    os.environ.get("TMUX_CARD_LOCAL_ARTIFACT_ROOT", str(Path.home()))
).expanduser()
# Optional: a public static host whose /path maps to LOCAL_ARTIFACT_ROOT/<dir>/path,
# so links to it can be previewed through the authenticated route instead.
PUBLIC_SHARE_HOST = os.environ.get("TMUX_CARD_PUBLIC_SHARE_HOST", "").strip()
PUBLIC_SHARE_DIR = os.environ.get("TMUX_CARD_PUBLIC_SHARE_DIR", "share-public").strip().strip("/")
LOCAL_ARTIFACT_SUFFIXES = frozenset({
    ".pdf",
    ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp", ".svg",
    ".mp4", ".webm", ".ogv", ".ogg", ".mov", ".m4v", ".mkv",
    ".mp3", ".wav", ".m4a", ".aac", ".flac",
    ".zip", ".7z", ".tar", ".gz", ".bz2", ".xz",
    ".html", ".htm",
})
LOCAL_ARTIFACT_SENSITIVE_NAME_RE = re.compile(
    r"(?:^|[._-])(?:credentials?|secrets?|passwords?|passwd|tokens?|api[-_]?keys?|id[-_](?:rsa|ed25519))"
    r"(?:[._-]|$)",
    re.I,
)
PREFS = STATE_DIR / "prefs.json"  # 卡片分类/别名/顺序/活动时间的服务端持久化(清缓存/换设备不丢)
PREFS_LOCK = threading.RLock()
SEND_REQUEST_LOCK = threading.RLock()
# Basic Auth credentials file (WEBTERM_USER / WEBTERM_PASS); TMUX_CARD_USER /
# TMUX_CARD_PASS environment variables take precedence.
WEBTERM_ENV = Path(os.environ.get("WEBTERM_ENV", str(cli_bridge.BUS / "webterm.env"))).expanduser()
HOST = os.environ.get("TMUX_CARD_HOST", "127.0.0.1")
PORT = int(os.environ.get("TMUX_CARD_PORT", "7795"))
COMMAND_TIMEOUT = float(os.environ.get("TMUX_CARD_TIMEOUT", "3"))
SUBMIT_DELAY = float(os.environ.get("TMUX_CARD_SUBMIT_DELAY", "0.18"))  # paste→C-m 间隔; 配合 bracketed paste 作余量
DEFAULT_SESSION = os.environ.get("TMUX_CARD_SESSION", "secretary_web")
TMUX_SOCKET = os.environ.get("TMUX_CARD_TMUX_SOCKET", "").strip()
TMUX_LABEL = os.environ.get("TMUX_CARD_TMUX_LABEL", "").strip()
_CAPTURE_SCOPE = TMUX_SOCKET or (f"label:{TMUX_LABEL}" if TMUX_LABEL else "default")
_CAPTURE_LOCK_SUFFIX = (
    "" if _CAPTURE_SCOPE == "default"
    else "-" + hashlib.sha256(_CAPTURE_SCOPE.encode("utf-8")).hexdigest()[:12]
)
PANE_CAPTURE_LOCK = Path(
    os.environ.get(
        "TMUX_CARD_CAPTURE_LOCK",
        f"/run/user/{os.getuid()}/tmux-card-preview-capture{_CAPTURE_LOCK_SUFFIX}.lock",
    )
).expanduser()
PANE_CAPTURE_LOCK_TIMEOUT = max(
    0.0, float(os.environ.get("TMUX_CARD_CAPTURE_LOCK_TIMEOUT", "5"))
)
URL_PREFIX = os.environ.get("TMUX_CARD_URL_PREFIX", "/cards").rstrip("/")
MAX_UPLOAD_BYTES = int(os.environ.get("TMUX_CARD_MAX_UPLOAD_BYTES", str(8 * 1024 * 1024)))
MAX_SHARED_FILE_BYTES = int(os.environ.get("TMUX_CARD_MAX_SHARED_FILE_BYTES", "0"))  # 0 = no app-level cap
SHARED_UPLOAD_CHUNK_BYTES = int(os.environ.get("TMUX_CARD_SHARED_UPLOAD_CHUNK_BYTES", str(32 * 1024 * 1024)))
SHARED_FILES_LIST_LIMIT = int(os.environ.get("TMUX_CARD_SHARED_FILES_LIST_LIMIT", "500"))
SHARED_CHUNK_TTL_SECONDS = int(os.environ.get("TMUX_CARD_SHARED_CHUNK_TTL_SECONDS", str(24 * 60 * 60)))
NOISE_RULE_RE = re.compile(r"[─━═—]{2,}")
ELAPSED_SEPARATOR_RE = re.compile(
    r"^\s*[─━═—]\s*Worked for\s+[0-9hms\s]+[─━═—\s]*",
    re.I,
)
CLAUDE_CHROME_STATUS_RE = re.compile(
    r"^[✻✳✢✶✷✸✹✺✽]\s+[\w -]+?\s+for\s+[0-9hms\s]+(?:\s+·\s+.*)?$",
    re.I,
)
AI_RUNNING_STATUS_RE = re.compile(
    r"^[+✻✳✢✶✷✸✹✺✽•●·◦◉○]?\s*"
    r"(?:working|running|thinking|composing|thundering|processing|writing|reading|editing|searching|analyzing|reasoning"
    r"|思考中|分析中|处理中|生成中|运行中|执行中|搜索中|编辑中|写入中)"
    r"(?:\s+[\w-]+){0,4}\s*(?:\.{1,3}|…)?\s*"
    r"\((?=[^)]*(?:tokens?|esc to interrupt|[↓↑]|[0-9]+\s*[hms][^)]*·|·[^)]*[0-9]+\s*[hms]))[^)]*\)",
    re.I,
)
# Provider wording is intentionally open-ended (Claude cycles many verbs and
# may show Reconnecting / Compacting / arbitrary gerunds).  These fallbacks
# key off terminal-control structure, not a vocabulary: a spinner-prefixed row
# that advertises an interrupt action, or the companion background-task
# controls, is runtime chrome by definition.
INTERRUPTIBLE_RUNTIME_STATUS_RE = re.compile(
    r"^[+*✻✳✢✶✷✸✹✺✽•●⠂⠐⠈⠁·⠿◦◉○]\s*.*\b(?:esc|ctrl\s*\+?\s*c)\s+to\s+interrupt\b.*$",
    re.I,
)
BACKGROUND_RUNTIME_CONTROL_RE = re.compile(
    r"^.*\b\d+\s+background\s+(?:terminals?|tasks?)\s+running\b"
    r".*(?:/ps\s+to\s+view|/stop\s+to\s+close).*$",
    re.I,
)
WORKFLOW_RUNNING_STATUS_RE = re.compile(
    r"(?:"
    r"\bwaiting\s+for\s+\d+\s+dynamic\s+workflows?\s+to\s+finish\b"
    # Claude Code's own background-subagent chrome ("✻ Waiting for 1
    # background agent to finish · N messages hidden (/focus to show)" and
    # the plain "Running 1 agent…" line) carries no elapsed-time parenthetical,
    # so it matched none of the "running" regexes and was misread as idle
    # during the gap before the subagent's own spinner appears (a job was then
    # marked completed while it was still processing).
    r"|\bwaiting\s+for\s+\d+\s+background\s+agents?\s+to\s+finish\b"
    r"|^running\s+\d+\s+agents?\s*(?:…|\.{2,3})?\s*$"
    # "✻ Sautéed for 39s · done 9:17 pm · 2 shells still running" —— 同一行里既有
    # "done" 又有还在跑的后台 shell。Claude 官方把这种算 busy;只看 "done" 会判成 idle,
    # 用户以为跑完了。
    r"|\b\d+\s+shells?\s+still(?:\s+running)?\b"
    r"|(?:^|[·\s])\d+\s*/\s*\d+\s+agents?\s+done\b.*(?:tokens?|[↓↑]|[0-9]+\s*[hms])"
    r"|^[◯○●]\s+\S+.*\b\d+\s*/\s*\d+\s+agents?\s+done\b"
    r")",
    re.I,
)
# Claude 的底栏/收尾行里列出的后台项："· 1 monitor ·"、"done 1:12 am · 1 monitor still running"、
# "2 shells still running"。
CLAUDE_BACKGROUND_RE = re.compile(r"\b(\d+)\s+(monitors?|shells?)\b", re.I)
CLAUDE_BACKGROUND_TAIL_LINES = 6
CLAUDE_TOOL_SUMMARY_RE = re.compile(
    r"^[•●·◦◉○]?\s*"
    r"(?:read|reading|ran|running|called|editing|edited|wrote|writing|created|opened|searched|listed|grepped|globbed|bash|write|edit)"
    r"\s+\d+\s+(?:files?|tools?|shell commands?|commands?)\b"
    r"[^.!?]{0,180}$",
    re.I,
)
# 中文 AI 思考状态（无时间括号形式，仅用于 preview 过滤，不用于状态判断）
ZH_THINKING_RE = re.compile(
    r"^[+✻✳✢✶✷✸✹✺✽•●·]?\s*(?:思考中|分析中|处理中|生成中|运行中|执行中|搜索中|编辑中|写入中)\s*$",
)
# Fallback "turn finished" signal when no structured end_turn is available:
# a Markdown heading whose text is one of these words (``|``-separated,
# optionally preceded by one symbol such as an emoji).  Configure with
# TMUX_CARD_SUMMARY_HEADINGS; the browser receives the same list.
SUMMARY_HEADINGS = [
    word.strip() for word in os.environ.get("TMUX_CARD_SUMMARY_HEADINGS", "Summary").split("|") if word.strip()
]
HUMAN_SUMMARY_RE = re.compile(
    r"^\s{0,3}#{1,6}\s*(?:[^\w\s]\s*)?(?:"
    + ("|".join(re.escape(word) for word in SUMMARY_HEADINGS) or r"(?!)")
    + r")\s*$",
    re.I | re.M,
)
CLAUDE_PROJECT_RULE_RE = re.compile(r"^[\s─━═—]{3,}.*[─━═—]{2,}\s*$")
CLAUDE_SURVEY_HEADER = "How is Claude doing this session?"
AUTH_CACHE_MTIME: float | None = None
AUTH_CACHE_VALUE: tuple[str, str] | None = None
PANE_ACTIVITY_GRACE = float(os.environ.get("TMUX_CARD_ACTIVITY_GRACE", "1.8"))
PANE_ACTIVITY_CACHE: dict[str, dict[str, object]] = {}
PANE_JOB_STALE_SECONDS = int(os.environ.get("TMUX_CARD_JOB_STALE_SECONDS", str(6 * 60 * 60)))
CARDS_RESPONSE_ECHO_GRACE_SECONDS = float(
    os.environ.get("TMUX_CARD_RESPONSE_ECHO_GRACE_SECONDS", "3.0")
)
JOB_CACHE_TTL_SECONDS = float(os.environ.get("TMUX_CARD_JOB_CACHE_TTL_SECONDS", "1.0"))
JOB_CACHE: dict[bool, dict[str, object]] = {}
NEWEST_CARDS_JOB_CACHE: dict[str, tuple[float, dict[str, object]]] = {}
JOB_EVENT_RECORDS_CACHE: dict[str, object] = {}
JOB_EVENT_RECORDS_LOCK = threading.RLock()
PROVIDER_RUNTIME_TTL_SECONDS = float(os.environ.get("TMUX_CARD_PROVIDER_RUNTIME_TTL_SECONDS", "2.0"))
PROVIDER_RUNTIME_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
CATEGORY_VIEWS = ("最近", "全部")
FIXED_ASSIGNABLE_CATEGORIES = ("开发", "论文", "私人", "待处理", "其他")
FIXED_CATEGORY_ORDER = (*CATEGORY_VIEWS, *FIXED_ASSIGNABLE_CATEGORIES)
# A single poll observing pane_status=="idle" is not proof a job is actually
# done - infer_status() only has a fixed vocabulary/pattern list for "still
# working" chrome, and real multi-step turns (dispatching a subagent, the gap
# between a message being sent and the first spinner frame rendering) can
# show no recognized indicator for a few real seconds without the task being
# finished. Require idle to be observed continuously for this long before
# completing a "running" job, instead of trusting one sample.
JOB_COMPLETION_IDLE_SECONDS = float(os.environ.get("TMUX_CARD_JOB_COMPLETION_IDLE_SECONDS", "6.0"))
JOB_IDLE_SINCE: dict[str, float] = {}
TRACE_MAX_SPANS = int(os.environ.get("TMUX_CARD_TRACE_MAX_SPANS", "2000"))
TRACE_MAX_RESPONSE_BYTES = int(os.environ.get("TMUX_CARD_TRACE_MAX_RESPONSE_BYTES", str(2 * 1024 * 1024)))
TRACE_CACHE_ITEMS = int(os.environ.get("TMUX_CARD_TRACE_CACHE_ITEMS", "8"))
TRACE_BUILD_SEMAPHORE = threading.BoundedSemaphore(
    max(1, int(os.environ.get("TMUX_CARD_TRACE_BUILD_CONCURRENCY", "1")))
)
TRACE_CACHE_LOCK = threading.RLock()
TRACE_CACHE: OrderedDict[tuple[object, ...], dict[str, object]] = OrderedDict()


@dataclass
class Pane:
    pane_id: str
    target: str
    session: str
    window_index: int
    pane_index: int
    window_name: str
    command: str
    cwd: str
    title: str
    active: bool
    kind: str
    project: str
    preview: str
    status: str
    # A tmux pane id such as ``%41`` may be reused after a pane closes.  The
    # PID + procfs start token freeze the exact pane instance for destructive
    # actions such as the Cards close button.
    pane_pid: str = ""
    pane_start_time: str = ""
    # SessionStart hook 钉在 pane 上的会话身份(见 contrib/claude-hooks/tmux-session-stamp.sh)。
    # 这是 Claude 自己交出来的值,不是靠屏幕内容猜的,所以优先级高于一切推断。
    ai_session_id: str = ""
    ai_transcript: str = ""
    ai_alive: bool = True
    # Identity quality is separate from runtime status.  A pane can be idle or
    # busy while its provider session is still ambiguous (for example when
    # Codex has two rollout files open); never silently present that as a
    # confidently completed conversation.
    identity_fidelity: str = ""
    identity_reason: str = ""
    # 回完话后仍在后台挂着的监控（只提示，不算工作中），如 "后台监控 1 个"。
    background: str = ""


def job_summary(job: dict[str, object] | None) -> dict[str, object]:
    if not job:
        return {}
    keys = [
        "id",
        "status",
        "source",
        "pane",
        "target",
        "task_preview",
        "message",
        "created_at",
        "updated_at",
        "response_started_at",
        "completed_at",
        "repo",
        "codex_run_id",
        "codex_thread_id",
        "queue_id",
        "secretary_report",
    ]
    return {key: job.get(key, "") for key in keys if job.get(key)}


def _job_creation_key(job: dict[str, object]) -> tuple[str, str]:
    return (str(job.get("created_at") or ""), str(job.get("id") or ""))


def _job_order_key(job: dict[str, object]) -> tuple[int, str, str]:
    """Prefer durable event order, with legacy creation timestamps as fallback."""
    return (
        int(job.get("_created_seq") or 0),
        str(job.get("created_at") or ""),
        str(job.get("id") or ""),
    )


def _job_events_signature() -> tuple[str, int, int, int]:
    """Return a cheap identity for the append-only event ledger."""
    path = event_ledger.EVENTS_FILE
    try:
        stat = path.stat()
    except FileNotFoundError:
        return (str(path), 0, 0, 0)
    return (str(path), int(stat.st_ino), int(stat.st_size), int(stat.st_mtime_ns))


def _apply_job_event(
    jobs: dict[str, dict[str, object]],
    event: dict[str, object],
    line_number: int,
) -> None:
    job_id = str(event.get("job_id") or "").strip()
    if not job_id:
        return
    job = jobs.setdefault(
        job_id,
        {
            "id": job_id,
            "created_at": str(event.get("ts") or ""),
            "_created_seq": int(event.get("id") or line_number),
        },
    )
    kind = str(event.get("kind") or "")
    timestamp = str(event.get("ts") or "")
    if kind == "job_created":
        job["created_at"] = timestamp
        job["_created_seq"] = int(event.get("id") or line_number)
        if event.get("message"):
            job["task_preview"] = str(event.get("message") or "")
    for key in ("pane", "target", "source"):
        if event.get(key):
            job[key] = str(event.get(key) or "")
    if event.get("status"):
        status = event_ledger.normalize_status(str(event.get("status") or ""))
        job["status"] = status
        if status in event_ledger.TERMINAL_STATUSES:
            job["completed_at"] = timestamp
        else:
            job.pop("completed_at", None)
    if event.get("message"):
        job["message"] = str(event.get("message") or "")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    if data.get("task_preview"):
        job["task_preview"] = str(data.get("task_preview") or "")
    if data.get("response_started_at"):
        job["response_started_at"] = str(data.get("response_started_at") or "")
    job["updated_at"] = timestamp


def _read_job_event_records(
    path: Path,
    *,
    offset: int = 0,
    line_count: int = 0,
    jobs: dict[str, dict[str, object]] | None = None,
) -> tuple[dict[str, dict[str, object]], int, int]:
    """Apply complete JSONL records from ``offset`` and return the safe tail.

    Writers append one newline-terminated record while holding the ledger lock.
    A reader may still meet an in-progress final line, so the cache advances
    only past complete lines and retries that partial tail on the next poll.
    """
    records = {key: dict(value) for key, value in (jobs or {}).items()}
    consumed = offset
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return records, consumed, line_count
    with handle:
        handle.seek(offset)
        while True:
            line = handle.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                break
            consumed += len(line)
            line_count += 1
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                continue
            if isinstance(event, dict):
                _apply_job_event(records, event, line_count)
    return records, consumed, line_count


def _enrich_small_job_records(jobs: dict[str, dict[str, object]]) -> None:
    # Tiny ledgers are common in unit tests and fresh installations.  Exact
    # enrichment preserves optional fields such as an explicitly supplied
    # ``created_at`` without creating the production failure mode: the live
    # ledger has thousands of files and therefore deliberately stays on the
    # sequential event-only path.
    if len(jobs) > 200:
        return
    for job_id, event_job in jobs.items():
        exact = event_ledger.get_job(job_id)
        if exact:
            created_seq = event_job.get("_created_seq")
            event_job.update(exact)
            event_job["_created_seq"] = created_seq


def _processed_job_signature(
    initial_signature: tuple[str, int, int, int],
    consumed: int,
) -> tuple[str, int, int, int]:
    """Represent only bytes actually parsed when a writer appended mid-read."""
    current = _job_events_signature()
    if current[:2] == initial_signature[:2] and current[2] == consumed:
        return current
    return (initial_signature[0], initial_signature[1], consumed, -1)


def _rebuild_job_records_from_events() -> tuple[
    tuple[str, int, int, int],
    dict[str, dict[str, object]],
    int,
    int,
]:
    """Reconstruct the job fields Cards needs from one sequential JSONL read.

    The per-job state directory contains thousands of tiny files.  Reading all
    of them for every card-grid poll becomes catastrophic when the data volume
    is under unrelated random-I/O load: one production refresh remained stuck
    for almost two hours and made ``/api/panes`` return 503.  The event ledger
    is append-only and records every job creation and status transition, so it
    is the appropriate hot-path index.  Exact job files remain the write-side
    source of truth and are still read by ``get_job`` for idempotent sends.
    """
    signature = _job_events_signature()
    path = event_ledger.EVENTS_FILE
    jobs, offset, line_count = _read_job_event_records(path)
    _enrich_small_job_records(jobs)
    return _processed_job_signature(signature, offset), jobs, offset, line_count


def _job_records_from_events_cached() -> tuple[tuple[str, int, int, int], dict[str, dict[str, object]]]:
    signature = _job_events_signature()
    with JOB_EVENT_RECORDS_LOCK:
        cached_signature = JOB_EVENT_RECORDS_CACHE.get("signature")
        cached_jobs = JOB_EVENT_RECORDS_CACHE.get("jobs")
        if cached_signature == signature and isinstance(cached_jobs, dict):
            return signature, cached_jobs  # type: ignore[return-value]
        cached_offset = JOB_EVENT_RECORDS_CACHE.get("offset")
        cached_line_count = JOB_EVENT_RECORDS_CACHE.get("line_count")
        can_apply_append = (
            isinstance(cached_signature, tuple)
            and len(cached_signature) == 4
            and isinstance(cached_jobs, dict)
            and isinstance(cached_offset, int)
            and isinstance(cached_line_count, int)
            and signature[:2] == cached_signature[:2]
            and signature[2] > cached_offset
        )
        if can_apply_append:
            jobs, offset, line_count = _read_job_event_records(
                event_ledger.EVENTS_FILE,
                offset=cached_offset,
                line_count=cached_line_count,
                jobs=cached_jobs,
            )
            _enrich_small_job_records(jobs)
            processed_signature = _processed_job_signature(signature, offset)
        else:
            processed_signature, jobs, offset, line_count = _rebuild_job_records_from_events()
        JOB_EVENT_RECORDS_CACHE["signature"] = processed_signature
        JOB_EVENT_RECORDS_CACHE["jobs"] = jobs
        JOB_EVENT_RECORDS_CACHE["offset"] = offset
        JOB_EVENT_RECORDS_CACHE["line_count"] = line_count
        return processed_signature, jobs


def event_jobs_cached(
    *,
    pane: str = "",
    status: str = "",
    limit: int = 50,
    include_terminal: bool = True,
) -> list[dict[str, object]]:
    """Fast Cards read view over job lifecycle events.

    It intentionally mirrors ``event_ledger.list_jobs`` for the fields and
    filters used by this server, without scanning every per-job JSON file.
    """
    _signature, records = _job_records_from_events_cached()
    wanted_status = event_ledger.normalize_status(status) if status else ""
    jobs: list[dict[str, object]] = []
    for value in records.values():
        job_status = event_ledger.normalize_status(str(value.get("status") or ""))
        if pane and str(value.get("pane") or "") != pane:
            continue
        if wanted_status and job_status != wanted_status:
            continue
        if not include_terminal and job_status in event_ledger.TERMINAL_STATUSES:
            continue
        jobs.append(value)
    jobs.sort(
        key=lambda item: (
            str(item.get("updated_at") or item.get("created_at") or ""),
            int(item.get("_created_seq") or 0),
        ),
        reverse=True,
    )
    return jobs[: max(1, limit)]


def latest_job_summary_for_pane(pane_id: str, *, include_terminal: bool = True) -> dict[str, object]:
    return job_summary(latest_jobs_by_pane_cached(include_terminal=include_terminal).get(pane_id))


def latest_jobs_by_pane_cached(*, include_terminal: bool = True) -> dict[str, dict[str, object]]:
    signature, _records = _job_records_from_events_cached()
    cached = JOB_CACHE.get(include_terminal)
    if cached and cached.get("signature") == signature:
        return cached.get("jobs", {})  # type: ignore[return-value]
    # A status update changes updated_at.  It must not make an older message
    # the pane's newest conversation turn (closing an old stale job after a
    # newer job completed would otherwise make the old one look newest).
    # Derive current lineage from immutable creation order instead.
    jobs: dict[str, dict[str, object]] = {}
    for job in event_jobs_cached(limit=50_000, include_terminal=include_terminal):
        pane_id = str(job.get("pane") or "")
        if not pane_id:
            continue
        previous = jobs.get(pane_id)
        job_order = _job_order_key(job)
        previous_order = _job_order_key(previous) if previous else (-1, "", "")
        if previous is None or job_order > previous_order:
            jobs[pane_id] = job
    JOB_CACHE[include_terminal] = {"signature": signature, "jobs": jobs}
    return jobs


def invalidate_job_cache() -> None:
    JOB_CACHE.clear()
    NEWEST_CARDS_JOB_CACHE.clear()


def provider_runtime_observation(job: dict[str, object], pane_status: str) -> dict[str, str]:
    """Resolve an ambiguous active supervisor job through exact provider state.

    Terminal chrome remains the cheap primary signal.  We only call the
    identity-safe provider adapter when an Agent Bus job says ``running`` but
    the terminal looks idle, and only accept a result bound to the same frozen
    pane id.  This avoids both regex blind spots and stale target remaps.
    """
    if pane_status not in {"idle", "no output", "shell"}:
        return {}
    if event_ledger.normalize_status(str(job.get("status") or "")) != "running":
        return {}
    if str(job.get("source") or "") != "secretary-bus-supervisor":
        return {}
    target = str(job.get("target") or "").strip()
    pane_id = str(job.get("pane") or "").strip()
    if not target or not pane_id:
        return {}
    cache_key = f"{target}|{pane_id}"
    now = time.time()
    cached = PROVIDER_RUNTIME_CACHE.get(cache_key)
    if cached and now - cached[0] <= PROVIDER_RUNTIME_TTL_SECONDS:
        return dict(cached[1])
    observation: dict[str, str] = {}
    try:
        snapshot = provider_state.snapshot_target(target, max_chars=160)
        runtime = snapshot.get("runtime") if isinstance(snapshot.get("runtime"), dict) else {}
        state = snapshot.get("state") if isinstance(snapshot.get("state"), dict) else {}
        value = str(state.get("value") or "unknown")
        confidence = str(state.get("confidence") or "low")
        source = str(state.get("source") or "unknown")
        if str(runtime.get("pane_id") or "") == pane_id:
            if value == "needs_input":
                observation = {"status": "waiting", "source": source, "confidence": confidence}
            elif value in {"busy", "idle"} and confidence in {"high", "authoritative"}:
                observation = {
                    "status": "running" if value == "busy" else "idle",
                    "source": source,
                    "confidence": confidence,
                }
    except (Exception, SystemExit):
        observation = {}
    PROVIDER_RUNTIME_CACHE[cache_key] = (now, observation)
    return dict(observation)


def newest_cards_job_for_pane_cached(pane_id: str) -> dict[str, object]:
    now = time.time()
    cached = NEWEST_CARDS_JOB_CACHE.get(pane_id)
    if cached and now - cached[0] <= JOB_CACHE_TTL_SECONDS:
        return cached[1]
    jobs = [
        job for job in event_jobs_cached(pane=pane_id, limit=500, include_terminal=True)
        if str(job.get("source") or "") == "card-dashboard"
    ]
    newest = max(
        jobs,
        key=_job_order_key,
    ) if jobs else {}
    NEWEST_CARDS_JOB_CACHE[pane_id] = (now, newest)
    return newest


def complete_older_cards_jobs(pane_id: str, newer_job: dict[str, object]) -> list[str]:
    """Make the newest Cards message the only active Cards job for a pane.

    Event-ledger's active-only query previously skipped a completed newest job
    and resurfaced an hours-old running/waiting job for the same pane. That made
    the UI timer jump backwards (a completed newer job exposed an older stale
    job). Older messages are superseded once a newer Cards message
    exists; close them instead of letting them become current again.
    """
    newer_id = str(newer_job.get("id") or "")
    pane_jobs = event_jobs_cached(pane=pane_id, limit=500, include_terminal=True)
    indexed_newer = next(
        (job for job in pane_jobs if str(job.get("id") or "") == newer_id),
        newer_job,
    )
    newer_key = _job_order_key(indexed_newer)
    if not pane_id or not newer_id or str(newer_job.get("source") or "") != "card-dashboard":
        return []
    closed: list[str] = []
    completed_at = event_ledger.now_iso()
    active_pane_jobs = (
        job
        for job in pane_jobs
        if event_ledger.normalize_status(str(job.get("status") or ""))
        not in event_ledger.TERMINAL_STATUSES
    )
    for old in active_pane_jobs:
        old_id = str(old.get("id") or "")
        if (
            not old_id
            or old_id == newer_id
            or str(old.get("source") or "") != "card-dashboard"
            or _job_order_key(old) >= newer_key
        ):
            continue
        event_ledger.upsert_job(
            old_id,
            source="card-dashboard",
            status="completed",
            pane=pane_id,
            target=str(old.get("target") or ""),
            message=f"superseded by newer Cards job {newer_id}",
            completed_at=completed_at,
        )
        event_ledger.append_event(
            "job_superseded",
            job_id=old_id,
            pane=pane_id,
            target=str(old.get("target") or ""),
            source="card-dashboard",
            status="completed",
            message=f"superseded by newer Cards job {newer_id}",
            data={"newer_job_id": newer_id},
        )
        closed.append(old_id)
    if closed:
        invalidate_job_cache()
    return closed


def _normalized_job_prompt(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _cards_job_current_user_index(
    job: dict[str, object], blocks: list[dict[str, object]]
) -> int:
    task = _normalized_job_prompt(job.get("task_preview"))
    if not task:
        return -1
    task_truncated = task.endswith("…")
    task_prefix = task[:-1].rstrip() if task_truncated else task
    user_indices = [index for index, block in enumerate(blocks) if block.get("role") == "user"]
    if not user_indices:
        return -1
    user_index = user_indices[-1]
    user_text = _normalized_job_prompt(blocks[user_index].get("text"))
    prompt_matches = user_text == task or (
        task_truncated and bool(task_prefix) and user_text.startswith(task_prefix)
    )
    return user_index if prompt_matches else -1


def _cards_job_echo_grace_elapsed(job: dict[str, object]) -> bool:
    try:
        created_at = datetime.fromisoformat(
            str(job.get("created_at") or "").replace("Z", "+00:00")
        ).timestamp()
    except (TypeError, ValueError):
        return False
    return time.time() - created_at >= CARDS_RESPONSE_ECHO_GRACE_SECONDS


def cards_job_has_started_response(
    job: dict[str, object], blocks: list[dict[str, object]]
) -> bool:
    """Detect the first visible assistant block for the current Cards turn.

    This is deliberately separate from final completion. A commentary/status
    update means the user is no longer waiting for a first reply even though
    the underlying job can continue running. Persisting this transition keeps
    the UI correct after capture-tail trimming, reloads, and pane switches.
    """
    if job.get("response_started_at"):
        return True
    if str(job.get("source") or "") != "card-dashboard":
        return False
    if event_ledger.normalize_status(str(job.get("status") or "")) not in {
        "created", "sent", "queued", "starting", "running"
    }:
        return False
    user_index = _cards_job_current_user_index(job, blocks)
    if user_index < 0:
        return False
    user_block = blocks[user_index]
    response_visible = any(
        block.get("role") == "assistant" and bool(str(block.get("text") or "").strip())
        for block in blocks[user_index + 1:]
    )
    if not response_visible:
        return False
    return bool(user_block.get("pending")) or _cards_job_echo_grace_elapsed(job)


def cards_job_has_visible_response(job: dict[str, object], blocks: list[dict[str, object]]) -> bool:
    """Return true only for strong, current-turn Cards completion evidence.

    A Claude pane may keep a stale/lingering "Waiting for 1 background agent"
    footer after its main reply is already visible. Pane status must remain
    operationally running so capture polling continues, but the user-facing
    wait timer should end. Match the active Cards job's prompt to the LAST user
    block and require a following Markdown Human Summary heading. Claude live
    transcript blocks can supply ``pending`` evidence; Codex/degraded panes use
    a short echo grace period because their screen parser never sets it.
    """
    if str(job.get("source") or "") != "card-dashboard":
        return False
    if event_ledger.normalize_status(str(job.get("status") or "")) not in {
        "created", "sent", "queued", "starting", "running"
    }:
        return False
    user_index = _cards_job_current_user_index(job, blocks)
    if user_index < 0:
        return False
    user_block = blocks[user_index]
    for block in blocks[user_index + 1:]:
        if block.get("role") != "assistant":
            continue
        # final(transcript 的 end_turn)是首选证据; HUMAN_SUMMARY_RE 留给抓屏降级路径
        # (quality=degraded 时没有 transcript, 拿不到 final)。
        if (block.get("final") or HUMAN_SUMMARY_RE.search(str(block.get("text") or ""))) and (
            block.get("pending")
            or user_block.get("pending")
            or job.get("response_started_at")
            or _cards_job_echo_grace_elapsed(job)
        ):
            return True
    return False


def sync_cards_job_response_started_from_blocks(
    pane_id: str,
    target: str,
    blocks: list[dict[str, object]],
    job: dict[str, object] | None = None,
) -> dict[str, object]:
    current = job or latest_jobs_by_pane_cached(include_terminal=False).get(pane_id) or {}
    if not current or current.get("response_started_at"):
        return job_summary(current)
    if not cards_job_has_started_response(current, blocks):
        return job_summary(current)
    job_id = str(current.get("id") or "")
    started_at = event_ledger.now_iso()
    updated = event_ledger.upsert_job(
        job_id,
        source=str(current.get("source") or "card-dashboard"),
        status=str(current.get("status") or "running"),
        pane=pane_id,
        target=target or str(current.get("target") or ""),
        response_started_at=started_at,
        message="visible assistant response started",
    )
    event_ledger.append_event(
        "pane_response_started",
        job_id=job_id,
        pane=pane_id,
        target=target or str(current.get("target") or ""),
        source="card-dashboard",
        status=str(updated.get("status") or "running"),
        message="visible assistant response started",
        data={"response_started_at": started_at},
    )
    invalidate_job_cache()
    return job_summary(updated)


def sync_cards_job_completion_from_blocks(
    pane_id: str,
    target: str,
    blocks: list[dict[str, object]],
    job: dict[str, object] | None = None,
) -> dict[str, object]:
    current = job or latest_jobs_by_pane_cached(include_terminal=False).get(pane_id) or {}
    if not current or not cards_job_has_visible_response(current, blocks):
        return job_summary(current)
    job_id = str(current.get("id") or "")
    completed = event_ledger.upsert_job(
        job_id,
        source=str(current.get("source") or "card-dashboard"),
        status="completed",
        pane=pane_id,
        target=target or str(current.get("target") or ""),
        message="visible final response delivered",
        completed_at=event_ledger.now_iso(),
    )
    event_ledger.append_event(
        "pane_response_delivered",
        job_id=job_id,
        pane=pane_id,
        target=target or str(current.get("target") or ""),
        source="card-dashboard",
        status="completed",
        message="visible final response delivered",
    )
    JOB_IDLE_SINCE.pop(pane_id, None)
    invalidate_job_cache()
    complete_older_cards_jobs(pane_id, completed)
    return job_summary(completed)


# (pane, job) pairs whose current idle episode was already reported to the ledger.
IDLE_OBSERVED_REPORTED: set[tuple[str, str]] = set()


def sync_pane_job_status(pane_id: str, target: str, pane_status: str) -> dict[str, object]:
    job = latest_jobs_by_pane_cached(include_terminal=False).get(pane_id)
    newest_cards_job = newest_cards_job_for_pane_cached(pane_id)
    if job and newest_cards_job and _job_order_key(newest_cards_job) > _job_order_key(job):
        complete_older_cards_jobs(pane_id, newest_cards_job)
        job = latest_jobs_by_pane_cached(include_terminal=False).get(pane_id)
    if not job:
        return {}
    job_id = str(job.get("id") or "")
    current = event_ledger.normalize_status(str(job.get("status") or ""))
    next_status = ""
    if pane_status != "idle":
        JOB_IDLE_SINCE.pop(pane_id, None)
        IDLE_OBSERVED_REPORTED.discard((pane_id, job_id))
    if pane_status == "quota_limited":
        # A provider pause is not job completion or a request for user input.
        # Preserve the durable job so automatic recovery can continue it.
        return job_summary(job)
    if pane_status == "running" and current in {"created", "sent", "queued", "starting", "waiting_user"}:
        next_status = "running"
    elif pane_status == "waiting":
        next_status = "waiting_user"
    elif pane_status == "needs attention":
        next_status = "waiting_user"
    elif pane_status == "idle" and current == "running":
        # The pane genuinely went back to idle after being observed running.
        # infer_pane_status() already debounces this against PANE_ACTIVITY_GRACE,
        # so this is a settled idle state, not a mid-response flicker. Without
        # this branch a job created via /api/send stayed stuck showing "处理中"
        # (running) until PANE_JOB_STALE_SECONDS (6h) elapsed, because nothing
        # ever marked it complete unless the AI happened to print an explicit
        # "COMPLETION_STATUS: ..." text marker - a convention only explicitly
        # dispatched, auditable tasks follow, never a normal chat turn.
        #
        # A single idle sample is still not proof of completion though: e.g.
        # the gap between a message being sent and Claude's first spinner
        # frame rendering can itself read as idle for a moment. Require idle
        # to hold for JOB_COMPLETION_IDLE_SECONDS before committing to
        # "completed".
        since = JOB_IDLE_SINCE.setdefault(pane_id, time.time())
        if time.time() - since >= JOB_COMPLETION_IDLE_SECONDS:
            next_status = "completed"
            JOB_IDLE_SINCE.pop(pane_id, None)
    elif current in {"created", "sent", "running", "waiting_user"}:
        try:
            updated_at = datetime.fromisoformat(str(job.get("updated_at") or "").replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            updated_at = time.time()
        if time.time() - updated_at > PANE_JOB_STALE_SECONDS:
            next_status = "stale"
    source = str(job.get("source") or "card-dashboard")
    if next_status in event_ledger.TERMINAL_STATUSES and source != "card-dashboard":
        # Once per idle episode: without this every poll (~6 s) re-armed the
        # idle timer and appended another observation, flooding the ledger
        # and every leader watching it.
        if (pane_id, job_id) in IDLE_OBSERVED_REPORTED:
            return job_summary(job)
        IDLE_OBSERVED_REPORTED.add((pane_id, job_id))
        # Leader / supervisor jobs are finished by their owner's verify, close or
        # collect — an idle screen is not independent evidence of completion
        # (idle and self-reports never prove done; see docs/safety-model.md).  Record what Cards
        # saw so watchers can wake up, but leave the status alone.
        event_ledger.append_event(
            "pane_idle_observed" if next_status == "completed" else "pane_stale_observed",
            job_id=job_id,
            pane=pane_id,
            target=target or str(job.get("target") or ""),
            source="card-dashboard",
            status=current,
            message=f"pane status inferred as {pane_status}; owner ({source}) decides completion",
        )
        return job_summary(job)
    if next_status and next_status != current:
        job = event_ledger.upsert_job(
            job_id,
            source=source,
            status=next_status,
            pane=pane_id,
            target=target or str(job.get("target") or ""),
            message=f"pane status inferred as {pane_status}",
            completed_at=event_ledger.now_iso() if next_status in event_ledger.TERMINAL_STATUSES else "",
        )
        event_ledger.append_event(
            "pane_status",
            job_id=job_id,
            pane=pane_id,
            target=target or str(job.get("target") or ""),
            source="card-dashboard",
            status=next_status,
            message=f"pane status inferred as {pane_status}",
        )
        invalidate_job_cache()
    return job_summary(job)


def run_tmux(args: list[str]) -> subprocess.CompletedProcess[str]:
    prefix = ["tmux"]
    if TMUX_SOCKET:
        prefix.extend(["-S", TMUX_SOCKET])
    elif TMUX_LABEL:
        prefix.extend(["-L", TMUX_LABEL])
    try:
        return subprocess.run(
            [*prefix, *args],
            text=True,
            capture_output=True,
            timeout=COMMAND_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"tmux command timed out: {' '.join(args)}") from exc


def capture(target: str, history: int = 240, *, join_wrapped: bool = True) -> str:
    history = max(40, min(history, 5000))
    # -J: ask tmux for its own authoritative "this row is a wrap continuation
    # of the previous row" tracking and join wrapped lines back into their
    # original logical line, instead of leaving server.py to guess this via
    # regex heuristics after the fact (see ``tmux capture-pane -J``). The existing wrap-heuristic
    # functions (unwrap_prose_soft_wraps, normalize_file_edit_output, the
    # picker continuation check) are kept in place as a safety net; they
    # should mostly become no-ops now that real wraps are resolved upstream.
    # The grid/identity paths prefer -J because it removes terminal soft-wraps
    # before matching.  The selected timeline also needs a faithful visual
    # capture: some TUIs mark intentional rows as wrapped, and -J then glues a
    # vertical workflow (e.g. Step 1 ↓ Step 2 ↓ Step 3) into one horizontal sentence.
    args = ["capture-pane", "-p"]
    if join_wrapped:
        args.append("-J")
    args.extend(["-t", target, "-S", f"-{history}"])
    cp = run_tmux(args)
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.strip() or f"capture-pane failed: {target}")
    return trim_capture(cp.stdout)


def load_auth() -> tuple[str, str] | None:
    global AUTH_CACHE_MTIME, AUTH_CACHE_VALUE
    user = os.environ.get("TMUX_CARD_USER", "")
    password = os.environ.get("TMUX_CARD_PASS", "")
    if user and password:
        return user, password
    env_mtime = WEBTERM_ENV.stat().st_mtime if WEBTERM_ENV.exists() else None
    if env_mtime == AUTH_CACHE_MTIME:
        return AUTH_CACHE_VALUE
    if WEBTERM_ENV.exists():
        for line in WEBTERM_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if key == "WEBTERM_USER" and not user:
                user = value
            elif key == "WEBTERM_PASS" and not password:
                password = value
    AUTH_CACHE_MTIME = env_mtime
    if user and password:
        AUTH_CACHE_VALUE = (user, password)
    else:
        AUTH_CACHE_VALUE = None
    return AUTH_CACHE_VALUE


def trim_capture(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\x00", "").splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


# 纯行谓词。一次卡片刷新里同一行会被重复判定很多次(cProfile: 一轮 28234 次调用、
# 约占列表刷新一半墙钟),而结果只取决于行本身,缓存是安全的。
@functools.lru_cache(maxsize=4096)
def is_noise_rule_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    compact = re.sub(r"\s+", "", stripped)
    if len(compact) >= 2 and NOISE_RULE_RE.fullmatch(compact):
        return True
    return bool(ELAPSED_SEPARATOR_RE.fullmatch(stripped))


@functools.lru_cache(maxsize=8192)
def is_claude_chrome_line(line: str, survey_context: bool = False) -> bool:
    stripped = line.strip().replace("\u00a0", " ")
    if not stripped:
        return False
    indent = len(line) - len(line.lstrip(" \t"))
    unmarked = re.sub(r"^[•●]\s*", "", stripped).strip()
    if not unmarked:
        return False
    # Provider notices may use the same bullets/check marks as replies/Todos.
    # Recognize their complete CLI form before the generic block markers.
    if re.match(r"^(?:[⚠⎿]\s*)?(?:Usage limit reached\s*·|You.ve hit your (?:session|usage) limit\s*·|Continuing automatically at .+\s*·\s*esc|/usage-credits to continue now\s*$)", unmarked, re.I):
        return True
    if re.match(r"^(?:[✔✓]\s*)?Update installed\s*·\s*Restart to update\s*$", unmarked, re.I):
        return True
    if CLAUDE_CHROME_STATUS_RE.match(unmarked):
        return True
    if CLAUDE_PROJECT_RULE_RE.match(unmarked):
        return True
    if unmarked == "❯":
        return True
    if survey_context and unmarked in {"(optional)", "Dismiss", "focus"}:
        return True
    if unmarked in {"Dismiss", "focus"} and indent >= 8:
        return True
    if unmarked.startswith(CLAUDE_SURVEY_HEADER):
        return True
    if "hidden (/focus to show)" in unmarked:
        # This decorative suffix is often glued onto a genuine "still working"
        # prefix ("Waiting for N background agent(s)/dynamic workflow(s) to
        # finish · N messages hidden (/focus to show)"). Discarding the whole
        # line here ran BEFORE infer_status()/WORKFLOW_RUNNING_STATUS_RE ever
        # saw it, so a pane actively waiting on a dispatched subagent or
        # workflow was misclassified as idle, and its job was marked completed
        # while still processing. Only the pure "already-done" forms
        # ("Cooked/Worked/Brewed for … · N messages hidden") should still be
        # treated as chrome and dropped.
        # 同一行尾部还可能挂着 "· 2 shells still running" —— 那是"后台还没跑完",
        # 和 "waiting for N background agents" 同性质。整行当 chrome 丢掉,状态判定就
        # 永远看不到它,一个还在跑后台任务的窗口会显示成空闲。
        if not re.search(
            r"\bwaiting\s+for\s+\d+\s+(?:dynamic\s+workflows?|background\s+agents?)\s+to\s+finish\b"
            r"|\b\d+\s+shells?\s+still\b",
            unmarked,
            re.I,
        ):
            return True
    if re.match(r"^\d+\s*%\s+until\s+auto-compact\b", unmarked, re.I):
        return True
    if re.search(r"\b\d+\s+monitors?\s+still(?:\s+running)?\b", unmarked, re.I):
        return True
    if "new task? /clear to save" in unmarked:
        return True
    if "auto mode" in unmarked and ("⏵" in unmarked or "shift+tab" in unmarked):
        return True
    if re.match(r"^1:\s*Bad\b.*\b2:\s*Fine\b.*\b3:\s*Good\b.*\b0:", unmarked):
        return True
    # Claude Code ⎿ Tip 消息 (Use /btw / Ask a quick side question 等)
    if stripped.startswith("⎿") and re.search(r"\bTip\b", stripped[:40], re.I):
        return True
    return False


@functools.lru_cache(maxsize=4096)
def is_claude_tool_summary_line(line: str) -> bool:
    stripped = line.strip().replace("\u00a0", " ")
    if not stripped:
        return False
    unmarked = re.sub(r"^[•●·◦◉○]\s*", "", stripped).strip()
    if len(unmarked) > 180:
        return False
    return bool(CLAUDE_TOOL_SUMMARY_RE.match(unmarked))


def is_wrapped_claude_monitor_prefix(line: str) -> bool:
    stripped = line.strip().replace("\u00a0", " ")
    unmarked = re.sub(r"^[•●]\s*", "", stripped).strip()
    return bool(re.search(r"\b\d+\s+monitors?\s+still\s*$", unmarked, re.I))


@functools.lru_cache(maxsize=8192)
def clean_display_line(line: str, survey_context: bool = False) -> str | None:
    if is_noise_rule_line(line):
        return None
    if is_claude_chrome_line(line, survey_context=survey_context):
        return None
    if is_claude_tool_summary_line(line):
        return None
    if ELAPSED_SEPARATOR_RE.match(line):
        return ELAPSED_SEPARATOR_RE.sub("", line, count=1).lstrip()
    return line


# 一次卡片列表刷新里,同一份抓屏文本会被 infer_pane_status、preview_from 等各自过滤一遍。
# 这里不能缓存完整抓屏字符串：每个条目同时保留原始文本和清洗后的副本，512 个
# 大型滚动窗口会把数 GB 内存固定在 Python 进程里。行级谓词仍有小型 LRU 缓存；
# 只对整段文本做一次普通计算，避免以延迟换取不可控的内存增长。
def filter_display_lines(text: str) -> str:
    lines: list[str] = []
    survey_ttl = 0
    tip_ttl = 0
    skip_wrapped_monitor_running = False
    for line in text.splitlines():
        stripped = line.strip().replace("\u00a0", " ")
        if skip_wrapped_monitor_running and stripped.lower() == "running":
            skip_wrapped_monitor_running = False
            continue
        skip_wrapped_monitor_running = False
        if CLAUDE_SURVEY_HEADER in line:
            survey_ttl = 5
        if is_wrapped_claude_monitor_prefix(line):
            skip_wrapped_monitor_running = True
        if CODEX_TRUNCATION_RE.match(stripped):
            continue
        if re.match(r"^⚠\s+Skipped loading\s+\d+\s+skill", stripped):
            continue
        if re.match(r"^⚠\s+/.+?/SKILL\.md:", stripped):
            continue
        # Claude Code \u23bf Tip \u6d88\u606f\u53ca\u5176\u540e\u7eed\u884c\uff08\u4e00\u822c 2-3 \u884c\uff09
        if stripped.startswith("\u23bf") and re.search(r"\bTip\b", stripped[:40], re.I):
            tip_ttl = 3
            continue
        if tip_ttl > 0:
            tip_ttl -= 1
            continue
        cleaned = clean_display_line(line, survey_context=survey_ttl > 0)
        if survey_ttl > 0:
            survey_ttl -= 1
        if cleaned is not None:
            lines.append(cleaned)
    return "\n".join(lines)


def is_markdown_boundary_line(stripped: str) -> bool:
    if not stripped:
        return True
    return bool(
        stripped.startswith(("```", ">", "|", "└", "⎿"))
        or re.match(r"^(?:#{1,6}\s+|[-*+]\s+|\d+\.\s+|\d+\s+[+-](?:\s|$))", stripped)
        or parse_todo_summary_line(stripped) is not None
    )


TODO_SUMMARY_ITEM_RE = re.compile(
    r"^\s*(?:[└⎿]\s*)?"
    r"(?P<mark>☐|☑|☒|✔|✓|✅|◻|◼|□|\[[ xX✓]\])\s+"
    r"(?P<text>.+?)\s*$"
)
TODO_IN_PROGRESS_RE = re.compile(r"^\s*(?:[└⎿]\s*)?◼\s+\S")
TODO_SUMMARY_HEADER_RE = re.compile(
    r"^\s*(?P<text>\d+\s+tasks?\s+\([^)]*(?:done|completed|in progress|open|pending)[^)]*\))\s*$",
    re.I,
)
TODO_SUMMARY_MORE_RE = re.compile(
    r"^\s*(?:[└⎿]\s*)?(?P<text>[.…]\s*\+\d+\s+(?:completed|pending)\b.*)$",
    re.I,
)


def parse_todo_summary_line(line: str) -> str | None:
    """Normalize Claude/Codex TUI todo summaries into Markdown checklist rows."""
    normalized = line.replace("\u00a0", " ")
    header = TODO_SUMMARY_HEADER_RE.match(normalized)
    if header:
        return f"**{header.group('text').strip()}**"
    item = TODO_SUMMARY_ITEM_RE.match(normalized)
    if item:
        mark = item.group("mark").strip()
        checked = mark in {"☑", "☒", "✔", "✓", "✅", "[x]", "[X]", "[✓]"}
        box = "x" if checked else " "
        prefix = "**进行中：** " if mark == "◼" else ""
        return f"- [{box}] {prefix}{item.group('text').strip()}"
    more = TODO_SUMMARY_MORE_RE.match(normalized)
    if more:
        return f"- {more.group('text').strip()}"
    return None


def should_merge_soft_wrapped_line(previous: str, current: str) -> bool:
    """Detect tmux hard-wrap continuation in prose blocks.

    Codex wraps long terminal lines into a following line with leading spaces.
    Those spaces are terminal layout, not content. Keep deliberate Markdown
    boundaries and label/value lines intact.
    """
    stripped = current.strip()
    prev = previous.rstrip()
    if not prev or not stripped:
        return False
    # Directional workflow rows are semantic separators, not prose soft-wraps.
    # With a visual (non--J) capture they must remain separate so the browser
    # can render the pipeline vertically.
    if re.match(r"^[↓↑→←↘↗↙↖➡⬅⬆⬇]", stripped) or re.search(r"[↓↑→←↘↗↙↖➡⬅⬆⬇]", prev):
        return False
    if not re.match(r"^[ \t]{2,}\S", current):
        return False
    if is_markdown_boundary_line(stripped):
        return False
    if re.match(r"^[─━═-]{8,}$", stripped):
        return False
    if prev.endswith((":","：")):
        return False
    if re.match(r"^(?:/|~/|[A-Z_][A-Z0-9_]{2,}$)", stripped):
        return False
    return True


def soft_wrap_join_separator(previous: str, current: str) -> str:
    prev = previous.rstrip()
    cur = current.strip()
    if not prev or not cur:
        return ""
    prev_ch = prev[-1]
    cur_ch = cur[0]
    if re.match(r"[\u4e00-\u9fff，。！？；：、）】》”’]", prev_ch) and re.match(r"[\u4e00-\u9fff（【《“‘]", cur_ch):
        return ""
    return " "


def unwrap_prose_soft_wraps(text: str) -> str:
    """Merge terminal soft-wraps in AI/user prose while preserving structure."""
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            out.append(line.rstrip())
            in_fence = not in_fence
            continue
        if in_fence or not out or not stripped:
            out.append(line.rstrip())
            continue
        if should_merge_soft_wrapped_line(out[-1], line):
            out[-1] = f"{out[-1].rstrip()}{soft_wrap_join_separator(out[-1], line)}{stripped}"
        else:
            out.append(line.rstrip())
    return "\n".join(out)


# Second alternative handles diffs of files whose content lines start with
# "|" (e.g. markdown tables): the +/- marker sits directly against the pipe
# with no separating space, so it can't match the whitespace-or-end branch.
FILE_EDIT_DIFF_LINE_RE = re.compile(r"^\s*\d+\s+(?:[+-](?:\s|$)|[+\- ]?\|)")


def file_edit_wrap_join_separator(previous: str, current: str) -> str:
    prev = previous.rstrip()
    cur = current.strip()
    if not prev or not cur:
        return ""
    if re.search(r"[A-Za-z0-9_./-]$", prev) and re.match(r"[A-Za-z0-9_./-]", cur):
        return ""
    return soft_wrap_join_separator(previous, current)


def normalize_file_edit_output(text: str) -> str:
    """Keep Codex file-edit summaries line-oriented despite terminal hard-wraps."""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            out.append("")
            continue
        if FILE_EDIT_DIFF_LINE_RE.match(stripped):
            out.append(stripped)
            continue
        if out and re.match(r"^[ \t]{6,}\S", line) and out[-1].strip():
            out[-1] = f"{out[-1].rstrip()}{file_edit_wrap_join_separator(out[-1], line)}{stripped}"
            continue
        if out and not FILE_EDIT_DIFF_LINE_RE.match(out[-1].strip()) and not re.match(r"^[ \t]{2,}\S", line):
            out[-1] = f"{out[-1].rstrip()}{file_edit_wrap_join_separator(out[-1], line)}{stripped}"
            continue
        out.append(stripped)
    return "\n".join(out)


# A numbered option row inside a Claude picker, e.g. "❯ 1. Keep" / "  2. Drop".
# Multi-select prompts may prefix rows with a checkbox: "❯ ☐ 1. Keep".
# Optional box border and highlight marker; group 1 is the marker, group 2 the
# checkbox token, group 3 the number, group 4 the visible label. NOTE: ASCII ">"
# is deliberately NOT a marker — otherwise a Markdown blockquote list would be
# mis-detected as a picker.
CHOICE_OPTION_RE = re.compile(
    r"^\s*(?:[│|]\s*)?([❯›]?)\s*(?:(☐|☑|☒|\[[ xX✓]\])\s*)?(\d+)[.)]\s+(.+?)\s*[│|]?\s*$"
)
# Footer text Claude shows under an interactive picker.
CHOICE_HINT_RE = re.compile(
    r"(enter to (?:select|confirm|continue)|to select|to confirm|space to select|press space|"
    r"select multiple|multi-select|multiple selections?|toggle|↑↓|press \d|esc to|"
    # 中文 picker footer 必须是"动作组合",不匹配正文里单独的"确认/提交/空格"等常见词
    # (claude 回复正文出现"提交前必须填""确认一下"被误判成 picker)。
    r"回车(?:确认|选择|提交|继续)|按回车|空格(?:键|选择|选中)|多选模式|选择多个)",
    re.I,
)
CUSTOM_CHOICE_RE = re.compile(
    r"(自定义|自定義|其他|其它|输入|輸入|填写|填寫|补充|補充|自由|"
    r"custom|other|specify|type your own|write[- ]?in|freeform)",
    re.I,
)
# Box-drawing / blank line (picker borders), used to skip non-content rows.
_BORDER_CHARS = set("│|╭╮╰╯┌┐└┘├┤┬┴┼─━═= -")


def _is_border_or_blank(line: str) -> bool:
    t = line.strip()
    return not t or all(ch in _BORDER_CHARS for ch in t)


def _split_choice_question(lines: list[str], region_start: int) -> tuple[list[str], str]:
    """Move the trailing prompt/question paragraph into the choice block.

    Claude usually renders a picker as:

        <question text>

        ❯ 1. ...
          2. ...
        Enter to select

    If we only extract the option rows, the dashboard shows buttons without the
    question being asked. Keep the last paragraph before the option region as
    the structured `question`, while leaving earlier assistant text in place.
    """
    end = region_start
    while end > 0 and _is_border_or_blank(lines[end - 1]):
        end -= 1
    if end <= 0:
        return lines[:region_start], ""

    start = end - 1
    while start >= 0:
        line = lines[start]
        if _is_border_or_blank(line):
            break
        if CHOICE_OPTION_RE.match(line) or CHOICE_HINT_RE.search(line.strip()):
            break
        if end - start > 6:
            break
        start -= 1

    question_lines = [line.strip() for line in lines[start + 1:end] if line.strip()]
    if not question_lines:
        return lines[:region_start], ""
    return lines[:start + 1], "\n".join(question_lines).strip()


def _choice_box_checked(token: str | None) -> bool:
    if not token:
        return False
    compact = token.strip().lower()
    return compact in {"☑", "☒", "[x]", "[✓]"}


def _choice_is_custom(text: str) -> bool:
    return bool(CUSTOM_CHOICE_RE.search(text or ""))


def _looks_like_decision_checklist(question: str, options: list[dict]) -> bool:
    """Distinguish real Claude pickers from AI-generated "please decide" lists.

    Claude sometimes writes numbered follow-up questions under headings such as
    "待你拍板". Those are prompts for a natural-language reply, not mutually
    exclusive UI choices. If the terminal later shows a generic footer near the
    same region, we still should not turn the list into option buttons.
    """
    q = question or ""
    texts = [str(opt.get("text") or "") for opt in options]
    joined = "\n".join([q, *texts])
    if re.search(r"(待你拍板|你来?定|你决定|需你裁定|下一步据此|等你定|待你定|拍板)", joined):
        return True
    question_like = sum(
        1
        for text in texts
        if re.search(r"(\?|？|吗\b|要不要|是否|到底|待定|怎么|如何|该不该)", text)
    )
    # A real picker offers answers; a list where (nearly) every item is itself
    # a question is a checklist asking for a free-form reply.
    if len(texts) >= 3 and question_like >= max(2, len(texts) - 1):
        return True
    return False


def extract_choice_block(text: str) -> tuple[str, dict | None]:
    """Pull a trailing interactive Claude picker out of the capture.

    Claude renders AskUserQuestion / permission prompts as a numbered option
    list with one highlighted ``❯`` row and/or an explicit select/confirm
    footer. The old behaviour deleted that whole region, so on the dashboard the
    Claude choices vanished and could not be clicked. Here we instead detect the
    picker at the very bottom of the (already chrome-filtered) text and return it
    as a structured block the front end renders as clickable buttons.

    Returns ``(text_without_picker, choice_block_or_None)``. A plain numbered
    list in prose is NOT treated as a picker because it has neither a ``❯``
    highlight nor a select/confirm footer.
    """
    lines = text.splitlines()
    while lines and (not lines[-1].strip() or _is_border_or_blank(lines[-1])):
        lines.pop()
    n = len(lines)
    scan_start = max(0, n - 40)
    opt_idxs = [i for i in range(scan_start, n) if CHOICE_OPTION_RE.match(lines[i])]
    if len(opt_idxs) < 2:
        return text, None
    region_start = opt_idxs[0]
    last_opt = opt_idxs[-1]
    has_highlight = any(
        (CHOICE_OPTION_RE.match(lines[i]).group(1) if CHOICE_OPTION_RE.match(lines[i]) else "")
        in ("❯", "›")
        for i in opt_idxs
    )
    has_later_hint = any(CHOICE_HINT_RE.search(lines[i].strip()) for i in range(last_opt + 1, n))
    # Everything between the last option row and the bottom must be only
    # hint / border / blank / indented-continuation, otherwise the picker is not
    # actually trailing (there is real content after it) and we must not eat it.
    saw_hint = False
    hint_lines: list[str] = []
    for i in range(last_opt + 1, n):
        s = lines[i].strip()
        if not s or _is_border_or_blank(lines[i]):
            continue
        if CHOICE_HINT_RE.search(s):
            saw_hint = True
            hint_lines.append(s)
            continue
        if lines[i][:1] in (" ", "\t"):  # wrapped continuation of the last option
            continue
        if not saw_hint and (has_highlight or has_later_hint):  # tmux hard-wrap may drop indentation
            continue
        return text, None
    # Parse the region forward, merging wrapped continuation lines into the
    # option above them (long labels wrap in narrow 37-col mirror panes).
    options: list[dict] = []
    for i in range(region_start, n):
        m = CHOICE_OPTION_RE.match(lines[i])
        if m:
            marker = m.group(1)
            box = m.group(2) or ""
            label = m.group(4).strip().strip("│|").strip()
            options.append(
                {
                    "n": int(m.group(3)),
                    "text": label,
                    "selected": marker in ("❯", "›"),
                    "checkbox": bool(box),
                    "checked": _choice_box_checked(box),
                    "custom": _choice_is_custom(label),
                }
            )
            continue
        s = lines[i].strip()
        if CHOICE_HINT_RE.search(s):
            hint_lines.append(s)
            continue
        if not s or _is_border_or_blank(lines[i]):
            continue
        if options:
            cont = s.strip("│|").strip()
            if cont:
                options[-1]["text"] = (options[-1]["text"] + " " + cont).strip()
                options[-1]["custom"] = _choice_is_custom(options[-1]["text"])
    if len(options) < 2:
        return text, None
    # A real picker has a highlighted ❯ row and/or an explicit select/confirm
    # footer; a plain numbered list in prose has neither, so it is left alone.
    if not (any(opt["selected"] for opt in options) or saw_hint):
        return text, None
    nums = [opt["n"] for opt in options]
    if len(set(nums)) != len(nums):
        return text, None
    remaining_lines, question = _split_choice_question(lines, region_start)
    if _looks_like_decision_checklist(question, options):
        return text, None
    remaining = "\n".join(remaining_lines).rstrip()
    hint_text = "\n".join(hint_lines)
    custom_probe = "\n".join([question, hint_text, *[str(opt["text"]) for opt in options]])
    multiple = any(bool(opt.get("checkbox")) for opt in options) or bool(
        re.search(r"(space to select|press space|select multiple|multi-select|multiple selections?|toggle|空格|多选|选择多个)", hint_text, re.I)
    )
    allow_custom = any(bool(opt.get("custom")) for opt in options) or _choice_is_custom(custom_probe)
    block = {
        "role": "choice",
        "label": "选择 · 点击发送",
        "question": question,
        "options": options,
        "multiple": multiple,
        "allow_custom": allow_custom,
        "text": "\n".join(
            f'{"[x] " if opt.get("checked") else "[ ] " if opt.get("checkbox") else ""}{opt["n"]}. {opt["text"]}'
            for opt in options
        ),
    }
    return remaining, block


def live_choice_block(capture_text: str) -> dict | None:
    """Return the picker that is live on screen right now, or None.

    A real Claude/Codex picker (AskUserQuestion, permission prompt, /model ...)
    takes over the provider's input area, so while the normal input box is
    drawn at the bottom nothing above it can be answered with a key press.
    Numbered lists in the conversation above an input box are therefore never
    a picker, whatever select-looking words sit near them (the running footer's
    ``esc to interrupt``, a quoted ``Enter to confirm``). Only the input region
    itself is searched in that case, in case a short dialog was split as one.
    Structure is read from the raw capture, before chrome filtering.
    """
    split = pane_detectors.split_screen(capture_text.splitlines())
    lines = split.input_lines if split.has_input_region else capture_text.splitlines()
    _rest, choice = extract_choice_block(filter_display_lines("\n".join(lines)))
    return choice


NAV_HIGHLIGHT_RE = re.compile(r"^(\s*)❯\s+(\S.*?)\s*$")
_DIALOG_RULE_RE = re.compile(r"^\s*[─━═▔]{8,}")


def live_nav_choice_block(capture_text: str) -> dict | None:
    """An unnumbered dialog (``❯ Yes, …`` / ``  No, …``) that owns the input area.

    Only called when the provider itself says a dialog is open, and only when no
    input box is drawn.  Options have no digits, so the block is marked ``nav``:
    answering moves the highlight with arrow keys and is verified on screen
    (``choose_nav_option``) instead of typing a number.
    """
    lines = capture_text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or pane_detectors.split_screen(lines).has_input_region:
        return None
    tail = lines[-24:]
    hi = next((i for i in range(len(tail) - 1, -1, -1)
               if NAV_HIGHLIGHT_RE.match(tail[i]) and not CHOICE_OPTION_RE.match(tail[i])), None)
    if hi is None:
        return None
    col = len(NAV_HIGHLIGHT_RE.match(tail[hi]).group(1))

    def option_text(line: str) -> str | None:
        m = NAV_HIGHLIGHT_RE.match(line)
        if m and len(m.group(1)) == col:
            return m.group(2)
        indent = len(line) - len(line.lstrip(" "))
        if line.strip() and indent == col + 2:
            return line.strip()
        return None

    start = hi
    while start > 0 and option_text(tail[start - 1]) is not None:
        start -= 1
    end = hi
    while end + 1 < len(tail) and option_text(tail[end + 1]) is not None:
        end += 1
    # 选项下面只允许空行和操作提示，否则这不是停在底部的对话框。
    if any(line.strip() and not CHOICE_HINT_RE.search(line) for line in tail[end + 1:]):
        return None
    texts = [option_text(line) or "" for line in tail[start:end + 1]]
    if len(texts) < 2:
        return None
    question_lines: list[str] = []
    for line in reversed(tail[:start]):
        if _DIALOG_RULE_RE.match(line) or len(question_lines) >= 6:
            break
        if line.strip():
            question_lines.insert(0, line.strip())
    options = [
        {"n": i + 1, "text": text, "selected": start + i == hi, "checkbox": False,
         "checked": False, "custom": _choice_is_custom(text)}
        for i, text in enumerate(texts)
    ]
    return {
        "role": "choice",
        "label": "选择 · 点击发送",
        "question": "\n".join(question_lines),
        "options": options,
        "multiple": False,
        "allow_custom": False,
        "nav": True,
        "text": "\n".join(f"{o['n']}. {o['text']}" for o in options),
    }


def choose_nav_option(pane_id: str, target_text: str, *, max_steps: int = 12) -> Pane:
    """Answer an unnumbered dialog: move the highlight one row at a time, re-reading
    the screen after every step, and press Enter only once the highlighted row is
    the requested option.  Fails closed if the dialog disappears or changes.
    The driver is shared with Agent Bus (scripts/dialogs.py)."""
    pane = pane_by_id(pane_id)
    if pane is None:
        raise ValueError(f"pane not found or not in {DEFAULT_SESSION}: {pane_id}")
    note_web_input(pane.pane_id)

    def send(key: str) -> None:
        sent = run_tmux(["send-keys", "-t", pane.pane_id, key])
        if sent.returncode != 0:
            raise RuntimeError(sent.stderr.strip() or f"tmux send {key} failed")

    dialogs.drive(
        target_text,
        capture=lambda: capture(pane.pane_id, history=60, join_wrapped=False),
        send=send,
        max_steps=max_steps,
        sleep=lambda seconds: time.sleep(seconds),
        pane_id=pane.pane_id,
        tmux=tmux_query,
    )
    return pane


def tmux_query(args: list[str]) -> str:
    """stdout of one tmux command on the Cards server ('' on failure)."""
    cp = run_tmux(args)
    return cp.stdout if cp.returncode == 0 else ""


# Shared by is_model_status_line and strip_inline_model_status: a Codex/Claude
# footer line looks like "<model name> [<effort tag>] · <rest>". This used to
# be two hand-maintained copies of the same pattern (one per function); fixing
# a bug in one copy without the other let content-deletion survive a whole fix
# cycle - now there is exactly one definition.
#
# The filler-word slot between the model name and "·" used to accept ANY 0-3
# words (`[\w.-]+`), so a normal sentence starting with a bare brand word
# (Claude/GPT-) that happens to contain "·" a few words later (common
# in Chinese prose comparing models) was indistinguishable from a real footer
# line - and got silently deleted by strip_inline_model_status. Real captured
# footer lines only ever have 0 or 1 word there, and that word is always an
# effort-level tag - so restrict to a whitelist instead of "any word"
# (confirmed against 6 real captured samples; residual risk: prose that
# happens to say e.g. "Claude high 的表现..." is still a false positive,
# accepted as rare).
_MODEL_STATUS_PREFIX = r"(?:gpt-[\w.-]+|claude[\w.-]*)(?:\s+(?:high|medium|low|minimal|xhigh|none))?\s+·\s+"


@functools.lru_cache(maxsize=4096)
def is_model_status_line(line: str) -> bool:
    stripped = line.strip()
    if "·" not in stripped:
        return False
    return bool(re.match(rf"^{_MODEL_STATUS_PREFIX}.*$", stripped, re.I))


def is_queued_message_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("Messages to be submitted after next tool call") or stripped.startswith("↳ ")


QUEUED_MESSAGES_FOOTER_RE = re.compile(r"^\s*[❯›]?\s*Press up to edit queued messages\b", re.I)
INDENTED_QUEUED_PROMPT_RE = re.compile(r"^[ \t]+❯\s+(.+?)\s*$")


def _extract_single_queued_messages(capture_text: str) -> list[str]:
    """Extract Claude's no-header rendering for exactly one queued message.

    The broad per-line predicate intentionally stays narrow: an indented "❯"
    line is only treated as queued when the queue-edit footer is present and the
    candidate sits after a volatile/thinking line newer than the latest
    column-0 user prompt. That keeps normal submitted prompts, quoted "❯" text,
    and picker rows out of the queue path.
    """
    lines = capture_text.splitlines()
    footer_idxs = [i for i, line in enumerate(lines) if QUEUED_MESSAGES_FOOTER_RE.match(line.strip())]
    if not footer_idxs:
        return []

    for footer_idx in reversed(footer_idxs):
        scan_start = max(0, footer_idx - 80)
        for idx in range(footer_idx - 1, scan_start - 1, -1):
            m = INDENTED_QUEUED_PROMPT_RE.match(lines[idx])
            if not m:
                continue

            last_column0_prompt = -1
            last_volatile = -1
            for j in range(scan_start, idx):
                stripped = lines[j].strip()
                if re.match(r"^[❯›]\s+", lines[j]):
                    last_column0_prompt = j
                if is_volatile_status_line(stripped):
                    last_volatile = j
            if last_volatile <= last_column0_prompt:
                continue

            text = m.group(1).strip()
            k = idx + 1
            while k < footer_idx:
                stripped = lines[k].strip()
                if (
                    not stripped
                    or is_noise_rule_line(stripped)
                    or QUEUED_MESSAGES_FOOTER_RE.match(stripped)
                    or INDENTED_QUEUED_PROMPT_RE.match(lines[k])
                    or re.match(r"^[❯›]\s+", lines[k])
                    or is_volatile_status_line(stripped)
                    or is_claude_chrome_line(stripped)
                    or CHOICE_OPTION_RE.match(lines[k])
                ):
                    break
                if should_merge_soft_wrapped_line(text, lines[k]):
                    text = f"{text}{soft_wrap_join_separator(text, stripped)}{stripped}"
                    k += 1
                    continue
                break
            if text:
                return [text]
    return []


def extract_queued_messages(capture_text: str) -> list[str]:
    """用户在 Claude 思考时发的"排队消息": 屏幕上显示为
    `Messages to be submitted after next tool call` 头 + `↳ <消息>` 行; 当前
    Claude Code 对单条排队消息会省略 header/list, 改用 spinner 后方的缩进
    `❯ <消息>` 行 + `Press up to edit queued messages` footer。
    它在被 Claude 消费前不进 JSONL transcript, 所以要像 picker 一样从 live capture
    拉出来显示, 否则用户看不到自己排队的话。"""
    msgs: list[str] = []
    in_queue = False
    for line in capture_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Messages to be submitted after next tool call"):
            in_queue = True
            continue
        if not in_queue:
            continue
        if stripped.startswith("↳ "):
            text = stripped[2:].strip()
            if text:
                msgs.append(text)
            continue
        if not stripped or (is_noise_rule_line(stripped) and not msgs):
            # 空行随时容忍。纯分隔线只在"还没抓到任何一条消息"时容忍(header 和
            # 第一条 "↳ " 之间可能夹一条版式边框, 之前任何非空
            # 非"↳ "行都会把 in_queue 直接关掉, 这种边框会把整批排队消息丢光)。
            # 已经抓到过消息后再出现分隔线, 更像是列表的收尾边框, 仍按"队列结束"
            # 处理 —— 否则边框后面的页面 chrome(auto-mode footer 等缩进行)会被
            # should_merge_soft_wrapped_line 误当续行粘到最后一条消息上。
            continue
        if msgs and should_merge_soft_wrapped_line(msgs[-1], line):
            # 一条排队消息本身很长时, 终端会像普通 "❯ " 提示那样把它软换行,
            # 续行不会重复 "↳ " 前缀。旧逻辑把这种续行当成"队列已结束", 既截断
            # 当前消息又连带丢弃它后面所有正确带 "↳ " 前缀的消息。
            # 复用 unwrap_prose_soft_wraps 同款的 should_merge_soft_wrapped_line
            # 判断续行, 而不是发明新的启发式。
            msgs[-1] = f"{msgs[-1]}{soft_wrap_join_separator(msgs[-1], stripped)}{stripped}"
            continue
        in_queue = False
    for msg in _extract_single_queued_messages(capture_text):
        if msg not in msgs:
            msgs.append(msg)
    return msgs


# Codex does not use Claude's explicit queue header.  While a Codex turn is
# running, a queued prompt is kept in the input area and the footer changes to
# ``tab to queue message``.  ``parse_blocks`` intentionally drops the final
# ``›`` input line as an unsent draft, so without this small provider-specific
# extraction the queued prompt disappears from Cards.
CODEX_QUEUE_FOOTER_RE = re.compile(r"\btab\s+to\s+queue\s+message\b", re.I)
CODEX_INPUT_PROMPT_RE = re.compile(r"^\s*[›❯]\s+(?!Ask\s+Codex\s+to\s+do\s+anything\b)(.+?)\s*$", re.I)


def extract_codex_queued_messages(capture_text: str) -> list[str]:
    """Extract queued Codex input without treating the normal empty prompt as text.

    The footer is the only stable queue-mode marker observed in Codex's inline
    TUI.  Restrict the scan to the short region immediately above that footer;
    old conversation prompts elsewhere in scrollback must never become queue
    blocks.  A queued multiline prompt is joined with the same soft-wrap
    heuristic used by the Claude queue extractor.
    """
    lines = capture_text.splitlines()
    footer_indices = [i for i, line in enumerate(lines) if CODEX_QUEUE_FOOTER_RE.search(line)]
    if not footer_indices:
        return []
    for footer_idx in reversed(footer_indices):
        scan_start = max(0, footer_idx - 16)
        for idx in range(footer_idx - 1, scan_start - 1, -1):
            match = CODEX_INPUT_PROMPT_RE.match(lines[idx])
            if not match:
                continue
            text = match.group(1).strip()
            if not text:
                continue
            k = idx + 1
            while k < footer_idx:
                stripped = lines[k].strip()
                if (
                    not stripped
                    or CODEX_QUEUE_FOOTER_RE.search(stripped)
                    or CODEX_INPUT_PROMPT_RE.match(lines[k])
                    or is_volatile_status_line(stripped)
                    or is_noise_rule_line(stripped)
                ):
                    break
                if should_merge_soft_wrapped_line(text, stripped):
                    text = f"{text}{soft_wrap_join_separator(text, stripped)}{stripped}"
                    k += 1
                    continue
                break
            return [text]
    return []


def strip_inline_model_status(text: str) -> str:
    return re.sub(rf"(?:\\n|\n)+\s*{_MODEL_STATUS_PREFIX}[^\\\n]*", "", text, flags=re.I).strip("\n")


# claude/codex 思考 spinner 行: "[符号] 任意思考词… (12s · ↓ 3k tokens / esc to interrupt)"。
# 不依赖思考词词表(claude 有几十个随机词 Cogitating/Reticulating/Frolicking...),靠结构识别。
# 之前 spinner 被当内容,导致 preview 只剩一行 spinner、与终端对不上。
# 省略号必须是真省略号(… 或 2-3 个点),不能是单个句号。
#   why: 旧 `[…\.]{1,3}` 允许单个 `.`, 于是 "All tests pass. (2 minutes)" 这类
#   "≤5 个英文词 + 句号 + (数字+s/m/h词)" 的正文行被误判 volatile → 在 preview_from /
#   transcript 块切分(见 is_volatile_status_line 调用点)里被整块丢弃, 用户看不到正式内容。
#   真 spinner 一律是 `…`/`...`, 从不是单个句号; 漏判某一帧 spinner 只是短暂视觉, 丢正文是数据丢失。
SPINNER_LINE_RE = re.compile(
    r"^[+*✻✳✢✶✷✸✹✺✽•●⠂⠐⠈⠁·⠿◦◉○\s]*"
    r"[A-Za-z][\w'-]*(?:\s+[A-Za-z][\w'-]*){0,4}(?:…|\.{2,3})\s*"
    r"\(\s*\d+\s*[smh].*?\)\s*$",
    re.I,
)
# Codex 终端截断标记: "│ … +11 lines" / "… +5 lines (ctrl + t to view transcript)"
CODEX_TRUNCATION_RE = re.compile(
    r"^\s*(?:[│|]\s*)?…\s*\+\d+\s+lines?\b",
    re.I,
)


def is_volatile_status_line(line: str) -> bool:
    stripped = line.strip()
    return bool(
        AI_RUNNING_STATUS_RE.match(stripped)
        or INTERRUPTIBLE_RUNTIME_STATUS_RE.match(stripped)
        or BACKGROUND_RUNTIME_CONTROL_RE.match(stripped)
        or WORKFLOW_RUNNING_STATUS_RE.search(stripped)
        or SPINNER_LINE_RE.match(stripped)
        or ZH_THINKING_RE.match(stripped)
        or re.match(r"^(?:[•●·◦◉○]\s+)?Working\s+\(", stripped)
        or re.match(r"^(?:[•●·◦◉○]\s+)?Running\s+\(", stripped)
        or re.match(r"^(?:[•●·◦◉○]\s+)?Thinking\s+\(", stripped)
        or is_claude_chrome_line(stripped)
        or is_claude_tool_summary_line(stripped)
    )


def classify(command: str, title: str, window_name: str) -> str:
    blob = f"{command} {title} {window_name}".lower()
    if command == "claude" or "claude" in blob:
        return "Claude"
    if command == "codex" or "codex" in blob:
        return "Codex"
    if command == "node":
        return "Codex"
    if command in {"bash", "zsh", "fish", "sh"}:
        return "Shell"
    return command or "Process"


# Window names that say nothing about the project (tmux defaults to the login
# user or the terminal server name).
_GENERIC_WINDOW_NAMES = frozenset(name for name in {os.environ.get("USER", ""), "ttyd"} if name)


def project_from(cwd: str, window_name: str, title: str) -> str:
    for value in (window_name, title):
        cleaned = value.strip()
        cleaned = re.sub(r"^[✻✳✢✶✷✸✹✺✽]\s+", "", cleaned).strip()
        if cleaned and cleaned not in _GENERIC_WINDOW_NAMES:
            return cleaned
    path = Path(cwd)
    if path.name:
        return path.name
    return cwd


STATUS_TAIL_LINES = 160


def status_tail_text(text: str) -> str:
    """Return the only capture region that can describe the current TUI state.

    Conversation rendering still parses the full requested history.  Status
    inference, however, only inspects the final 24-80 visible rows.  Trimming
    before the relatively expensive display-line cleanup avoids reprocessing
    thousands of historical terminal rows on every live poll.
    """
    # 输入框下方的子智能体面板行数不定，先剥掉，免得它把状态行挤出固定窗口。
    lines = pane_detectors.strip_agent_panel(text.splitlines())
    return "\n".join(lines[-STATUS_TAIL_LINES:])


def split_input_region(raw_lines: list[str]) -> tuple[list[str], list[str]]:
    """把一屏切成 (对话区, 输入区)。具体每种 AI 长什么样见 pane_detectors。"""
    split = pane_detectors.split_screen(raw_lines)
    return split.conversation, split.input_lines


# Claude 每轮结束会留一行 "✻ Cooked for 13m 14s · done Monday, 11:51 am"。它出现在对话区
# 末尾就说明这一轮已经收尾了 —— 那时屏幕上再出现"要不要继续""按 enter 继续"之类的字样,
# 只可能是 AI 自己写在回答里的,不是一个真在等人操作的选择器。
TURN_FINISHED_RE = re.compile(r"·\s*done\b|\bdone\s+(?:\w+day|\d{1,2}:\d{2})", re.I)
# 同一行里 "done" 后面还挂着没跑完的东西时,这一轮并没有真的收尾。
TURN_STILL_BUSY_RE = re.compile(
    r"\b\d+\s+shells?\s+still\b|\bstill\s+running\b|\bwaiting\s+for\s+\d+\b", re.I)
# 选择器只可能紧贴输入框。放宽到整屏去找,正文里随口一句就会被当成在等人。
WAITING_SCAN_LINES = 6


def _waiting_scan_region(conversation_lines: list[str]) -> list[str]:
    trimmed = [line for line in conversation_lines if line.strip()]
    return trimmed[-WAITING_SCAN_LINES:]


def infer_status(text: str, command: str, ai_alive: bool = True) -> str:
    if not text.strip():
        return "no output"
    tail_raw = status_tail_text(text)
    _conversation_raw, input_region_raw = split_input_region(tail_raw.splitlines())
    has_input_region = bool(input_region_raw)
    visible_text = filter_display_lines(tail_raw)
    lines = [line.strip().lower() for line in visible_text.splitlines() if line.strip()]
    tail = "\n".join(lines[-24:])
    active_tail = "\n".join(lines[-8:])
    error_tokens = ["traceback", "error:", "failed", "exception", "permission denied"]
    prompt_tokens = ["tab to queue message", "›", ">"]
    command_name = Path(command).name.lower()
    if not ai_alive and command_name in {"claude", "codex", "node"}:
        return "needs attention"
    # Only the live footer can pause a Claude pane: historical/quoted notices
    # in the conversation must not keep a recovered session blocked.
    if ai_alive and any(re.match(r"^\s*⚠\s*Usage limit reached\b", line, re.I)
                        for line in input_region_raw):
        return "quota_limited"
    # Codex 账号额度耗尽会打一条固定横幅("■ You've hit your usage limit. ...
    # purchase more credits or try again at ...")然后照常留一个正常输入框在屏幕
    # 底部。光看这个输入框会被下面 has_input_region 分支判成 idle——窗口看起来
    # "啥事没有、在等你打字",实际上账号被挡住了,发什么消息都会原样弹回同一条横幅。这个横幅
    # 比其它任何状态判定都优先——但只在非 Claude 窗口、且这一行本身就是 Codex 用
    # "■ " 开头打出来的系统横幅时才认,避免任何窗口只是在讨论/引用这句话(比如
    # 复述这个 bug 本身)时被误判。
    if command_name != "claude" and any(
        re.match(r"^■\s*you.ve hit your usage limit", line) for line in lines[-24:]
    ):
        return "needs attention"
    # 强工作信号优先于"末行是输入提示=idle": Codex/Claude 工作时底部仍显示 `›` 输入框,
    # 真正的 `Working (… · esc to interrupt)` / `Composing… (…)` 在其上方。若先按末行提示符判 idle,
    # 思考态就永远不显示。只认带 elapsed `(` / `…`
    # 或中断提示的强指示器, 不误伤正文里出现的 "working" 字样。
    if any(
        AI_RUNNING_STATUS_RE.match(line)
        or INTERRUPTIBLE_RUNTIME_STATUS_RE.match(line)
        or WORKFLOW_RUNNING_STATUS_RE.search(line)
        or SPINNER_LINE_RE.match(line)
        for line in lines[-12:]
    ):
        return "running"
    # "还在跑"的信号也要在**原始行**上再找一遍。它常常长在一行 chrome 的尾巴上
    # ("✻ Sautéed for 39s · done · 2 messages hidden · 2 shells still running"),
    # 而那种行会被 filter_display_lines 整行丢掉 —— 于是一个后台任务没跑完的窗口
    # 显示成空闲。
    if WORKFLOW_RUNNING_STATUS_RE.search("\n".join(tail_raw.splitlines()[-12:])):
        return "running"
    # A filled TODO is useful only while the task panel has not yet returned to
    # a later input prompt. Once `›`/`>` appears below it, the checkbox is a
    # stale screen remnant; strong spinner/interrupt signals above still win.
    todo_indices = [index for index, line in enumerate(lines[-24:]) if TODO_IN_PROGRESS_RE.match(line)]
    prompt_indices = [
        index for index, line in enumerate(lines[-24:])
        if re.match(r"^[›>]\s*", line)
    ]
    if todo_indices and not any(index > todo_indices[-1] for index in prompt_indices):
        return "running"
    if re.search(r"(?:working|thinking|running|composing|thundering)\s*(?:[.…]+)?\s*[(…]|esc to interrupt", active_tail):
        return "running"
    # A highlighted numbered option / select footer means an interactive picker
    # is waiting. Keep this narrower than generic words such as "confirm" so a
    # normal answer does not become a false waiting state.
    # 只在紧贴输入框的那几行里找选择器,并且这一轮不能已经收尾。
    waiting_region = "\n".join(
        line.strip().lower()
        for line in _waiting_scan_region(filter_display_lines("\n".join(_conversation_raw)).splitlines())
    )
    if has_input_region:
        # 有输入框时,"在等人操作"的证据只能来自紧贴输入框的那几行 —— 绝不能因为这几行
        # 里没有,就退回去扫整屏,那正是"AI 在回答里说了句『要不要继续』就被当成在等人"的
        # 来源。完成标记要在**原始行**上找: filter_display_lines 会把
        # "✻ Cooked for 1m · done" 当 chrome 剥掉,过滤后的行里根本没有它。
        recent_raw = "\n".join(_conversation_raw[-8:])
        turn_finished = bool(
            TURN_FINISHED_RE.search(recent_raw) and not TURN_STILL_BUSY_RE.search(recent_raw)
        )
        active_tail = "" if turn_finished else waiting_region
    picker_waiting = bool(re.search(r"[❯›]\s*\d+[.)]", active_tail))
    footer_waiting = bool(
        re.search(r"\benter to (?:select|confirm|continue)\b", active_tail)
        or re.search(r"\b(?:tab/arrow keys|arrow keys) to navigate\b", active_tail)
        or re.search(r"\bpress enter to\b", active_tail)
    )
    prompt_waiting = bool(
        re.search(r"\bdo you want\b", active_tail)
        or re.search(r"\b(?:approve|allow|continue)\?", active_tail)
    )
    if picker_waiting or footer_waiting or prompt_waiting:
        return "waiting"
    if ai_alive and not picker_waiting and lines and (
        any(lines[-1].startswith(token) or token in lines[-1] for token in prompt_tokens)
        or is_model_status_line(lines[-1])
    ):
        return "idle"
    # 屏幕上还立着输入框、**而且进程确实还活着** = 会话正停在那里等人打字。
    # 光看屏幕不够: Codex 是 inline 渲染,进程死了最后一屏还留在 scrollback 里,
    # 那个残留的输入框会把刚崩掉的会话压成 idle,needs attention 告警就此失效
    # (审查实测: `database is locked` 崩溃 + 残留输入框 -> idle)。
    # 此时正文里出现什么词都不改变这个
    # 事实,所以这一条必须挡在 error_tokens 前面 —— 否则 AI 只要在回答里引用一句
    # "error:"、贴一段 traceback,或者 Claude 播报一条后台命令失败,整个会话就会被
    # 报成 needs attention。错误词只有在"连输入框都没有"时才说明真的出事了。
    if has_input_region and ai_alive:
        return "idle"
    if any(token in tail for token in error_tokens):
        return "needs attention"
    if command in {"bash", "zsh", "fish", "sh"}:
        return "shell"
    return "idle"


# 必须 <= capture() 的历史下限(40 行),否则浅抓屏根本凑不满这个窗口,两条路径取到的
# 就不是同一段内容 —— 那样"与抓屏深度无关"只是在当前数据上碰巧成立。
ACTIVITY_RAW_TAIL_LINES = 30


def pane_activity_signature(text: str) -> str:
    """活跃度指纹:用来判断"这个 pane 的内容还在不在变"。

    窗口必须锚在**原始行**上,不能锚在过滤后的行上。两条调用路径喂进来的抓屏深度不同
    (卡片列表 40 行、卡片详情 5000 行),过滤会吃掉不同数量的行,于是"过滤后的最后 N 行"
    在两条路径下根本不是同一段内容。同一个 pane 的指纹来回跳,"内容没变"就永远不成立,
    一个 idle 的窗口会被永久锁死显示 running —— 打开卡片详情后必现。
    """
    lines: list[str] = []
    # 不再对 filter_display_lines 的输出重跑 clean_display_line: 它吐出来的每一行都已经
    # 清理过,第二遍实测 0 行被改、0 行被丢,纯属白烧 CPU。
    raw_tail = "\n".join(text.splitlines()[-ACTIVITY_RAW_TAIL_LINES:])
    for line in filter_display_lines(raw_tail).splitlines():
        stripped = line.strip()
        if (
            not stripped
            or is_noise_rule_line(stripped)
            or is_model_status_line(stripped)
            or is_volatile_status_line(stripped)
            or is_queued_message_line(stripped)
            or re.match(r"^[›❯>](?:\s+.*)?$", stripped)
        ):
            continue
        lines.append(re.sub(r"\s+", " ", stripped))
    return "\n".join(lines)


TRANSCRIPT_TAIL_BYTES = 64 * 1024
_TRANSCRIPT_ACTIVITY_CACHE: dict[str, tuple[float, int, str | None]] = {}


def transcript_activity_status(path: str) -> str | None:
    """从落盘记录判断这一轮结没结束。返回 "running" / "idle" / None(没有可用信号)。

    判据是「最后一条 assistant 之后还有没有 turn_duration」:有就说明那一轮已经收尾。
    user / attachment / queue-operation 都不算 —— 那些是用户排进队列的消息,AI 未必
    开始处理(官方说 idle 时,按"有新记录就算在跑"会判错)。

    为什么值得读文件而不是继续看屏幕: 屏幕是给人看的,focus 模式、清屏、改窗口宽度、
    上游换一版 UI 文案都会让解析漂移;而这份 JSONL 是 Claude 自己写的事实。实测这条
    判据 21/22 与 `claude agents --json` 一致,唯一的差异(后台 shell 还在跑)由屏幕补上。
    """
    try:
        stat = os.stat(path)
    except OSError:
        return None
    cached = _TRANSCRIPT_ACTIVITY_CACHE.get(path)
    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    try:
        with open(path, "rb") as handle:
            handle.seek(max(0, stat.st_size - TRANSCRIPT_TAIL_BYTES))
            data = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    last_assistant = last_turn = -1
    # 首行可能是被 seek 截断的半行,丢掉
    for index, line in enumerate(data.splitlines()[1:]):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        row_type = row.get("type")
        if row_type == "assistant":
            # Claude writes a synthetic assistant record after local slash
            # commands such as `/model`: ``No response requested.``.  This is
            # not a new model turn, and must not move the activity cursor past
            # the preceding ``turn_duration``.  Treating it as a real
            # assistant response leaves an idle pane stuck at “处理中” until
            # another turn is completed.
            message = row.get("message") if isinstance(row.get("message"), dict) else {}
            content = message.get("content")
            text_parts: list[str] = []
            has_tool_use = False
            if isinstance(content, str):
                text_parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        has_tool_use = True
                    if block.get("type") == "text" and isinstance(block.get("text"), str):
                        text_parts.append(block["text"])
            assistant_text = "\n".join(text_parts).strip()
            if has_tool_use or assistant_text.lower() != "no response requested.":
                last_assistant = index
        elif row_type == "system" and row.get("subtype") == "turn_duration":
            last_turn = index
    status = None
    if last_assistant >= 0 or last_turn >= 0:
        status = "idle" if last_turn > last_assistant else "running"
    _TRANSCRIPT_ACTIVITY_CACHE[path] = (stat.st_mtime, stat.st_size, status)
    if len(_TRANSCRIPT_ACTIVITY_CACHE) > 512:
        _TRANSCRIPT_ACTIVITY_CACHE.clear()
    return status


_SUBAGENT_COUNT_CACHE: dict[str, tuple[float, int]] = {}
SUBAGENT_COUNT_TTL = 2.0

# `claude agents --json`（见 scripts/claude_sessions.py）是 Claude 自己报的
# 会话状态：idle / busy / waiting（waitingFor: "dialog open"）。一次调用列出全部会话，缓存 3 秒。
def claude_agent_records() -> list[dict]:
    records, _error = claude_sessions.agent_records()
    return records


def _descendant_pids(root_pid: str, limit: int = 64) -> set[str]:
    found: set[str] = set()
    frontier = [root_pid] if root_pid and root_pid.isdigit() else []
    while frontier and len(found) < limit:
        children = _proc_children(frontier) or []
        frontier = [c for c in children if c not in found]
        found.update(frontier)
    return found


def claude_agent_record(session_id: str = "", pane_pid: str = "") -> dict | None:
    """This pane's Claude session record (pid in the pane's process tree, else a unique session id)."""
    if not pane_pid and not session_id:
        return None
    pids = (_descendant_pids(pane_pid) | {pane_pid}) if pane_pid else set()
    return claude_sessions.record_for(claude_agent_records(), pids, session_id)


_CODEX_OPEN_CHILDREN_CACHE: dict[str, tuple[float, list[str]]] = {}


def codex_open_child_rollouts(pane_pid: str) -> list[str]:
    """Rollouts of the Codex subagents living in this pane's codex process.

    Codex runs spawned agents in-process and keeps their rollout files open,
    so the open descriptors say exactly which subagents belong to this pane
    (no guessing by directory or cwd).
    """
    if not pane_pid:
        return []
    now = time.time()
    cached = _CODEX_OPEN_CHILDREN_CACHE.get(pane_pid)
    if cached and now - cached[0] < SUBAGENT_COUNT_TTL:
        return cached[1]
    paths = [p for p in _codex_rollout_fd_candidates(pane_pid) if codex_subagents.is_subagent_rollout(p)]
    _CODEX_OPEN_CHILDREN_CACHE[pane_pid] = (now, paths)
    return paths


def codex_running_subagent_count(pane_pid: str) -> int:
    now = time.time()
    states = [codex_subagents.child_state(p, now) for p in codex_open_child_rollouts(pane_pid)]
    return sum(1 for s in states if s and s.status == "running")


def pane_running_subagents(kind: str, ai_alive: bool, transcript_path: str = "", pane_pid: str = "") -> int:
    if not ai_alive:
        return 0
    if kind == "Claude":
        return running_subagent_count(transcript_path)
    if kind == "Codex":
        return codex_running_subagent_count(pane_pid)
    return 0


def running_subagent_count(transcript_path: str) -> int:
    """Running subagents of a Claude session (short TTL: /api/panes polls every pane)."""
    if not transcript_path:
        return 0
    now = time.time()
    cached = _SUBAGENT_COUNT_CACHE.get(transcript_path)
    if cached and now - cached[0] < SUBAGENT_COUNT_TTL:
        return cached[1]
    count = len(claude_subagents.running_subagents(transcript_path, now))
    _SUBAGENT_COUNT_CACHE[transcript_path] = (now, count)
    return count


def claude_background_counts(text: str) -> dict[str, int]:
    """Background monitors / shells Claude lists in its footer and "done" line."""
    counts = {"monitor": 0, "shell": 0}
    tail = status_tail_text(text).splitlines()[-CLAUDE_BACKGROUND_TAIL_LINES:]
    for line in tail:
        for number, noun in CLAUDE_BACKGROUND_RE.findall(line):
            key = "monitor" if noun.lower().startswith("monitor") else "shell"
            counts[key] = max(counts[key], int(number))
    return counts


def claude_background_label(text: str) -> str:
    monitors = claude_background_counts(text)["monitor"]
    return f"后台监控 {monitors} 个" if monitors else ""


def claude_turn_done_with_monitors_only(text: str, transcript_path: str) -> bool:
    """Claude reports ``busy`` while a background monitor is armed, even after the
    turn has ended and the reply is waiting for the user.  A monitor only
    watches; the answer is complete.  Background shells, sub-agents and
    workflows are still work in progress (the win27 case: "done · 2 shells
    still running" was mistaken for finished), so they keep ``busy``."""
    counts = claude_background_counts(text)
    if not counts["monitor"] or counts["shell"] or not transcript_path:
        return False
    if transcript_activity_status(transcript_path) != "idle" or running_subagent_count(transcript_path):
        return False
    return not WORKFLOW_RUNNING_STATUS_RE.search("\n".join(status_tail_text(text).splitlines()[-12:]))


def infer_pane_status(pane_id: str, text: str, command: str, kind: str,
                      ai_alive: bool = True, transcript_path: str = "", pane_pid: str = "",
                      session_id: str = "") -> str:
    status = infer_status(text, command, ai_alive)
    if status == "quota_limited":
        return status
    if kind == "Claude" and ai_alive:
        # Claude 自己报的状态优先于看屏幕：对话框没有编号、选项文字也不固定，屏幕上
        # 认不出来时窗口会被当成空闲甚至"完成"。
        record = claude_agent_record(session_id, pane_pid)
        official = str((record or {}).get("status") or "")
        if official == "waiting":
            return "waiting"
        if official == "busy":
            if claude_turn_done_with_monitors_only(text, transcript_path):
                return status if status in {"waiting", "needs attention"} else "idle"
            return "running"
    # Codex 的主对话回完话后，派出去的子智能体可以继续干：活没干完，仍是处理中。
    if kind == "Codex" and status == "idle" and pane_running_subagents(kind, ai_alive, pane_pid=pane_pid):
        return "running"
    if kind == "Claude" and ai_alive and transcript_path:
        recorded = transcript_activity_status(transcript_path)
        if recorded == "running":
            return "running"
        # 主对话这一轮已经回完话，但派出去的子智能体还在干活：活没干完，仍是处理中。
        # 依据是子智能体自己的日志，不看屏幕（面板行数一多，屏幕上的提示会被挤掉）。
        if status not in {"waiting", "needs attention"} and running_subagent_count(transcript_path):
            return "running"
        if recorded == "idle":
            # 记录说这轮收尾了。但记录里没有"后台 shell 还在跑"这种信息,屏幕上有,
            # 所以那一类强信号仍然可以翻盘;waiting / needs attention 同理,记录也管不着。
            if status in {"waiting", "needs attention"}:
                return status
            if WORKFLOW_RUNNING_STATUS_RE.search("\n".join(status_tail_text(text).splitlines()[-12:])):
                return "running"
            return "idle"
    if kind not in {"Claude", "Codex"} or status in {"waiting", "needs attention", "no output"}:
        return status

    now = time.time()
    sig = pane_activity_signature(text)
    cached = PANE_ACTIVITY_CACHE.get(pane_id)
    if not sig:
        if cached and status == "idle" and now - float(cached.get("changed_at", 0)) <= PANE_ACTIVITY_GRACE:
            return "running"
        return status

    if cached is None:
        PANE_ACTIVITY_CACHE[pane_id] = {"sig": sig, "changed_at": 0.0, "seen_at": now}
        return status

    if sig != cached.get("sig"):
        cached["sig"] = sig
        cached["changed_at"] = now
        cached["seen_at"] = now
        if status == "idle":
            return "running"
    elif status == "idle" and now - float(cached.get("changed_at", 0)) <= PANE_ACTIVITY_GRACE:
        cached["seen_at"] = now
        return "running"

    cached["seen_at"] = now
    return status


def preview_from(text: str, max_lines: int = 6) -> str:
    """卡片预览: 从底往上收集最近 max_lines 行真实内容(跳过 spinner/chrome/输入框)。

    原来只返回 1 行,且 claude 思考时那行常是 spinner(Cogitating…),
    卡片几乎空、与终端对不上。现收集多行实际内容并保持原顺序。
    """
    split = pane_detectors.split_screen(text.splitlines())
    if split.provider == "claude":
        text = "\n".join(split.conversation)
    collected: list[str] = []
    for line in reversed(filter_display_lines(text).splitlines()):
        cleaned = clean_display_line(line)
        if cleaned is None:
            continue
        stripped = cleaned.strip()
        if (
            stripped
            and not is_noise_rule_line(stripped)
            and not is_model_status_line(stripped)
            and not is_volatile_status_line(stripped)
            and not CODEX_TRUNCATION_RE.match(stripped)
            and not re.search(r"\+\d+\s+lines?\s*(?:\(ctrl|\))", stripped, re.I)
            and not re.fullmatch(r"[─━═\-\s]+", stripped)
            and not stripped.startswith("›")
            and not stripped.startswith("> ")
            and not stripped.startswith("⎿")
            and "User prompt" not in stripped
            and "ctrl + t to view transcript" not in stripped
        ):
            collected.append(stripped[:220])
            if len(collected) >= max_lines:
                break
    collected.reverse()
    return "\n".join(collected)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_prefs_unlocked() -> dict[str, object]:
    try:
        data = json.loads(PREFS.read_text(encoding="utf-8")) if PREFS.is_file() else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["categories"] = normalized_category_names(data.get("categories"))
    return data


def read_prefs() -> dict[str, object]:
    """Read one coherent prefs snapshot while POST handlers may be writing."""
    with PREFS_LOCK:
        return _read_prefs_unlocked()


def _write_prefs_unlocked(data: dict[str, object]) -> None:
    PREFS.parent.mkdir(parents=True, exist_ok=True)
    tmp = PREFS.with_name(f".{PREFS.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, PREFS)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def normalized_category_names(raw: object) -> list[str]:
    """Keep fixed views/categories present and preserve custom-category order."""
    values = list(raw) if isinstance(raw, list) else []
    custom: list[str] = []
    seen = set(FIXED_CATEGORY_ORDER)
    for item in values:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        custom.append(value)
    return [*FIXED_CATEGORY_ORDER, *custom]


def _prune_empty_custom_categories_unlocked(
    prefs: dict[str, object],
    *,
    live_keys: set[str] | None = None,
) -> bool:
    """Remove empty custom categories while fixed categories always survive.

    Without ``live_keys`` a category is considered non-empty when any stored
    pane mapping uses it.  With ``live_keys`` only currently visible pane
    identities count, which also retires leader-created categories after their
    last tmux window disappears outside the Cards UI.
    """
    categories = normalized_category_names(prefs.get("categories"))
    raw_mapping = prefs.get("paneCategories")
    mapping = dict(raw_mapping) if isinstance(raw_mapping, dict) else {}
    # The item-level mapping is the durable truth. A stale browser may still
    # POST an older whole ``categories`` list; never let that hide a custom
    # category which still owns panes.
    for value in mapping.values():
        category = str(value or "").strip()
        if category and category not in categories:
            categories.append(category)
    if live_keys is None:
        used = {str(value) for value in mapping.values() if str(value)}
    else:
        used = {
            str(value)
            for key, value in mapping.items()
            if str(value) and str(key) in live_keys
        }
    removable = {
        category
        for category in categories
        if category not in FIXED_CATEGORY_ORDER and category not in used
    }
    next_categories = [category for category in categories if category not in removable]
    next_mapping = {
        str(key): value
        for key, value in mapping.items()
        if str(value) not in removable
    }
    changed = next_categories != categories or next_mapping != mapping
    prefs["categories"] = next_categories
    prefs["paneCategories"] = next_mapping
    return changed


def merge_prefs(patch: dict[str, object]) -> dict[str, object]:
    """Atomically merge top-level preference groups instead of replacing all.

    Older Cards clients POST the whole organization snapshot.  Merging keeps
    fields introduced by newer clients intact, while dedicated item endpoints
    for favorites and categories prevent devices/AIs from clobbering one another
    inside a shared group.
    """
    with PREFS_LOCK:
        prefs = _read_prefs_unlocked()
        safe_patch = dict(patch)
        # A browser tab opened before the item-level endpoint deployment still
        # POSTs complete (possibly stale) favorite/category objects. Allow
        # those shapes to seed genuinely absent fields once, but never replace
        # existing maps. New tabs mutate one stable key at a time.
        if "paneFavorites" in prefs:
            safe_patch.pop("paneFavorites", None)
        if "paneCategories" in prefs:
            safe_patch.pop("paneCategories", None)
        # Aliases are item-level too (/api/prefs/pane); a stale whole-map POST
        # must not undo a rename made from the CLI or another device.
        if "paneAliases" in prefs:
            safe_patch.pop("paneAliases", None)
        prefs.update(safe_patch)
        prefs["categories"] = normalized_category_names(prefs.get("categories"))
        _prune_empty_custom_categories_unlocked(prefs)
        _write_prefs_unlocked(prefs)
        return prefs


def update_favorite_pref(key: str, favorite: bool, legacy_key: str = "") -> dict[str, object]:
    """Atomically update exactly one favorite for safe multi-device use."""
    key = str(key or "").strip()
    legacy_key = str(legacy_key or "").strip()
    if not key or len(key) > 2_000 or any(ord(char) < 32 for char in key):
        raise ValueError("invalid favorite key")
    if len(legacy_key) > 2_000 or any(ord(char) < 32 for char in legacy_key):
        raise ValueError("invalid legacy favorite key")
    with PREFS_LOCK:
        prefs = _read_prefs_unlocked()
        current = prefs.get("paneFavorites")
        favorites = dict(current) if isinstance(current, dict) else {}
        if favorite:
            favorites[key] = True
        else:
            favorites.pop(key, None)
        if legacy_key and legacy_key != key:
            favorites.pop(legacy_key, None)
        prefs["paneFavorites"] = favorites
        _write_prefs_unlocked(prefs)
        return prefs


def _validate_pref_key(key: str, label: str = "preference") -> str:
    value = str(key or "").strip()
    if not value or len(value) > 2_000 or any(ord(char) < 32 for char in value):
        raise ValueError(f"invalid {label} key")
    return value


def _set_category_unlocked(
    prefs: dict[str, object],
    key: str,
    category: str,
    legacy_key: str = "",
    *,
    create_category: bool = False,
) -> None:
    category = str(category or "").strip()
    if len(category) > 32 or any(ord(char) < 32 for char in category):
        raise ValueError("invalid category")
    if category in {"最近", "全部"}:
        raise ValueError(f"{category} is a view, not an assignable category")
    categories = normalized_category_names(prefs.get("categories"))
    if category and category not in categories:
        if not create_category:
            available = ", ".join(item for item in categories if item not in {"最近", "全部"}) or "(none)"
            raise ValueError(f"unknown category: {category}; available: {available}")
        categories.append(category)
    prefs["categories"] = categories

    raw_mapping = prefs.get("paneCategories")
    mapping = dict(raw_mapping) if isinstance(raw_mapping, dict) else {}
    if category:
        mapping[key] = category
    else:
        mapping.pop(key, None)
    if legacy_key and legacy_key != key:
        mapping.pop(legacy_key, None)
    prefs["paneCategories"] = mapping
    _prune_empty_custom_categories_unlocked(prefs)


def delete_category_pref(category: str) -> tuple[dict[str, object], int]:
    """Delete one custom category and atomically unassign its panes."""
    category = str(category or "").strip()
    if not category or len(category) > 32 or any(ord(char) < 32 for char in category):
        raise ValueError("invalid category")
    if category in FIXED_CATEGORY_ORDER:
        raise ValueError(f"fixed category cannot be deleted: {category}")
    with PREFS_LOCK:
        prefs = _read_prefs_unlocked()
        categories = normalized_category_names(prefs.get("categories"))
        if category not in categories:
            raise ValueError(f"unknown category: {category}")
        raw_mapping = prefs.get("paneCategories")
        mapping = dict(raw_mapping) if isinstance(raw_mapping, dict) else {}
        removed = sum(1 for value in mapping.values() if str(value) == category)
        prefs["categories"] = [value for value in categories if value != category]
        prefs["paneCategories"] = {
            str(key): value for key, value in mapping.items() if str(value) != category
        }
        _write_prefs_unlocked(prefs)
        return prefs, removed


def update_category_pref(
    key: str,
    category: str,
    legacy_key: str = "",
    *,
    create_category: bool = False,
) -> dict[str, object]:
    """Atomically update one card category without replacing other panes."""
    key = _validate_pref_key(key, "category")
    legacy_key = str(legacy_key or "").strip()
    if len(legacy_key) > 2_000 or any(ord(char) < 32 for char in legacy_key):
        raise ValueError("invalid legacy category key")
    with PREFS_LOCK:
        prefs = _read_prefs_unlocked()
        _set_category_unlocked(
            prefs,
            key,
            category,
            legacy_key,
            create_category=create_category,
        )
        _write_prefs_unlocked(prefs)
        return prefs


def pane_preference_key(pane: Pane) -> str:
    target = f"{pane.session}:{pane.window_index}.{pane.pane_index}"
    cwd = str(pane.cwd or "").strip()
    return f"{target}|{cwd}" if cwd else target


def update_pane_preferences(
    pane: Pane,
    *,
    favorite: bool | None = None,
    category: str | None = None,
    alias: str | None = None,
    create_category: bool = False,
) -> tuple[dict[str, object], str]:
    """Atomically mutate organization data for one identity-verified pane."""
    if favorite is None and category is None and alias is None:
        raise ValueError("favorite, category, or alias mutation is required")
    if alias is not None and (
        len(alias.strip()) > 40 or any(ord(char) < 32 for char in alias)
    ):
        raise ValueError("invalid alias")
    key = pane_preference_key(pane)
    with PREFS_LOCK:
        prefs = _read_prefs_unlocked()
        if favorite is not None:
            raw_favorites = prefs.get("paneFavorites")
            favorites = dict(raw_favorites) if isinstance(raw_favorites, dict) else {}
            if favorite:
                favorites[key] = True
            else:
                favorites.pop(key, None)
            favorites.pop(pane.pane_id, None)
            prefs["paneFavorites"] = favorites
        if category is not None:
            _set_category_unlocked(
                prefs,
                key,
                category,
                pane.pane_id,
                create_category=create_category,
            )
        if alias is not None:
            raw_aliases = prefs.get("paneAliases")
            aliases = dict(raw_aliases) if isinstance(raw_aliases, dict) else {}
            normalized_alias = alias.strip()
            if normalized_alias:
                aliases[key] = normalized_alias
            else:
                aliases.pop(key, None)
            aliases.pop(pane.target, None)
            aliases.pop(pane.pane_id, None)
            prefs["paneAliases"] = aliases
        _write_prefs_unlocked(prefs)
        return prefs, key


def update_pane_group_preferences(
    panes: list[Pane],
    category: str,
    *,
    create_category: bool = False,
) -> tuple[dict[str, object], list[str]]:
    """Assign one identity-verified pane set in a single prefs transaction."""
    if not panes:
        raise ValueError("at least one pane is required")
    pane_ids = [pane.pane_id for pane in panes]
    if len(set(pane_ids)) != len(pane_ids):
        raise ValueError("duplicate pane in group")
    keys = [pane_preference_key(pane) for pane in panes]
    with PREFS_LOCK:
        prefs = _read_prefs_unlocked()
        for index, (pane, key) in enumerate(zip(panes, keys)):
            _set_category_unlocked(
                prefs,
                key,
                category,
                pane.pane_id,
                create_category=create_category and index == 0,
            )
        _write_prefs_unlocked(prefs)
        return prefs, keys


def safe_upload_name(filename: str) -> str:
    stem = Path(filename or "image").stem
    suffix = Path(filename or "").suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        suffix = ".png"
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-")[:80] or "image"
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}-{stem}{suffix}"


def safe_shared_file_name(filename: str) -> str:
    source = Path(filename or "file")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", source.stem).strip(".-")[:90] or "file"
    suffix = re.sub(r"[^A-Za-z0-9.]+", "", source.suffix.lower())[:20]
    if suffix and not suffix.startswith("."):
        suffix = f".{suffix}"
    return f"{datetime.now().astimezone().strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(4)}-{stem}{suffix}"


def safe_upload_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value or "").strip(".-")[:120]
    return cleaned or secrets.token_hex(16)


def _decode_base64_payload(content: str, label: str) -> bytes:
    if "," in content and content.split(",", 1)[0].startswith("data:"):
        content = content.split(",", 1)[1]
    if not content:
        raise ValueError(f"missing {label} content")
    try:
        return base64.b64decode(content, validate=True)
    except Exception as exc:
        try:
            padded = content + ("=" * (-len(content) % 4))
            return base64.urlsafe_b64decode(padded)
        except Exception:
            raise ValueError(f"invalid base64 {label} content") from exc


def detect_image_ext(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    return None


HTML_PREVIEW_CSP = "sandbox allow-scripts allow-downloads; connect-src 'none'; object-src 'none'; form-action 'none'"
SVG_PREVIEW_CSP = "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'"


def preview_security_headers(content_type: str) -> list[tuple[str, str]]:
    """Headers that keep a previewed file from running as this site.

    HTML and SVG can carry scripts.  Opened in its own tab, an unsandboxed one
    would run with the dashboard's origin and could call /api/send like the
    page itself (same-origin requests pass the cross-site write check), so both
    get a sandbox CSP (an opaque origin); SVG gets no scripts at all.  An SVG
    shown through <img> never runs scripts, so previews are unaffected."""
    if content_type == "text/html":
        return [("Content-Disposition", "inline"), ("Content-Security-Policy", HTML_PREVIEW_CSP)]
    if content_type == "image/svg+xml":
        return [("Content-Security-Policy", SVG_PREVIEW_CSP)]
    return []


def image_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(suffix, "application/octet-stream")


def shared_file_content_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def shared_file_rel(path: Path) -> str:
    return path.relative_to(SHARED_FILES_DIR).as_posix()


def shared_file_download_path(rel_name: str) -> str:
    return "/".join(quote(part) for part in Path(rel_name).parts)


def shared_file_url(name: str) -> str:
    download_path = shared_file_download_path(name)
    return f"{URL_PREFIX}/files/{download_path}" if URL_PREFIX else f"/files/{download_path}"


def shared_file_preview_url(name: str) -> str:
    preview_path = shared_file_download_path(name)
    return f"{URL_PREFIX}/files/preview/{preview_path}" if URL_PREFIX else f"/files/preview/{preview_path}"


def shared_upload_dir() -> Path:
    return SHARED_FILES_DIR / datetime.now().astimezone().strftime("%Y-%m-%d")


def shared_upload_date_dir() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def shared_file_path_from_request(name: str) -> Path | None:
    raw = unquote(str(name or "")).strip().lstrip("/")
    rel = Path(raw)
    if not raw or rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        return None
    try:
        root = SHARED_FILES_DIR.resolve()
        path = (SHARED_FILES_DIR / rel).resolve()
    except Exception:
        return None
    if path == root or root not in path.parents:
        return None
    return path


def local_artifact_path_from_request(alias: str, name: str) -> Path | None:
    """Resolve one explicitly linked deliverable without exposing arbitrary files.

    The route intentionally has no directory-listing API.  It only accepts the
    configured workspace alias, regular artifact extensions, non-hidden path
    components, and a small denylist for credential-like path components.
    """
    if str(alias or "").strip().lower() != "workspace":
        return None
    raw = unquote(str(name or "")).strip().lstrip("/")
    rel = Path(raw)
    if (
        not raw
        or rel.is_absolute()
        or any(part in {"", ".", ".."} or part.startswith(".") for part in rel.parts)
        or rel.suffix.lower() not in LOCAL_ARTIFACT_SUFFIXES
        or any(LOCAL_ARTIFACT_SENSITIVE_NAME_RE.search(part) for part in rel.parts)
    ):
        return None
    try:
        root = LOCAL_ARTIFACT_ROOT.resolve()
        path = (LOCAL_ARTIFACT_ROOT / rel).resolve()
        resolved_rel = path.relative_to(root)
    except (OSError, ValueError):
        return None
    if (
        path == root
        or any(part.startswith(".") for part in resolved_rel.parts)
        or path.suffix.lower() not in LOCAL_ARTIFACT_SUFFIXES
        or any(LOCAL_ARTIFACT_SENSITIVE_NAME_RE.search(part) for part in resolved_rel.parts)
        or not path.is_file()
    ):
        return None
    return path


def attachment_content_disposition(path: Path) -> str:
    """Return an ASCII-safe attachment header, including Unicode filenames."""
    suffix = re.sub(r"[^A-Za-z0-9.]", "", path.suffix)[:20]
    fallback_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", path.stem).strip(".-")[:80]
    fallback = f"{fallback_stem or 'download'}{suffix}"
    encoded = quote(path.name, safe="")
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


def shared_file_item(path: Path) -> dict[str, object]:
    stat = path.stat()
    rel = shared_file_rel(path)
    return {
        "filename": rel,
        "display_name": path.name,
        "relative_path": rel,
        "path": str(path),
        "url": shared_file_url(rel),
        "preview_url": shared_file_preview_url(rel),
        "bytes": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "content_type": shared_file_content_type(path),
    }


def cleanup_stale_shared_chunks() -> int:
    if SHARED_CHUNK_TTL_SECONDS <= 0 or not SHARED_CHUNKS_DIR.exists():
        return 0
    cutoff = time.time() - SHARED_CHUNK_TTL_SECONDS
    removed = 0
    try:
        children = list(SHARED_CHUNKS_DIR.iterdir())
    except OSError:
        return 0
    for child in children:
        try:
            if child.is_symlink() or not child.is_dir():
                continue
            meta_path = child / "meta.json"
            marker = meta_path if meta_path.is_file() else child
            if marker.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(child)
            removed += 1
        except OSError:
            continue
    return removed


def list_shared_files() -> dict[str, object]:
    SHARED_FILES_DIR.mkdir(mode=0o700, exist_ok=True)
    cleaned_chunks = cleanup_stale_shared_chunks()
    files = [
        item
        for item in SHARED_FILES_DIR.rglob("*")
        if item.is_file()
        and item.parent != SHARED_CHUNKS_DIR
        and not any(part.startswith(".") for part in item.relative_to(SHARED_FILES_DIR).parts)
    ]
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    limit = max(1, SHARED_FILES_LIST_LIMIT)
    visible = files[:limit]
    return {
        "files": [shared_file_item(item) for item in visible],
        "total": len(files),
        "limit": limit,
        "truncated": len(files) > limit,
        "cleaned_chunks": cleaned_chunks,
    }


def save_shared_file(payload: dict[str, object]) -> dict[str, object]:
    filename = str(payload.get("filename", "file"))
    data = _decode_base64_payload(str(payload.get("content_base64", "")), "file")
    if not data or (MAX_SHARED_FILE_BYTES > 0 and len(data) > MAX_SHARED_FILE_BYTES):
        raise ValueError(f"file must be 1 byte to {MAX_SHARED_FILE_BYTES} bytes")
    upload_dir = shared_upload_dir()
    upload_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = safe_shared_file_name(filename)
    path = upload_dir / name
    path.write_bytes(data)
    item = shared_file_item(path)
    item["ok"] = True
    item["markdown"] = f"请查看这个文件：{path}"
    return item


def save_shared_file_stream(handler: BaseHTTPRequestHandler, filename: str, length: int) -> dict[str, object]:
    if length < 0:
        raise ValueError("invalid content length")
    if MAX_SHARED_FILE_BYTES > 0 and length > MAX_SHARED_FILE_BYTES:
        raise ValueError(f"file must be no larger than {MAX_SHARED_FILE_BYTES} bytes")
    upload_dir = shared_upload_dir()
    upload_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = safe_shared_file_name(filename)
    final_path = upload_dir / name
    tmp_path = upload_dir / f".{name}.part-{secrets.token_hex(4)}"
    remaining = length
    written = 0
    try:
        with tmp_path.open("wb") as fh:
            while remaining > 0:
                chunk = handler.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("upload ended before content length")
                fh.write(chunk)
                written += len(chunk)
                remaining -= len(chunk)
        os.replace(tmp_path, final_path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    item = shared_file_item(final_path)
    item["ok"] = True
    item["bytes"] = written
    item["markdown"] = f"请查看这个文件：{final_path}"
    return item


def _write_request_body_to_path(handler: BaseHTTPRequestHandler, path: Path, length: int) -> int:
    remaining = length
    written = 0
    with path.open("wb") as fh:
        while remaining > 0:
            chunk = handler.rfile.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError("upload ended before content length")
            fh.write(chunk)
            written += len(chunk)
            remaining -= len(chunk)
    return written


def save_shared_file_chunk(handler: BaseHTTPRequestHandler, query: dict[str, list[str]], length: int) -> dict[str, object]:
    upload_id = safe_upload_id(query.get("upload_id", [""])[0])
    filename = query.get("filename", ["file"])[0] or "file"
    chunk_index = int(query.get("chunk_index", ["-1"])[0])
    total_chunks = int(query.get("total_chunks", ["0"])[0])
    total_size = int(query.get("total_size", ["0"])[0])
    if chunk_index < 0 or total_chunks <= 0 or chunk_index >= total_chunks:
        raise ValueError("invalid chunk index")
    if total_chunks > 20000:
        raise ValueError("too many chunks")
    if length < 0:
        raise ValueError("invalid content length")
    if MAX_SHARED_FILE_BYTES > 0 and total_size > MAX_SHARED_FILE_BYTES:
        raise ValueError(f"file must be no larger than {MAX_SHARED_FILE_BYTES} bytes")

    cleanup_stale_shared_chunks()
    SHARED_CHUNKS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    upload_dir = SHARED_CHUNKS_DIR / upload_id
    upload_dir.mkdir(mode=0o700, exist_ok=True)
    meta_path = upload_dir / "meta.json"
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    else:
        meta = {
            "filename": filename,
            "final_name": safe_shared_file_name(filename),
            "date_dir": shared_upload_date_dir(),
            "total_chunks": total_chunks,
            "total_size": total_size,
            "created_at": now_iso(),
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    if int(meta.get("total_chunks", 0)) != total_chunks:
        raise ValueError("chunk metadata mismatch")

    part_path = upload_dir / f"{chunk_index:08d}.part"
    tmp_path = upload_dir / f".{chunk_index:08d}.tmp-{secrets.token_hex(4)}"
    try:
        written = _write_request_body_to_path(handler, tmp_path, length)
        os.replace(tmp_path, part_path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise

    present = sum(1 for part in upload_dir.glob("*.part") if re.fullmatch(r"\d{8}\.part", part.name))
    if present < total_chunks:
        return {"ok": True, "upload_id": upload_id, "chunk_index": chunk_index, "received": present, "total_chunks": total_chunks, "bytes": written, "complete": False}

    final_rel = "/".join(
        [
            str(meta.get("date_dir") or shared_upload_date_dir()).strip(),
            safe_shared_file_name(str(meta.get("final_name") or filename)),
        ]
    )
    final_path = shared_file_path_from_request(final_rel)
    if final_path is None:
        raise ValueError("invalid chunk target")
    final_dir = final_path.parent
    final_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp_final = final_dir / f".{final_path.name}.assembling-{secrets.token_hex(4)}"
    try:
        with tmp_final.open("wb") as out:
            for index in range(total_chunks):
                part = upload_dir / f"{index:08d}.part"
                with part.open("rb") as inp:
                    shutil.copyfileobj(inp, out, length=1024 * 1024)
        os.replace(tmp_final, final_path)
        shutil.rmtree(upload_dir, ignore_errors=True)
    except Exception:
        try:
            tmp_final.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    item = shared_file_item(final_path)
    item["ok"] = True
    item["upload_id"] = upload_id
    item["complete"] = True
    item["markdown"] = f"请查看这个文件：{final_path}"
    return item


def delete_shared_file(payload: dict[str, object]) -> dict[str, object]:
    name = str(payload.get("filename", ""))
    path = shared_file_path_from_request(name)
    if path is None:
        raise ValueError("missing filename")
    if not path.is_file():
        raise FileNotFoundError("file not found")
    path.unlink()
    return {"ok": True, "filename": name}


def save_uploaded_image(payload: dict[str, object]) -> dict[str, object]:
    filename = str(payload.get("filename", "image.png"))
    data = _decode_base64_payload(str(payload.get("content_base64", "")), "image")
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"image must be 1 byte to {MAX_UPLOAD_BYTES} bytes")
    detected = detect_image_ext(data)
    if detected is None:
        raise ValueError("unsupported image type")
    upload_name = safe_upload_name(filename)
    suffix = Path(upload_name).suffix.lower()
    if detected == ".jpg" and suffix not in {".jpg", ".jpeg"}:
        upload_name = f"{Path(upload_name).stem}.jpg"
    elif detected != ".jpg" and suffix != detected:
        upload_name = f"{Path(upload_name).stem}{detected}"
    UPLOAD_DIR.mkdir(mode=0o700, exist_ok=True)
    path = UPLOAD_DIR / upload_name
    path.write_bytes(data)
    return {
        "ok": True,
        "filename": upload_name,
        "path": str(path),
        "url": f"{URL_PREFIX}/uploads/{upload_name}" if URL_PREFIX else f"/uploads/{upload_name}",
        "bytes": len(data),
        "markdown": f"请查看这张图片：{path}",
    }


def pane_by_id(pane_id: str) -> Pane | None:
    """Resolve ONE pane's metadata with a single tmux query.

    Old impl called list_panes() which enumerates ALL panes and, per pane, runs
    a tmux capture + a pgrep child-command probe + status inference — ~645ms just
    to find one pane on a heavily loaded host. /api/capture only needs
    this one pane's identity fields (it recomputes status itself from the deep
    capture), so query just the target pane via display-message (~10-90ms).
    preview/status are left empty on purpose; the caller fills status."""
    if not pane_id:
        return None
    fmt = "\t".join([
        "#{pane_id}", "#{session_name}", "#{window_index}", "#{pane_index}",
        "#{window_name}", "#{pane_current_command}", "#{pane_current_path}",
        "#{pane_title}", "#{window_active}", "#{pane_pid}",
        "#{@ai_session_id}", "#{@ai_transcript}",
    ])
    cp = run_tmux(["display-message", "-p", "-t", pane_id, "-F", fmt])
    if cp.returncode != 0:
        return None
    # Keep trailing tabs: unset tmux user options are emitted as empty final
    # fields.  ``strip()`` removed them and made every unstamped pane look like
    # malformed metadata, so single-pane capture/history could not resolve it.
    line = cp.stdout.rstrip("\r\n").splitlines()
    if not line:
        return None
    parts = line[0].split("\t")
    if len(parts) != 12:
        return None
    (
        pid, session, win, pane, window_name, command, cwd, title, active,
        pane_pid, ai_session_id, ai_transcript,
    ) = parts
    if DEFAULT_SESSION and session != DEFAULT_SESSION:
        return None
    ai_alive = True
    if command in {"bash", "zsh", "fish", "sh"}:
        child = _foreground_child_command(pane_pid)
        if child:
            command = child
        else:
            # ai-session-shell intentionally leaves an interactive shell after
            # the provider exits.  Keep the pane's provider identity here too,
            # not only in the full /api/panes scan; /api/capture uses this
            # lightweight lookup to choose assistant-vs-terminal parsing.
            command = _shell_wrapper_provider(pane_pid) or command
            ai_alive = False
    kind = classify(command, title, window_name)
    return Pane(
        pane_id=pid,
        target=f"{session}:{win}.{pane}",
        session=session,
        window_index=int(win),
        pane_index=int(pane),
        window_name=window_name,
        command=command,
        cwd=cwd,
        title=title,
        active=active == "1",
        kind=kind,
        project=project_from(cwd, window_name, title),
        preview="",
        status="",
        pane_pid=pane_pid,
        pane_start_time=_process_start_token(pane_pid),
        ai_session_id=ai_session_id.strip(),
        ai_transcript=ai_transcript.strip(),
        ai_alive=ai_alive,
    )


def active_pane(session: str = DEFAULT_SESSION) -> dict[str, object] | None:
    """Return the session's current active pane with one lightweight tmux query.

    This deliberately does not call list_panes(), capture(), pgrep, or the job
    ledger.  It exists for small UI labels that only need to know which tmux
    window/pane is active and should not pay the cost of a full card-grid poll.
    """
    if not session:
        return None
    fmt = "\t".join([
        "#{pane_id}", "#{session_name}", "#{window_index}", "#{pane_index}",
        "#{window_name}", "#{pane_current_command}", "#{pane_current_path}",
        "#{pane_title}", "#{window_active}", "#{pane_active}",
    ])
    cp = run_tmux(["display-message", "-p", "-t", session, "-F", fmt])
    if cp.returncode != 0:
        return None
    lines = cp.stdout.rstrip("\n").splitlines()
    if not lines:
        return None
    parts = lines[0].split("\t")
    if len(parts) != 10:
        return None
    pane_id, actual_session, win, pane, window_name, command, cwd, title, window_active, pane_active = parts
    try:
        window_index = int(win)
        pane_index = int(pane)
    except ValueError:
        return None
    return {
        "pane": pane_id,
        "pane_id": pane_id,
        "target": f"{actual_session}:{win}.{pane}",
        "session": actual_session,
        "window_index": window_index,
        "pane_index": pane_index,
        "window_name": window_name,
        "command": command,
        "cwd": cwd,
        "title": title,
        "active": window_active == "1" and pane_active == "1",
        "kind": classify(command, title, window_name),
        "project": project_from(cwd, window_name, title),
    }


def validate_input_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ValueError("empty input")
    if len(text) > 8000:
        raise ValueError("input too long; keep it under 8000 characters")
    for char in text:
        code = ord(char)
        if code < 32 and char not in {"\n", "\t"}:
            raise ValueError(f"control character U+{code:04X} is blocked")
        if code == 127:
            raise ValueError("DEL control character is blocked")
    return text


# When a person last sent input to a pane from this page; the background
# auto-approve loop leaves such a pane alone for a short while.
_WEB_INPUT_AT: dict[str, float] = {}


def note_web_input(pane_id: str) -> None:
    _WEB_INPUT_AT[pane_id] = time.time()


def send_text_to_pane(pane_id: str, text: str, enter: bool) -> Pane:
    pane = pane_by_id(pane_id)
    if pane is None:
        raise ValueError(f"pane not found or not in {DEFAULT_SESSION}: {pane_id}")
    note_web_input(pane.pane_id)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if enter:
        text = text.rstrip("\n")
    text = validate_input_text(text)
    tmux_delivery.paste_and_submit(
        pane.pane_id,
        text,
        enter,
        timeout=COMMAND_TIMEOUT,
        submit_delay=SUBMIT_DELAY,
    )
    return pane


def send_key_to_pane(pane_id: str, key: str) -> Pane:
    # Keep this endpoint narrow. Digits + Enter are needed for structured
    # Claude/Codex pickers; Escape/C-c are the floating interrupt controls.
    allowed = {"Escape", "C-c", "C-m", *[str(i) for i in range(10)]}
    if key not in allowed:
        raise ValueError("unsupported key")
    pane = pane_by_id(pane_id)
    if pane is None:
        raise ValueError(f"pane not found or not in {DEFAULT_SESSION}: {pane_id}")
    note_web_input(pane.pane_id)
    sent = run_tmux(["send-keys", "-t", pane.pane_id, key])
    if sent.returncode != 0:
        raise RuntimeError(sent.stderr.strip() or f"tmux send {key} failed")
    return pane


UPLOAD_PATHS = {"/api/files/upload", "/api/files/upload-chunk"}


def cross_site_write_refusal(headers: Mapping[str, str], path: str) -> str:
    """Why a write request must be refused as possibly cross-site ('' = allowed).

    The browser keeps sending cached Basic Auth credentials, so any web page
    could otherwise submit a text/plain form to /api/send and type into a pane.
    A write must look like our own page's request: a browser marks cross-site
    requests with Sec-Fetch-Site, JSON endpoints need an application/json body
    (a cross-site page cannot send that without a CORS preflight, which this
    server never grants), and uploads need the X-Cards-Upload header (a custom
    header forces the same preflight).  Local clients (cards_control) send
    JSON and no Sec-Fetch-Site."""
    site = str(headers.get("Sec-Fetch-Site") or "").strip().lower()
    if site and site not in {"same-origin", "none"}:
        return "cross-site write refused"
    if path in UPLOAD_PATHS:
        return "" if headers.get("X-Cards-Upload") == "1" else "upload needs the X-Cards-Upload header"
    content_type = str(headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    return "" if content_type == "application/json" else "write requests must be application/json"


class PaneIdentityConflict(RuntimeError):
    """The browser is acting on a pane card that no longer names that process."""


class SendRequestConflict(RuntimeError):
    """A send request id was reused for a different or uncertain delivery."""


def validate_pane_instance(pane: Pane, expected_pid: str = "", expected_start_time: str = "") -> None:
    """Fail closed when a browser card no longer identifies this pane process."""
    expected_pid = str(expected_pid or "").strip()
    expected_start_time = str(expected_start_time or "").strip()
    if bool(expected_pid) != bool(expected_start_time):
        raise ValueError("complete pane identity is required")
    if not expected_pid:
        return  # Backward compatibility for tabs opened before identity fields shipped.
    if (
        not pane.pane_pid
        or not pane.pane_start_time
        or pane.pane_pid != expected_pid
        or pane.pane_start_time != expected_start_time
    ):
        raise PaneIdentityConflict("pane changed; refresh before sending")


def _send_request_fingerprint(pane_id: str, text: str, enter: bool) -> str:
    raw = f"{pane_id}\0{int(enter)}\0{text}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _send_receipt(job: dict[str, object], text: str, enter: bool, *, deduplicated: bool) -> dict[str, object]:
    return {
        "ok": True,
        "pane": str(job.get("pane") or ""),
        "target": str(job.get("target") or ""),
        "chars": len(text),
        "enter": enter,
        "job_id": str(job.get("id") or ""),
        "job": job_summary(job),
        "delivery": "terminal",
        "deduplicated": deduplicated,
    }


def send_message_with_receipt(
    pane_id: str,
    text: str,
    enter: bool,
    *,
    job_id: str = "",
    expected_pid: str = "",
    expected_start_time: str = "",
) -> dict[str, object]:
    """Deliver a Cards message once and return a retry-safe receipt.

    A client-generated job id is the idempotency key. The job is written as
    ``starting`` before tmux input and ``sent`` afterwards. If the HTTP response
    is lost, retrying the same id returns the durable receipt without pasting a
    duplicate message into the pane.
    """
    pane_id = str(pane_id or "").strip()
    if not pane_id:
        raise ValueError("missing pane")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if enter:
        text = text.rstrip("\n")
    text = validate_input_text(text)
    if not enter:
        pane = pane_by_id(pane_id)
        if pane is None:
            raise ValueError(f"pane not found or not in {DEFAULT_SESSION}: {pane_id}")
        validate_pane_instance(pane, expected_pid, expected_start_time)
        pane = send_text_to_pane(pane_id, text, False)
        validate_pane_instance(pane, expected_pid, expected_start_time)
        return {
            "ok": True,
            "pane": pane.pane_id,
            "target": pane.target,
            "chars": len(text),
            "enter": False,
            "job_id": "",
            "job": {},
            "delivery": "terminal",
            "deduplicated": False,
        }

    job_id = str(job_id or "").strip() or event_ledger.new_job_id("cards")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,180}", job_id):
        raise ValueError("invalid job_id")
    fingerprint = _send_request_fingerprint(pane_id, text, True)

    with SEND_REQUEST_LOCK:
        existing = event_ledger.get_job(job_id)
        if existing:
            existing_hash = str(existing.get("request_hash") or "")
            fallback_matches = (
                str(existing.get("source") or "") == "card-dashboard"
                and str(existing.get("pane") or "") == pane_id
                and str(existing.get("task_preview") or "") == event_ledger.compact(text, 220)
            )
            if (existing_hash and existing_hash != fingerprint) or (not existing_hash and not fallback_matches):
                raise SendRequestConflict("job_id already belongs to another message")
            status = event_ledger.normalize_status(str(existing.get("status") or ""))
            if status in {"starting", "failed"}:
                raise SendRequestConflict("previous delivery is uncertain; submit again after refreshing")
            return _send_receipt(existing, text, True, deduplicated=True)

        pane = pane_by_id(pane_id)
        if pane is None:
            raise ValueError(f"pane not found or not in {DEFAULT_SESSION}: {pane_id}")
        validate_pane_instance(pane, expected_pid, expected_start_time)
        job = event_ledger.upsert_job(
            job_id,
            source="card-dashboard",
            status="starting",
            pane=pane.pane_id,
            target=pane.target,
            repo=pane.cwd,
            task_preview=event_ledger.compact(text, 220),
            request_hash=fingerprint,
            message="delivering from /cards composer",
        )
        try:
            pane = send_text_to_pane(pane_id, text, True)
            validate_pane_instance(pane, expected_pid, expected_start_time)
        except Exception as exc:
            event_ledger.upsert_job(
                job_id,
                status="failed",
                message=f"tmux delivery failed: {type(exc).__name__}",
                completed_at=event_ledger.now_iso(),
            )
            event_ledger.append_event(
                "tmux_send_failed",
                job_id=job_id,
                pane=pane_id,
                target=pane.target,
                source="card-dashboard",
                status="failed",
                message=f"tmux delivery failed: {type(exc).__name__}",
            )
            invalidate_job_cache()
            raise

        PANE_ACTIVITY_CACHE.setdefault(pane_id, {})["changed_at"] = time.time()
        PANE_ACTIVITY_CACHE[pane_id]["sig"] = ""
        job = event_ledger.upsert_job(
            job_id,
            source="card-dashboard",
            status="sent",
            pane=pane.pane_id,
            target=pane.target,
            repo=pane.cwd,
            task_preview=event_ledger.compact(text, 220),
            request_hash=fingerprint,
            message="sent from /cards composer",
        )
        event_ledger.append_event(
            "tmux_send",
            job_id=job_id,
            pane=pane.pane_id,
            target=pane.target,
            source="card-dashboard",
            status="sent",
            message="sent from /cards composer",
            data={
                "chars": len(text),
                "enter": True,
                "idempotent": True,
                # The Cards hot-path job index is reconstructed from the
                # append-only event log.  Keep the safe, compact prompt here
                # so visible-response matching does not need to reopen a
                # random per-job JSON file.
                "task_preview": event_ledger.compact(text, 220),
            },
        )
        invalidate_job_cache()
        complete_older_cards_jobs(pane.pane_id, job)
        return _send_receipt(job, text, True, deduplicated=False)


def close_pane(pane_id: str, expected_pid: str, expected_start_time: str) -> dict[str, object]:
    """Close one exact tmux pane instance and reconcile Agent Bus state.

    ``kill-pane`` intentionally closes only the represented pane. tmux itself
    closes the containing window when this was its final pane. The exact
    process identity check prevents a stale phone/browser tab from closing a
    newly-created pane that reused the same ``%pane_id``.
    """
    pane_id = str(pane_id or "").strip()
    expected_pid = str(expected_pid or "").strip()
    expected_start_time = str(expected_start_time or "").strip()
    if not pane_id or not expected_pid or not expected_start_time:
        raise ValueError("pane identity is required")
    pane = pane_by_id(pane_id)
    if pane is None:
        raise FileNotFoundError(f"pane not found or not in {DEFAULT_SESSION}: {pane_id}")
    validate_pane_instance(pane, expected_pid, expected_start_time)

    killed = run_tmux(["kill-pane", "-t", pane.pane_id])
    if killed.returncode != 0:
        raise RuntimeError(killed.stderr.strip() or "tmux kill-pane failed")
    if pane_by_id(pane.pane_id) is not None:
        raise RuntimeError("pane still exists after tmux kill-pane")

    cancelled_job_ids: list[str] = []
    closed_at = event_ledger.now_iso()
    for job in event_jobs_cached(pane=pane.pane_id, limit=5_000, include_terminal=False):
        job_id = str(job.get("id") or "")
        if not job_id:
            continue
        event_ledger.upsert_job(
            job_id,
            status="cancelled",
            message="pane closed from Cards",
            completed_at=closed_at,
        )
        cancelled_job_ids.append(job_id)
    event_ledger.append_event(
        "pane_closed",
        pane=pane.pane_id,
        target=pane.target,
        source="card-dashboard",
        status="cancelled",
        message="pane closed from Cards",
        data={"cancelled_job_ids": cancelled_job_ids},
    )
    PANE_ACTIVITY_CACHE.pop(pane.pane_id, None)
    JOB_IDLE_SINCE.pop(pane.pane_id, None)
    invalidate_job_cache()
    with _PANES_RESP_CONDITION:
        _PANES_RESP_CACHE.pop(pane.session, None)
        _PANES_RESP_FAILURES.pop(pane.session, None)
        _PANES_RESP_CONDITION.notify_all()
    return {
        "ok": True,
        "pane": pane.pane_id,
        "target": pane.target,
        "window_index": pane.window_index,
        "cancelled_jobs": len(cancelled_job_ids),
    }


def _process_start_token(pid: str) -> str:
    """Return a stable token for a live process instance: pid + kernel starttime."""
    if not pid or not pid.isdigit():
        return ""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        rest = stat.rsplit(") ", 1)[1].split()
        # procfs field 22 (starttime) becomes index 19 after removing pid/comm.
        return f"{pid}:{rest[19]}"
    except Exception:
        return ""


_CHILD_CMD_CACHE: dict[str, tuple[float, tuple[str, str]]] = {}
_CHILD_CMD_TTL = 2.0  # seconds


_FOREGROUND_CHILD_MAX_DEPTH = 4


def _foreground_child_pid_commands(pane_pids: set[str]) -> dict[str, tuple[str, str]]:
    """Resolve the nearest Claude/Codex descendant for many shell panes at once.

    The old hot path spawned one ``pgrep -P`` process per shell pane.  With
    dozens of Cards windows that made a cold Claude capture spend several
    seconds merely checking whether another live pane shared its transcript.
    One bounded ``ps`` snapshot preserves the same semantics and populates the
    existing short-TTL cache for every pane in the batch.

    Direct-child-only used to be enough because ``agent_window.sh`` normally
    ``exec``s straight into ai-session-shell, collapsing that wrapper into the
    pane's own pid. When a pane instead reaches codex through an un-exec'd
    layer (e.g. a manually typed ``env CODEX_HOME=... ai-session-shell codex``,
    or a recovery relaunch that doesn't exec) the real agent sits at the
    grandchild or great-grandchild instead of the direct child, the direct-
    child lookup finds nothing, and the pane falls back to a bare "Shell" card
    even though codex is alive and interactive (the pane pid can stay a plain
    bash blocked on `"$@"; exec bash` while codex runs three levels down as
    pid->node->codex-binary). Walk the
    same bounded depth `agent_window.sh` already uses for its own foreground
    checks (``pane_session_id`` / ``cwd_has_live_agent``) instead of stopping
    at depth 1.
    """
    valid = {pid for pid in pane_pids if pid and pid.isdigit()}
    if not valid:
        return {}

    now = time.time()
    found: dict[str, tuple[str, str]] = {}
    missing: set[str] = set()
    for pane_pid in valid:
        hit = _CHILD_CMD_CACHE.get(pane_pid)
        if hit and now - hit[0] < _CHILD_CMD_TTL:
            found[pane_pid] = hit[1]
        else:
            missing.add(pane_pid)

    if missing:
        resolved = {pane_pid: ("", "") for pane_pid in missing}
        try:
            out = subprocess.run(
                ["ps", "-eo", "pid=,ppid=,comm="],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if out.returncode == 0:
                children_by_ppid: dict[str, list[tuple[str, str]]] = {}
                for raw in out.stdout.splitlines():
                    parts = raw.split(None, 2)
                    if len(parts) != 3:
                        continue
                    child_pid, parent_pid, comm = parts
                    children_by_ppid.setdefault(parent_pid, []).append((child_pid, comm))
                for pane_pid in missing:
                    frontier = [pane_pid]
                    for _depth in range(_FOREGROUND_CHILD_MAX_DEPTH):
                        next_frontier: list[str] = []
                        for parent_pid in frontier:
                            for child_pid, comm in children_by_ppid.get(parent_pid, ()):
                                if comm in {"claude", "codex", "node"}:
                                    resolved[pane_pid] = (child_pid, comm)
                                    break
                                next_frontier.append(child_pid)
                            if resolved[pane_pid][0]:
                                break
                        if resolved[pane_pid][0] or not next_frontier:
                            break
                        frontier = next_frontier
        except Exception:
            pass
        for pane_pid, result in resolved.items():
            _CHILD_CMD_CACHE[pane_pid] = (now, result)
            found[pane_pid] = result

    return {pane_pid: found.get(pane_pid, ("", "")) for pane_pid in valid}


def _foreground_child_pid_command(pane_pid: str) -> tuple[str, str]:
    """pane leader 是 shell 时,返回其下 claude/codex/node 子进程 pid + comm。

    单窗口调用也复用批量 ``ps`` 快照和短 TTL 缓存；list_panes / Claude 上下文会
    一次解析全部 shell pane，避免为每个窗口串行启动 pgrep。子进程身份极少变化，
    2 秒 TTL 仍能让新启动的 claude/codex 在下一轮被识别。"""
    return _foreground_child_pid_commands({pane_pid}).get(pane_pid, ("", ""))


def _foreground_child_command(pane_pid: str) -> str:
    """pane leader 是 shell 时,返回其下 claude/codex/node 子进程命令(让 classify 判对类型)。"""
    _pid, comm = _foreground_child_pid_command(pane_pid)
    return comm


def _shell_wrapper_provider(pane_pid: str) -> str:
    """Recover the provider from an exited ``ai-session-shell`` wrapper.

    The wrapper deliberately leaves an interactive bash after Codex/Claude
    exits so the user can resume the same session.  At that point there is no
    live child for ``_foreground_child_command`` to find, but the pane is still
    an AI card and its captured transcript still follows the AI block format.
    Keep this detection narrow to the managed wrapper command; arbitrary shell
    commands containing the words ``codex`` or ``claude`` must remain Shell.
    """
    if not pane_pid or not pane_pid.isdigit():
        return ""
    try:
        raw = Path(f"/proc/{pane_pid}/cmdline").read_bytes()
    except OSError:
        return ""
    command_line = raw.replace(b"\0", b" ").decode("utf-8", errors="replace")
    match = re.search(r"(?:^|[ /])ai-session-shell\s+(codex|claude)(?:\s|$)", command_line, re.I)
    return match.group(1).lower() if match else ""


def _is_shell_process(pid: str) -> bool:
    """Whether pid's own live process is literally a shell, checked against
    /proc directly - NOT tmux's #{pane_current_command}, which reports the
    current FOREGROUND process in the pane and can already read e.g. "claude"
    while pid itself is still the pane's original (and unrelated) shell. See
    _pane_agent_process_identity for why that distinction matters."""
    if not pid or not pid.isdigit():
        return False
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8") as fh:
            comm = fh.read().strip()
    except Exception:
        return False
    return comm in {"bash", "zsh", "fish", "sh"}


_PROC_IDENTITY_CACHE: dict[str, tuple[float, str]] = {}
_PROC_IDENTITY_TTL = 1.0  # seconds


def _pane_agent_process_identity(pane: Pane) -> str:
    """Bind pane-level caches to the actual live agent process, not just pane_id.

    tmux pane ids can be reused after windows are closed/reopened. Claude's JSONL
    transcript mapping is therefore only safe to reuse when the pane still hosts
    the same process instance.

    Short-TTL cached (1s): this spawns tmux display-message + pgrep + /proc reads
    (~74ms under load) and is called 2x within a single /api/capture (once here,
    once inside claude_transcript_for_pane). 1s TTL < the poll interval so a
    genuine process restart is still detected promptly.
    """
    now = time.time()
    hit = _PROC_IDENTITY_CACHE.get(pane.pane_id)
    if hit and now - hit[0] < _PROC_IDENTITY_TTL:
        return hit[1]
    token = _compute_pane_agent_process_identity(pane)
    _PROC_IDENTITY_CACHE[pane.pane_id] = (now, token)
    return token


def _compute_pane_agent_process_identity(pane: Pane) -> str:
    try:
        cp = run_tmux(["display-message", "-p", "-t", pane.pane_id, "#{pane_pid}"])
    except Exception:
        return ""
    if cp.returncode != 0:
        return ""
    pane_pid = cp.stdout.strip()
    # #{pane_pid} is fixed at pane creation (normally the login shell) and does
    # NOT change when a program running inside that shell exits and a new one
    # starts. #{pane_current_command} was used here before to decide whether
    # to look for a foreground child, but that reports the live FOREGROUND
    # process name, not what pane_pid itself is - once it read "claude", the
    # code assumed pane_pid was already the agent process and stopped
    # looking, while pane_pid was still the same long-lived shell. Restarting
    # Claude in that same pane then never produced a new identity token and a
    # stale transcript stayed cached forever (a pane's shell can outlive many
    # Claude restarts).
    # Check pane_pid's own /proc comm instead: only look for a foreground
    # child when pane_pid is genuinely a shell. This must stay narrow - a
    # pane whose top-level process already IS claude/codex (no shell wrapper)
    # can itself have a claude/codex/node *child* (a subprocess, a spawned
    # worker); unconditionally preferring any such child would rebind
    # identity to a transient process and cause spurious cache churn on a
    # pane that was already correctly identified.
    agent_pid = pane_pid
    if _is_shell_process(pane_pid):
        child_pid, child_command = _foreground_child_pid_command(pane_pid)
        if child_pid and child_command in {"claude", "codex", "node"}:
            agent_pid = child_pid
    token = _process_start_token(agent_pid)
    if token:
        return token
    return ""


_PANE_CAPTURE_WORKERS = max(
    1,
    min(8, int(os.environ.get("TMUX_CARD_CAPTURE_WORKERS", "8"))),
)


def _acquire_capture_lock(lock) -> bool:
    """Acquire the inventory-capture lock without hanging an HTTP worker."""
    deadline = time.monotonic() + PANE_CAPTURE_LOCK_TIMEOUT
    while True:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


def list_panes(session_filter: str = DEFAULT_SESSION, preview_history: int = 30, include_preview: bool = True) -> list[Pane]:
    fmt = "\t".join(
        [
            "#{pane_id}",
            "#{session_name}",
            "#{window_index}",
            "#{pane_index}",
            "#{window_name}",
            "#{pane_current_command}",
            "#{pane_current_path}",
            "#{pane_title}",
            "#{window_active}",
            "#{pane_pid}",
            "#{@ai_session_id}",
            "#{@ai_transcript}",
        ]
    )
    cp = run_tmux(["list-panes", "-a", "-F", fmt])
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.strip() or "tmux list-panes failed")

    rows: list[list[str]] = []
    for line in cp.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 12:
            continue
        session = parts[1]
        if session_filter and session != session_filter:
            continue
        rows.append(parts)

    child_commands = _foreground_child_pid_commands({
        parts[9] for parts in rows if parts[5] in {"bash", "zsh", "fish", "sh"}
    })
    wrapper_providers = {
        parts[9]: _shell_wrapper_provider(parts[9])
        for parts in rows
        if parts[5] in {"bash", "zsh", "fish", "sh"}
    }
    # 抓屏是纯 I/O(每次 fork 一个 tmux),53 个 pane 串行要 0.28s,占了整次列表刷新的大头。
    # 并行开销几乎为零,又不改变任何语义。抓失败的照旧当空串处理。
    def _preview_capture(pane_id: str) -> tuple[str, str]:
        try:
            return pane_id, capture(pane_id, history=preview_history)
        except RuntimeError:
            return pane_id, ""

    recent_by_pane: dict[str, str] = {}
    if rows and include_preview:
        # Multiple benchmark/test processes used to fan out captures against
        # the production tmux server at the same time.  tmux is a single
        # server process, so that multiplies load across every live AI pane and
        # can kill the whole server.  Serialize each inventory-wide capture
        # batch across processes and cap the per-batch fan-out.  Tests can use
        # TMUX_CARD_TMUX_LABEL/TMUX_CARD_TMUX_SOCKET for complete isolation.
        PANE_CAPTURE_LOCK.parent.mkdir(parents=True, exist_ok=True)
        with PANE_CAPTURE_LOCK.open("a+", encoding="utf-8") as lock:
            if _acquire_capture_lock(lock):
                with ThreadPoolExecutor(max_workers=_PANE_CAPTURE_WORKERS) as pool:
                    recent_by_pane = dict(pool.map(_preview_capture, [parts[0] for parts in rows]))
            else:
                print(
                    f"card-dashboard: preview capture lock timed out: {PANE_CAPTURE_LOCK}",
                    file=sys.stderr,
                )

    panes: list[Pane] = []
    for parts in rows:
        (
            pane_id, session, win, pane, window_name, command, cwd, title, active,
            pane_pid, ai_session_id, ai_transcript,
        ) = parts
        target = f"{session}:{win}.{pane}"
        ai_alive = True
        # tmux pane_current_command 取进程组 leader;若 leader 是 shell 但其子进程是 claude/codex
        # (如 agent_window.sh 用 `claude; exec bash` 起的窗口,claude 是 bash 子进程),
        # 用子进程命令来判类型,否则会误判成 Shell。
        if command in {"bash", "zsh", "fish", "sh"}:
            _child_pid, child = child_commands.get(pane_pid, ("", ""))
            if child:
                command = child
            else:
                # A completed ai-session-shell leaves bash behind. Preserve
                # the stable Codex/Claude card identity instead of reverting
                # the pane to a blue/neutral Shell card.
                command = wrapper_providers.get(pane_pid) or command
                # 卡片身份保留,但状态判定必须知道"AI 已经不在了" —— 否则屏幕上残留的
                # 输入框会让一个崩掉的会话显示成正常的 idle 卡片。
                ai_alive = False
        recent = recent_by_pane.get(pane_id, "")
        kind = classify(command, title, window_name)
        pane_obj = Pane(
                pane_id=pane_id,
                target=target,
                session=session,
                window_index=int(win),
                pane_index=int(pane),
                window_name=window_name,
                command=command,
                cwd=cwd,
                title=title,
                active=active == "1",
                kind=kind,
                project=project_from(cwd, window_name, title),
                preview=preview_from(recent) if include_preview else "",
                status=infer_pane_status(
                    pane_id, recent, command, kind, ai_alive, ai_transcript.strip(), pane_pid,
                    ai_session_id.strip(),
                ),
                pane_pid=pane_pid,
                pane_start_time=_process_start_token(pane_pid),
                ai_session_id=ai_session_id.strip(),
                ai_transcript=ai_transcript.strip(),
                ai_alive=ai_alive,
                identity_fidelity="exact" if ai_session_id.strip() else "unresolved",
                background=claude_background_label(recent) if kind == "Claude" and ai_alive else "",
            )
        # Codex does not stamp a session id on the pane.  Resolve its open
        # rollout descriptors using the same screen-bound disambiguator used
        # by trace/history; ties remain explicitly ambiguous instead of being
        # guessed from cwd or mtime.
        # Rollout-FD disambiguation is intentionally lazy: scanning open
        # descriptors for every Codex pane makes the global Cards poll slow.
        # Probe only the currently active pane; opening a pane can perform the
        # same check through the capture/trace path when needed.
        if kind == "Codex" and ai_alive and not ai_session_id.strip() and active == "1":
            try:
                _rollout, resolve_meta = codex_rollout_for_pane(pane_obj, recent)
                quality = str(resolve_meta.get("quality") or "")
                reason = str(resolve_meta.get("reason") or "")
                if quality == "full":
                    pane_obj = replace(pane_obj, identity_fidelity="inferred-exact", identity_reason="")
                else:
                    pane_obj = replace(pane_obj, identity_fidelity="ambiguous", identity_reason=reason or "no-rollout")
            except Exception:
                pane_obj = replace(pane_obj, identity_fidelity="ambiguous", identity_reason="identity-probe-failed")
        panes.append(pane_obj)
    return sorted(panes, key=lambda item: (item.session, item.window_index, item.pane_index))


_PANES_RESP_CACHE: dict[str, tuple[float, str, list[dict[str, object]]]] = {}
_PANES_RESP_TTL = 0.9  # seconds
_PANES_RESP_MAX_STALE = float(os.environ.get("TMUX_CARD_PANES_MAX_STALE_SECONDS", "30"))
_PANES_RESP_FAILURE_BACKOFF = float(os.environ.get("TMUX_CARD_PANES_FAILURE_BACKOFF_SECONDS", "1.5"))
_PANES_RESP_REFRESHING: set[str] = set()
_PANES_RESP_FAILURES: dict[str, tuple[float, str]] = {}
_PANES_RESP_CONDITION = threading.Condition()


@dataclass(frozen=True)
class PanesResponseSnapshot:
    panes: list[dict[str, object]]
    snapshot_at: str
    snapshot_age_ms: int
    stale: bool
    refreshing: bool
    refresh_error: str = ""


class PanesResponseUnavailable(RuntimeError):
    def __init__(self, message: str, *, snapshot_at: str = "", snapshot_age_ms: int = 0) -> None:
        super().__init__(message)
        self.snapshot_at = snapshot_at
        self.snapshot_age_ms = snapshot_age_ms


def _store_panes_response(session: str, panes: list[dict[str, object]]) -> None:
    with _PANES_RESP_CONDITION:
        _PANES_RESP_CACHE[session] = (time.monotonic(), now_iso(), panes)
        _PANES_RESP_FAILURES.pop(session, None)
        _PANES_RESP_REFRESHING.discard(session)
        _PANES_RESP_CONDITION.notify_all()


def _record_panes_response_failure(session: str, exc: BaseException) -> None:
    with _PANES_RESP_CONDITION:
        _PANES_RESP_FAILURES[session] = (time.monotonic(), str(exc) or type(exc).__name__)
        _PANES_RESP_REFRESHING.discard(session)
        _PANES_RESP_CONDITION.notify_all()


def _refresh_panes_response_in_background(session: str) -> None:
    try:
        panes = build_panes_response(session)
    except Exception as exc:  # noqa: BLE001 - stale snapshot remains usable
        _record_panes_response_failure(session, exc)
        sys.stderr.write(f"background pane refresh failed for {session!r}: {exc}\n")
        return
    _store_panes_response(session, panes)


def _panes_response_snapshot(session: str) -> PanesResponseSnapshot:
    """Single-flight, stale-while-revalidate cache for ``/api/panes``.

    A fresh snapshot is returned directly.  Once it expires, callers receive
    the stale snapshot immediately while at most one request-triggered daemon
    thread refreshes it.  On a true cold start there is no stale value to serve,
    so one caller builds and concurrent callers wait for that same build rather
    than multiplying the expensive full-pane enumeration.  There is no timer or
    resident refresh loop: an incoming request is the only refresh trigger.
    """
    expired_snapshot_at = ""
    expired_snapshot_age_ms = 0
    while True:
        launch_background = False
        stale: list[dict[str, object]] | None = None
        with _PANES_RESP_CONDITION:
            now = time.monotonic()
            hit = _PANES_RESP_CACHE.get(session)
            failure = _PANES_RESP_FAILURES.get(session)
            recent_failure = bool(failure and now - failure[0] < _PANES_RESP_FAILURE_BACKOFF)
            if hit and now - hit[0] < _PANES_RESP_TTL:
                return PanesResponseSnapshot(
                    panes=hit[2],
                    snapshot_at=hit[1],
                    snapshot_age_ms=max(0, int((now - hit[0]) * 1000)),
                    stale=False,
                    refreshing=False,
                )
            if hit and now - hit[0] <= _PANES_RESP_MAX_STALE:
                stale = hit[2]
                if session not in _PANES_RESP_REFRESHING and not recent_failure:
                    _PANES_RESP_REFRESHING.add(session)
                    launch_background = True
                result = PanesResponseSnapshot(
                    panes=hit[2],
                    snapshot_at=hit[1],
                    snapshot_age_ms=max(0, int((now - hit[0]) * 1000)),
                    stale=True,
                    refreshing=launch_background or session in _PANES_RESP_REFRESHING,
                    refresh_error=failure[1] if failure else "",
                )
            elif session in _PANES_RESP_REFRESHING:
                # A snapshot older than the advertised maximum is no longer
                # served as if it were live. Wait briefly for the shared
                # rebuild, then fail explicitly instead of hanging forever.
                notified = _PANES_RESP_CONDITION.wait(timeout=8)
                if not notified:
                    snapshot_at = hit[1] if hit else ""
                    age_ms = max(0, int((now - hit[0]) * 1000)) if hit else 0
                    raise PanesResponseUnavailable(
                        "pane snapshot refresh timed out",
                        snapshot_at=snapshot_at,
                        snapshot_age_ms=age_ms,
                    )
                continue
            elif recent_failure:
                snapshot_at = hit[1] if hit else ""
                age_ms = max(0, int((now - hit[0]) * 1000)) if hit else 0
                raise PanesResponseUnavailable(
                    failure[1],
                    snapshot_at=snapshot_at,
                    snapshot_age_ms=age_ms,
                )
            else:
                # Cold start, or a snapshot beyond the maximum stale age:
                # one leader rebuilds outside the lock while followers wait.
                if hit:
                    expired_snapshot_at = hit[1]
                    expired_snapshot_age_ms = max(0, int((now - hit[0]) * 1000))
                _PANES_RESP_REFRESHING.add(session)
                break

        if stale is not None:
            if launch_background:
                try:
                    threading.Thread(
                        target=_refresh_panes_response_in_background,
                        args=(session,),
                        name=f"cards-panes-refresh-{session}",
                        daemon=True,
                    ).start()
                except RuntimeError as exc:
                    _record_panes_response_failure(session, exc)
                    sys.stderr.write(f"could not start pane refresh for {session!r}: {exc}\n")
                    result = PanesResponseSnapshot(
                        panes=result.panes,
                        snapshot_at=result.snapshot_at,
                        snapshot_age_ms=result.snapshot_age_ms,
                        stale=True,
                        refreshing=False,
                        refresh_error=str(exc),
                    )
            return result

    try:
        panes = build_panes_response(session)
    except Exception as exc:
        _record_panes_response_failure(session, exc)
        raise PanesResponseUnavailable(
            str(exc) or "pane snapshot refresh failed",
            snapshot_at=expired_snapshot_at,
            snapshot_age_ms=expired_snapshot_age_ms,
        ) from exc
    _store_panes_response(session, panes)
    with _PANES_RESP_CONDITION:
        stored = _PANES_RESP_CACHE[session]
    return PanesResponseSnapshot(
        panes=stored[2],
        snapshot_at=stored[1],
        snapshot_age_ms=0,
        stale=False,
        refreshing=False,
    )


def _panes_response_cached(session: str) -> list[dict[str, object]]:
    """Compatibility wrapper for callers that only need the pane list."""
    return _panes_response_snapshot(session).panes


def build_panes_response(session: str, *, preview_history: int = 30, include_preview: bool = True) -> list[dict[str, object]]:
    """Build the /api/panes payload, keeping each pane's job pill in sync with
    its live status.

    sync_pane_job_status() used to be called ONLY from /api/capture (a
    pane's detail view), so a job pill on the CARD GRID (what /api/panes
    renders) never refreshed unless someone happened to open that specific
    pane's detail - confirmed by an external Codex review as the actual
    reason "处理中" (running) stayed stuck on cards whose pane was long idle,
    even after the idle->completed fix was added to sync_pane_job_status()
    itself. Extracted out of the /api/panes handler so this can
    be unit tested without a live HTTP round trip."""
    latest_jobs = latest_jobs_by_pane_cached()
    pane_items = list_panes(session, preview_history=preview_history, include_preview=include_preview)
    panes: list[dict[str, object]] = []
    job_cache_dirty = False
    for item in pane_items:
        pane_dict = asdict(item)
        subagents = pane_running_subagents(item.kind, item.ai_alive, item.ai_transcript, item.pane_pid)
        if subagents:
            pane_dict["subagents_running"] = subagents
        job = latest_jobs.get(item.pane_id)
        runtime_observation: dict[str, str] = {}
        # Only bother re-syncing jobs still in a non-terminal state; a job
        # with no active record, or already completed/failed/etc., has
        # nothing to sync.
        if job and event_ledger.normalize_status(str(job.get("status") or "")) in event_ledger.ACTIVE_STATUSES:
            runtime_observation = provider_runtime_observation(job, item.status)
            sync_status = runtime_observation.get("status") or item.status
            synced = sync_pane_job_status(item.pane_id, item.target, sync_status)
            refreshed = synced or latest_job_summary_for_pane(item.pane_id, include_terminal=True)
            if refreshed != job:
                job = refreshed
                job_cache_dirty = True
        pane_dict["job"] = job_summary(job)
        pane_dict["job_status"] = pane_dict.get("job", {}).get("status", "")
        pane_dict["job_id"] = pane_dict.get("job", {}).get("id", "")
        if runtime_observation:
            pane_dict["runtime_status"] = runtime_observation.get("status", "")
            pane_dict["runtime_status_source"] = runtime_observation.get("source", "")
            pane_dict["runtime_status_confidence"] = runtime_observation.get("confidence", "")
        panes.append(pane_dict)
    if job_cache_dirty:
        invalidate_job_cache()
    return panes


def parse_blocks(text: str, pane_kind: str = "") -> list[dict[str, str]]:
    """Heuristic terminal-to-output blocks.

    We keep raw text available, but split obvious AI/user/tool boundaries so the
    browser can render a cleaner timeline.
    """
    markers = [
        (re.compile(r"^\s*[›❯]\s+(.+)"), "user", "User prompt"),
        (re.compile(r"^\s*•\s+((?:Added|Modified|Updated|Deleted|Created)\b.+)"), "tool", "File edit"),
        (re.compile(r"^\s*•\s+(.+)"), "assistant", "AI step"),
        (re.compile(r"^\s*(?:●\s+)?(Calling\s+\d+\s+tools?…)"), "assistant", "AI step"),
        (re.compile(r"^\s*●\s+(.+)"), "assistant", "AI output"),
        (re.compile(r"^\s*☐\s+(.+)"), "assistant", "AI step"),
        (re.compile(r"^\s*(Ran|Read|Explored|Edited|Applied|Searched|Opened)\b(.*)"), "tool", "Tool"),
        (re.compile(r"^\s*[└⎿]\s+(.+)"), "tool", "Tool result"),
    ]
    blocks: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    saw_marker = False

    def push() -> None:
        nonlocal current
        draft_like_user = False
        if current and current["text"].strip():
            raw_lines = current["text"].splitlines()
            draft_like_user = current["role"] == "user" and (
                any(is_model_status_line(line) for line in raw_lines)
                or bool(re.search(r"\\n\s*(?:gpt-[\w.-]+|claude[\w.-]*)(?:\s+[\w.-]+){0,3}\s+·", current["text"], re.I))
            )
            cleaned_lines = [
                line.rstrip()
                for line in raw_lines
                if not is_model_status_line(line) and not is_queued_message_line(line)
            ]
            current["text"] = strip_inline_model_status(filter_display_lines("\n".join(cleaned_lines)))
            if current["role"] in {"assistant", "user"}:
                current["text"] = unwrap_prose_soft_wraps(current["text"])
            if current["label"] == "File edit":
                current["text"] = normalize_file_edit_output(current["text"])
        if current and current["text"].strip():
            if not (
                current["role"] == "assistant"
                and current["label"] in {"AI step", "AI output"}
                and all(is_volatile_status_line(line) for line in current["text"].splitlines() if line.strip())
            ) and not (
                current["role"] == "user"
                and (
                    draft_like_user
                    or current["text"].startswith("Messages to be submitted after next tool call")
                    # 系统注入的 user turn(子智能体完成通知 / 系统提醒)不是用户真实输入,别当 User prompt 显示
                    or current["text"].lstrip().startswith("<task-notification>")
                    or current["text"].lstrip().startswith("<system-reminder>")
                    or current["text"].lstrip().startswith("<task-")
                )
            ):
                blocks.append(current)
        current = None

    raw_text = text
    split = pane_detectors.split_screen(text.splitlines())
    if split.provider == "claude":
        text = "\n".join(split.conversation)
    text = filter_display_lines(text)
    if split.has_input_region:
        choice_block = live_choice_block(raw_text)
    else:
        text, choice_block = extract_choice_block(text)
    # spinner / "Working (…)" 等易变状态行每帧切换字形(•↔◦…): 字形不同会让同一行时而匹配
    # block marker 自成一块被丢弃、时而并入上一块, 导致块高一行级横跳 → 前端追底把视图钉底 → 整屏抖动。
    # 切块前统一剥掉这些行(无论字形), 块高才稳定; 仅动 blocks, raw 视图保真不受影响。
    text = "\n".join(
        line for line in text.splitlines()
        if not is_volatile_status_line(line.rstrip())
    )
    default_role = "assistant" if pane_kind in {"Claude", "Codex"} else "terminal"
    default_label = "AI output" if default_role == "assistant" else "Terminal"
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        todo_line = parse_todo_summary_line(line)
        if todo_line is not None:
            saw_marker = True
            if current is None or current.get("label") != "Todo":
                push()
                current = {"role": "assistant", "label": "Todo", "text": todo_line}
            else:
                current["text"] += "\n" + todo_line
            continue
        matched = None
        for regex, role, label in markers:
            match = regex.match(line)
            if match:
                matched = (role, label, match.group(0).strip())
                break
        if matched:
            saw_marker = True
            push()
            role, label, first = matched
            if label in {"User prompt", "AI step", "AI output", "Tool result", "File edit"}:
                marker_match = next(regex.match(line) for regex, marker_role, marker_label in markers if marker_label == label and marker_role == role and regex.match(line))
                first = marker_match.group(1).strip()
            current = {"role": role, "label": label, "text": first}
        else:
            if current is None:
                current = {"role": default_role, "label": default_label, "text": line}
            else:
                current["text"] += "\n" + line
    push()
    if blocks and blocks[-1]["role"] == "user" and split.provider != "claude":
        # A final terminal prompt line is the user's current draft, not a
        # submitted historical message. Submitted prompts are followed by
        # assistant/tool output before the next prompt appears.
        blocks.pop()
    if not blocks and text.strip() and not saw_marker:
        blocks.append({"role": default_role, "label": default_label, "text": text.strip()})
    if choice_block:
        blocks.append(choice_block)
    return blocks[-120:]


# --- Claude conversation history via JSONL transcript -----------------------
# Claude Code runs in the terminal alternate screen and repaints in place, so
# tmux keeps ZERO scrollback for it (alt_on=1 history_size=0;
# even with alternate-screen off the in-place repaint never feeds scrollback).
# The only real source of Claude history is its own session transcript JSONL at
# ~/.claude/projects/<encoded-cwd>/<session>.jsonl. We read the tail of that file
# so history is near-real-time (Claude appends each turn as it completes), and we
# still pull the live picker from the current screen capture.

def _read_tail_lines(path: str, max_bytes: int = 8_000_000, max_lines: int = 600) -> list[str]:
    # Robust tail: a single huge JSONL line (e.g. a multi-hundred-KB tool_result)
    # must not wipe out all history. Read the last max_bytes; drop the partial
    # leading line ONLY when there is complete content after it. If the whole
    # window is a single (huge, partial) line, keep it rather than returning
    # nothing. The 8MB window is large enough to also include earlier complete
    # lines before a big last record.
    size = os.path.getsize(path)
    start = max(0, size - max_bytes)
    with open(path, "rb") as fh:
        fh.seek(start)
        data = fh.read()
    text = data.decode("utf-8", "replace")
    if start > 0:
        nl = text.find("\n")
        if nl != -1 and nl < len(text) - 1:
            text = text[nl + 1:]
    return text.splitlines()[-max_lines:]


def _truncate_tool_value(value: object, limit: int = 700) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _summarize_tool_input(name: str, inp: object) -> str:
    tool_name = str(name or "tool")
    if not isinstance(inp, dict):
        return tool_name

    lower = tool_name.lower()
    # Codex rollout function_call arguments use "cmd" (Claude tool_use input uses
    # "command"); accept both so this summarizer is reusable for Codex too
    # (Phase 3b).
    command = inp.get("command") or inp.get("cmd")
    file_path = inp.get("file_path") or inp.get("path")
    pattern = inp.get("pattern")
    query = inp.get("query")
    url = inp.get("url")
    description = inp.get("description")

    if command:
        return f"Shell command\n{_truncate_tool_value(command)}"
    if file_path and lower in {"read", "open", "edit", "multiedit", "write", "notebookread", "notebookedit"}:
        label = {
            "read": "Read file",
            "open": "Open file",
            "edit": "Edit file",
            "multiedit": "Edit file",
            "write": "Write file",
            "notebookread": "Read notebook",
            "notebookedit": "Edit notebook",
        }.get(lower, tool_name)
        return f"{label}\n{_truncate_tool_value(file_path, 300)}"
    if lower == "grep" and pattern:
        target = f"\n{_truncate_tool_value(file_path, 300)}" if file_path else ""
        return f"Search pattern\n{_truncate_tool_value(pattern, 300)}{target}"
    if lower == "glob" and pattern:
        target = f"\n{_truncate_tool_value(file_path, 300)}" if file_path else ""
        return f"Find files\n{_truncate_tool_value(pattern, 300)}{target}"
    if lower in {"ls", "list"} and file_path:
        return f"List directory\n{_truncate_tool_value(file_path, 300)}"
    if lower == "webfetch" and url:
        return f"Fetch URL\n{_truncate_tool_value(url, 500)}"
    if lower == "websearch" and query:
        return f"Web search\n{_truncate_tool_value(query, 500)}"
    if lower == "workflow" and description:
        return f"Workflow\n{_truncate_tool_value(description, 700)}"

    for key in ("file_path", "path", "pattern", "query", "url", "description"):
        val = inp.get(key)
        if val:
            return f"{tool_name}\n{_truncate_tool_value(val)}"
    if inp:
        return f"{tool_name}\n..."
    return tool_name


def _is_system_injected_user(txt: str, is_meta: bool = False) -> bool:
    # 系统注入到 user turn 的内容(子智能体完成通知 / 系统提醒 / 命令输出包裹 / harness 自动续接提示
    # 如 "[Your previous response had no visible output...]")不是用户真实输入,别当 User prompt 显示。
    # Claude 自己的 transcript 已经用 "isMeta": true 标出
    # 这类条目——优先信这个权威信号,而不是逐一枚举每一种可能出现的系统提示文本(永远列不全)。
    if is_meta:
        return True
    s = txt.lstrip()
    return s.startswith((
        "<task-notification>", "<system-reminder>", "<task-",
        "<command-name>", "<command-message>", "<command-args>", "<local-command-stdout>",
    ))


# Claude 的子智能体调用。Agent 是现名，Task 是旧版本的同一个工具。
SUBAGENT_TOOL_NAMES = {"Agent", "Task"}
# 后台子智能体启动时 Claude 回给父会话的内部回执（原文还要求不要转述给用户）。
# 它不含任何进展信息，状态改由子智能体条目自己显示。
ASYNC_AGENT_RECEIPT_PREFIX = "Async agent launched successfully"
# Workflow 在后台启动时的回执；里面的 Task ID 和运行目录用来把后续进度接回这条调用。
WORKFLOW_RECEIPT_PREFIX = "Workflow launched in background"
TASK_STATUS_TEXT = {
    "running": "运行中",
    "completed": "已完成",
    "failed": "失败",
    "killed": "已终止",
    "stopped": "已停止",
    "stale": "长时间无动静",
}


def _transcript_tool_use_block(blk: dict) -> dict:
    """Transcript tool_use -> timeline block; a subagent call keeps its tool_use id."""
    name = blk.get("name", "tool")
    inp = blk.get("input")
    if name in SUBAGENT_TOOL_NAMES and isinstance(inp, dict):
        kind = str(inp.get("subagent_type") or "agent")
        desc = str(inp.get("description") or "").strip()
        return {
            "role": "tool",
            "label": "子智能体",
            "text": f"{kind} · {desc}" if desc else kind,
            "agent_ref": str(blk.get("id") or ""),
        }
    if name == "Workflow" and isinstance(inp, dict):
        script = str(inp.get("script") or "")
        name_m = re.search(r"\bname\s*:\s*(['\"`])(.*?)\1", script, re.S)
        desc_m = re.search(r"\bdescription\s*:\s*(['\"`])(.*?)\1", script, re.S)
        title = " · ".join(x for x in (name_m.group(2) if name_m else "", desc_m.group(2) if desc_m else "") if x)
        return {
            "role": "tool",
            "label": "工作流",
            "text": title or str(inp.get("description") or inp.get("name") or "Workflow"),
            "workflow_ref": str(blk.get("id") or ""),
        }
    return {"role": "tool", "label": "Tool", "text": _summarize_tool_input(name, inp)}


def _transcript_tool_result_block(
    blk: dict, txt: str, agent_tool_ids: set[str], blocks: list[dict]
) -> dict | None:
    """Transcript tool_result -> timeline block, or None for internal launch receipts.

    A Workflow receipt carries the task id and run directory; both are copied
    onto the matching Workflow block so its progress can be looked up later.
    """
    if txt.startswith(ASYNC_AGENT_RECEIPT_PREFIX):
        return None
    tool_use_id = blk.get("tool_use_id")
    if txt.startswith(WORKFLOW_RECEIPT_PREFIX):
        task = re.search(r"Task ID:\s*(\S+)", txt)
        run = re.search(r"/(wf_[0-9a-f-]+)(?:\s|$|\|)", txt)
        for block in reversed(blocks[-60:]):
            if block.get("workflow_ref") == tool_use_id:
                if task:
                    block["workflow_task"] = task.group(1)
                if run:
                    block["workflow_run"] = run.group(1)
                break
        return None
    if tool_use_id in agent_tool_ids:
        return {"role": "tool", "label": "子智能体结果", "text": txt[:4000], "agent_result_for": str(tool_use_id)}
    return {"role": "tool", "label": "Tool result", "text": txt[:4000]}


def _task_notification_block(txt: str, timestamp: object) -> dict | None:
    """Render a background task/subagent completion notice as readable text.

    The raw ``<task-notification>`` XML used to be shown verbatim.  Its fields
    are also the only record of terminal states that never write ``end_turn``
    (failed / killed / stopped), so they are kept on the block for
    ``attach_subagent_status``.
    """
    if not txt.lstrip().startswith("<task-notification>"):
        return None

    def field(tag: str) -> str:
        match = re.search(rf"<{tag}>(.*?)</{tag}>", txt, re.S)
        return match.group(1).strip() if match else ""

    task_id, status, summary = field("task-id"), field("status"), field("summary")
    if not task_id:
        return None
    body = field("result") or field("event")
    head = f"**{summary or task_id}**" + (f"（{TASK_STATUS_TEXT.get(status, status)}）" if status else "")
    if summary.startswith('Agent "'):
        label = "子智能体结果"
    elif summary.startswith("Monitor"):
        label = "监控事件"
    else:
        label = "后台任务结果"
    block = {"role": "system", "label": label, "text": f"{head}\n\n{body}" if body else head, "task_id": task_id}
    if status:
        block["task_status"] = status
        block["task_ts"] = claude_subagents._parse_ts(timestamp)
    return block


# 本地斜杠命令（/model 等）之后 Claude 会补一条合成的 assistant 记录，不是模型的回复。
SYNTHETIC_NO_RESPONSE = "no response requested."
# 识别出来但不该出现在时间线里的注入内容（例如本地命令的免责说明）。
SKIP_BLOCK: dict = {"skip": True}
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _injected_user_block(txt: str, timestamp: object) -> dict | None:
    """Render text Claude Code injects into user turns, by kind.

    Returns None for text this function does not recognise (the caller then
    falls back to the generic 系统通知 block), SKIP_BLOCK for pure noise.
    """
    s = txt.lstrip()

    def field(tag: str) -> str:
        match = re.search(rf"<{tag}>(.*?)</{tag}>", s, re.S)
        return match.group(1).strip() if match else ""

    if s.startswith("<task-notification>"):
        return _task_notification_block(s, timestamp)
    if s.startswith(("<command-name>", "<command-message>")):
        # 用户敲的斜杠命令（/model、/compact、技能命令……）就是用户输入本身。
        name = field("command-name")
        if name:
            return {"role": "user", "label": "斜杠命令", "text": f"{name} {field('command-args')}".strip()}
        return None
    if s.startswith("<local-command-stdout>") or s.startswith("<local-command-stderr>"):
        out = ANSI_ESCAPE_RE.sub("", field("local-command-stdout") or field("local-command-stderr")).strip()
        return {"role": "system", "label": "命令输出", "text": out} if out else SKIP_BLOCK
    if s.startswith("<local-command-caveat>"):
        return SKIP_BLOCK
    if s.startswith("Base directory for this skill:"):
        # 技能正文只是塞给模型的上下文；时间线里只说明加载了哪个技能。
        skill_dir = s.splitlines()[0].split(":", 1)[1].strip().rstrip("/")
        return {"role": "system", "label": "加载技能", "text": f"已加载技能 {os.path.basename(skill_dir) or skill_dir}"}
    return None


def attach_subagent_status(
    blocks: list[dict],
    transcript_path: str,
    now: float | None = None,
    *,
    live_panel: bool = True,
    later_blocks: list[dict] | None = None,
) -> list[dict]:
    """Attach live subagent state to their timeline entries, plus a live panel.

    State comes from Claude's own subagent logs (see ``claude_subagents``),
    never from the terminal's agents panel.  The panel block is appended only
    while at least one subagent of the current batch is still running, and
    never to older history pages (``live_panel=False``).
    """
    refs = {str(b["agent_ref"]) for b in blocks if b.get("agent_ref")}
    notes: dict[str, tuple[str, float | None]] = {}
    finished: set[str] = set()
    # 一次调用的结束记录（通知、前台结果）写在它后面；翻历史时要连同更新的块一起看。
    for b in later_blocks if later_blocks is not None else blocks:
        if b.get("task_id") and b.get("task_status"):
            notes[str(b["task_id"])] = (str(b["task_status"]), b.get("task_ts"))
        if b.get("agent_result_for"):
            finished.add(str(b["agent_result_for"]))
    states = claude_subagents.load_subagents(transcript_path, refs, notes, now, finished)
    runs: dict[str, claude_subagents.SubagentState] = {}
    for b in blocks:
        run_id = str(b.get("workflow_run") or "")
        if run_id and run_id not in runs:
            run = claude_subagents.load_workflow_run(
                transcript_path, run_id, str(b.get("text") or ""), notes.get(str(b.get("workflow_task") or "")), now
            )
            if run:
                runs[run_id] = run
    by_ref: dict[str, claude_subagents.SubagentState] = {s.tool_use_id: s for s in states if s.tool_use_id}
    by_ref.update({f"workflow:{run_id}": run for run_id, run in runs.items()})
    return _attach_agent_states(blocks, by_ref, states + list(runs.values()), live_panel=live_panel)


def _attach_agent_states(
    blocks: list[dict],
    by_ref: dict[str, "claude_subagents.SubagentState"],
    states: list["claude_subagents.SubagentState"],
    *,
    live_panel: bool,
) -> list[dict]:
    """Shared by Claude and Codex: status chip on each subagent entry + live panel.

    An entry is matched by ``agent_ref`` (Claude tool_use id / Codex agent path)
    or ``workflow_run``.
    """
    if not states:
        return blocks
    out = []
    for b in blocks:
        ref = str(b.get("agent_ref") or "") or (f"workflow:{b['workflow_run']}" if b.get("workflow_run") else "")
        state = by_ref.get(ref) if ref else None
        out.append({**b, "agent": state.to_dict()} if state else b)
    batch = claude_subagents.live_batch(states) if live_panel else []
    if batch:
        running = sum(1 for s in batch if s.status == "running")
        lines = [
            f"- {TASK_STATUS_TEXT.get(s.status, s.status)} · {s.agent_type} · {s.description}"
            f" — {s.activity or '启动中'}（{s.tool_calls} 次工具调用）"
            for s in batch
        ]
        out.append({
            "role": "agents",
            "label": "子智能体进度",
            "pending": True,
            "text": f"子智能体：{running} 个运行中，共 {len(batch)} 个\n" + "\n".join(lines),
            "agents": [s.to_dict() for s in batch],
        })
    return out


def parse_transcript_tail(path: str, max_blocks: int = 120) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    agent_tool_ids: set[str] = set()
    for line in _read_tail_lines(path):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if entry.get("type") not in ("user", "assistant"):
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        # 轮次终结的结构化信号: Claude 只在一轮真正说完时写 stop_reason == "end_turn";
        # 中途还要继续调工具的 assistant 消息一律是 "tool_use"。把它标到可见的 assistant
        # 文本块上, 卡片就能用 transcript 里的事实判断"这轮答完了", 不必再依赖回复里
        # 是否手写了「Summary」标题这种书写约定 —— 短回复、debug 轮次和 Codex 窗口本来
        # 就不写, 结果卡片一直转圈、不置顶。
        turn_final = role == "assistant" and msg.get("stop_reason") == "end_turn"
        content = msg.get("content")
        items = [content] if isinstance(content, str) else (content if isinstance(content, list) else [])
        for blk in items:
            if isinstance(blk, str):
                txt = blk.strip()
                if txt:
                    if role == "assistant":
                        if txt.lower() != SYNTHETIC_NO_RESPONSE:
                            blocks.append({"role": "assistant", "label": "AI output", "text": txt, "final": turn_final})
                    elif notice := _injected_user_block(txt, entry.get("timestamp")):
                        if notice is not SKIP_BLOCK:
                            blocks.append(notice)
                    elif _is_system_injected_user(txt, entry.get("isMeta")):
                        blocks.append({"role": "system", "label": "系统通知", "text": txt})
                    else:
                        blocks.append({"role": "user", "label": "User prompt", "text": txt})
                continue
            if not isinstance(blk, dict):
                continue
            btype = blk.get("type")
            if btype == "text":
                txt = (blk.get("text") or "").strip()
                if not txt:
                    continue
                if role == "assistant":
                    if txt.lower() != SYNTHETIC_NO_RESPONSE:
                        blocks.append({"role": "assistant", "label": "AI output", "text": txt, "final": turn_final})
                elif notice := _injected_user_block(txt, entry.get("timestamp")):
                    if notice is not SKIP_BLOCK:
                        blocks.append(notice)
                elif _is_system_injected_user(txt, entry.get("isMeta")):
                    blocks.append({"role": "system", "label": "系统通知", "text": txt})
                else:
                    blocks.append({"role": "user", "label": "User prompt", "text": txt})
            elif btype == "tool_use":
                tool_block = _transcript_tool_use_block(blk)
                if tool_block.get("agent_ref"):
                    agent_tool_ids.add(tool_block["agent_ref"])
                blocks.append(tool_block)
            elif btype == "tool_result":
                c = blk.get("content")
                if isinstance(c, list):
                    c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                txt = (c or "").strip() if isinstance(c, str) else ""
                if txt and (result_block := _transcript_tool_result_block(blk, txt, agent_tool_ids, blocks)):
                    blocks.append(result_block)
            elif btype == "thinking":
                txt = (blk.get("thinking") or "").strip()
                if txt:
                    blocks.append({"role": "assistant", "label": "AI step", "text": txt})
    return blocks[-max_blocks:]


def _live_block_match_strength(
    transcript_block: dict[str, str],
    live_block: dict[str, str],
    *,
    allow_short_exact: bool = False,
) -> int:
    """Score a role-aware transcript/live-screen block match.

    Long containment is strong enough to start an alignment.  A short exact
    match is only useful after that alignment has started; otherwise generic
    replies such as ``OK`` could bind a pane to an unrelated transcript turn.
    """
    if transcript_block.get("role") != live_block.get("role"):
        return 0
    transcript_text = _match_norm(str(transcript_block.get("text") or ""))
    live_text = _match_norm(str(live_block.get("text") or ""))
    if not transcript_text or not live_text:
        return 0
    if min(len(transcript_text), len(live_text)) >= 12 and (
        transcript_text in live_text or live_text in transcript_text
        or _mostly_same_text(transcript_text, live_text)
    ):
        return 2
    if allow_short_exact and len(transcript_text) >= 2 and transcript_text == live_text:
        return 1
    return 0


def _extend_transcript_anchor_from_live_blocks(
    blocks: list[dict[str, str]],
    capture_text: str,
    anchor_index: int,
) -> int:
    """Extend a trusted old anchor through a newer visible turn in order.

    Claude often wraps a newly sent prompt into one physical screen fragment,
    followed by a very short reply.  Neither satisfies the two-snippet rule
    used to choose the original anti-cross-pane anchor.  Once a long,
    role-aware prompt match starts a transcript/screen alignment, consecutive
    exact short blocks are safe to retain as part of that same visible turn.
    """
    live_blocks = parse_blocks(capture_text, pane_kind="Claude")
    if not live_blocks or anchor_index >= len(blocks) - 1:
        return anchor_index

    start: tuple[int, int] | None = None
    for transcript_index in range(anchor_index + 1, len(blocks)):
        for live_index, live_block in enumerate(live_blocks):
            if _live_block_match_strength(blocks[transcript_index], live_block) >= 2:
                start = (transcript_index, live_index)
                break
        if start:
            break
    if not start:
        return anchor_index

    latest_index, live_cursor = start
    for transcript_index in range(latest_index + 1, len(blocks)):
        matched_live_index = -1
        for live_index in range(live_cursor + 1, len(live_blocks)):
            if _live_block_match_strength(
                blocks[transcript_index],
                live_blocks[live_index],
                allow_short_exact=True,
            ):
                matched_live_index = live_index
                break
        if matched_live_index < 0:
            break
        latest_index = transcript_index
        live_cursor = matched_live_index
    return latest_index


def trim_transcript_blocks_to_screen(
    blocks: list[dict[str, str]],
    capture_text: str,
    max_blocks: int = 120,
) -> list[dict[str, str]]:
    """When two Claude panes share one transcript file, the transcript tail can
    contain another pane's later conversation. Anchor the rendered history to the
    current screen so a pane never shows blocks after what is visibly open."""
    if not blocks:
        return blocks
    snips = list(dict.fromkeys(_screen_match_snippets(capture_text)))
    if len(snips) < 2:
        return blocks[-max_blocks:]

    # The screen can contain both an older long answer and a newer short
    # answer. Picking the block with the most snippet hits anchors to the long
    # answer and drops the newer visible tail. Prefer the latest transcript
    # block that is sufficiently visible on screen; only fall back to max hits
    # when no block reaches the normal confidence threshold.
    scored: list[tuple[int, int]] = []
    for index, block in enumerate(blocks):
        blob = _match_norm(str(block.get("text") or ""))
        hits = sum(1 for snip in snips if snip in blob)
        if hits:
            scored.append((index, hits))

    confident = [item for item in scored if item[1] >= 2]
    if confident:
        best_index = confident[-1][0]
    elif scored:
        best_index, best_hits = max(scored, key=lambda item: item[1])
        if best_hits < 2:
            return blocks[-max_blocks:]
    else:
        return blocks[-max_blocks:]
    best_index = _extend_transcript_anchor_from_live_blocks(blocks, capture_text, best_index)
    start = max(0, best_index + 1 - max_blocks)
    return blocks[start:best_index + 1]


def _block_text_norm(block: dict[str, str]) -> str:
    return _match_norm(str(block.get("text") or ""))


# 屏幕上的一段和记录里的一段"基本是同一段话"：把较短的一边切成小段，大部分都能在
# 较长的一边找到。整段包含太苛刻 —— 终端会把右侧面板的 ✕、截断省略号、表格边框
# 夹进正文，一个字符不同整段就对不上，同一条回复会被当成新内容再显示一遍。
_FUZZY_CHUNK = 16
_FUZZY_MIN_CHARS = 48
_FUZZY_SHARE = 0.7


def _mostly_same_text(left_norm: str, right_norm: str) -> bool:
    short, long_ = sorted((left_norm, right_norm), key=len)
    if len(short) < _FUZZY_MIN_CHARS:
        return False
    chunks = [short[i:i + _FUZZY_CHUNK] for i in range(0, len(short) - _FUZZY_CHUNK + 1, _FUZZY_CHUNK)]
    return sum(1 for chunk in chunks if chunk in long_) >= _FUZZY_SHARE * len(chunks)


def _blocks_equivalent(left: dict[str, str], right: dict[str, str]) -> bool:
    left_norm = _block_text_norm(left)
    right_norm = _block_text_norm(right)
    if len(left_norm) < 12 or len(right_norm) < 12:
        return False
    if left.get("role") != right.get("role"):
        return False
    return left_norm in right_norm or right_norm in left_norm or _mostly_same_text(left_norm, right_norm)


# Sticky anchor cache: pane process identity -> {matched_norm, transcript_sig, ts}
# Stops the live-tail anchor from flapping frame to frame when a similar/
# duplicate short line appears twice on screen (the match loop below has no
# `break`, so it always lands on the *last* match found in a single pass, which
# can jump forward past a duplicate that is actually new, unrecorded content).
# The transcript itself is the ground truth for "did anything real advance":
# if `transcript_blocks` has not grown since the previous poll but the anchor
# scan nonetheless lands later than before, that is the false-positive-
# duplicate case, not genuine progress, so the cached anchor is kept. Keyed by
# process identity (not raw pane_id) so a reused tmux pane never inherits a
# stale anchor.
_live_tail_anchor_cache: dict[str, dict] = {}
_LIVE_TAIL_ANCHOR_TTL = 8.0  # seconds


_FOCUS_VERB = r"(?:ran|edited|read|called|searched|wrote|created|fetched|listed|updated|deleted|explored)"
FOCUS_SUMMARY_RE = re.compile(
    rf"^\s*{_FOCUS_VERB}\s+\d+\b[^,\n]*(?:,\s*{_FOCUS_VERB}\s+\d+\b[^,\n]*)*\s*$", re.I
)


# Claude's fullscreen view draws this pill while the conversation is scrolled up
# ("Jump to bottom (ctrl+end)", or "3 new messages (…)" when more arrived since).
CLAUDE_SCROLLED_BACK_RE = re.compile(r"(?:Jump to bottom|\b\d+ new messages?)\s*\(\s*(?:ctr|fn|cmd|⌘|click)", re.I)


def claude_view_scrolled_back(capture_text: str) -> bool:
    """The Claude screen shows older history, not the live end of the conversation."""
    return bool(CLAUDE_SCROLLED_BACK_RE.search("\n".join(status_tail_text(capture_text).splitlines()[-12:])))


def merge_live_screen_tail(
    transcript_blocks: list[dict[str, str]],
    capture_text: str,
    pane_kind: str = "Claude",
    max_blocks: int = 120,
    pane_identity: str = "",
) -> list[dict[str, str]]:
    """Append visible Claude alt-screen blocks that have not reached JSONL yet.

    Claude focus/hidden-message mode can show assistant/tool progress on screen
    before the JSONL transcript receives those blocks. Use the visible screen as
    a pane-scoped live tail, anchored after the latest visible block already
    present in the transcript, so we do not duplicate older visible history.
    Appended blocks are marked `pending: True` since they are not yet durably
    recorded in the transcript.
    """
    if not transcript_blocks:
        return transcript_blocks
    # 屏幕往上翻着时显示的是旧历史，不是还没落盘的新内容；这时屏幕上没有任何东西能接在
    # 记录后面（改前会把翻到的旧段落、甚至首条消息里粘贴的整段对话当新内容追加）。
    if pane_kind == "Claude" and claude_view_scrolled_back(capture_text):
        return transcript_blocks
    # 聚焦模式把一串工具调用折成一行 "Ran 5 agents, ran 11 shell commands"。那些调用
    # 在记录里逐条都有，这行摘要只会把同一批动作再显示一遍，所以实时尾部不要它。
    live_blocks = [
        block for block in parse_blocks(capture_text, pane_kind=pane_kind)
        if not (block.get("role") == "tool" and FOCUS_SUMMARY_RE.match(str(block.get("text") or "")))
    ]
    if not live_blocks:
        return transcript_blocks

    anchor_index = -1
    anchor_transcript_index = -1
    transcript_tail = transcript_blocks[-40:]
    for index, live_block in enumerate(live_blocks):
        for transcript_index, block in enumerate(transcript_tail):
            if _live_block_match_strength(block, live_block) >= 2:
                anchor_index = index
                anchor_transcript_index = transcript_index

    # A strong prompt match can be followed by a deliberately tiny reply
    # ("OK", "是", etc.).  Advance the anchor through consecutive exact
    # transcript/live blocks so that already-flushed short replies are not
    # appended again as pending live output.
    while (
        anchor_index >= 0
        and anchor_transcript_index >= 0
        and anchor_index + 1 < len(live_blocks)
        and anchor_transcript_index + 1 < len(transcript_tail)
        and _live_block_match_strength(
            transcript_tail[anchor_transcript_index + 1],
            live_blocks[anchor_index + 1],
            allow_short_exact=True,
        )
    ):
        anchor_index += 1
        anchor_transcript_index += 1

    if pane_identity and anchor_index >= 0:
        now = time.time()
        transcript_sig = _block_text_norm(transcript_blocks[-1])
        cached = _live_tail_anchor_cache.get(pane_identity)
        if (
            cached
            and (now - cached.get("ts", 0)) < _LIVE_TAIL_ANCHOR_TTL
            and cached.get("transcript_sig") == transcript_sig
        ):
            # Find the EARLIEST occurrence matching the previously-anchored
            # text, not the latest — a later poll's screen can contain a new
            # duplicate of that same text further down, and re-anchoring to
            # the latest occurrence is exactly the false-positive this cache
            # exists to prevent.
            cached_norm = cached.get("matched_norm", "")
            cached_pos = -1
            for i, live_block in enumerate(live_blocks):
                if _block_text_norm(live_block) == cached_norm:
                    cached_pos = i
                    break
            if cached_pos >= 0 and anchor_index > cached_pos:
                anchor_index = cached_pos
        _live_tail_anchor_cache[pane_identity] = {
            "matched_norm": _block_text_norm(live_blocks[anchor_index]),
            "transcript_sig": transcript_sig,
            "ts": now,
        }

    if anchor_index < 0 or anchor_index >= len(live_blocks) - 1:
        return transcript_blocks

    merged = list(transcript_blocks)
    recorded_norms = [_block_text_norm(block) for block in transcript_blocks]
    for live_block in live_blocks[anchor_index + 1:]:
        if any(_blocks_equivalent(live_block, block) for block in merged[-60:]):
            continue
        # Text already recorded anywhere (an older turn, or a paste inside a
        # prompt) is history on screen, not new output.
        live_norm = _block_text_norm(live_block)
        if len(live_norm) >= 24 and any(live_norm in norm for norm in recorded_norms):
            continue
        pending_block = dict(live_block)
        pending_block["pending"] = True
        merged.append(pending_block)
    return merged[-max_blocks:]


# Per-pane mapping cache: pane_id -> {"sig": str, "path": str|None, "ts": float}
_claude_map_cache: dict[str, dict] = {}
_CLAUDE_MAP_TTL = 4.0  # seconds; re-resolve when the on-screen content changes
# Bounded max-staleness for the sticky "last_good still matches, keep it without
# a full rescan" fast path. Even when last_good keeps matching the screen, force
# a full candidate rescan if the last full scan is older than this. This turns
# _claude_map_cache from a correctness-load-bearing cache (whose only reliable
# hard reset was the process-identity check) into a latency optimization with a bounded drift ceiling, matching
# the robust design the Codex rollout / live-tail-anchor / job caches already
# use (short TTL + authoritative recompute). If BOTH the process-identity reset
# and content-match stickiness ever fail simultaneously, a stale transcript now
# self-heals within this window instead of persisting indefinitely.
_CLAUDE_MAP_FULL_RESOLVE_MAX_AGE = 20.0  # seconds
_CLAUDE_MAP_FILE = STATE_DIR / "claude_map.json"  # 持久化每个 pane 上次匹配到的 transcript(跨重启)
_LIVE_CLAUDE_CONTEXT_CACHE: dict[str, object] = {"ts": 0.0}


def _load_claude_map() -> None:
    """启动时把上次每个 pane 匹配到的 transcript(last_good)从磁盘读回内存缓存:
    服务重启后即使当前屏幕内容稀疏(提示/spinner/home), 也能立刻显示该 pane 历史,
    不必等用户在对话里产生新内容才重新匹配。"""
    try:
        data = json.loads(_CLAUDE_MAP_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    if isinstance(data, dict):
        for pane_id, item in data.items():
            if isinstance(item, str):
                path = item
                cached = {"last_good": path}
            elif isinstance(item, dict):
                path = str(item.get("last_good") or item.get("path") or "")
                cached = {
                    "last_good": path,
                    "proc_identity": item.get("proc_identity") or "",
                    "cwd": item.get("cwd") or "",
                    "saved_at": item.get("saved_at") or "",
                }
            else:
                continue
            if path and os.path.isfile(path):
                _claude_map_cache[str(pane_id)] = cached


def _save_claude_map() -> None:
    snapshot = {
        pid: {
            "last_good": c["last_good"],
            "proc_identity": c.get("proc_identity") or "",
            "cwd": c.get("cwd") or "",
            "saved_at": c.get("saved_at") or now_iso(),
        }
        for pid, c in _claude_map_cache.items()
        if c.get("last_good")
    }
    try:
        tmp = _CLAUDE_MAP_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _CLAUDE_MAP_FILE)
    except Exception:
        pass


_load_claude_map()


def _live_claude_context(ttl: float = 2.0) -> dict[str, object]:
    """Lightweight live Claude pane inventory.

    Claude JSONL transcript files are conversation-scoped, not pane-scoped. The
    dashboard needs a cheap current view of live Claude panes so it can fail
    closed only when two live panes resolve to the same transcript file.
    """
    now = time.time()
    cached_ts = float(_LIVE_CLAUDE_CONTEXT_CACHE.get("ts") or 0.0)
    if now - cached_ts < ttl:
        return _LIVE_CLAUDE_CONTEXT_CACHE

    fmt = "\t".join([
        "#{pane_id}",
        "#{session_name}",
        "#{window_name}",
        "#{pane_current_command}",
        "#{pane_current_path}",
        "#{pane_title}",
        "#{pane_pid}",
    ])
    cp = run_tmux(["list-panes", "-a", "-F", fmt])
    rows: list[list[str]] = []
    if cp.returncode == 0:
        for line in cp.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 7:
                continue
            session = parts[1]
            if session != DEFAULT_SESSION:
                continue
            rows.append(parts)

    child_commands = _foreground_child_pid_commands({
        parts[6] for parts in rows if parts[3] in {"bash", "zsh", "fish", "sh"}
    })
    pane_cwd: dict[str, str] = {}
    claude_ids: set[str] = set()
    cwd_counts: dict[str, int] = {}
    live_ids: set[str] = set()
    for pane_id, _session, window_name, command, cwd, title, pane_pid in rows:
        live_ids.add(pane_id)
        if command in {"bash", "zsh", "fish", "sh"}:
            _child_pid, child = child_commands.get(pane_pid, ("", ""))
            if child:
                command = child
        if classify(command, title, window_name) != "Claude":
            continue
        claude_ids.add(pane_id)
        pane_cwd[pane_id] = cwd
        cwd_counts[cwd] = cwd_counts.get(cwd, 0) + 1

    _LIVE_CLAUDE_CONTEXT_CACHE.clear()
    _LIVE_CLAUDE_CONTEXT_CACHE.update({
        "ts": now,
        "live_ids": live_ids,
        "claude_ids": claude_ids,
        "pane_cwd": pane_cwd,
        "cwd_counts": cwd_counts,
    })
    return _LIVE_CLAUDE_CONTEXT_CACHE


def _prune_claude_map_cache(live_ids: set[str]) -> None:
    removed = False
    for pane_id in list(_claude_map_cache):
        if pane_id not in live_ids:
            _claude_map_cache.pop(pane_id, None)
            removed = True
    if removed:
        _save_claude_map()


def _shared_live_transcript_panes(path: str, current_pane_id: str, live_ids: set[str] | None = None) -> list[str]:
    if not path:
        return []
    pane_cwd: dict[str, str] = {}
    if live_ids is None:
        live_context = _live_claude_context()
        # Pane ids are reusable.  ``live_ids`` includes every tmux pane, so a
        # persisted Claude map entry for a pane that now runs Codex used to
        # survive pruning and fabricate a shared-transcript collision.  Only
        # panes classified as Claude may participate in this Claude-specific
        # identity check.  ``pane_cwd`` is the compatibility fallback for
        # tests/cached contexts created before ``claude_ids`` was explicit.
        live_ids = set(
            live_context.get("claude_ids")
            or (live_context.get("pane_cwd") or {}).keys()
        )
        pane_cwd = dict(live_context.get("pane_cwd") or {})
        _prune_claude_map_cache(live_ids)
    shared: list[str] = []
    for pane_id, cached in _claude_map_cache.items():
        if pane_id == current_pane_id or pane_id not in live_ids:
            continue
        # Being live under this pane_id only proves *some* Claude process
        # currently owns it, not that it's the same conversation the cache
        # entry was written for: pane_ids are reused across unrelated tmux
        # windows/projects, and _prune_claude_map_cache only drops entries for
        # pane_ids that are no longer live at all, not ones that were recycled
        # into a different project. A stale entry left behind by the PREVIOUS
        # occupant of this pane_id must not be allowed to fabricate a
        # collision against the pane's CURRENT, unrelated conversation
        # (otherwise panes that had long since been recycled into entirely
        # different projects show "degraded/shared-transcript"). Cross-check against the
        # pane's live cwd, which _live_claude_context() already computed for
        # free.
        live_cwd = pane_cwd.get(pane_id)
        if live_cwd and cached.get("cwd") and live_cwd != cached.get("cwd"):
            continue
        if path in {cached.get("path"), cached.get("last_good")}:
            shared.append(pane_id)
    return sorted(shared)


def _claude_project_dir_for_cwd(cwd: str) -> Path | None:
    """Claude Code encodes a session's cwd into its project directory name by
    replacing every non-alphanumeric character 1:1 with "-" (no collapsing of
    consecutive dashes). Used to scope transcript matching to the ONE project
    directory a pane's cwd actually belongs to, instead of scanning every
    project on the machine."""
    if not cwd:
        return None
    encoded = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    d = Path.home() / ".claude" / "projects" / encoded
    return d if d.is_dir() else None


def _recent_transcripts(limit: int = 200, max_age_s: float = 30 * 24 * 3600, dirs: list[Path] | None = None) -> list[Path]:
    if dirs is None:
        base = Path.home() / ".claude" / "projects"
        if not base.is_dir():
            return []
        dirs = [d for d in base.iterdir() if d.is_dir()]
    now = time.time()
    files: list[tuple[float, Path]] = []
    for d in dirs:
        if not d.is_dir():
            continue
        for f in d.glob("*.jsonl"):
            try:
                mt = f.stat().st_mtime
            except OSError:
                continue
            if now - mt <= max_age_s:
                files.append((mt, f))
    files.sort(key=lambda x: x[0], reverse=True)
    return [f for _mt, f in files[:limit]]


def _match_norm(text: str) -> str:
    text = text.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"').replace("\\/", "/")
    return re.sub(r"[\s\\\"'“”‘’`*]+", "", text)


_SNIPPET_WINDOW = 60
_SNIPPET_STRIDE = 48
_SNIPPET_MAX_WINDOWS_PER_LINE = 6


def _screen_match_snippets(capture_text: str) -> list[str]:
    """Whitespace-stripped distinctive fragments from the visible screen. Strip
    handles the 37-col wrapping so a fragment is still a substring of the
    transcript's unwrapped text.

    Windows each line into multiple overlapping ~60-char fragments (not just
    the first 60) instead of one fragment per physical line: once `capture()`
    joins tmux soft-wraps (-J), what used to be several separate wrapped
    physical lines (each contributing its own fragment) can arrive as one long
    logical line, and taking only its first 60 chars would silently lose most
    of the match signal those wrapped lines used to provide."""
    snips: list[str] = []
    for line in filter_display_lines(capture_text).splitlines():
        norm = _match_norm(line)
        norm = re.sub(r"^[•●❯>│|\-*#]+", "", norm)
        if len(norm) >= 12:
            windows = 0
            start = 0
            while windows < _SNIPPET_MAX_WINDOWS_PER_LINE:
                chunk = norm[start:start + _SNIPPET_WINDOW]
                if len(chunk) < 12:
                    break
                snips.append(chunk)
                windows += 1
                if start + _SNIPPET_WINDOW >= len(norm):
                    break
                start += _SNIPPET_STRIDE
        for sep in (":", "："):
            if sep in norm:
                tail = norm.split(sep, 1)[1]
                if len(tail) >= 12:
                    snips.append(tail[:60])
                break
    return snips[-24:]


class _BoundedLRUCache:
    """Small thread-safe LRU with both entry-count and approximate-byte caps.

    Values stay opaque to the cache; callers provide a byte weight for each
    item.  A value larger than ``max_item_bytes`` is returned to the current
    request but deliberately not retained, preventing one exceptional history
    from consuming the resident cache budget.  Setting any limit to zero
    disables retention for that cache.
    """

    def __init__(self, *, max_entries: int, max_bytes: int, max_item_bytes: int) -> None:
        self.max_entries = max(0, max_entries)
        self.max_bytes = max(0, max_bytes)
        self.max_item_bytes = max(0, max_item_bytes)
        self._items: OrderedDict[str, tuple[object, int]] = OrderedDict()
        self._total_bytes = 0
        self._lock = threading.RLock()

    def get(self, key: str):
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            self._items.move_to_end(key)
            return item[0]

    def put(self, key: str, value: object, weight: int) -> bool:
        weight = max(0, int(weight))
        with self._lock:
            previous = self._items.pop(key, None)
            if previous is not None:
                self._total_bytes -= previous[1]
            if (
                not self.max_entries
                or not self.max_bytes
                or not self.max_item_bytes
                or weight > self.max_item_bytes
                or weight > self.max_bytes
            ):
                return False
            while self._items and (
                len(self._items) >= self.max_entries
                or self._total_bytes + weight > self.max_bytes
            ):
                _old_key, (_old_value, old_weight) = self._items.popitem(last=False)
                self._total_bytes -= old_weight
            self._items[key] = (value, weight)
            self._total_bytes += weight
            return True

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._total_bytes = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._total_bytes


def _blocks_cache_weight(blocks: list[dict[str, str]]) -> int:
    """Approximate retained bytes, including a small per-block object cost."""
    total = len(blocks) * 128
    for block in blocks:
        for key, value in block.items():
            total += len(str(key).encode("utf-8")) + len(str(value).encode("utf-8"))
    return total


# Perf: claude_transcript_for_pane's full scan (cache miss / every
# _CLAUDE_MAP_FULL_RESOLVE_MAX_AGE) walks up to 200 candidate transcripts per
# _recent_transcripts()'s default limit. A small budget (e.g. 32 entries /
# 16MB) cannot hold even one heavily-used project's candidate set (200 files
# can normalize to ~60MB), so every full scan evicted earlier candidates before
# the loop finished and each resolve paid the full cold-read cost (seconds).
# Sized to comfortably hold a full 200-candidate scan for several
# concurrently-active heavy projects at once.
_BLOB_CACHE_MAX_ENTRIES = int(os.environ.get("TMUX_CARD_BLOB_CACHE_MAX_ENTRIES", "512"))
_BLOB_CACHE_MAX_BYTES = int(os.environ.get("TMUX_CARD_BLOB_CACHE_MAX_BYTES", str(200 * 1024 * 1024)))
_BLOB_CACHE_MAX_ITEM_BYTES = int(os.environ.get("TMUX_CARD_BLOB_CACHE_MAX_ITEM_BYTES", str(2 * 1024 * 1024)))
_HISTORY_CACHE_MAX_ENTRIES = int(os.environ.get("TMUX_CARD_HISTORY_CACHE_MAX_ENTRIES", "6"))
_HISTORY_CACHE_MAX_BYTES = int(os.environ.get("TMUX_CARD_HISTORY_CACHE_MAX_BYTES", str(48 * 1024 * 1024)))
_HISTORY_CACHE_MAX_ITEM_BYTES = int(os.environ.get("TMUX_CARD_HISTORY_CACHE_MAX_ITEM_BYTES", str(16 * 1024 * 1024)))

# Cache normalized blobs keyed on path; invalidated when the file changes, so an
# unchanged transcript is never re-read/re-stripped on subsequent resolves.
_blob_cache = _BoundedLRUCache(
    max_entries=_BLOB_CACHE_MAX_ENTRIES,
    max_bytes=_BLOB_CACHE_MAX_BYTES,
    max_item_bytes=_BLOB_CACHE_MAX_ITEM_BYTES,
)


def _transcript_norm_blob(path: str) -> str:
    # No JSON parsing: conversation text (Chinese/alnum) is stored literally in
    # the raw JSONL, so a whitespace-stripped raw tail is enough for substring
    # matching and is an order of magnitude faster than parsing every line.
    try:
        st = os.stat(path)
    except OSError:
        return ""
    cached = _blob_cache.get(path)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]
    blob = _match_norm("".join(_read_tail_lines(path, max_bytes=1_000_000, max_lines=400)))
    _blob_cache.put(path, (st.st_mtime, st.st_size, blob), len(blob.encode("utf-8")))
    return blob


def claude_transcript_for_pane(pane: Pane, capture_text: str) -> str | None:
    """Identify which Claude conversation a pane is showing by matching the
    on-screen text against recent transcripts (content-match auto-follow).

    Why not simpler: Claude does not keep the transcript open (lsof fails) and
    the v2.1 home panes have cwd=~, so neither lsof nor cwd identifies the active
    conversation. Matching distinctive on-screen fragments to transcript text
    does, and auto-follows as the user switches conversations. Cached per pane
    and re-resolved only when the visible content changes. Returns None (→ caller
    falls back to the live capture) when nothing matches confidently, e.g. on the
    session-list home screen."""
    snips = _screen_match_snippets(capture_text)
    sig = str(hash("".join(snips)))
    cached = _claude_map_cache.get(pane.pane_id) or {}
    proc_identity = _pane_agent_process_identity(pane)
    cached_identity = str(cached.get("proc_identity") or "")
    legacy_unbound_cache = bool(proc_identity and cached.get("last_good") and not cached_identity)
    if proc_identity and cached_identity and cached_identity != proc_identity:
        _claude_map_cache.pop(pane.pane_id, None)
        _save_claude_map()
        cached = {}
        legacy_unbound_cache = False
    last_good = cached.get("last_good")
    # Only trust the TTL cache for a CONFIDENT match; otherwise always re-resolve
    # so a just-flushed transcript / conversation switch is followed immediately
    # (real-time priority).
    if (
        cached.get("sig") == sig
        and cached.get("confident")
        and not legacy_unbound_cache
        and (time.time() - cached.get("ts", 0)) < _CLAUDE_MAP_TTL
    ):
        return cached.get("path")
    now = time.time()
    last_full_scan_ts = float(cached.get("full_scan_ts", 0) or 0)
    did_full_scan = False
    match: str | None = None
    if len(snips) >= 2:
        # Fast path (real-time): if the conversation we last matched is still on
        # screen, keep it without rescanning all candidates. A full scan only runs
        # on a genuine switch (the previous transcript no longer matches) OR when
        # the sticky match has been ridden for longer than
        # _CLAUDE_MAP_FULL_RESOLVE_MAX_AGE without a fresh full scan - the bounded
        # max-staleness backstop that keeps this cache from being able to hold a
        # wrong-but-still-partially-matching transcript indefinitely (see that
        # constant's comment).
        if last_good and (now - last_full_scan_ts) < _CLAUDE_MAP_FULL_RESOLVE_MAX_AGE:
            try:
                if sum(1 for s in snips if s in _transcript_norm_blob(last_good)) >= 2:
                    match = last_good
            except Exception:
                pass
    if match is None and len(snips) >= 2:
        did_full_scan = True
        best_hits = 0
        second_hits = 0
        # Scope candidates to the pane's OWN project directory when known -
        # a full-machine scan lets an unrelated pane's short generic snippet
        # (e.g. a common phrase from this user's standard reply format) win
        # a "confident" match against the wrong, older transcript.
        own_dir = _claude_project_dir_for_cwd(pane.cwd)
        candidates = _recent_transcripts(dirs=[own_dir]) if own_dir else _recent_transcripts()
        if own_dir and not candidates:
            candidates = _recent_transcripts()  # fall back if the scoped dir is empty/unexpected
        for cand in candidates:
            blob = _transcript_norm_blob(str(cand))
            hits = sum(1 for s in snips if s in blob)
            if hits > best_hits:
                best_hits, second_hits, match = hits, best_hits, str(cand)
            elif hits > second_hits:
                second_hits = hits
        # Confident only with a clear unique winner (a tie must NOT pick newest).
        if not (best_hits >= 2 and best_hits > second_hits):
            match = None
    if match:
        last_good = match
        path: str | None = match
    elif len(snips) < 2:
        # No matchable signal (tips / survey / spinner / home): hold the last
        # conversation so history does not flicker away. Do not do this for old
        # unbound cache entries after restart: pane ids can be reused, and only a
        # process-bound cache is safe to trust on sparse screens.
        path = None if legacy_unbound_cache else last_good
    else:
        # Screen has conversation text but no confident match: show the live
        # current screen rather than a possibly-WRONG stale history.
        path = None
    prev_last_good = cached.get("last_good")
    _claude_map_cache[pane.pane_id] = {
        "sig": sig,
        "path": path,
        "ts": now,
        "last_good": last_good,
        "confident": bool(match),
        "proc_identity": proc_identity,
        "cwd": pane.cwd,
        # Timestamp of the last FULL candidate rescan (not just a sticky
        # fast-path re-confirm), so the bounded max-staleness gate above can
        # force a periodic rescan even while last_good keeps matching.
        "full_scan_ts": now if did_full_scan else last_full_scan_ts,
    }
    if last_good and (last_good != prev_last_good or proc_identity != cached_identity):
        _save_claude_map()  # last_good 变了才落盘(对话切换时, 不是每次轮询)
    return path


# ---------------------------------------------------------------------------
# Phase 3a: on-demand "load earlier Claude history" pagination.
#
# The 3x/second hot path (parse_transcript_tail) only ever returns the last
# max_blocks=120 parsed blocks, so older conversation scrolls out of the live
# view - but nothing is deleted, the full conversation stays durably recorded
# in the JSONL transcript on disk. This section adds a SEPARATE, user-
# triggered path (only hit when someone scrolls near the top of a pane's
# timeline) that pages backwards through the blocks already parsed off the
# tail window. It deliberately does NOT touch parse_transcript_tail,
# trim_transcript_blocks_to_screen, or merge_live_screen_tail - those are the
# already-hardened hot-path functions Phase 1/2 fixed - and it does not
# reimplement pane->transcript resolution (reuses claude_transcript_for_pane).
# ---------------------------------------------------------------------------

# (path, mtime, size)-validated cache of fully parsed transcript blocks.  It is
# intentionally bounded: pagination is user-triggered and correctness does not
# depend on retention, so an evicted/oversized item is simply parsed again.
_history_blocks_cache = _BoundedLRUCache(
    max_entries=_HISTORY_CACHE_MAX_ENTRIES,
    max_bytes=_HISTORY_CACHE_MAX_BYTES,
    max_item_bytes=_HISTORY_CACHE_MAX_ITEM_BYTES,
)


def _parse_transcript_all_blocks(path: str) -> list[dict[str, str]]:
    """Parse EVERY user/assistant JSONL entry in the transcript into blocks.

    This is the same per-entry conversion as parse_transcript_tail, but over
    the full file and without the max_blocks tail cutoff. It is kept as its
    own separate function - not refactored to share a helper with
    parse_transcript_tail - so that already-tested hot-path function is never
    touched by this additive pagination feature (see module docstring above)."""
    blocks: list[dict[str, str]] = []
    agent_tool_ids: set[str] = set()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            raw_lines = fh.read().splitlines()
    except OSError:
        return blocks
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if entry.get("type") not in ("user", "assistant"):
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        # 轮次终结的结构化信号: Claude 只在一轮真正说完时写 stop_reason == "end_turn";
        # 中途还要继续调工具的 assistant 消息一律是 "tool_use"。把它标到可见的 assistant
        # 文本块上, 卡片就能用 transcript 里的事实判断"这轮答完了", 不必再依赖回复里
        # 是否手写了「Summary」标题这种书写约定 —— 短回复、debug 轮次和 Codex 窗口本来
        # 就不写, 结果卡片一直转圈、不置顶。
        turn_final = role == "assistant" and msg.get("stop_reason") == "end_turn"
        content = msg.get("content")
        items = [content] if isinstance(content, str) else (content if isinstance(content, list) else [])
        for blk in items:
            if isinstance(blk, str):
                txt = blk.strip()
                if txt:
                    if role == "assistant":
                        if txt.lower() != SYNTHETIC_NO_RESPONSE:
                            blocks.append({"role": "assistant", "label": "AI output", "text": txt, "final": turn_final})
                    elif notice := _injected_user_block(txt, entry.get("timestamp")):
                        if notice is not SKIP_BLOCK:
                            blocks.append(notice)
                    elif _is_system_injected_user(txt, entry.get("isMeta")):
                        blocks.append({"role": "system", "label": "系统通知", "text": txt})
                    else:
                        blocks.append({"role": "user", "label": "User prompt", "text": txt})
                continue
            if not isinstance(blk, dict):
                continue
            btype = blk.get("type")
            if btype == "text":
                txt = (blk.get("text") or "").strip()
                if not txt:
                    continue
                if role == "assistant":
                    if txt.lower() != SYNTHETIC_NO_RESPONSE:
                        blocks.append({"role": "assistant", "label": "AI output", "text": txt, "final": turn_final})
                elif notice := _injected_user_block(txt, entry.get("timestamp")):
                    if notice is not SKIP_BLOCK:
                        blocks.append(notice)
                elif _is_system_injected_user(txt, entry.get("isMeta")):
                    blocks.append({"role": "system", "label": "系统通知", "text": txt})
                else:
                    blocks.append({"role": "user", "label": "User prompt", "text": txt})
            elif btype == "tool_use":
                tool_block = _transcript_tool_use_block(blk)
                if tool_block.get("agent_ref"):
                    agent_tool_ids.add(tool_block["agent_ref"])
                blocks.append(tool_block)
            elif btype == "tool_result":
                c = blk.get("content")
                if isinstance(c, list):
                    c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                txt = (c or "").strip() if isinstance(c, str) else ""
                if txt and (result_block := _transcript_tool_result_block(blk, txt, agent_tool_ids, blocks)):
                    blocks.append(result_block)
            elif btype == "thinking":
                txt = (blk.get("thinking") or "").strip()
                if txt:
                    blocks.append({"role": "assistant", "label": "AI step", "text": txt})
    return blocks


def _all_transcript_blocks_cached(path: str) -> list[dict[str, str]]:
    """(path, mtime, size)-keyed lookup of the full parsed block list, re-
    parsing only when the file has actually changed since the last call."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    cached = _history_blocks_cache.get(path)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]
    blocks = _parse_transcript_all_blocks(path)
    _history_blocks_cache.put(
        path,
        (st.st_mtime, st.st_size, blocks),
        _blocks_cache_weight(blocks),
    )
    return blocks


def transcript_path_for_pane_id(pane_id: str) -> str | None:
    """Resolve which JSONL transcript a Claude pane is currently bound to.

    Reuses claude_transcript_for_pane (does not reimplement pane->transcript
    resolution) with a fresh tmux capture of that pane, the same way
    blocks_for_pane_with_meta resolves it for the live 3x/second view. Returns
    None when the pane can't be found, isn't a Claude pane, or has no
    confidently-resolved transcript - callers must treat that as "no history
    available" rather than an error."""
    pane = pane_by_id(pane_id)
    if pane is None or pane.kind != "Claude":
        return None
    try:
        capture_text = capture(pane_id, history=240)
    except Exception:
        return None
    return claude_transcript_for_pane(pane, capture_text)


def history_before(path: str, cursor: int | None, count: int = 60) -> dict[str, object]:
    """Return up to `count` parsed blocks older than `cursor` from the full
    transcript at `path`, plus the cursor to request the next-older window.

    Cursor contract (deliberately NOT the JSONL entry's own uuid, to avoid
    touching parse_transcript_tail's tested return shape): an opaque integer
    index into the full parsed block list for this transcript, where the
    index is the position of the oldest block already returned (index 0 =
    the very first block ever recorded). Pass `cursor=None` to start from the
    end of the transcript. The response's `next_cursor` is None once index 0
    has been reached, i.e. there is nothing earlier left to load."""
    count = max(1, min(count, 500))
    all_blocks = _all_transcript_blocks_cached(path)
    total = len(all_blocks)
    end = total if cursor is None else max(0, min(cursor, total))
    start = max(0, end - count)
    window = attach_subagent_status(all_blocks[start:end], path, live_panel=False, later_blocks=all_blocks[start:])
    return {
        "blocks": window,
        "next_cursor": start if start > 0 else None,
        "has_more": start > 0,
        "total": total,
    }


# ---------------------------------------------------------------------------
# Phase 3b: Codex rollout-JSONL history + pagination.
#
# Codex CLI writes its own durable per-session JSONL, structurally parallel to
# Claude's transcript, at ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl.
# Unlike Claude, Codex's live 3x/second view stays capture-based (unchanged in
# this pass - Codex prints append-only into the normal tmux scrollback, so
# capture already works there). This section only adds the SAME KIND of
# separate, user-triggered "load earlier history" pagination Phase 3a added for
# Claude, reading from the full rollout file instead of tmux's bounded capture
# window. It does not touch parse_blocks or blocks_for_pane_with_meta's
# existing Codex (tmux-capture) branch.
# ---------------------------------------------------------------------------

_CODEX_SYSTEM_INJECTED_PREFIXES = (
    "<environment_context>",
    "<subagent_notification>",
    "<codex_internal_context",
    "<turn_aborted>",
    "<skill>",
    "# AGENTS.md instructions",
)


def _is_codex_system_injected_user(txt: str) -> bool:
    # Codex wraps its own system-injected content (environment context,
    # AGENTS.md instructions, subagent-completion notifications, skill-
    # invocation wrappers, aborted-turn notices) into a literal payload.type==
    # "message", payload.role=="user" entry. This is a DIFFERENT wrapper shape
    # than Claude's _is_system_injected_user (Claude uses <task-notification>/
    # <system-reminder>/<command-...> tags), so it needs its own marker set
    # rather than reusing Claude's prefixes. Marker list confirmed live against
    # real rollout files on this host: "<environment_context>"
    # and "# AGENTS.md instructions" are by far the most common, matching the
    # fact-check in the task brief.
    s = txt.lstrip()
    return s.startswith(_CODEX_SYSTEM_INJECTED_PREFIXES)


def _codex_json_loads_maybe(value: object) -> object:
    """Codex rollout function_call/custom_tool_call "arguments"/"input" and
    their "*_output" fields are stored as JSON-ENCODED STRINGS, not nested
    objects (confirmed live: e.g. arguments == '{"cmd":"pwd",...}' as a raw
    string that itself needs json.loads). Some tool outputs are plain text,
    not JSON at all (e.g. exec_command's "Chunk ID: ...\\nOutput:\\n..." text
    wrapper) - fall back to the original string when it does not parse."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


def _codex_payload_to_blocks(payload: dict) -> list[dict[str, str]]:
    """Convert one Codex rollout response_item payload into 0+ blocks, in the
    same {"role", "label", "text"} shape parse_transcript_tail uses for Claude.
    Own conversion logic (not shared with parse_transcript_tail) since Codex's
    payload.type set (message/reasoning/function_call/function_call_output/
    custom_tool_call/custom_tool_call_output) and field names (arguments/input/
    output vs Claude's tool_use/tool_result) differ from Claude's schema."""
    blocks: list[dict[str, str]] = []
    ptype = payload.get("type")
    if ptype == "message":
        role = payload.get("role")
        # "developer" role (system/permission preamble) is never a real user
        # prompt or assistant reply - drop it entirely, do not even run it
        # through the system-injected-text filter below.
        if role not in ("user", "assistant"):
            return blocks
        content = payload.get("content")
        items = content if isinstance(content, list) else ([content] if isinstance(content, str) else [])
        for item in items:
            if isinstance(item, str):
                txt = item.strip()
            elif isinstance(item, dict):
                txt = str(item.get("text") or "").strip()
            else:
                txt = ""
            if not txt:
                continue
            if role == "assistant":
                # Codex 的中途产物是 reasoning / function_call; role=assistant 的
                # message 就是这一轮的终结发言, 与 Claude 的 end_turn 等价。
                blocks.append({"role": "assistant", "label": "AI output", "text": txt, "final": True})
            elif _is_codex_system_injected_user(txt):
                blocks.append({"role": "system", "label": "系统通知", "text": txt})
            else:
                blocks.append({"role": "user", "label": "User prompt", "text": txt})
    elif ptype == "reasoning":
        # Codex's reasoning summary is usually empty (encrypted_content is opaque
        # and not meant for display); only emit a block when a real summary
        # string is present.
        summary = payload.get("summary")
        if isinstance(summary, list):
            for item in summary:
                if isinstance(item, str):
                    txt = item.strip()
                elif isinstance(item, dict):
                    txt = str(item.get("text") or "").strip()
                else:
                    txt = ""
                if txt:
                    blocks.append({"role": "assistant", "label": "AI step", "text": txt})
    elif ptype == "function_call":
        name = payload.get("name") or "tool"
        args = _codex_json_loads_maybe(payload.get("arguments"))
        blocks.append({"role": "tool", "label": "Tool", "text": _summarize_tool_input(name, args)})
    elif ptype == "custom_tool_call":
        # e.g. apply_patch: "input" is often a raw diff/patch string, not JSON -
        # _codex_json_loads_maybe falls back to the original string, and
        # _summarize_tool_input falls back to just the tool name for non-dict
        # input, so the diff body is shown via _truncate_tool_value instead.
        name = payload.get("name") or "tool"
        args = _codex_json_loads_maybe(payload.get("input"))
        if isinstance(args, dict):
            text = _summarize_tool_input(name, args)
        else:
            text = f"{name}\n{_truncate_tool_value(args)}"
        blocks.append({"role": "tool", "label": "Tool", "text": text})
    elif ptype in ("function_call_output", "custom_tool_call_output"):
        output = _codex_json_loads_maybe(payload.get("output"))
        if isinstance(output, dict):
            text = str(output.get("output") or output.get("content") or "")
        else:
            text = str(output or "")
        text = text.strip()
        if text:
            blocks.append({"role": "tool", "label": "Tool result", "text": text[:4000]})
    return blocks


# Codex 多智能体工具里只是"等一下/发句话"的调用；结果在子智能体条目和实时面板里看。
CODEX_AGENT_WAIT_TOOLS = {"wait_agent"}
CODEX_AGENT_MESSAGE_TOOLS = {"send_message", "followup_task", "send_input", "interrupt_agent", "close_agent"}


def _codex_rollout_lines_to_blocks(lines: list[str]) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    spawns: dict[str, dict] = {}
    silent_calls: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if entry.get("type") != "response_item":
            continue
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        ptype = payload.get("type")
        name = payload.get("name")
        call_id = str(payload.get("call_id") or "")
        if ptype == "function_call" and name == "spawn_agent":
            args = _codex_json_loads_maybe(payload.get("arguments"))
            task = str(args.get("task_name") or "").strip() if isinstance(args, dict) else ""
            kind = str(args.get("agent_type") or args.get("agent_role") or "codex") if isinstance(args, dict) else "codex"
            block = {"role": "tool", "label": "子智能体", "text": f"{kind} · {task}" if task else kind,
                     "agent_ref": f"/root/{task}" if task else ""}
            spawns[call_id] = block
            blocks.append(block)
            continue
        if ptype == "function_call" and name in CODEX_AGENT_WAIT_TOOLS:
            silent_calls.add(call_id)
            continue
        if ptype == "function_call" and name in CODEX_AGENT_MESSAGE_TOOLS:
            silent_calls.add(call_id)
            blocks.append({"role": "tool", "label": "Tool",
                           "text": codex_subagents.describe_codex_tool(name, payload.get("arguments"))})
            continue
        if ptype == "function_call_output" and call_id in spawns:
            # 回执给出子智能体的完整路径（如 /root/x），用它对上子智能体自己的 rollout。
            output = _codex_json_loads_maybe(payload.get("output"))
            if isinstance(output, dict) and output.get("task_name"):
                spawns[call_id]["agent_ref"] = str(output["task_name"])
            continue
        if ptype == "function_call_output" and call_id in silent_calls:
            continue
        blocks.extend(_codex_payload_to_blocks(payload))
    return blocks


def attach_codex_subagent_status(
    blocks: list[dict], rollout_path: str, pane_pid: str = "", *, live_panel: bool = True
) -> list[dict]:
    """Codex counterpart of attach_subagent_status (see codex_subagents)."""
    now = time.time()
    states = {s.tool_use_id: s for s in codex_subagents.children_of(rollout_path, now)}
    for path in codex_open_child_rollouts(pane_pid):
        state = codex_subagents.child_state(path, now)
        if state and state.tool_use_id not in states:
            states[state.tool_use_id] = state
    return _attach_agent_states(blocks, states, list(states.values()), live_panel=live_panel)


def parse_codex_rollout_tail(path: str, max_blocks: int = 120) -> list[dict[str, str]]:
    """Codex counterpart of parse_transcript_tail: reads only the rollout
    file's tail (reuses the same bounded _read_tail_lines Claude's tail reader
    uses) and parses response_item entries into the same block shape."""
    return _codex_rollout_lines_to_blocks(_read_tail_lines(path))[-max_blocks:]


# (path, mtime, size)-keyed full-parse cache for Codex rollout pagination, same
# pattern as Claude's _history_blocks_cache above - kept as its own dict/
# function pair rather than sharing Claude's, so Phase 3a's cache is never
# touched by this additive Codex feature.
_codex_history_blocks_cache = _BoundedLRUCache(
    max_entries=_HISTORY_CACHE_MAX_ENTRIES,
    max_bytes=_HISTORY_CACHE_MAX_BYTES,
    max_item_bytes=_HISTORY_CACHE_MAX_ITEM_BYTES,
)


def _parse_codex_rollout_all_blocks(path: str) -> list[dict[str, str]]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    return _codex_rollout_lines_to_blocks(lines)


def _all_codex_rollout_blocks_cached(path: str) -> list[dict[str, str]]:
    try:
        st = os.stat(path)
    except OSError:
        return []
    cached = _codex_history_blocks_cache.get(path)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]
    blocks = _parse_codex_rollout_all_blocks(path)
    _codex_history_blocks_cache.put(
        path,
        (st.st_mtime, st.st_size, blocks),
        _blocks_cache_weight(blocks),
    )
    return blocks


def codex_history_before(path: str, cursor: int | None, count: int = 60) -> dict[str, object]:
    """Codex counterpart of history_before: same opaque-index cursor contract,
    over the full Codex rollout file instead of Claude's JSONL transcript."""
    count = max(1, min(count, 500))
    all_blocks = _all_codex_rollout_blocks_cached(path)
    total = len(all_blocks)
    end = total if cursor is None else max(0, min(cursor, total))
    start = max(0, end - count)
    window = attach_codex_subagent_status(all_blocks[start:end], path, live_panel=False)
    return {
        "blocks": window,
        "next_cursor": start if start > 0 else None,
        "has_more": start > 0,
        "total": total,
    }


def _proc_children(pids: list[str]) -> list[str] | None:
    """Child pids via /proc/<pid>/task/<tid>/children (no subprocess).

    `pgrep -P` spawns a process per call; under this host's load that cost
    ~150 ms per Codex pane, which is why rollout discovery had to stay lazy.
    Every thread's file is read because a child is listed under the thread
    that forked it.  Falls back to pgrep only if /proc does not expose it.
    """
    children: list[str] = []
    live_pid_without_children_file = False
    for pid in pids:
        try:
            tids = os.listdir(f"/proc/{pid}/task")
        except OSError:
            continue  # process already gone
        readable = False
        for tid in tids:
            try:
                with open(f"/proc/{pid}/task/{tid}/children", encoding="ascii") as fh:
                    children.extend(fh.read().split())
                readable = True
            except OSError:
                continue
        live_pid_without_children_file |= not readable
    if not live_pid_without_children_file:
        return children
    try:
        out = subprocess.run(["pgrep", "-P", ",".join(pids)], capture_output=True, text=True, timeout=2)
    except Exception:
        return None
    return out.stdout.split()


def _codex_native_pids_for_pane(pane_pid: str, max_hops: int = 4) -> list[str]:
    """Walk down from the pane's root pid to native `codex` binary process(es).

    Live check on this host: the process tree
    is pane shell -> node (npm wrapper, e.g. `node .../bin/codex resume ...`)
    -> native `codex` binary (musl build) - one 'node' hop, and the native
    binary is the process holding an open fd on its own rollout file (verified
    via /proc/<pid>/fd/*). The task brief's fact-check describes an extra
    'node' hop on some Codex CLI versions (shell -> node -> node -> native), so
    this walks breadth-first through consecutive 'node' children rather than
    hardcoding the hop count, stopping at any child whose comm is exactly
    'codex'. Bounded to max_hops so an unrelated deep process tree cannot cause
    a runaway walk.
    """
    if not pane_pid or not pane_pid.isdigit():
        return []
    frontier = [pane_pid]
    native: list[str] = []
    seen: set[str] = set()
    for _hop in range(max_hops):
        parents = [p for p in frontier if p not in seen]
        if not parents:
            break
        seen.update(parents)
        children = _proc_children(parents)
        if children is None:
            break
        next_frontier: list[str] = []
        for cpid in children:
            try:
                with open(f"/proc/{cpid}/comm", encoding="utf-8") as fh:
                    comm = fh.read().strip()
            except Exception:
                continue
            if comm == "codex":
                native.append(cpid)
            elif comm == "node":
                next_frontier.append(cpid)
        if not next_frontier:
            break
        frontier = next_frontier
    return native


def _codex_rollout_fd_candidates(pane_pid: str) -> list[str]:
    """List open rollout-*.jsonl fd targets across all native codex processes
    found under this pane. Normally exactly one; subagent fan-out can leave
    several open at once (each subagent gets its own rollout file, the parent
    keeps all open) - observed live on this host: one pane had 7 simultaneously
    open rollout fds. De-duped, sorted by mtime (newest first) purely for a
    deterministic scan order - selection among multiple candidates is done by
    screen-content match (codex_rollout_for_pane), NOT by this order."""
    paths: list[str] = []
    seen: set[str] = set()
    for pid in _codex_native_pids_for_pane(pane_pid):
        fd_dir = f"/proc/{pid}/fd"
        try:
            entries = os.listdir(fd_dir)
        except OSError:
            continue
        for entry in entries:
            try:
                target = os.readlink(f"{fd_dir}/{entry}")
            except OSError:
                continue
            if "/rollout-" in target and target.endswith(".jsonl") and target not in seen:
                seen.add(target)
                paths.append(target)
    try:
        paths.sort(key=lambda p: os.stat(p).st_mtime if os.path.exists(p) else 0, reverse=True)
    except Exception:
        pass
    return paths


def _score_rollout_candidates(snips: list[str], blobs: dict[str, str]) -> tuple[str | None, int, int]:
    """Pure disambiguation scoring: given screen-match snippets and a
    path->normalized-blob map, return (winner_or_None, best_hits, second_hits).
    Uses the SAME confidence bar Claude's own transcript disambiguation uses
    (best_hits >= 2 and best_hits > second_hits; see claude_transcript_for_pane
    / trim_transcript_blocks_to_screen) so a tie or a weak single-hit match
    never silently picks a winner. Kept standalone (not inlined into
    codex_rollout_for_pane) so it is unit-testable with synthetic strings
    without needing real /proc fds."""
    best_path: str | None = None
    best_hits = 0
    second_hits = 0
    for path, blob in blobs.items():
        hits = sum(1 for s in snips if s in blob)
        if hits > best_hits:
            best_hits, second_hits, best_path = hits, best_hits, path
        elif hits > second_hits:
            second_hits = hits
    if best_hits >= 2 and best_hits > second_hits:
        return best_path, best_hits, second_hits
    return None, best_hits, second_hits


# Per-pane-process-identity cache: proc_identity -> {"path", "meta", "ts"}.
# Keyed by _pane_agent_process_identity (the same durable pid:starttime token
# Claude's own _claude_map_cache is bound to - see that cache) so a reused tmux pane id can never
# inherit a stale rollout-file resolution from a previous, unrelated Codex
# process.
_codex_rollout_cache: dict[str, dict] = {}
_CODEX_ROLLOUT_TTL = 4.0  # seconds; mirrors _CLAUDE_MAP_TTL


def codex_rollout_for_pane(pane: Pane, capture_text: str) -> tuple[str | None, dict[str, object]]:
    """Resolve which Codex rollout JSONL file a pane is currently backed by.

    Candidates are discovered via open file descriptors on the native codex
    binary process(es) reached by walking down from the pane's shell pid
    (_codex_native_pids_for_pane) - the native binary keeps a write-only fd
    open on its own rollout file. This is cheap and pane-scoped, unlike
    Claude's global recent-transcript scan (only a handful of fd-derived
    candidates per pane, never hundreds of files).

    Cases:
      0 candidates -> (None, quality=degraded, reason="no-rollout-yet") - e.g.
        a brand-new pane with nothing written yet; must be reported as "no
        history available yet", not an error.
      1 candidate -> that file, confidently (no ambiguity possible).
      >1 candidates (subagent fan-out) -> disambiguate via
        _score_rollout_candidates (screen-match is PRIMARY; mtime is only used
        to order the scan, never to pick a winner). No confident winner ->
        (None, quality=degraded, reason="ambiguous-rollout") rather than
        guessing wrong and showing the wrong conversation.

    Cached per pane process identity with a short TTL so a reused tmux pane id
    never inherits a stale resolution from a previous process.
    """
    proc_identity = _pane_agent_process_identity(pane)
    now = time.time()
    if proc_identity:
        cached = _codex_rollout_cache.get(proc_identity)
        if cached and (now - cached.get("ts", 0)) < _CODEX_ROLLOUT_TTL:
            return cached.get("path"), dict(cached.get("meta") or {})

    pane_pid = ""
    try:
        cp = run_tmux(["display-message", "-p", "-t", pane.pane_id, "#{pane_pid}"])
        if cp.returncode == 0:
            pane_pid = cp.stdout.strip()
    except Exception:
        pane_pid = ""

    candidates = _codex_rollout_fd_candidates(pane_pid) if pane_pid else []
    # 子智能体的 rollout 也由同一个进程打开，而且 fork 出来的子智能体复制了父会话的
    # 历史，按屏幕内容比对会和父会话打平。窗口显示的永远是根会话，先按结构排除子智能体。
    roots = [cand for cand in candidates if not codex_subagents.is_subagent_rollout(cand)]
    if roots:
        candidates = roots
    path: str | None = None
    meta: dict[str, object]
    if not candidates:
        meta = {
            "quality": "degraded",
            "reason": "no-rollout-yet",
            "match_quality": "orphan",
        }
    elif len(candidates) == 1:
        path = candidates[0]
        # The live Codex process itself holds an fd to this sole rollout.
        meta = {"quality": "full", "reason": "", "match_quality": "structural"}
    else:
        snips = _screen_match_snippets(capture_text)
        if len(snips) < 2:
            meta = {
                "quality": "degraded",
                "reason": "ambiguous-rollout",
                "candidates": len(candidates),
                "match_quality": "orphan",
            }
        else:
            blobs = {cand: _transcript_norm_blob(cand) for cand in candidates}
            winner, _best, _second = _score_rollout_candidates(snips, blobs)
            if winner:
                path = winner
                meta = {
                    "quality": "full",
                    "reason": "",
                    "match_quality": "semi",
                }
            else:
                meta = {
                    "quality": "degraded",
                    "reason": "ambiguous-rollout",
                    "candidates": len(candidates),
                    "match_quality": "orphan",
                }

    if proc_identity:
        _codex_rollout_cache[proc_identity] = {"path": path, "meta": dict(meta), "ts": now}
    return path, meta


def codex_rollout_path_for_pane_id(pane_id: str) -> tuple[str | None, dict[str, object]]:
    """Resolve a Codex pane's rollout path from just a pane_id, the same
    entrypoint shape transcript_path_for_pane_id gives Claude's pagination
    endpoint. Returns (None, meta) with an explicit reason when the pane can't
    be found, isn't a Codex pane, or has no resolvable rollout file - callers
    must treat that as "no history available" rather than an error."""
    pane = pane_by_id(pane_id)
    if pane is None or pane.kind != "Codex":
        return None, {"reason": "not-codex"}
    try:
        capture_text = capture(pane_id, history=240)
    except Exception:
        capture_text = ""
    return codex_rollout_for_pane(pane, capture_text)


def pane_is_alt_screen(pane: Pane) -> bool:
    try:
        return run_tmux(["display-message", "-p", "-t", pane.pane_id, "#{alternate_on}"]).stdout.strip() == "1"
    except Exception:
        return False


SESSION_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def panes_stamped_to(path: str, exclude_pane_id: str = "") -> list[str]:
    """除自己之外，还有哪些 pane 的 hook 标记也指向这份 transcript。

    同一条会话可以被 `claude --resume` 在两个窗口同时打开，
    那时两个 pane 的标记会指向同一个文件。标记是权威的不代表它是独占的 —— 不检测就会
    有两张卡片默默显示同一段对话，而用户以为它们是两个不同的会话。
    """
    if not path:
        return []
    try:
        cp = run_tmux(["list-panes", "-a", "-F", "#{pane_id}\t#{@ai_transcript}"])
    except Exception:
        return []
    if cp.returncode != 0:
        return []
    out = []
    for line in cp.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        pane_id, stamped = parts[0].strip(), parts[1].strip()
        if stamped == path and pane_id != exclude_pane_id:
            out.append(pane_id)
    return out


CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def official_claude_record(pane: Pane | None) -> dict | None:
    """Claude's own record for the session running in this pane, matched by pid only.

    ``claude agents --json`` is the provider's truth for which conversation a
    process is in right now; a pid in the pane's process tree ties it to the
    pane exactly (no session-id fallback, which could belong to another window)."""
    pane_pid = str(getattr(pane, "pane_pid", "") or "") if pane else ""
    if not pane_pid:
        return None
    pids = _descendant_pids(pane_pid) | {pane_pid}
    return claude_sessions.record_for(claude_agent_records(), pids, "")


def claude_transcript_for_session(session_id: str, cwd: str = "") -> str:
    """The transcript file of ``session_id``.  One session resumed in two
    directories has one file per directory; the one in the process's cwd wins."""
    if not session_id:
        return ""
    hits = [path for path in CLAUDE_PROJECTS_DIR.glob(f"*/{session_id}.jsonl") if path.is_file()]
    if len(hits) > 1 and cwd:
        slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
        hits = [path for path in hits if path.parent.name == slug] or hits
    return str(max(hits, key=lambda path: path.stat().st_mtime)) if hits else ""


def stamped_transcript_for_pane(pane: Pane | None) -> str:
    """The transcript of the conversation this pane is running, or ''.

    The SessionStart hook stamps the pane with its session; Claude's own
    session list says which session the pane's process is in *now*.  When a
    window was reopened or /resume'd and the stamp did not follow, the official
    session wins (the background loop also rewrites the stale stamp).  Only an
    existing file counts: a reused pane keeps its old stamp."""
    path = (getattr(pane, "ai_transcript", "") or "").strip() if pane else ""
    record = official_claude_record(pane) if pane and pane.kind == "Claude" and pane.ai_alive else None
    official = str((record or {}).get("sessionId") or "")
    cwd = str((record or {}).get("cwd") or (pane.cwd if pane else "") or "")
    if official and official != (getattr(pane, "ai_session_id", "") or "").strip():
        return claude_transcript_for_session(official, cwd)
    try:
        if path and os.path.isfile(path):
            return path
    except OSError:
        pass
    # The stamped id is confirmed but its file is elsewhere: a session that
    # entered a worktree writes its transcript under the worktree's directory.
    return claude_transcript_for_session(official, cwd) if official else ""


def reconcile_claude_stamps(session: str = "") -> list[dict[str, str]]:
    """Rewrite pane stamps that no longer name the session the pane runs.

    Recovery snapshots and every reader of the stamp trust it, so a stale one
    (window reopened, /resume, a hook that did not fire) would restore or show
    the wrong conversation.  The correction comes only from Claude's own
    session list matched by pid; panes without such a record are left alone."""
    fixed: list[dict[str, str]] = []
    for pane in list_panes(session or DEFAULT_SESSION, include_preview=False):
        if pane.kind != "Claude" or not pane.ai_alive:
            continue
        record = official_claude_record(pane)
        official = str((record or {}).get("sessionId") or "")
        if not official:
            continue
        transcript = claude_transcript_for_session(official, str((record or {}).get("cwd") or pane.cwd))
        stamp_file_ok = bool(pane.ai_transcript) and os.path.isfile(pane.ai_transcript)
        if official == pane.ai_session_id and (stamp_file_ok or not transcript):
            continue
        options = [("@ai_session_id", official), ("@ai_provider", "claude")]
        if transcript:
            options.append(("@ai_transcript", transcript))
        if any(run_tmux(["set-option", "-p", "-t", pane.pane_id, name, value]).returncode for name, value in options):
            continue
        item = {"pane": pane.pane_id, "old_session_id": pane.ai_session_id, "new_session_id": official}
        event_ledger.append_event(
            "pane_identity_restamped", pane=pane.pane_id, target=pane.target, source="card-dashboard",
            message=(
                f"stamp {pane.ai_session_id or '-'} -> {official} (claude agents --json)"
                if official != pane.ai_session_id else f"transcript path -> {transcript}"
            ),
            data={**item, "transcript": transcript},
        )
        fixed.append(item)
    return fixed


def _with_dialog_choice(blocks: list[dict], pane: Pane, capture_text: str) -> list[dict]:
    """Screen-only Claude timelines still show an open (unnumbered) dialog as a choice."""
    if blocks and blocks[-1].get("role") == "choice":
        return blocks
    if str((claude_agent_record(pane.ai_session_id, pane.pane_pid) or {}).get("status")) != "waiting":
        return blocks
    choice = live_nav_choice_block(capture_text)
    if not choice:
        return blocks
    # 读屏幕的时间线已经把对话框的问题当成一段 AI 输出读进来了；问题归到选择卡里，
    # 不在上面再显示一遍。
    question = [line for line in str(choice.get("question") or "").splitlines() if line.strip()]
    kept = list(blocks)
    if question and kept and kept[-1].get("role") == "assistant":
        lines = str(kept[-1].get("text") or "").splitlines()
        first = next((i for i, line in enumerate(lines) if line.strip() == question[0]), None)
        if first is not None:
            rest = "\n".join(lines[:first]).rstrip()
            kept = kept[:-1] + ([{**kept[-1], "text": rest}] if rest else [])
    return [*kept, {**choice, "pending": True}]


def blocks_for_pane_with_meta(pane: Pane | None, capture_text: str) -> tuple[list[dict[str, str]], dict[str, object]]:
    """Return provider-structured history first, with tmux as a live fallback.

    The terminal is a presentation surface and its wording/layout changes with
    every provider release.  Claude and Codex both write a durable, role-aware
    JSONL stream, so that stream is the source of truth for submitted messages
    and assistant output.  tmux is retained only for provider UI state that has
    not reached disk yet (queued input, pickers, and a brand-new turn).
    """
    if pane and pane.kind == "Claude" and pane_is_alt_screen(pane):
        # SessionStart hook 钉在 pane 上的 transcript 是 Claude 自己给的,不需要推断,
        # 也就不可能和别的 pane 撞车 —— 撞车只会发生在"靠屏幕内容猜"的路径上。同一个
        # 目录开多个窗口时,猜出来的两条会指向同一个 JSONL,双方一起降级成抓屏,而 focus
        # 模式下屏幕本就只剩最后一条消息,界面于是近乎空白。
        stamped = stamped_transcript_for_pane(pane)
        pane_session_id = (getattr(pane, "ai_session_id", "") or "").strip().lower()
        # 故意不按 session id 跨目录反查 transcript: 同一个会话 id 可以同时属于两个窗口
        # (`claude agents --json` 可能对两个不同 cwd 的 pid 报同一个
        # sessionId,多半是同一条会话被 --resume 开了两次)。此时只有 hook 给的完整路径
        # (含 cwd)能区分它们;按 id 反查会让两个窗口显示同一份对话,比显示不出来更糟。
        if not stamped and pane_session_id:
            # 这个 pane 有 hook 给的权威会话 id,但它的 transcript 还没落盘(会话刚起、
            # 或刚被 resume 到新目录还没产生第一条消息)。此时**绝不能**回退到内容指纹
            # 匹配 —— 屏幕上那点内容会把它匹配到别人的 transcript 上去,于是两个窗口
            # 显示同一段对话。宁可先显示这个 pane 自己的屏幕,等文件出现自动转正。
            return _with_dialog_choice(parse_blocks(capture_text, pane.kind), pane, capture_text), {
                "source": "tmux-screen",
                "quality": "pending-transcript",
                "reason": "transcript-not-yet-written",
                "shared_panes": [],
                "transcript_id": pane_session_id,
            }
        path = stamped or claude_transcript_for_pane(pane, capture_text)
        shared = _shared_live_transcript_panes(path, pane.pane_id) if path else []
        if shared and stamped:
            # 有权威标记时不再无条件跳过冲突检测 —— 那等于在最该报警的场景关掉唯一的
            # 探测器。分两种情况:别人是靠屏幕内容猜到这个文件的,那以我为准照常显示;
            # 别人也钉着同一个文件(同一条会话被双开),那两边都要标出来,不能默默同屏。
            duplicates = panes_stamped_to(path, pane.pane_id)
            if duplicates:
                blocks = parse_transcript_tail(path)
                return blocks, {
                    "source": "claude-transcript",
                    "quality": "duplicate-session",
                    "reason": "same-session-open-in-multiple-panes",
                    "shared_panes": duplicates,
                    "transcript_id": Path(path).stem,
                }
            shared = []
        if path and shared:
            return _with_dialog_choice(parse_blocks(capture_text, pane.kind), pane, capture_text), {
                "source": "tmux-screen",
                "quality": "degraded",
                "reason": "shared-transcript",
                "shared_panes": shared,
                "transcript_id": Path(path).stem,
            }
        if path:
            try:
                blocks = parse_transcript_tail(path)
            except Exception:
                blocks = []
            if blocks:
                # Anchoring to the screen only protects a *guessed* transcript that
                # another pane may share.  An authoritative one is shown whole: the
                # screen may be scrolled up ("Jump to bottom"), in copy mode or
                # behind a picker, and cutting at what it shows hid the newest turns.
                if not stamped:
                    blocks = trim_transcript_blocks_to_screen(blocks, capture_text)
                blocks = merge_live_screen_tail(
                    blocks, capture_text, pane_kind=pane.kind,
                    pane_identity=_pane_agent_process_identity(pane),
                )
                blocks = attach_subagent_status(blocks, path)
                # The live on-screen picker is not in the transcript until it is
                # answered, so append it from the current capture.
                try:
                    choice = live_choice_block(capture_text)
                    if not choice and str((claude_agent_record(pane.ai_session_id, pane.pane_pid) or {}).get("status")) == "waiting":
                        choice = live_nav_choice_block(capture_text)
                    if choice:
                        choice["pending"] = True
                        blocks.append(choice)
                except Exception:
                    pass
                # 排队消息(Claude 思考时用户发的、还没被消费的话)也从 live capture 补显示,
                # 否则只看 transcript 会漏掉"已排队但未提交"的用户消息。
                # merge_live_screen_tail 刚才已经从同一份 capture_text 里扫过"屏幕上新出现
                # 的一行"并可能已经把这条排队消息当成普通 pending 用户块加过一次;这里再用
                # _blocks_equivalent 查一遍已有 blocks,避免同一句话被两条独立的屏幕扫描逻辑
                # 各加一次、渲染成两条重复气泡。
                try:
                    for queued in extract_queued_messages(capture_text):
                        candidate = {"role": "user", "label": "排队中", "text": queued, "pending": True}
                        if any(_blocks_equivalent(candidate, existing) for existing in blocks[-20:]):
                            continue
                        blocks.append(candidate)
                except Exception:
                    pass
                return blocks, {
                    "source": "claude-transcript",
                    "quality": "full",
                    "reason": "",
                    "transcript_id": Path(path).stem,
                }
        return _with_dialog_choice(parse_blocks(capture_text, pane.kind), pane, capture_text), {
            "source": "tmux-screen",
            "quality": "degraded",
            "reason": "no-confident-transcript",
            "shared_panes": [],
            "transcript_id": "",
        }
    kind = pane.kind if pane else ""
    if kind == "Codex" and pane is not None:
        # Codex rollout JSONL is the durable counterpart of Claude's transcript.
        # Resolve it by the exact live process identity; ambiguous/no-rollout
        # cases deliberately fall back to screen parsing instead of guessing a
        # different session's history.
        try:
            rollout_path, rollout_meta = codex_rollout_for_pane(pane, capture_text)
        except Exception:
            rollout_path, rollout_meta = None, {"reason": "rollout-resolve-error"}
        if rollout_path:
            try:
                blocks = parse_codex_rollout_tail(rollout_path)
            except Exception:
                blocks = []
            if blocks:
                blocks = attach_codex_subagent_status(blocks, rollout_path, pane.pane_pid)
                # Queued input is intentionally absent from rollout JSONL until
                # Codex consumes it, so add only this live, pending fragment.
                for queued in extract_codex_queued_messages(capture_text):
                    candidate = {"role": "user", "label": "排队中", "text": queued, "pending": True}
                    if any(_blocks_equivalent(candidate, existing) for existing in blocks[-20:]):
                        continue
                    blocks.append(candidate)
                return blocks[-120:], {
                    "source": "codex-rollout",
                    "quality": str(rollout_meta.get("quality") or "full"),
                    "reason": "",
                    "shared_panes": [],
                    "transcript_id": Path(rollout_path).stem,
                }

    source = "tmux-capture" if kind in {"Codex", "Claude"} else "tmux-screen"
    blocks = parse_blocks(capture_text, kind)
    if kind == "Codex":
        # No safe rollout yet (brand-new turn, process transition, or
        # ambiguity): retain the live queue marker without treating old screen
        # prompts as submitted messages.
        for queued in extract_codex_queued_messages(capture_text):
            candidate = {"role": "user", "label": "排队中", "text": queued, "pending": True}
            if any(_blocks_equivalent(candidate, existing) for existing in blocks[-20:]):
                continue
            blocks.append(candidate)
        blocks = blocks[-120:]
    return blocks, {
        "source": source,
        "quality": "degraded" if kind == "Codex" else "full",
        "reason": str((rollout_meta if kind == "Codex" else {}).get("reason") or ""),
        "shared_panes": [],
        "transcript_id": "",
    }


def blocks_for_pane(pane: Pane | None, capture_text: str) -> list[dict[str, str]]:
    blocks, _meta = blocks_for_pane_with_meta(pane, capture_text)
    return blocks


def json_response(handler: BaseHTTPRequestHandler, payload: object, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        # Browser navigation/AbortController cancellation is a normal client
        # disconnect, not an application 500 and not worth a traceback.
        return


def frontend_config() -> dict[str, object]:
    """Deployment settings the browser needs (injected into index.html)."""
    root = str(LOCAL_ARTIFACT_ROOT).rstrip("/") + "/"
    return {
        "localArtifactRoot": root,
        "publicShareHost": PUBLIC_SHARE_HOST,
        "publicShareDir": PUBLIC_SHARE_DIR,
        "summaryHeadings": SUMMARY_HEADINGS,
    }


def render_index() -> str:
    html = INDEX.read_text(encoding="utf-8")
    payload = json.dumps(frontend_config(), ensure_ascii=False).replace("</", "<\\/")
    snippet = f"<script>window.AGENT_BUS_CONFIG = {payload};</script>"
    return html.replace("<head>", "<head>\n" + snippet, 1)


def text_response(handler: BaseHTTPRequestHandler, text: str, status: int = 200, content_type: str = "text/html") -> None:
    body = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", f"{content_type}; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        return


def trace_response_for_pane(
    pane_id: str,
    *,
    expected_pid: str = "",
    expected_start_time: str = "",
    max_spans: int = 2000,
    known_source_signature: str = "",
) -> tuple[int, dict[str, object]]:
    """Build one on-demand, read-only trace response for an exact live pane.

    Trace rendering is deliberately separate from ``/api/capture``.  It reads
    the already-existing provider JSONL only when the user opens the trace
    view, and it never writes hooks, a second event ledger, or provider state.
    """

    pane = pane_by_id(pane_id)
    if pane is None:
        return 404, {"available": False, "reason": "pane-not-found", "pane": pane_id}
    if not expected_pid or not expected_start_time:
        return 400, {"available": False, "reason": "pane-identity-required", "pane": pane_id}
    try:
        validate_pane_instance(pane, expected_pid, expected_start_time)
    except (PaneIdentityConflict, ValueError):
        return 409, {"available": False, "reason": "stale-pane-instance", "pane": pane_id}
    # A tmux pane's leader is commonly a long-lived shell. PID/start therefore
    # does not change when Claude or Codex exits and another agent starts in the
    # same pane. Freeze the actual foreground agent process identity as well so
    # a parse started for one agent can never be returned to its successor.
    expected_agent_identity = _compute_pane_agent_process_identity(pane)
    if not expected_agent_identity:
        return 200, {
            "available": False,
            "reason": "agent-identity-unavailable",
            "pane": pane_id,
        }

    source = ""
    trace_path: str | None = None
    resolve_reason = ""
    log_association_quality = "orphan"
    if pane.kind == "Claude":
        source = "claude-code"
        trace_path = transcript_path_for_pane_id(pane_id)
        # Claude's current resolver correlates bounded live-screen text with
        # provider transcripts. That is useful evidence, but not an explicit
        # provider-recorded pane->session link.
        log_association_quality = "heuristic" if trace_path else "orphan"
        if trace_path and _shared_live_transcript_panes(trace_path, pane_id):
            # A conversation-scoped Claude JSONL shared by two live panes
            # cannot be attributed to either pane safely.
            return 200, {
                "available": False,
                "reason": "shared-transcript",
                "pane": pane_id,
                "source": source,
            }
    elif pane.kind == "Codex":
        source = "codex"
        trace_path, resolve_meta = codex_rollout_path_for_pane_id(pane_id)
        resolve_reason = str(resolve_meta.get("reason") or "")
        candidate_quality = str(resolve_meta.get("match_quality") or "")
        log_association_quality = (
            candidate_quality
            if candidate_quality in {"structural", "semi", "heuristic", "orphan"}
            else ("semi" if trace_path else "orphan")
        )
    else:
        return 200, {
            "available": False,
            "reason": "unsupported-pane-kind",
            "pane": pane_id,
            "source": str(pane.kind or "").lower(),
        }

    if not trace_path:
        return 200, {
            "available": False,
            "reason": resolve_reason if resolve_reason in {"no-rollout-yet", "ambiguous-rollout"} else "no-provider-log",
            "pane": pane_id,
            "source": source,
        }

    bounded_spans = max(1, min(int(max_spans), TRACE_MAX_SPANS))
    cache_key: tuple[object, ...] | None = None
    parsed_source_signature = ""
    source_session_identity = ""
    try:
        trace_file = Path(trace_path).resolve(strict=True)
        stat = trace_file.stat()
        cache_key = (
            source,
            str(trace_file),
            stat.st_dev,
            stat.st_ino,
            stat.st_mtime_ns,
            stat.st_size,
            bounded_spans,
        )
        parsed_source_signature = hashlib.sha256(
            f"{source}\0{stat.st_dev}\0{stat.st_ino}\0"
            f"{stat.st_size}\0{stat.st_mtime_ns}".encode("utf-8")
        ).hexdigest()[:20]
        # Stable for the lifetime of this provider log, unlike the content
        # signature above which must change on every append.
        source_session_identity = hashlib.sha256(
            f"{source}\0{stat.st_dev}\0{stat.st_ino}".encode("utf-8")
        ).hexdigest()[:20]
    except OSError:
        # Let the parser own the final source/path validation so the public
        # response remains generic and never includes a filesystem path.
        cache_key = None

    def current_instance() -> Pane | None:
        current = pane_by_id(pane_id)
        if current is None:
            return None
        try:
            validate_pane_instance(current, expected_pid, expected_start_time)
        except (PaneIdentityConflict, ValueError):
            return None
        if not hmac.compare_digest(
            _compute_pane_agent_process_identity(current),
            expected_agent_identity,
        ):
            return None
        return current

    def identity_payload(
        current: Pane,
        *,
        source_id: str = "",
        source_signature: str = "",
        stable_source_identity: str = "",
    ) -> dict[str, object]:
        session_signature = hashlib.sha256(
            f"{source}\0{stable_source_identity or source_id}\0"
            f"{expected_pid}\0{expected_start_time}\0"
            f"{expected_agent_identity}".encode("utf-8")
        ).hexdigest()[:20]
        return {
            "match_quality": log_association_quality,
            "pane_instance_quality": "exact",
            "log_association_quality": log_association_quality,
            "exact": log_association_quality == "structural",
            "pane_target": str(current.target or "")[:160],
            "pane_pid": str(expected_pid)[:32],
            "pane_start_time": str(expected_start_time)[:80],
            "source_kind": source,
            "source_id": "",
            "source_alias": str(source_id or "")[:180],
            "source_alias_kind": "local-path-hash" if source_id else "",
            "provider_log": "resolved-read-only",
            "absolute_path_exposed": False,
            "session_signature": session_signature,
        }

    # The open trace panel polls at a deliberately low rate.  When the exact
    # provider file stat is unchanged, validate the pane identity a second
    # time and return only a tiny receipt instead of reparsing/retransmitting
    # up to 2,000 spans.  This stays outside the normal capture/pane loops.
    if (
        parsed_source_signature
        and known_source_signature
        and hmac.compare_digest(parsed_source_signature, str(known_source_signature))
    ):
        current = current_instance()
        if current is None:
            return 409, {"available": False, "reason": "stale-pane-instance", "pane": pane_id}
        if source == "claude-code" and _shared_live_transcript_panes(trace_path, pane_id):
            return 200, {
                "available": False,
                "reason": "shared-transcript",
                "pane": pane_id,
                "source": source,
            }
        identity = identity_payload(
            current,
            source_signature=parsed_source_signature,
            stable_source_identity=source_session_identity,
        )
        return 200, {
            "available": True,
            "unchanged": True,
            "reason": "",
            "pane": pane_id,
            "source": source,
            "source_signature": parsed_source_signature,
            "session_signature": identity["session_signature"],
            "identity": identity,
            "captured_at": now_iso(),
        }

    trace: dict[str, object] | None = None
    if cache_key is not None:
        with TRACE_CACHE_LOCK:
            cached = TRACE_CACHE.get(cache_key)
            if cached is not None:
                TRACE_CACHE.move_to_end(cache_key)
                trace = cached

    acquired = False
    try:
        if trace is None:
            acquired = TRACE_BUILD_SEMAPHORE.acquire(blocking=False)
            if not acquired:
                return 429, {
                    "available": False,
                    "reason": "trace-busy",
                    "pane": pane_id,
                    "source": source,
                }
            trace = trace_data.build_trace(source, trace_path, max_spans=bounded_spans)
            if not isinstance(trace, dict):
                raise ValueError("trace parser returned a non-object")
            encoded_size = len(
                json.dumps(trace, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
            if encoded_size > TRACE_MAX_RESPONSE_BYTES:
                return 200, {
                    "available": False,
                    "reason": "trace-too-large",
                    "pane": pane_id,
                    "source": source,
                }
            if cache_key is not None and TRACE_CACHE_ITEMS > 0:
                with TRACE_CACHE_LOCK:
                    TRACE_CACHE[cache_key] = trace
                    TRACE_CACHE.move_to_end(cache_key)
                    while len(TRACE_CACHE) > TRACE_CACHE_ITEMS:
                        TRACE_CACHE.popitem(last=False)
    except Exception:  # noqa: BLE001 - public response must not expose parser internals
        # Do not echo parser exceptions: they can contain absolute paths or
        # fragments of malformed provider payloads.
        return 200, {
            "available": False,
            "reason": "trace-parse-failed",
            "pane": pane_id,
            "source": source,
        }
    finally:
        if acquired:
            TRACE_BUILD_SEMAPHORE.release()

    # Resolving/parsing can take long enough for tmux to reuse ``%pane_id``.
    # Re-read and validate the same PID/start token before returning the trace.
    current_pane = current_instance()
    if current_pane is None:
        return 409, {"available": False, "reason": "stale-pane-instance", "pane": pane_id}
    if source == "claude-code" and _shared_live_transcript_panes(trace_path, pane_id):
        # A second Claude pane may begin sharing this conversation while the
        # bounded parser is running. Recheck attribution after parsing just as
        # we recheck pane process identity.
        return 200, {
            "available": False,
            "reason": "shared-transcript",
            "pane": pane_id,
            "source": source,
        }

    trace_id = str((trace or {}).get("trace_id") or (trace or {}).get("session_id") or "")
    source_changed_during_parse = False
    try:
        final_stat = Path(trace_path).resolve(strict=True).stat()
        final_source_signature = hashlib.sha256(
            f"{source}\0{final_stat.st_dev}\0{final_stat.st_ino}\0"
            f"{final_stat.st_size}\0{final_stat.st_mtime_ns}".encode("utf-8")
        ).hexdigest()[:20]
        if not source_session_identity:
            source_session_identity = hashlib.sha256(
                f"{source}\0{final_stat.st_dev}\0{final_stat.st_ino}".encode("utf-8")
            ).hexdigest()[:20]
        source_changed_during_parse = bool(
            parsed_source_signature and final_source_signature != parsed_source_signature
        )
        # The signature attached to a parsed payload must describe the file
        # version parsing started from.  If the provider appended while the
        # parser ran, the next low-rate poll sees the newer stat and reparses.
        source_signature = parsed_source_signature or final_source_signature
    except OSError:
        source_signature = hashlib.sha256(
            json.dumps(trace or {}, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:20]
    identity = identity_payload(
        current_pane,
        source_id=trace_id,
        source_signature=source_signature,
        stable_source_identity=source_session_identity,
    )
    session_signature = str(identity["session_signature"])
    trace_payload = dict(trace or {})
    trace_payload["identity"] = identity

    return 200, {
        "available": True,
        "unchanged": False,
        "reason": "",
        "pane": pane_id,
        "source": source,
        "trace": trace_payload,
        "source_id": trace_id,
        "source_signature": source_signature,
        "session_signature": session_signature,
        "source_changed_during_parse": source_changed_during_parse,
        "captured_at": now_iso(),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "TmuxCardDashboard/0.1"

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if URL_PREFIX and path == URL_PREFIX:
            path = "/"
        elif URL_PREFIX and path.startswith(URL_PREFIX + "/"):
            path = path[len(URL_PREFIX):] or "/"
        query = parse_qs(parsed.query)
        try:
            if not self.authorized():
                self.require_auth()
                return
            if path in {"/", "/index.html"}:
                text_response(self, render_index())
                return
            if path == "/trace_view.js":
                text_response(
                    self,
                    TRACE_VIEW_JS.read_text(encoding="utf-8"),
                    content_type="application/javascript",
                )
                return
            if path == "/trace_view.css":
                text_response(
                    self,
                    TRACE_VIEW_CSS.read_text(encoding="utf-8"),
                    content_type="text/css",
                )
                return
            if path.startswith("/uploads/"):
                name = Path(path.removeprefix("/uploads/")).name
                file_path = UPLOAD_DIR / name
                if not file_path.is_file() or file_path.parent != UPLOAD_DIR:
                    text_response(self, "not found", status=404, content_type="text/plain")
                    return
                body = file_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", image_content_type(file_path))
                self.send_header("Cache-Control", "private, max-age=3600")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path.startswith("/local-files/preview/"):
                route = path.removeprefix("/local-files/preview/")
                alias, separator, name = route.partition("/")
                file_path = local_artifact_path_from_request(alias, name) if separator else None
                if file_path is None:
                    text_response(self, "not found", status=404, content_type="text/plain")
                    return
                content_type = shared_file_content_type(file_path)
                if not (content_type.startswith("image/") or content_type.startswith("video/") or content_type == "text/html"):
                    text_response(self, "preview unavailable", status=415, content_type="text/plain")
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                for name, value in preview_security_headers(content_type):
                    self.send_header(name, value)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "private, no-cache, no-transform" if file_path.suffix.lower() in {".html", ".htm"} else "private, max-age=3600")
                self.send_header("Content-Length", str(file_path.stat().st_size))
                self.end_headers()
                with file_path.open("rb") as fh:
                    while True:
                        chunk = fh.read(1024 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                return
            if path.startswith("/local-files/"):
                route = path.removeprefix("/local-files/")
                alias, separator, name = route.partition("/")
                file_path = local_artifact_path_from_request(alias, name) if separator else None
                if file_path is None:
                    text_response(self, "not found", status=404, content_type="text/plain")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream" if file_path.suffix.lower() in {".html", ".htm"} else shared_file_content_type(file_path))
                self.send_header("Content-Disposition", attachment_content_disposition(file_path))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "private, no-cache, no-transform" if file_path.suffix.lower() in {".html", ".htm"} else "private, max-age=3600")
                self.send_header("Content-Length", str(file_path.stat().st_size))
                self.end_headers()
                with file_path.open("rb") as fh:
                    while True:
                        chunk = fh.read(1024 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                return
            if path.startswith("/files/preview/"):
                name = path.removeprefix("/files/preview/")
                file_path = shared_file_path_from_request(name)
                if file_path is None or not file_path.is_file():
                    text_response(self, "not found", status=404, content_type="text/plain")
                    return
                content_type = shared_file_content_type(file_path)
                if not (content_type.startswith("image/") or content_type.startswith("video/") or content_type == "text/html"):
                    text_response(self, "preview unavailable", status=415, content_type="text/plain")
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                for name, value in preview_security_headers(content_type):
                    self.send_header(name, value)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "private, no-cache, no-transform" if file_path.suffix.lower() in {".html", ".htm"} else "private, max-age=3600")
                self.send_header("Content-Length", str(file_path.stat().st_size))
                self.end_headers()
                with file_path.open("rb") as fh:
                    while True:
                        chunk = fh.read(1024 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                return
            if path.startswith("/files/"):
                name = path.removeprefix("/files/")
                file_path = shared_file_path_from_request(name)
                if file_path is None or not file_path.is_file():
                    text_response(self, "not found", status=404, content_type="text/plain")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream" if file_path.suffix.lower() in {".html", ".htm"} else shared_file_content_type(file_path))
                self.send_header("Content-Disposition", attachment_content_disposition(file_path))
                self.send_header("Cache-Control", "private, no-cache, no-transform" if file_path.suffix.lower() in {".html", ".htm"} else "private, max-age=3600")
                self.send_header("Content-Length", str(file_path.stat().st_size))
                self.end_headers()
                with file_path.open("rb") as fh:
                    while True:
                        chunk = fh.read(1024 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                return
            if path == "/api/files":
                shared_files = list_shared_files()
                json_response(
                    self,
                    {
                        **shared_files,
                        "root": str(SHARED_FILES_DIR),
                        "max_bytes": MAX_SHARED_FILE_BYTES,
                        "chunk_bytes": SHARED_UPLOAD_CHUNK_BYTES,
                    },
                )
                return
            if path == "/api/trace":
                pane = query.get("pane", [""])[0]
                if not pane:
                    json_response(self, {"error": "missing pane"}, status=400)
                    return
                max_spans_raw = query.get("max_spans", ["2000"])[0]
                try:
                    max_spans = int(max_spans_raw)
                except ValueError:
                    json_response(self, {"error": "invalid max_spans"}, status=400)
                    return
                status, payload = trace_response_for_pane(
                    pane,
                    expected_pid=query.get("pane_pid", [""])[0],
                    expected_start_time=query.get("pane_start_time", [""])[0],
                    max_spans=max_spans,
                    known_source_signature=query.get("known_source_signature", [""])[0],
                )
                json_response(self, payload, status=status)
                return
            if path == "/api/active-pane":
                session = query.get("session", [DEFAULT_SESSION])[0]
                if session != DEFAULT_SESSION:
                    json_response(self, {"error": "unsupported session"}, status=400)
                    return
                pane = active_pane(session)
                if pane is None:
                    json_response(self, {"error": "active pane not found"}, status=404)
                    return
                json_response(self, {"active_pane": pane, "captured_at": now_iso()})
                return
            if path == "/api/panes":
                session = query.get("session", [DEFAULT_SESSION])[0]
                if session != DEFAULT_SESSION:
                    json_response(self, {"error": "unsupported session"}, status=400)
                    return
                light = query.get("light", ["0"])[0] in {"1", "true", "yes"}
                # light 模式也算 preview + 给够行数。capture 本就为状态判断
                # 做了(每轮都 capture),preview_from 只是解析已有文本、开销极小 → light 卡片
                # 也实时显示内容,不再每 6s 才更新一次(根治桌面端"暂无最近输出"/内容滞后)。
                # light 参数保留兼容,但不再砍 preview。
                #
                # 短 TTL 缓存(0.9s): build_panes_response 要枚举全部窗口(~700ms-1.2s),而轮询
                # 每 1.2-2.5s 一次、且常有多个标签页/手机同时开 → 并发/密集轮询会各自重算一遍,
                # 把系统 load 顶到 ~90。缓存把这些请求合并成一次计算, 大幅降低持续负载(进而加速
                # 所有请求)。0.9s < 轮询间隔 → 网格新鲜度基本无损。
                try:
                    snapshot = _panes_response_snapshot(session)
                except PanesResponseUnavailable as exc:
                    json_response(
                        self,
                        {
                            "error": str(exc),
                            "stale": True,
                            "snapshot_at": exc.snapshot_at,
                            "snapshot_age_ms": exc.snapshot_age_ms,
                            "captured_at": now_iso(),
                        },
                        status=503,
                    )
                    return
                json_response(
                    self,
                    {
                        "panes": snapshot.panes,
                        "snapshot_at": snapshot.snapshot_at,
                        "snapshot_age_ms": snapshot.snapshot_age_ms,
                        "stale": snapshot.stale,
                        "refreshing": snapshot.refreshing,
                        "refresh_error": snapshot.refresh_error,
                        "captured_at": now_iso(),
                    },
                )
                return
            if path == "/api/jobs":
                pane = query.get("pane", [""])[0]
                status = query.get("status", [""])[0]
                limit = int(query.get("limit", ["50"])[0])
                active_only = query.get("active_only", ["0"])[0] in {"1", "true", "yes"}
                jobs = event_jobs_cached(
                    pane=pane,
                    status=status,
                    limit=limit,
                    include_terminal=not active_only,
                )
                json_response(self, {"jobs": [job_summary(job) for job in jobs], "captured_at": now_iso()})
                return
            if path == "/api/events":
                after = int(query.get("after", ["0"])[0] or 0)
                limit = int(query.get("limit", ["100"])[0] or 100)
                pane = query.get("pane", [""])[0]
                job_id = query.get("job_id", [""])[0]
                events, last_id = event_ledger.read_events(after=after, limit=limit, pane=pane, job_id=job_id)
                json_response(
                    self,
                    {
                        "events": events,
                        "last_id": last_id,
                        "head_id": event_ledger.current_committed_event_id(),
                        "captured_at": now_iso(),
                    },
                )
                return
            if path == "/api/capture":
                pane = query.get("pane", [""])[0]
                history = int(query.get("history", ["900"])[0])
                include_raw = query.get("raw", ["0"])[0] in {"1", "true", "yes"}
                compact_payload = query.get("compact", ["0"])[0] in {"1", "true", "yes"}
                if not pane:
                    json_response(self, {"error": "missing pane"}, status=400)
                    return
                pane_info = pane_by_id(pane)
                # Detail/timeline view must mirror the actual tmux geometry.
                # Keep wrapped rows intact here; the grid and identity probes
                # continue using the faster logical (-J) capture path.
                try:
                    text = capture(pane, history=history, join_wrapped=False)
                except TypeError as exc:
                    # A few embedders/tests replace capture() with the legacy
                    # two-argument callable. Keep that compatibility boundary
                    # without weakening the real visual-capture path.
                    if "join_wrapped" not in str(exc):
                        raise
                    text = capture(pane, history=history)
                blocks, history_meta = blocks_for_pane_with_meta(pane_info, text)
                status = (
                    infer_pane_status(
                        pane, text, pane_info.command, pane_info.kind,
                        pane_info.ai_alive, pane_info.ai_transcript, pane_info.pane_pid,
                        pane_info.ai_session_id,
                    )
                    if pane_info
                    else infer_status(text, "")
                )
                active_job = latest_jobs_by_pane_cached(include_terminal=False).get(pane) or {}
                runtime_observation = provider_runtime_observation(active_job, status) if active_job else {}
                sync_status = runtime_observation.get("status") or status
                job = sync_pane_job_status(pane, pane_info.target if pane_info else pane, sync_status)
                if not job:
                    # Keep the latest terminal state in capture responses.  If
                    # we return an empty object here, a refreshed tab forgets
                    # that the newest Cards turn already completed and may
                    # restart its wait clock from a stale pane-level signal.
                    job = latest_job_summary_for_pane(pane, include_terminal=True)
                job = sync_cards_job_response_started_from_blocks(
                    pane,
                    pane_info.target if pane_info else pane,
                    blocks,
                    job,
                )
                subagents_running = (
                    pane_running_subagents(pane_info.kind, pane_info.ai_alive, pane_info.ai_transcript, pane_info.pane_pid)
                    if pane_info
                    else 0
                )
                # 主对话写完回复不等于活干完了：子智能体还在跑时，这一单不能记成完成。
                if not subagents_running:
                    job = sync_cards_job_completion_from_blocks(
                        pane,
                        pane_info.target if pane_info else pane,
                        blocks,
                        job,
                    )
                payload = {
                    "pane": pane,
                    "raw_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "blocks": blocks,
                    "history": history_meta,
                    "status": status,
                    "job": job,
                    "captured_at": now_iso(),
                }
                if subagents_running:
                    payload["subagents_running"] = subagents_running
                if runtime_observation:
                    payload["runtime_status"] = runtime_observation.get("status", "")
                    payload["runtime_status_source"] = runtime_observation.get("source", "")
                    payload["runtime_status_confidence"] = runtime_observation.get("confidence", "")
                # Compatibility: tabs opened before this deployment still
                # expect ``raw`` on an unqualified request. New clients opt
                # into the compact response; Raw mode explicitly asks for it.
                if include_raw or not compact_payload:
                    payload["raw"] = text
                json_response(self, payload)
                return
            if path == "/api/prefs":
                json_response(self, {"prefs": read_prefs(), "asset_version": asset_version()})
                return
            if path == "/api/history_before":
                # Phase 3a: on-demand pagination for Claude history that has
                # already scrolled off parse_transcript_tail's max_blocks=120
                # hot-path window. Separate from /api/capture; never called on
                # the 3x/second poll.
                pane = query.get("pane", [""])[0]
                if not pane:
                    json_response(self, {"error": "missing pane"}, status=400)
                    return
                count = int(query.get("count", ["60"])[0])
                cursor_raw = query.get("cursor", [""])[0].strip()
                cursor = int(cursor_raw) if cursor_raw else None
                # Phase 3b: same endpoint/param shape as Claude's pagination
                # above, dispatched to the Codex rollout-JSONL path for Codex
                # panes - the response shape (blocks/next_cursor/has_more/
                # total/transcript_id/reason) is identical, only the
                # underlying transcript source differs.
                pane_info = pane_by_id(pane)
                if pane_info and pane_info.kind == "Codex":
                    rollout_path, resolve_meta = codex_rollout_path_for_pane_id(pane)
                    if not rollout_path:
                        json_response(self, {
                            "blocks": [],
                            "next_cursor": None,
                            "has_more": False,
                            "total": 0,
                            "transcript_id": "",
                            "reason": str(resolve_meta.get("reason") or "no-transcript"),
                        })
                        return
                    result = codex_history_before(rollout_path, cursor, count)
                    result["transcript_id"] = Path(rollout_path).stem
                    result["reason"] = ""
                    json_response(self, result)
                    return
                transcript_path = transcript_path_for_pane_id(pane)
                if not transcript_path:
                    json_response(self, {
                        "blocks": [],
                        "next_cursor": None,
                        "has_more": False,
                        "total": 0,
                        "transcript_id": "",
                        "reason": "no-transcript",
                    })
                    return
                shared = _shared_live_transcript_panes(transcript_path, pane)
                if shared:
                    # The live capture path deliberately falls back to the
                    # pane-scoped tmux screen when a transcript is shared.  Do
                    # not let pagination silently reintroduce that ambiguous
                    # transcript above the screen blocks: it produces a mixed,
                    # duplicated timeline that belongs to neither source.
                    json_response(self, {
                        "blocks": [],
                        "next_cursor": None,
                        "has_more": False,
                        "total": 0,
                        "transcript_id": Path(transcript_path).stem,
                        "reason": "shared-transcript",
                        "shared_panes": shared,
                    })
                    return
                result = history_before(transcript_path, cursor, count)
                result["transcript_id"] = Path(transcript_path).stem
                result["reason"] = ""
                json_response(self, result)
                return
            text_response(self, "not found", status=404, content_type="text/plain")
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001 - API should return JSON errors
            json_response(self, {"error": str(exc)}, status=500)

    def do_HEAD(self) -> None:
        if not self.authorized():
            self.require_auth()
            return
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: C901 - one dispatcher for every write endpoint
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if URL_PREFIX and path == URL_PREFIX:
            path = "/"
        elif URL_PREFIX and path.startswith(URL_PREFIX + "/"):
            path = path[len(URL_PREFIX):] or "/"
        try:
            if not self.authorized():
                self.require_auth()
                return
            if path not in {
                "/api/send", "/api/upload", "/api/key", "/api/choose", "/api/prefs", "/api/prefs/favorite",
                "/api/prefs/category", "/api/prefs/category/delete", "/api/prefs/pane", "/api/prefs/group",
                "/api/pane/close", "/api/files/upload", "/api/files/upload-chunk", "/api/files/delete",
            }:
                json_response(self, {"error": "not found"}, status=404)
                return
            refused = cross_site_write_refusal(self.headers, path)
            if refused:
                json_response(self, {"error": refused}, status=403)
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            if path == "/api/files/upload-chunk":
                json_response(self, save_shared_file_chunk(self, query, length))
                return
            if path == "/api/files/upload":
                filename = query.get("filename", [""])[0] or self.headers.get("X-File-Name", "") or "file"
                if length <= 0 or (MAX_SHARED_FILE_BYTES > 0 and length > MAX_SHARED_FILE_BYTES):
                    json_response(self, {"error": "invalid request body"}, status=400)
                    return
                json_response(self, save_shared_file_stream(self, filename, length))
                return
            if path == "/api/prefs":
                max_length = 500_000
            elif path in {
                "/api/send", "/api/key", "/api/choose", "/api/prefs/favorite", "/api/prefs/category",
                "/api/prefs/category/delete", "/api/prefs/pane", "/api/prefs/group", "/api/pane/close",
            }:
                max_length = 20_000
            elif path == "/api/files/delete":
                max_length = 20_000
            else:
                max_length = MAX_UPLOAD_BYTES * 2
            if length <= 0 or length > max_length:
                json_response(self, {"error": "invalid request body"}, status=400)
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if path == "/api/prefs":
                if not isinstance(payload, dict):
                    json_response(self, {"error": "prefs must be an object"}, status=400)
                    return
                json_response(self, {"ok": True, "prefs": merge_prefs(payload)})
                return
            if path == "/api/prefs/favorite":
                if not isinstance(payload, dict):
                    json_response(self, {"error": "favorite mutation must be an object"}, status=400)
                    return
                if not isinstance(payload.get("favorite"), bool):
                    json_response(self, {"error": "favorite must be boolean"}, status=400)
                    return
                try:
                    prefs = update_favorite_pref(
                        str(payload.get("key") or ""),
                        bool(payload.get("favorite", False)),
                        str(payload.get("legacy_key") or ""),
                    )
                except ValueError as exc:
                    json_response(self, {"error": str(exc)}, status=400)
                    return
                json_response(self, {"ok": True, "prefs": prefs})
                return
            if path == "/api/prefs/category":
                if not isinstance(payload, dict):
                    json_response(self, {"error": "category mutation must be an object"}, status=400)
                    return
                if not isinstance(payload.get("category"), str):
                    json_response(self, {"error": "category must be a string"}, status=400)
                    return
                if "create_category" in payload and not isinstance(payload.get("create_category"), bool):
                    json_response(self, {"error": "create_category must be boolean"}, status=400)
                    return
                try:
                    prefs = update_category_pref(
                        str(payload.get("key") or ""),
                        str(payload.get("category") or ""),
                        str(payload.get("legacy_key") or ""),
                        create_category=bool(payload.get("create_category", False)),
                    )
                except ValueError as exc:
                    json_response(self, {"error": str(exc)}, status=400)
                    return
                json_response(self, {"ok": True, "prefs": prefs})
                return
            if path == "/api/prefs/category/delete":
                if not isinstance(payload, dict) or not isinstance(payload.get("category"), str):
                    json_response(self, {"error": "category deletion requires a category string"}, status=400)
                    return
                try:
                    prefs, unassigned = delete_category_pref(str(payload.get("category") or ""))
                except ValueError as exc:
                    json_response(self, {"error": str(exc)}, status=400)
                    return
                json_response(self, {
                    "ok": True,
                    "category": str(payload.get("category") or "").strip(),
                    "unassigned": unassigned,
                    "prefs": prefs,
                })
                return
            if path == "/api/prefs/pane":
                if not isinstance(payload, dict):
                    json_response(self, {"error": "pane preference mutation must be an object"}, status=400)
                    return
                has_favorite = "favorite" in payload
                has_category = "category" in payload
                has_alias = "alias" in payload
                if not has_favorite and not has_category and not has_alias:
                    json_response(self, {"error": "favorite, category, or alias mutation is required"}, status=400)
                    return
                if has_favorite and not isinstance(payload.get("favorite"), bool):
                    json_response(self, {"error": "favorite must be boolean"}, status=400)
                    return
                if has_category and not isinstance(payload.get("category"), str):
                    json_response(self, {"error": "category must be a string"}, status=400)
                    return
                if has_alias and not isinstance(payload.get("alias"), str):
                    json_response(self, {"error": "alias must be a string"}, status=400)
                    return
                if "create_category" in payload and not isinstance(payload.get("create_category"), bool):
                    json_response(self, {"error": "create_category must be boolean"}, status=400)
                    return
                pane_id = str(payload.get("pane") or "")
                expected_pid = str(payload.get("pane_pid") or "")
                expected_start_time = str(payload.get("pane_start_time") or "")
                if not pane_id or not expected_pid or not expected_start_time:
                    json_response(self, {"error": "pane identity is required"}, status=400)
                    return
                pane = pane_by_id(pane_id)
                if pane is None:
                    json_response(self, {"error": f"pane not found or not in {DEFAULT_SESSION}: {pane_id}"}, status=404)
                    return
                try:
                    validate_pane_instance(
                        pane,
                        expected_pid,
                        expected_start_time,
                    )
                    prefs, key = update_pane_preferences(
                        pane,
                        favorite=bool(payload.get("favorite")) if has_favorite else None,
                        category=str(payload.get("category") or "") if has_category else None,
                        alias=str(payload.get("alias") or "") if has_alias else None,
                        create_category=bool(payload.get("create_category", False)),
                    )
                except ValueError as exc:
                    json_response(self, {"error": str(exc)}, status=400)
                    return
                except PaneIdentityConflict as exc:
                    json_response(self, {"error": str(exc)}, status=409)
                    return
                json_response(self, {
                    "ok": True,
                    "pane": pane.pane_id,
                    "target": pane.target,
                    "key": key,
                    "favorite": bool(prefs.get("paneFavorites", {}).get(key)) if isinstance(prefs.get("paneFavorites"), dict) else False,
                    "category": str(prefs.get("paneCategories", {}).get(key) or "") if isinstance(prefs.get("paneCategories"), dict) else "",
                    "alias": str(prefs.get("paneAliases", {}).get(key) or "") if isinstance(prefs.get("paneAliases"), dict) else "",
                    "prefs": prefs,
                })
                return
            if path == "/api/prefs/group":
                if not isinstance(payload, dict):
                    json_response(self, {"error": "group mutation must be an object"}, status=400)
                    return
                category = payload.get("category")
                members = payload.get("members")
                if not isinstance(category, str):
                    json_response(self, {"error": "category must be a string"}, status=400)
                    return
                if not isinstance(members, list) or not members or len(members) > 64:
                    json_response(self, {"error": "members must contain 1 to 64 panes"}, status=400)
                    return
                if "create_category" in payload and not isinstance(payload.get("create_category"), bool):
                    json_response(self, {"error": "create_category must be boolean"}, status=400)
                    return
                panes: list[Pane] = []
                seen: set[str] = set()
                try:
                    for member in members:
                        if not isinstance(member, dict):
                            raise ValueError("each member must be an object")
                        pane_id = str(member.get("pane") or "")
                        expected_pid = str(member.get("pane_pid") or "")
                        expected_start_time = str(member.get("pane_start_time") or "")
                        if not pane_id or not expected_pid or not expected_start_time:
                            raise ValueError("complete pane identity is required for every member")
                        if pane_id in seen:
                            raise ValueError(f"duplicate pane in group: {pane_id}")
                        pane = pane_by_id(pane_id)
                        if pane is None:
                            json_response(
                                self,
                                {"error": f"pane not found or not in {DEFAULT_SESSION}: {pane_id}"},
                                status=404,
                            )
                            return
                        validate_pane_instance(pane, expected_pid, expected_start_time)
                        seen.add(pane_id)
                        panes.append(pane)
                    prefs, keys = update_pane_group_preferences(
                        panes,
                        category,
                        create_category=bool(payload.get("create_category", False)),
                    )
                except ValueError as exc:
                    json_response(self, {"error": str(exc)}, status=400)
                    return
                except PaneIdentityConflict as exc:
                    json_response(self, {"error": str(exc)}, status=409)
                    return
                json_response(self, {
                    "ok": True,
                    "category": category.strip(),
                    "members": [
                        {"pane": pane.pane_id, "target": pane.target, "key": key}
                        for pane, key in zip(panes, keys)
                    ],
                    "prefs": prefs,
                })
                return
            if path == "/api/pane/close":
                if not isinstance(payload, dict):
                    json_response(self, {"error": "close request must be an object"}, status=400)
                    return
                try:
                    result = close_pane(
                        str(payload.get("pane") or ""),
                        str(payload.get("pane_pid") or ""),
                        str(payload.get("pane_start_time") or ""),
                    )
                except ValueError as exc:
                    json_response(self, {"error": str(exc)}, status=400)
                    return
                except FileNotFoundError as exc:
                    json_response(self, {"error": str(exc)}, status=404)
                    return
                except PaneIdentityConflict as exc:
                    json_response(self, {"error": str(exc)}, status=409)
                    return
                json_response(self, result)
                return
            if path == "/api/upload":
                json_response(self, save_uploaded_image(payload))
                return
            if path == "/api/files/delete":
                json_response(self, delete_shared_file(payload))
                return
            if path == "/api/choose":
                pane_id = str(payload.get("pane", ""))
                if not pane_id:
                    json_response(self, {"error": "missing pane"}, status=400)
                    return
                pane = choose_nav_option(pane_id, str(payload.get("text", "")))
                PANE_ACTIVITY_CACHE.pop(pane_id, None)
                json_response(self, {"ok": True, "pane": pane.pane_id, "target": pane.target})
                return
            if path == "/api/key":
                pane_id = str(payload.get("pane", ""))
                key = str(payload.get("key", ""))
                if not pane_id:
                    json_response(self, {"error": "missing pane"}, status=400)
                    return
                pane = send_key_to_pane(pane_id, key)
                PANE_ACTIVITY_CACHE.pop(pane_id, None)
                json_response(self, {"ok": True, "pane": pane.pane_id, "target": pane.target, "key": key})
                return
            pane_id = str(payload.get("pane", ""))
            text = str(payload.get("text", ""))
            enter = bool(payload.get("enter", False))
            try:
                result = send_message_with_receipt(
                    pane_id,
                    text,
                    enter,
                    job_id=str(payload.get("job_id") or ""),
                    expected_pid=str(payload.get("pane_pid") or ""),
                    expected_start_time=str(payload.get("pane_start_time") or ""),
                )
            except ValueError as exc:
                json_response(self, {"error": str(exc)}, status=400)
                return
            except (PaneIdentityConflict, SendRequestConflict) as exc:
                json_response(self, {"error": str(exc)}, status=409)
                return
            json_response(self, result)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001 - API should return JSON errors
            json_response(self, {"error": str(exc)}, status=500)

    def authorized(self) -> bool:
        auth = load_auth()
        if auth is None:
            return False
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        except Exception:
            return False
        expected = f"{auth[0]}:{auth[1]}"
        return hmac.compare_digest(decoded, expected)

    def require_auth(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="agent-bus"')
        self.send_header("Content-Length", "0")
        self.end_headers()


AUTO_APPROVE_INTERVAL = float(os.environ.get("CARDS_AUTO_APPROVE_INTERVAL", "6"))


def dashboard_auto_approve_requested() -> bool:
    """The background auto-approve loop is opt-in: CARDS_AUTO_APPROVE=1 or
    AGENT_BUS_AUTO_APPROVE=1 starts it (and CARDS_AUTO_APPROVE=1 also enables
    each pass for the dashboard even when the bus config switch is off)."""
    cards = os.environ.get("CARDS_AUTO_APPROVE", "").strip()
    if cards == "0":
        return False
    return cards == "1" or os.environ.get("AGENT_BUS_AUTO_APPROVE", "").strip() == "1"
AUTO_APPROVE_COOLDOWN = 30.0
_AUTO_APPROVE_RECENT: dict[tuple[str, str], float] = {}


def auto_approve_waiting_panes(now: float | None = None) -> list[dict[str, str]]:
    """Answer permission-type dialogs in every Cards window by the opt-in policy
    in scripts/dialogs.py (enabled by CARDS_AUTO_APPROVE=1, or by the bus switch
    ``AGENT_BUS_AUTO_APPROVE`` / config ``auto_approve_permissions``).  Work
    questions are left for the user; the choice card stays visible for them.
    One attempt per dialog per 30 s; every answer is recorded as a
    ``pane_prompt_auto_answered`` ledger event.  A pane someone just used (from
    this page or an attached terminal), one in copy mode, or one another sender
    is driving is skipped this round."""
    now = time.time() if now is None else now
    cards = os.environ.get("CARDS_AUTO_APPROVE", "").strip()
    if cards == "0" or (cards != "1" and not cli_bridge.auto_approve_enabled()):
        return []
    answered: list[dict[str, str]] = []
    for pane in list_panes(DEFAULT_SESSION, include_preview=True):
        if pane.kind not in {"Claude", "Codex"} or pane.status != "waiting":
            continue
        pane_id = pane.pane_id
        if now - _WEB_INPUT_AT.get(pane_id, 0.0) < dialogs.HUMAN_ACTIVE_SECONDS:
            continue

        def screen(pane_id: str = pane_id) -> str:
            return capture(pane_id, history=60, join_wrapped=False)

        dialog = dialogs.read_dialog(screen())
        if dialog is None or dialogs.auto_answer_index(dialog) is None:
            continue
        key = (pane_id, dialog.question[:200])
        if now - _AUTO_APPROVE_RECENT.get(key, 0.0) < AUTO_APPROVE_COOLDOWN:
            continue
        _AUTO_APPROVE_RECENT[key] = now

        def send(key_name: str, pane_id: str = pane_id) -> None:
            sent = run_tmux(["send-keys", "-t", pane_id, key_name])
            if sent.returncode != 0:
                raise RuntimeError(sent.stderr.strip() or f"tmux send {key_name} failed")

        try:
            result = dialogs.auto_approve(capture=screen, send=send, pane_id=pane_id, tmux=tmux_query)
        except dialogs.PaneBusy:
            _AUTO_APPROVE_RECENT.pop(key, None)  # not attempted; look again next round
            continue
        except (ValueError, RuntimeError) as exc:
            event_ledger.append_event(
                "pane_prompt_auto_answer_failed", pane=pane_id, target=pane.target, source="card-dashboard",
                message=str(exc)[:300],
            )
            continue
        if result:
            PANE_ACTIVITY_CACHE.pop(pane_id, None)
            event_ledger.append_event(
                "pane_prompt_auto_answered", pane=pane_id, target=pane.target, source="card-dashboard",
                message=f"{result['kind']}: {result['answer']}"[:300], data=dict(result),
            )
            answered.append({"pane": pane_id, **result})
    return answered


IDENTITY_RECONCILE_INTERVAL = float(os.environ.get("CARDS_IDENTITY_RECONCILE_INTERVAL", "60"))


def _identity_loop() -> None:
    while True:
        try:
            reconcile_claude_stamps()
        except Exception as exc:  # noqa: BLE001 - keep the loop alive; the error is logged
            print(f"identity reconcile error: {exc}", file=sys.stderr, flush=True)
        time.sleep(IDENTITY_RECONCILE_INTERVAL)


def _auto_approve_loop() -> None:
    while True:
        try:
            auto_approve_waiting_panes()
        except Exception as exc:  # noqa: BLE001 - keep the loop alive; the error is logged
            print(f"auto-approve loop error: {exc}", file=sys.stderr, flush=True)
        time.sleep(AUTO_APPROVE_INTERVAL)


USAGE = """usage: python3 dashboard/server.py [-h]

Serve the Agent Bus Cards dashboard (no options; configured by environment).

  TMUX_CARD_HOST / TMUX_CARD_PORT   bind address (default 127.0.0.1:7795)
  TMUX_CARD_URL_PREFIX              URL prefix (default /cards)
  TMUX_CARD_SESSION                 tmux session to show (default secretary_web)
  WEBTERM_ENV                       Basic Auth file with WEBTERM_USER / WEBTERM_PASS
                                    (or TMUX_CARD_USER / TMUX_CARD_PASS); every
                                    request is refused while none is configured
  CARDS_AUTO_APPROVE=1              opt in to the background auto-approve loop
  CARDS_IDENTITY_RECONCILE_INTERVAL seconds between stale session-stamp repairs
                                    (default 60; 0 disables)

See docs/dashboard.md and docs/configuration.md for the full list.
"""


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if any(arg in {"-h", "--help"} for arg in args):
        print(USAGE, end="")
        return 0
    if args:
        print(f"unexpected arguments: {' '.join(args)}\n\n{USAGE}", end="", file=sys.stderr)
        return 2
    # Build the only large read-side index before accepting traffic.  The
    # ledger is append-only, so later requests consume just its new tail; this
    # keeps the first real browser from paying the one-time cold scan.
    try:
        _job_records_from_events_cached()
    except Exception as exc:  # noqa: BLE001 - startup remains available and observable
        print(f"card dashboard cache warmup failed: {exc}", file=sys.stderr, flush=True)
    if dashboard_auto_approve_requested():
        threading.Thread(target=_auto_approve_loop, name="auto-approve", daemon=True).start()
    if IDENTITY_RECONCILE_INTERVAL > 0:
        threading.Thread(target=_identity_loop, name="identity-reconcile", daemon=True).start()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"tmux card dashboard listening on http://{HOST}:{PORT}", flush=True)
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
