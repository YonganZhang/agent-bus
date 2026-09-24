#!/usr/bin/env python3
"""Bounded, read-only trace summaries for Claude Code and Codex JSONL logs.

This module deliberately has no dependency on the Cards server or Agent Bus:

* source files are opened read-only and must resolve below the provider's
  normal transcript root;
* no hook, database, cache, or sidecar file is created;
* only bounded, redacted summaries are returned -- never raw JSON payloads;
* inferred timing/attachment is labelled instead of presented as authoritative.

The public entry point is :func:`build_trace`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Callable, Iterator


CLAUDE_ROOT = Path.home() / ".claude" / "projects"
CODEX_ROOT = Path.home() / ".codex" / "sessions"

HARD_MAX_SPANS = 2_000
MAX_RECORDS = 50_000
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 2 * 1024 * 1024
MAX_SUBAGENT_FILES = 32
MAX_NAME_CHARS = 80
MAX_SUMMARY_CHARS = 500
MAX_DURATION_MS = 31 * 24 * 60 * 60 * 1_000

_ERROR_TYPES = {
    "tool_error",
    "model_error",
    "command_error",
    "timeout",
    "interrupted",
    "parse_error",
    "unknown",
}
_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "reasoning_tokens",
    "total_tokens",
)
_CODEX_SYSTEM_PREFIXES = (
    "<environment_context>",
    "<subagent_notification>",
    "<codex_internal_context",
    "<turn_aborted>",
    "<skill>",
    "# AGENTS.md instructions",
)
_CLAUDE_SYSTEM_PREFIXES = (
    "<task-notification>",
    "<system-reminder>",
    "<task-",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
)

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
    r"(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)",
    re.IGNORECASE | re.DOTALL,
)
_TOKEN_RES = (
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}", re.IGNORECASE),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}", re.IGNORECASE),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{8,}", re.IGNORECASE),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{10,}", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
_BASIC_AUTH_RE = re.compile(r"\bBasic\s+[A-Za-z0-9+/=]{8,}", re.IGNORECASE)
_CREDENTIAL_HEADER_RE = re.compile(
    r"(?im)^\s*(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*[^\r\n]+"
)
_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}"
    r"\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)
_COMMON_TOKEN_RE = re.compile(
    r"\b(?:xox[baprs]-[A-Za-z0-9-]{10,}|npm_[A-Za-z0-9]{12,}|"
    r"AIza[0-9A-Za-z_-]{20,}|(?:rk|sk|pk)_live_[A-Za-z0-9]{12,})\b",
    re.IGNORECASE,
)
_HIGH_ENTROPY_CANDIDATE_RE = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z0-9_+/=-]{32,}(?![A-Za-z0-9_])"
)
_SECRET_ASSIGN_RE = re.compile(
    r"""(?ix)
    ["']?\b
    (?:api[_-]?key|x-api-key|access[_-]?token|refresh[_-]?token|
       id[_-]?token|auth[_-]?token|token|secret(?:[_-]?key)?|
       private[_-]?key|client[_-]?secret|password|passwd|credential)
    \b["']?\s*[:=]\s*
    (?:
        "(?:\\.|[^"])*" |
        '(?:\\.|[^'])*' |
        [^\s,;}\]]+
    )
    """
)
_ENV_ASSIGN_RE = re.compile(
    r"(?<![\w])(?:export\s+)?[A-Z][A-Z0-9_]{1,63}\s*=\s*"
    r"(?:\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|[^\s,;]+)"
)
_ENV_REF_RE = re.compile(r"\$(?:\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*)")
_WINDOWS_PATH_RE = re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:\\(?:[^\\\s\"']+\\?)+")
_HOME_PATH_RE = re.compile(r"(?<![\w])~/(?:[^\s\"'`]+)")
# Do not match URL slashes: a URL's first slash follows ":" and later slashes
# follow "/" or a hostname character.
_UNIX_PATH_RE = re.compile(r"(?<![A-Za-z0-9:/])/(?:[^\s\"'`]+)")


class TracePathError(ValueError):
    """Raised when a requested transcript path is outside the safe roots."""


@dataclass
class _Budget:
    bytes_read: int = 0
    records_read: int = 0
    malformed_records: int = 0
    oversized_records: int = 0
    ignored_records: int = 0
    unreadable_subagents: int = 0
    unreadable_files: int = 0
    parse_error_records: int = 0
    input_truncated: bool = False
    trailing_partial_records: int = 0


@dataclass
class _TokenUsage:
    values: dict[str, int] = field(default_factory=dict)
    seen: bool = False

    def record(self, value: object, *, cumulative: bool = False) -> None:
        normalized = _normalize_token_usage(value)
        if not normalized:
            return
        self.seen = True
        for key, number in normalized.items():
            if cumulative:
                self.values[key] = number
            else:
                self.values[key] = self.values.get(key, 0) + number


@dataclass
class _Span:
    span_id: str
    parent_id: str | None
    kind: str
    name: str
    start_ms: int
    end_ms: int
    status: str = "ok"
    input_summary: str = ""
    output_summary: str = ""
    approx: bool = False
    join_quality: str = "structural"
    incomplete: bool = False
    detached: bool = False
    agent_name: str = ""
    error_type: str = ""
    error_message: str = ""


@dataclass
class _Builder:
    session_key: str
    max_spans: int
    spans: list[_Span] = field(default_factory=list)
    by_id: dict[str, _Span] = field(default_factory=dict)
    truncated: bool = False

    @property
    def full(self) -> bool:
        return len(self.spans) >= self.max_spans

    def stable_id(self, record_key: str, kind: str) -> str:
        material = f"{self.session_key}\0{record_key}\0{kind}".encode("utf-8", "replace")
        return hashlib.sha256(material).hexdigest()[:24]

    def add(
        self,
        *,
        record_key: str,
        parent_id: str | None,
        kind: str,
        name: str,
        start_ms: int,
        end_ms: int | None = None,
        status: str = "ok",
        input_summary: object = "",
        output_summary: object = "",
        approx: bool = False,
        join_quality: str = "structural",
        incomplete: bool = False,
        detached: bool = False,
        agent_name: object = "",
        error_type: str = "",
        error_message: object = "",
    ) -> str | None:
        if self.full:
            self.truncated = True
            return None
        span_id = self.stable_id(record_key, kind)
        existing = self.by_id.get(span_id)
        if existing is not None:
            return existing.span_id
        start = _safe_ms(start_ms)
        finish = start if end_ms is None else max(start, _safe_ms(end_ms))
        normalized_status = status if status in {"ok", "error", "unknown"} else "unknown"
        normalized_error_type = (
            error_type if normalized_status == "error" and error_type in _ERROR_TYPES else ""
        )
        if normalized_status == "error" and not normalized_error_type:
            normalized_error_type = "unknown"
        span = _Span(
            span_id=span_id,
            parent_id=parent_id if parent_id in self.by_id else None,
            kind=kind,
            name=_sanitize_text(name, MAX_NAME_CHARS) or kind.lower(),
            start_ms=start,
            end_ms=finish,
            status=normalized_status,
            input_summary=_sanitize_text(input_summary, MAX_SUMMARY_CHARS),
            output_summary=_sanitize_text(output_summary, MAX_SUMMARY_CHARS),
            approx=bool(approx),
            join_quality=(
                join_quality
                if join_quality in {"structural", "semi", "heuristic", "orphan"}
                else "orphan"
            ),
            incomplete=bool(incomplete),
            detached=bool(detached),
            agent_name=_sanitize_text(agent_name, MAX_NAME_CHARS),
            error_type=normalized_error_type,
            error_message=(
                _sanitize_text(error_message, MAX_SUMMARY_CHARS)
                if normalized_status == "error"
                else ""
            ),
        )
        self.spans.append(span)
        self.by_id[span_id] = span
        return span_id

    def close(
        self,
        span_id: str | None,
        *,
        end_ms: int,
        status: str | None = None,
        output_summary: object | None = None,
        approx: bool | None = None,
        incomplete: bool | None = None,
        error_type: str | None = None,
        error_message: object | None = None,
    ) -> None:
        if span_id is None:
            return
        span = self.by_id.get(span_id)
        if span is None:
            return
        span.end_ms = max(span.start_ms, _safe_ms(end_ms))
        if status in {"ok", "error", "unknown"}:
            span.status = status
        if output_summary is not None:
            span.output_summary = _sanitize_text(output_summary, MAX_SUMMARY_CHARS)
        if approx is not None:
            span.approx = bool(approx)
        if incomplete is not None:
            span.incomplete = bool(incomplete)
        if span.status == "error":
            if error_type in _ERROR_TYPES:
                span.error_type = error_type
            elif not span.error_type:
                span.error_type = "unknown"
            if error_message is not None:
                span.error_message = _sanitize_text(error_message, MAX_SUMMARY_CHARS)
        else:
            span.error_type = ""
            span.error_message = ""


@dataclass(frozen=True)
class TraceAdapter:
    """Small provider contract; adapters share all safety and rendering policy."""

    source: str
    root_provider: Callable[[], Path]
    accepts_path: Callable[[Path], bool]
    parse: Callable[["_Builder", "_Budget", "_TokenUsage", Path, Path, str], None]


def build_trace(source: str, transcript_path: str | os.PathLike[str], max_spans: int = 2_000) -> dict[str, Any]:
    """Build a bounded trace summary from one provider-owned JSONL transcript.

    ``source`` must be ``"claude-code"`` or ``"codex"``. The path is
    realpath-checked beneath ``~/.claude/projects`` or ``~/.codex/sessions``;
    symlink escapes are rejected. ``max_spans`` is clamped to 1..2000.

    Returned strings are bounded and redacted. No original JSON object,
    absolute path, environment value, API token, or private key is returned.
    """

    adapter = TRACE_ADAPTERS.get(source)
    if adapter is None:
        supported = " or ".join(sorted(TRACE_ADAPTERS))
        raise ValueError(f"source must be {supported}")
    effective_max = _bounded_max_spans(max_spans)
    path, root = _resolve_safe_path(adapter, transcript_path)
    relative = path.relative_to(root).as_posix()
    session_key = hashlib.sha256(f"{source}\0{relative}".encode("utf-8")).hexdigest()
    session_id = session_key[:24]
    builder = _Builder(session_key=session_key, max_spans=effective_max)
    budget = _Budget()
    usage = _TokenUsage()
    root_id = builder.add(
        record_key="session",
        parent_id=None,
        kind="SESSION",
        name=f"{source} session",
        start_ms=0,
        approx=True,
        incomplete=True,
    )
    if root_id is None:  # max_spans is clamped to at least one
        raise RuntimeError("unable to allocate session span")

    adapter.parse(builder, budget, usage, path, root, root_id)

    if budget.input_truncated:
        builder.truncated = True
    _finalize_root(builder, root_id, budget)
    return _render_trace(source, session_id, builder, budget, usage)


def _bounded_max_spans(value: object) -> int:
    if isinstance(value, bool):
        return 1
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = HARD_MAX_SPANS
    return min(HARD_MAX_SPANS, max(1, parsed))


def _resolve_safe_path(
    adapter: TraceAdapter, transcript_path: str | os.PathLike[str]
) -> tuple[Path, Path]:
    root_hint = adapter.root_provider()
    try:
        root = root_hint.expanduser().resolve(strict=True)
        candidate = Path(transcript_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        raise TracePathError("transcript path is unavailable") from None
    try:
        candidate.relative_to(root)
    except ValueError:
        raise TracePathError("transcript path is outside the allowed source root") from None
    if not candidate.is_file() or candidate.suffix != ".jsonl":
        raise TracePathError("transcript path is not an allowed JSONL file")
    if not adapter.accepts_path(candidate):
        raise TracePathError("transcript path is not allowed for this source")
    return candidate, root


def _iter_jsonl(path: Path, budget: _Budget) -> Iterator[tuple[int, dict[str, Any]]]:
    """Yield bounded JSON objects without ever reading an unbounded line."""

    try:
        fh = path.open("rb")
    except OSError:
        budget.input_truncated = True
        budget.unreadable_files += 1
        return
    with fh:
        line_no = 0
        while budget.records_read < MAX_RECORDS and budget.bytes_read < MAX_TOTAL_BYTES:
            remaining = MAX_TOTAL_BYTES - budget.bytes_read
            raw = fh.readline(min(MAX_LINE_BYTES + 1, remaining + 1))
            if not raw:
                return
            line_no += 1
            budget.bytes_read += len(raw)
            if len(raw) > MAX_LINE_BYTES or (
                not raw.endswith(b"\n") and len(raw) >= remaining
            ):
                budget.oversized_records += 1
                while raw and not raw.endswith(b"\n") and budget.bytes_read < MAX_TOTAL_BYTES:
                    chunk = fh.readline(min(64 * 1024, MAX_TOTAL_BYTES - budget.bytes_read))
                    if not chunk:
                        break
                    budget.bytes_read += len(chunk)
                    raw = chunk
                if budget.bytes_read >= MAX_TOTAL_BYTES:
                    budget.input_truncated = True
                    return
                continue
            budget.records_read += 1
            try:
                value = json.loads(raw.decode("utf-8", "replace"))
            except (json.JSONDecodeError, UnicodeError):
                # Provider logs are append-only. A malformed final fragment
                # without a newline is normally the writer's in-flight record,
                # not a corrupt completed event; keep the earlier spans and
                # mark the snapshot incomplete instead of failing the trace.
                if not raw.endswith(b"\n"):
                    budget.trailing_partial_records += 1
                else:
                    budget.malformed_records += 1
                    budget.parse_error_records += 1
                continue
            if not isinstance(value, dict):
                budget.ignored_records += 1
                continue
            yield line_no, value
        budget.input_truncated = True


def _parse_claude(
    builder: _Builder,
    budget: _Budget,
    usage: _TokenUsage,
    path: Path,
    root: Path,
    root_id: str,
) -> None:
    tool_ids: dict[str, str] = {}
    _parse_claude_file(
        builder,
        budget,
        usage,
        path,
        root_id,
        file_tag="main",
        join_quality="structural",
        tool_ids=tool_ids,
        agent_name="",
    )
    if builder.full:
        builder.truncated = True
        return

    # Newer Claude Code layout:
    # <project>/<session-id>/subagents/agent-*.jsonl
    subagents_dir = path.parent / path.stem / "subagents"
    try:
        resolved_subagents = subagents_dir.resolve(strict=True)
        resolved_subagents.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return
    try:
        children = sorted(
            p for p in resolved_subagents.iterdir() if p.is_file() and p.suffix == ".jsonl"
        )
    except OSError:
        budget.unreadable_subagents += 1
        return
    if len(children) > MAX_SUBAGENT_FILES:
        children = children[:MAX_SUBAGENT_FILES]
        builder.truncated = True
    for child in children:
        if builder.full or budget.input_truncated:
            builder.truncated = True
            break
        try:
            real_child = child.resolve(strict=True)
            real_child.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            budget.unreadable_subagents += 1
            continue
        parent_tool = _peek_claude_parent_tool_id(real_child)
        parent_id = tool_ids.get(parent_tool or "") or root_id
        quality = "structural" if parent_tool and parent_id != root_id else "orphan"
        child_agent_name = (
            builder.by_id[parent_id].agent_name if parent_id in builder.by_id else ""
        )
        child_key = hashlib.sha256(
            real_child.relative_to(root).as_posix().encode("utf-8", "replace")
        ).hexdigest()[:16]
        _parse_claude_file(
            builder,
            budget,
            usage,
            real_child,
            parent_id,
            file_tag=f"subagent:{child_key}",
            join_quality=quality,
            tool_ids=tool_ids,
            agent_name=child_agent_name,
        )


def _parse_claude_file(
    builder: _Builder,
    budget: _Budget,
    usage: _TokenUsage,
    path: Path,
    parent_id: str,
    *,
    file_tag: str,
    join_quality: str,
    tool_ids: dict[str, str],
    agent_name: str,
) -> None:
    current_turn: str | None = None
    open_tools: dict[str, str] = {}
    last_ms = 0
    last_activity_ms = 0
    turn_index = 0

    for line_no, entry in _iter_jsonl(path, budget):
        if builder.full:
            builder.truncated = True
            break
        timestamp = _timestamp_ms(entry, last_ms + 1)
        last_ms = max(last_ms, timestamp)
        entry_type = str(entry.get("type") or "")
        message = entry.get("message")
        if not isinstance(message, dict):
            if entry_type in {"error", "api_error", "model_error", "stream_error"}:
                current_turn = current_turn or builder.add(
                    record_key=f"{file_tag}:turn:{turn_index}:{line_no}:error",
                    parent_id=parent_id,
                    kind="AGENT_TURN",
                    name="agent turn" if file_tag != "main" else "turn",
                    start_ms=timestamp,
                    join_quality=join_quality,
                    approx=True,
                    agent_name=_explicit_agent_name(entry) or agent_name,
                )
                builder.add(
                    record_key=f"{file_tag}:model-error:{line_no}",
                    parent_id=current_turn,
                    kind="LLM_CALL",
                    name="model error",
                    start_ms=timestamp,
                    end_ms=timestamp,
                    status="error",
                    error_type="model_error",
                    error_message=_explicit_error_text(entry)
                    or "The model request failed.",
                    approx=True,
                    agent_name=_explicit_agent_name(entry) or agent_name,
                )
                turn_index += 1
                last_activity_ms = timestamp
                continue
            budget.ignored_records += 1
            continue
        role = str(message.get("role") or "")
        blocks = _content_blocks(message.get("content"))
        entry_agent_name = _explicit_agent_name(entry) or agent_name
        usage.record(message.get("usage"))

        real_user_texts: list[str] = []
        tool_results: list[dict[str, Any]] = []
        if entry_type == "user" or role == "user":
            for block in blocks:
                if isinstance(block, str):
                    text = block.strip()
                    if text and not _is_claude_system_text(text, bool(entry.get("isMeta"))):
                        real_user_texts.append(text)
                    continue
                btype = str(block.get("type") or "")
                if btype == "text":
                    text = str(block.get("text") or "").strip()
                    if text and not _is_claude_system_text(text, bool(entry.get("isMeta"))):
                        real_user_texts.append(text)
                elif btype == "tool_result":
                    tool_results.append(block)

        for result in tool_results:
            call_id = str(result.get("tool_use_id") or result.get("toolUseId") or "")
            span_id = open_tools.pop(call_id, None)
            output = _extract_text(result.get("content"))
            is_error = bool(result.get("is_error") or result.get("isError"))
            error_type, error_message = _classify_error(
                builder.by_id[span_id].name if span_id in builder.by_id else "",
                result,
                output,
                default_type="tool_error",
            )
            builder.close(
                span_id,
                end_ms=timestamp,
                status="error" if is_error else "ok",
                output_summary=output,
                incomplete=False,
                error_type=error_type if is_error else None,
                error_message=error_message if is_error else None,
            )

        if real_user_texts:
            if current_turn is not None:
                builder.close(
                    current_turn,
                    end_ms=timestamp,
                    approx=True,
                    incomplete=bool(open_tools),
                )
            current_turn = builder.add(
                record_key=f"{file_tag}:turn:{turn_index}:{line_no}",
                parent_id=parent_id,
                kind="AGENT_TURN",
                name="agent turn" if file_tag != "main" else "turn",
                start_ms=timestamp,
                input_summary="\n".join(real_user_texts),
                join_quality=join_quality,
                approx=True,
                agent_name=entry_agent_name,
            )
            turn_index += 1

        if entry_type != "assistant" and role != "assistant":
            last_activity_ms = timestamp
            continue
        if current_turn is None:
            current_turn = builder.add(
                record_key=f"{file_tag}:turn:{turn_index}:{line_no}:implicit",
                parent_id=parent_id,
                kind="AGENT_TURN",
                name="agent turn" if file_tag != "main" else "turn",
                start_ms=last_activity_ms or timestamp,
                join_quality=join_quality,
                approx=True,
                agent_name=entry_agent_name,
            )
            turn_index += 1
        elif entry_agent_name and current_turn in builder.by_id:
            turn = builder.by_id[current_turn]
            if not turn.agent_name:
                turn.agent_name = _sanitize_text(entry_agent_name, MAX_NAME_CHARS)
        assistant_texts: list[str] = []
        tool_uses: list[dict[str, Any]] = []
        for block in blocks:
            if isinstance(block, str):
                if block.strip():
                    assistant_texts.append(block.strip())
                continue
            btype = str(block.get("type") or "")
            if btype == "text":
                text = str(block.get("text") or "").strip()
                if text:
                    assistant_texts.append(text)
            elif btype == "tool_use":
                tool_uses.append(block)
            # Deliberately do not expose "thinking" payloads.

        explicit_model_error = _explicit_error_text(entry) or _explicit_error_text(
            message
        )
        builder.add(
            record_key=f"{file_tag}:llm:{line_no}:{_record_key(entry, line_no)}",
            parent_id=current_turn,
            kind="LLM_CALL",
            name="model response",
            start_ms=last_activity_ms or timestamp,
            end_ms=timestamp,
            status="error" if explicit_model_error else "ok",
            output_summary="\n".join(assistant_texts),
            approx=True,
            join_quality="structural",
            agent_name=entry_agent_name,
            error_type="model_error" if explicit_model_error else "",
            error_message=explicit_model_error,
        )
        for index, tool in enumerate(tool_uses):
            call_id = str(tool.get("id") or f"line-{line_no}-{index}")
            name = str(tool.get("name") or "tool")
            detached = _explicit_background(tool.get("input"))
            spawned_agent_name = _spawned_agent_name(name, tool.get("input"))
            span_id = builder.add(
                record_key=f"{file_tag}:tool:{call_id}",
                parent_id=current_turn,
                kind="TOOL_CALL",
                name=name,
                start_ms=timestamp,
                status="unknown",
                input_summary=_summarize_tool_input(name, tool.get("input")),
                detached=detached,
                join_quality="structural",
                incomplete=True,
                agent_name=spawned_agent_name or entry_agent_name,
            )
            if span_id is not None:
                open_tools[call_id] = span_id
                tool_ids[call_id] = span_id
        last_activity_ms = timestamp

    for span_id in open_tools.values():
        builder.close(
            span_id,
            end_ms=last_ms,
            status="unknown",
            approx=True,
            incomplete=True,
        )
    if current_turn is not None:
        builder.close(
            current_turn,
            end_ms=last_ms,
            approx=True,
            incomplete=(
                bool(open_tools)
                or budget.input_truncated
                or bool(budget.trailing_partial_records)
            ),
        )


def _parse_codex(
    builder: _Builder,
    budget: _Budget,
    usage: _TokenUsage,
    path: Path,
    root: Path,
    root_id: str,
) -> None:
    del root  # The shared adapter contract passes the already-validated root.
    current_turn: str | None = None
    open_tools: dict[str, str] = {}
    last_ms = 0
    last_activity_ms = 0
    turn_index = 0
    saw_terminal_turn = False
    current_agent_name = ""

    def ensure_turn(line_no: int, timestamp: int, summary: object = "") -> str | None:
        nonlocal current_turn, turn_index
        if current_turn is None:
            current_turn = builder.add(
                record_key=f"turn:{turn_index}:{line_no}",
                parent_id=root_id,
                kind="AGENT_TURN",
                name="turn",
                start_ms=timestamp,
                input_summary=summary,
                approx=True,
                agent_name=current_agent_name,
            )
            turn_index += 1
        elif summary and current_turn in builder.by_id:
            builder.by_id[current_turn].input_summary = _sanitize_text(
                summary, MAX_SUMMARY_CHARS
            )
        return current_turn

    def add_llm(line_no: int, timestamp: int, output: object = "") -> None:
        parent = ensure_turn(line_no, last_activity_ms or timestamp)
        builder.add(
            record_key=f"llm:{line_no}",
            parent_id=parent,
            kind="LLM_CALL",
            name="model response",
            start_ms=last_activity_ms or timestamp,
            end_ms=timestamp,
            output_summary=output,
            approx=True,
            agent_name=current_agent_name,
        )

    for line_no, entry in _iter_jsonl(path, budget):
        if builder.full:
            builder.truncated = True
            break
        timestamp = _timestamp_ms(entry, last_ms + 1)
        last_ms = max(last_ms, timestamp)
        entry_type = str(entry.get("type") or "")
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            budget.ignored_records += 1
            continue
        payload_type = str(payload.get("type") or "")
        explicit_agent_name = _explicit_agent_name(payload)
        if explicit_agent_name:
            current_agent_name = explicit_agent_name

        if entry_type == "session_meta":
            usage.record(payload.get("usage"), cumulative=True)
            last_activity_ms = timestamp
            continue

        if entry_type == "event_msg":
            if payload_type == "token_count":
                info = payload.get("info")
                total_usage = (
                    info.get("total_token_usage") if isinstance(info, dict) else None
                )
                usage.record(
                    total_usage
                    if total_usage is not None
                    else payload.get("usage") or payload.get("token_usage"),
                    cumulative=True,
                )
            if payload_type == "task_started":
                if current_turn is not None:
                    builder.close(
                        current_turn,
                        end_ms=timestamp,
                        approx=True,
                        incomplete=True,
                    )
                current_turn = None
                ensure_turn(line_no, timestamp)
                saw_terminal_turn = False
            elif payload_type in {"task_complete", "turn_complete"}:
                builder.close(
                    current_turn,
                    end_ms=timestamp,
                    status="ok",
                    approx=False,
                    incomplete=False,
                )
                current_turn = None
                saw_terminal_turn = True
            elif payload_type in {"turn_aborted", "task_aborted"}:
                builder.close(
                    current_turn,
                    end_ms=timestamp,
                    status="error",
                    output_summary="turn aborted",
                    approx=False,
                    incomplete=False,
                    error_type="interrupted",
                    error_message="The turn was interrupted before normal completion.",
                )
                current_turn = None
                saw_terminal_turn = True
            elif payload_type in {"user_message", "user_prompt"}:
                text = _codex_event_text(payload)
                if text and not _is_codex_system_text(text):
                    ensure_turn(line_no, timestamp, text)
            elif payload_type in {"agent_message", "assistant_message"}:
                add_llm(line_no, timestamp, _codex_event_text(payload))
            elif payload_type in {"error", "model_error", "api_error", "stream_error"}:
                message = _codex_event_text(payload) or "The model request failed."
                parent = ensure_turn(line_no, last_activity_ms or timestamp)
                builder.add(
                    record_key=f"model-error:{line_no}",
                    parent_id=parent,
                    kind="LLM_CALL",
                    name="model error",
                    start_ms=last_activity_ms or timestamp,
                    end_ms=timestamp,
                    status="error",
                    error_type="model_error",
                    error_message=message,
                    approx=True,
                    agent_name=current_agent_name,
                )
            elif payload_type.startswith("collab_agent_spawn_"):
                call_id = str(payload.get("call_id") or payload.get("callId") or "")
                if payload_type.endswith("_begin"):
                    span_id = builder.add(
                        record_key=f"collab:{call_id or line_no}",
                        parent_id=ensure_turn(line_no, timestamp),
                        kind="TOOL_CALL",
                        name="spawn_agent",
                        start_ms=timestamp,
                        status="unknown",
                        join_quality="structural",
                        incomplete=True,
                        agent_name=current_agent_name,
                    )
                    if span_id is not None:
                        open_tools[call_id or f"collab:{line_no}"] = span_id
                elif call_id:
                    failed = _output_is_error(payload, _explicit_error_text(payload))
                    error_type, error_message = _classify_error(
                        "spawn_agent",
                        payload,
                        _explicit_error_text(payload),
                        default_type="tool_error",
                    )
                    builder.close(
                        open_tools.pop(call_id, None),
                        end_ms=timestamp,
                        status="error" if failed else "ok",
                        incomplete=False,
                        error_type=error_type if failed else None,
                        error_message=error_message if failed else None,
                    )
            last_activity_ms = timestamp
            continue

        if entry_type != "response_item":
            budget.ignored_records += 1
            last_activity_ms = timestamp
            continue

        if payload_type == "message":
            usage.record(payload.get("usage"))
            role = str(payload.get("role") or "")
            text = _extract_text(payload.get("content"))
            if role == "user" and text and not _is_codex_system_text(text):
                ensure_turn(line_no, timestamp, text)
            elif role == "assistant":
                add_llm(line_no, timestamp, text)
            # developer/system roles are deliberately not surfaced.
        elif payload_type == "reasoning":
            # Preserve timing structure without exposing hidden reasoning.
            parent = ensure_turn(line_no, last_activity_ms or timestamp)
            builder.add(
                record_key=f"reasoning:{line_no}",
                parent_id=parent,
                kind="LLM_CALL",
                name="reasoning",
                start_ms=last_activity_ms or timestamp,
                end_ms=timestamp,
                approx=True,
                agent_name=current_agent_name,
            )
        elif payload_type in {
            "function_call",
            "custom_tool_call",
            "tool_search_call",
            "web_search_call",
        }:
            call_id = str(
                payload.get("call_id")
                or payload.get("callId")
                or payload.get("id")
                or f"line-{line_no}"
            )
            name = str(payload.get("name") or payload_type.removesuffix("_call") or "tool")
            raw_input = payload.get("arguments")
            if raw_input is None:
                raw_input = payload.get("input")
            decoded_input = _json_loads_maybe(raw_input)
            span_id = builder.add(
                record_key=f"tool:{call_id}",
                parent_id=ensure_turn(line_no, timestamp),
                kind="TOOL_CALL",
                name=name,
                start_ms=timestamp,
                status="unknown",
                input_summary=_summarize_tool_input(name, decoded_input),
                detached=_explicit_background(decoded_input),
                incomplete=True,
                agent_name=current_agent_name,
            )
            if span_id is not None:
                open_tools[call_id] = span_id
        elif payload_type in {
            "function_call_output",
            "custom_tool_call_output",
            "tool_search_call_output",
            "web_search_call_output",
        }:
            call_id = str(
                payload.get("call_id")
                or payload.get("callId")
                or payload.get("id")
                or ""
            )
            raw_output = payload.get("output")
            if raw_output is None:
                raw_output = payload.get("result")
            decoded_output = _json_loads_maybe(raw_output)
            output = _extract_output(decoded_output)
            status = "error" if _output_is_error(payload, output) else "ok"
            span_id = open_tools.pop(call_id, None)
            error_type, error_message = _classify_error(
                builder.by_id[span_id].name if span_id in builder.by_id else "",
                payload,
                output,
                default_type="tool_error",
            )
            builder.close(
                span_id,
                end_ms=timestamp,
                status=status,
                output_summary=output,
                incomplete=False,
                error_type=error_type if status == "error" else None,
                error_message=error_message if status == "error" else None,
            )
        last_activity_ms = timestamp

    for span_id in open_tools.values():
        builder.close(
            span_id,
            end_ms=last_ms,
            status="unknown",
            approx=True,
            incomplete=True,
        )
    if current_turn is not None:
        builder.close(
            current_turn,
            end_ms=last_ms,
            approx=True,
            incomplete=True,
        )
    if not saw_terminal_turn and current_turn is None and builder.spans:
        # There may have been only structural records. The session snapshot is
        # still approximate, but do not fabricate an incomplete turn.
        pass


def _finalize_root(builder: _Builder, root_id: str, budget: _Budget) -> None:
    root = builder.by_id[root_id]
    children = [span for span in builder.spans if span.span_id != root_id]
    if children:
        start = min(span.start_ms for span in children)
        end = max(span.end_ms for span in children)
        root.start_ms = start
        root.end_ms = max(start, end)
    root.incomplete = (
        budget.input_truncated
        or bool(budget.trailing_partial_records)
        or any(span.incomplete for span in children)
    )
    first_error = next((span for span in children if span.status == "error"), None)
    if budget.parse_error_records:
        root.status = "error"
        root.error_type = "parse_error"
        root.error_message = "One or more completed log records could not be parsed."
    elif first_error is not None:
        root.status = "error"
        root.error_type = first_error.error_type or "unknown"
        root.error_message = "One or more child operations failed."
    else:
        root.status = "ok"
        root.error_type = ""
        root.error_message = ""


def _render_trace(
    source: str,
    session_id: str,
    builder: _Builder,
    budget: _Budget,
    usage: _TokenUsage,
) -> dict[str, Any]:
    session_start = min((span.start_ms for span in builder.spans), default=0)
    rendered: list[dict[str, Any]] = []
    counts = {"SESSION": 0, "AGENT_TURN": 0, "LLM_CALL": 0, "TOOL_CALL": 0}
    errors = 0
    incomplete = 0
    for span in builder.spans:
        counts[span.kind] = counts.get(span.kind, 0) + 1
        errors += int(span.kind != "SESSION" and span.status == "error")
        incomplete += int(span.incomplete)
        duration = min(MAX_DURATION_MS, max(0, span.end_ms - span.start_ms))
        rendered.append(
            {
                "id": span.span_id,
                "parent_id": span.parent_id,
                "kind": span.kind,
                "name": span.name,
                "start_ms": max(0, span.start_ms - session_start),
                "duration_ms": duration,
                "status": span.status,
                "input_summary": span.input_summary,
                "output_summary": span.output_summary,
                "approx": span.approx,
                "join_quality": span.join_quality,
                "incomplete": span.incomplete,
                "detached": span.detached,
                "agent_name": span.agent_name,
                "error_type": span.error_type,
                "error_message": span.error_message,
            }
        )
    duration_ms = max(
        (item["start_ms"] + item["duration_ms"] for item in rendered), default=0
    )
    result = {
        "schema_version": 1,
        "source": source,
        "session_id": session_id,
        "summary": {
            "span_count": len(rendered),
            "turn_count": counts.get("AGENT_TURN", 0),
            "llm_count": counts.get("LLM_CALL", 0),
            "tool_count": counts.get("TOOL_CALL", 0),
            "error_count": errors,
            "incomplete_count": incomplete,
            "duration_ms": min(MAX_DURATION_MS, duration_ms),
            "status": "error" if errors else ("incomplete" if incomplete else "ok"),
        },
        "spans": rendered,
        "truncated": bool(builder.truncated),
        "quality": _quality_summary(builder, budget),
        "usage": {
            "tokens": {
                "quality": "exact" if usage.seen else "unknown",
                **{key: usage.values.get(key) for key in _TOKEN_FIELDS},
            },
            "cost": {
                "quality": "unknown",
                "amount": None,
                "currency": None,
                "reason": "本地日志没有可核验的账单边界，因此不估算金额。",
            },
        },
        "warnings": {
            "malformed_records": budget.malformed_records,
            "oversized_records": budget.oversized_records,
            "ignored_records": budget.ignored_records,
            "unreadable_subagents": budget.unreadable_subagents,
            "input_truncated": budget.input_truncated,
            "trailing_partial_records": budget.trailing_partial_records,
        },
    }
    # Last-line defense: every user/provider-derived string has already gone
    # through _sanitize_text. This assertion keeps future fields honest.
    _assert_safe_shape(result)
    return result


def _quality_summary(builder: _Builder, budget: _Budget) -> dict[str, Any]:
    join_counts = {
        "structural": 0,
        "semi": 0,
        "heuristic": 0,
        "orphan": 0,
    }
    for span in builder.spans:
        if span.kind != "SESSION":
            join_counts[span.join_quality] = join_counts.get(span.join_quality, 0) + 1
    signals = {
        "approx": sum(int(span.approx) for span in builder.spans),
        "incomplete": sum(int(span.incomplete) for span in builder.spans),
        "truncated": int(builder.truncated),
        "trailing_partial": budget.trailing_partial_records,
        "malformed": budget.malformed_records,
        "oversized": budget.oversized_records,
        "unreadable": budget.unreadable_subagents + budget.unreadable_files,
    }
    if builder.truncated or budget.input_truncated:
        completeness = "truncated"
    elif budget.parse_error_records:
        completeness = "malformed"
    elif budget.trailing_partial_records or any(span.incomplete for span in builder.spans):
        completeness = "incomplete"
    else:
        completeness = "closed"
    return {
        "join_quality": join_counts,
        "signals": signals,
        "completeness": completeness,
        "time_quality": "approximate"
        if any(span.approx for span in builder.spans)
        else "exact",
        "reasons": {
            "structural": "提供方日志明确记录了父子关系。",
            "semi": "强结构字段支持这个关系，但日志没有直接父节点链接。",
            "heuristic": "父子关系来自时间或邻近上下文推测，不是日志明示事实。",
            "orphan": "现有日志无法可靠确认父节点。",
            "approx": "部分时间来自相邻日志时间戳，不是精确模型调用边界。",
            "incomplete": "部分节点尚未闭合；日志可能仍在写入，也可能曾被中断。",
            "truncated": "安全上限提前终止了快照，因此未包含全部可用记录。",
            "trailing_partial": "提供方仍在写最后一条 JSON；这是未完成，不是日志损坏。",
            "malformed": "至少一条已经写完的日志不是有效 JSON。",
            "oversized": "至少一条记录超过单条安全上限，已跳过。",
            "unreadable": "读取期间至少一个已验证轨迹文件变得不可用。",
        },
    }


def _assert_safe_shape(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > 64:
                raise RuntimeError("unsafe trace field")
            _assert_safe_shape(child)
    elif isinstance(value, list):
        if len(value) > HARD_MAX_SPANS:
            raise RuntimeError("unsafe trace list")
        for child in value:
            _assert_safe_shape(child)
    elif isinstance(value, str):
        if len(value) > MAX_SUMMARY_CHARS:
            raise RuntimeError("unsafe trace string")
        if _contains_sensitive_text(value):
            raise RuntimeError("trace redaction invariant failed")
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise RuntimeError("unsafe trace value")


def _contains_sensitive_text(text: str) -> bool:
    if (
        _PRIVATE_KEY_RE.search(text)
        or _BEARER_RE.search(text)
        or _BASIC_AUTH_RE.search(text)
        or _CREDENTIAL_HEADER_RE.search(text)
        or _JWT_RE.search(text)
        or _COMMON_TOKEN_RE.search(text)
    ):
        return True
    if any(pattern.search(text) for pattern in _TOKEN_RES):
        return True
    if _SECRET_ASSIGN_RE.search(text) or _ENV_ASSIGN_RE.search(text):
        return True
    if _ENV_REF_RE.search(text):
        return True
    if _WINDOWS_PATH_RE.search(text) or _HOME_PATH_RE.search(text):
        return True
    if _UNIX_PATH_RE.search(text):
        return True
    return any(_looks_high_entropy(match.group(0)) for match in _HIGH_ENTROPY_CANDIDATE_RE.finditer(text))


def _looks_high_entropy(candidate: str) -> bool:
    """Conservative secret heuristic: catch random base64-like values, not prose/hashes."""

    if len(candidate) < 32:
        return False
    alphabet = set(candidate)
    # Hex digests and repeated test fixtures are common trace facts. Their
    # restricted alphabet is not enough evidence of a credential.
    if alphabet <= set("0123456789abcdefABCDEF-"):
        return False
    counts: dict[str, int] = {}
    for char in candidate:
        counts[char] = counts.get(char, 0) + 1
    length = len(candidate)
    entropy = -sum(
        (count / length) * math.log2(count / length)
        for count in counts.values()
    )
    return entropy >= 4.25


def _redact_high_entropy(text: str) -> str:
    return _HIGH_ENTROPY_CANDIDATE_RE.sub(
        lambda match: "[REDACTED HIGH-ENTROPY]"
        if _looks_high_entropy(match.group(0))
        else match.group(0),
        text,
    )


def _sanitize_text(value: object, limit: int = MAX_SUMMARY_CHARS) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        # Do not serialize an original payload. Extract a compact description
        # from known fields at the call site instead.
        text = "[structured data]"
    else:
        text = str(value)
    text = text.replace("\x00", "")
    # Normalize control characters before matching secrets. Otherwise a token
    # split by a control byte can evade the first redaction pass, be joined
    # later, and make the final fail-closed invariant reject the entire trace.
    text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
    text = _PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", text)
    for pattern in _TOKEN_RES:
        text = pattern.sub("[REDACTED]", text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _BASIC_AUTH_RE.sub("Basic [REDACTED]", text)
    text = _CREDENTIAL_HEADER_RE.sub("[REDACTED CREDENTIAL HEADER]", text)
    text = _JWT_RE.sub("[REDACTED JWT]", text)
    text = _COMMON_TOKEN_RE.sub("[REDACTED]", text)
    text = _SECRET_ASSIGN_RE.sub("[REDACTED SECRET]", text)
    text = _ENV_ASSIGN_RE.sub("[REDACTED ENV]", text)
    text = _ENV_REF_RE.sub("[REDACTED ENV]", text)
    text = _WINDOWS_PATH_RE.sub("[PATH]", text)
    text = _HOME_PATH_RE.sub("[PATH]", text)
    text = _UNIX_PATH_RE.sub("[PATH]", text)
    text = _redact_high_entropy(text)
    text = text.strip()
    if len(text) > limit:
        # Do not let truncation turn an otherwise harmless trailing slash
        # followed by whitespace into a synthetic ``/…`` path that the final
        # redaction invariant correctly (but misleadingly) rejects.
        return text[: max(0, limit - 1)].rstrip().rstrip("/\\") + "…"
    return text


def _safe_ms(value: object) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(number, 9_999_999_999_999_999))


def _timestamp_ms(entry: dict[str, Any], fallback: int) -> int:
    candidates: list[object] = [
        entry.get("timestamp"),
        entry.get("time"),
        entry.get("created_at"),
        entry.get("createdAt"),
    ]
    payload = entry.get("payload")
    if isinstance(payload, dict):
        candidates.extend(
            [payload.get("timestamp"), payload.get("time"), payload.get("created_at")]
        )
    for value in candidates:
        if isinstance(value, (int, float)):
            if value > 10_000_000_000:
                return _safe_ms(value)
            return _safe_ms(value * 1_000)
        if not isinstance(value, str) or not value.strip():
            continue
        raw = value.strip()
        try:
            numeric = float(raw)
        except ValueError:
            numeric = None
        if numeric is not None:
            return _safe_ms(numeric if numeric > 10_000_000_000 else numeric * 1_000)
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        return _safe_ms(parsed.timestamp() * 1_000)
    return _safe_ms(fallback)


def _record_key(entry: dict[str, Any], line_no: int) -> str:
    for key in ("uuid", "id", "message_id", "messageId"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]
    return str(line_no)


def _content_blocks(content: object) -> list[str | dict[str, Any]]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [item for item in content if isinstance(item, (str, dict))]
    return []


def _extract_text(content: object) -> str:
    texts: list[str] = []
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    for item in content:
        if isinstance(item, str):
            if item.strip():
                texts.append(item.strip())
        elif isinstance(item, dict):
            # Exclude encrypted/thinking/reasoning fields.
            if str(item.get("type") or "") in {
                "thinking",
                "reasoning",
                "encrypted_content",
            }:
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
    return "\n".join(texts)


def _extract_output(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("output", "content", "text", "message"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
        return "[structured result]"
    if isinstance(value, list):
        texts = [
            str(item.get("text") or "")
            for item in value
            if isinstance(item, dict) and item.get("text")
        ]
        return "\n".join(texts) if texts else "[structured result]"
    return ""


def _json_loads_maybe(value: object) -> object:
    if not isinstance(value, str):
        return value
    if len(value) > MAX_LINE_BYTES:
        return "[oversized input]"
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def _summarize_tool_input(name: object, value: object) -> str:
    tool_name = _sanitize_text(name, MAX_NAME_CHARS) or "tool"
    if not isinstance(value, dict):
        if isinstance(value, str):
            return f"{tool_name}\n{_sanitize_text(value, MAX_SUMMARY_CHARS - len(tool_name) - 1)}"
        return tool_name
    lower = tool_name.lower()
    if lower in {"task", "spawn_agent"}:
        description = value.get("description") or value.get("task_name") or value.get(
            "subagent_type"
        )
        return (
            f"{tool_name}\n{_sanitize_text(description, 380)}"
            if description
            else tool_name
        )
    selected: object | None = None
    label = tool_name
    for key, candidate_label in (
        ("command", "Shell command"),
        ("cmd", "Shell command"),
        ("file_path", "File"),
        ("path", "File"),
        ("pattern", "Search"),
        ("query", "Query"),
        ("url", "URL"),
        ("description", tool_name),
    ):
        candidate = value.get(key)
        if candidate not in (None, ""):
            selected = candidate
            label = candidate_label
            break
    if selected is None:
        return tool_name
    room = max(40, MAX_SUMMARY_CHARS - len(label) - 1)
    return f"{label}\n{_sanitize_text(selected, room)}"


def _explicit_background(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return any(
        value.get(key) is True
        for key in ("background", "run_in_background", "runInBackground", "detached")
    )


def _is_claude_system_text(text: str, is_meta: bool) -> bool:
    return is_meta or text.lstrip().startswith(_CLAUDE_SYSTEM_PREFIXES)


def _is_codex_system_text(text: str) -> bool:
    return text.lstrip().startswith(_CODEX_SYSTEM_PREFIXES)


def _codex_event_text(payload: dict[str, Any]) -> str:
    for key in ("message", "text", "content"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


def _output_is_error(payload: dict[str, Any], output: str) -> bool:
    if payload.get("is_error") is True or payload.get("isError") is True:
        return True
    if payload.get("success") is False or payload.get("ok") is False:
        return True
    status = str(payload.get("status") or "").lower()
    if status in {
        "error",
        "failed",
        "failure",
        "cancelled",
        "canceled",
        "timeout",
        "timed_out",
        "interrupted",
        "aborted",
    }:
        return True
    candidates = [payload]
    for key in ("result", "output", "response", "metadata"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)
    for candidate in candidates:
        if candidate.get("success") is False or candidate.get("ok") is False:
            return True
        for key in ("exit_code", "exitCode", "returncode", "return_code", "code"):
            value = candidate.get(key)
            if isinstance(value, bool):
                continue
            try:
                if value is not None and int(value) != 0:
                    return True
            except (TypeError, ValueError, OverflowError):
                continue
    lowered = output.lower()
    return bool(
        re.search(r"\b(?:exit code|process exited with code)\s*[:=]?\s*[1-9]\d*\b", lowered)
    )


def _peek_claude_parent_tool_id(path: Path) -> str | None:
    """Best-effort explicit join evidence from the first few bounded records."""

    try:
        with path.open("rb") as fh:
            for _ in range(12):
                raw = fh.readline(64 * 1024)
                if not raw:
                    break
                try:
                    entry = json.loads(raw.decode("utf-8", "replace"))
                except (json.JSONDecodeError, UnicodeError):
                    continue
                if not isinstance(entry, dict):
                    continue
                for key in (
                    "parentToolUseId",
                    "parent_tool_use_id",
                    "taskToolUseId",
                    "spawnedByToolUseId",
                ):
                    value = entry.get(key)
                    if isinstance(value, str) and value:
                        return value
    except OSError:
        return None
    return None


def _accept_claude_path(path: Path) -> bool:
    return path.suffix == ".jsonl"


def _accept_codex_path(path: Path) -> bool:
    return path.suffix == ".jsonl" and path.name.startswith("rollout-")


def _explicit_agent_name(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    for key in (
        "agent_name",
        "agentName",
        "agent_id",
        "agentId",
        "subagent_type",
        "subagentType",
        "nickname",
    ):
        candidate = value.get(key)
        if isinstance(candidate, (str, int)) and str(candidate).strip():
            return _sanitize_text(candidate, MAX_NAME_CHARS)
    return ""


def _explicit_error_text(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("error_message", "errorMessage", "error", "failure", "exception"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return _sanitize_text(candidate, MAX_SUMMARY_CHARS)
        if isinstance(candidate, dict):
            for nested_key in ("message", "detail", "reason", "type"):
                nested = candidate.get(nested_key)
                if isinstance(nested, str) and nested.strip():
                    return _sanitize_text(nested, MAX_SUMMARY_CHARS)
    return ""


def _spawned_agent_name(tool_name: object, tool_input: object) -> str:
    if str(tool_name or "").strip().lower() not in {
        "task",
        "spawn_agent",
        "collaboration.spawn_agent",
    }:
        return ""
    if not isinstance(tool_input, dict):
        return ""
    for key in ("task_name", "nickname", "subagent_type", "agent_name", "name"):
        candidate = tool_input.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return _sanitize_text(candidate, MAX_NAME_CHARS)
    return ""


def _classify_error(
    tool_name: object,
    payload: object,
    output: object,
    *,
    default_type: str = "unknown",
) -> tuple[str, str]:
    explicit = _explicit_error_text(payload)
    output_text = str(output or "")
    evidence = explicit or output_text or "The operation failed."
    lowered = f"{tool_name}\n{evidence}".lower()
    if re.search(r"\b(?:timed?\s*out|timeout|deadline exceeded)\b", lowered):
        error_type = "timeout"
    elif re.search(r"\b(?:interrupt(?:ed)?|cancel(?:led|ed)?|abort(?:ed)?)\b", lowered):
        error_type = "interrupted"
    elif (
        str(tool_name or "").lower()
        in {"exec", "exec_command", "bash", "shell", "terminal", "run_command"}
        or re.search(r"\b(?:exit code|process exited with code)\s*[:=]?\s*[1-9]\d*\b", lowered)
    ):
        error_type = "command_error"
    else:
        error_type = default_type if default_type in _ERROR_TYPES else "unknown"
    return error_type, _sanitize_text(evidence, MAX_SUMMARY_CHARS)


def _normalize_token_usage(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    aliases = {
        "input_tokens": (
            "input_tokens",
            "inputTokens",
            "input_token_count",
            "inputTokenCount",
        ),
        "output_tokens": (
            "output_tokens",
            "outputTokens",
            "output_token_count",
            "outputTokenCount",
        ),
        "cached_input_tokens": (
            "cached_input_tokens",
            "cache_read_input_tokens",
            "cacheReadInputTokens",
            "cachedInputTokens",
        ),
        "cache_creation_input_tokens": (
            "cache_creation_input_tokens",
            "cacheCreationInputTokens",
            "cache_write_input_tokens",
        ),
        "reasoning_tokens": (
            "reasoning_tokens",
            "reasoningTokens",
            "reasoning_output_tokens",
        ),
        "total_tokens": ("total_tokens", "totalTokens"),
    }
    normalized: dict[str, int] = {}
    for target, candidates in aliases.items():
        for key in candidates:
            raw = value.get(key)
            if isinstance(raw, bool):
                continue
            try:
                number = int(raw)
            except (TypeError, ValueError):
                continue
            if number < 0:
                continue
            normalized[target] = number
            break
    return normalized


# Provider-specific parsing is registered behind one deliberately small
# contract.  Identity discovery stays in Cards' existing pane resolvers; this
# registry only owns safe-path policy plus read-only parsing of the already
# resolved provider log.
TRACE_ADAPTERS: dict[str, TraceAdapter] = {
    "claude-code": TraceAdapter(
        source="claude-code",
        root_provider=lambda: CLAUDE_ROOT,
        accepts_path=_accept_claude_path,
        parse=_parse_claude,
    ),
    "codex": TraceAdapter(
        source="codex",
        root_provider=lambda: CODEX_ROOT,
        accepts_path=_accept_codex_path,
        parse=_parse_codex,
    ),
}
