#!/usr/bin/env python3
"""Codex 子智能体（spawn_agent）的实时状态。

真源是每个子智能体自己的 rollout JSONL，不看屏幕：

- 第一行 ``session_meta``：``thread_source == "subagent"``，带 ``parent_thread_id``、
  ``agent_path``（如 ``/root/example_review``，与父会话 spawn_agent 的回执一致）、
  ``agent_nickname``；``subagent_history_start_ordinal`` 之前的行是从父会话复制过来的
  历史，不算子智能体自己的活；
- ``event_msg`` 的 ``task_started`` / ``task_complete`` / ``turn_aborted``：最后一个
  事件说明它此刻在不在干活；
- ``response_item`` 的 function_call / custom_tool_call：做了几步、最近一步是什么。

Codex 的子智能体跑在父进程里，活着的子智能体 rollout 由同一个 codex 进程打开着，
所以"这个窗口有哪些子智能体"由调用方从进程打开的文件给出，不按目录猜。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path

from claude_subagents import STALE_SECONDS, SubagentState, _parse_ts, _short

_MAX_CACHED = 512
_EXEC_CMD_RE = re.compile(r"""\bcmd\s*:\s*(["'`])((?:\\.|(?!\1).)*)\1""", re.S)
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.M)


def _loads(value: object) -> object:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def describe_codex_tool(name: object, raw_args: object) -> str:
    """把 Codex 的一次工具调用说成一句中文短语。"""
    name = str(name or "工具")
    args = _loads(raw_args)
    data = args if isinstance(args, dict) else {}
    text = args if isinstance(args, str) else ""
    if name in {"exec_command", "shell", "local_shell"}:
        cmd = data.get("cmd") or data.get("command") or ""
        if isinstance(cmd, list):
            cmd = " ".join(str(part) for part in cmd)
        return f"运行命令：{_short(str(cmd).strip().splitlines()[0] if str(cmd).strip() else '', 60)}"
    if name == "exec":
        # 新版 Codex 把命令包在一段脚本里：tools.exec_command({cmd:"..."})
        match = _EXEC_CMD_RE.search(text or str(raw_args or ""))
        if match:
            cmd = match.group(2).encode().decode("unicode_escape", "ignore").strip().splitlines()
            return f"运行命令：{_short(cmd[0] if cmd else '', 60)}"
        return "执行脚本"
    if name == "apply_patch":
        files = _PATCH_FILE_RE.findall(text or str(data.get("input") or ""))
        if files:
            extra = f" 等 {len(files)} 个文件" if len(files) > 1 else ""
            return f"修改文件 {os.path.basename(files[0].strip())}{extra}"
        return "修改文件"
    if name == "spawn_agent":
        return f"派出子智能体 {_short(data.get('task_name'), 40)}"
    if name in {"wait_agent", "wait"}:
        return "等待子智能体" if name == "wait_agent" else "等待命令输出"
    if name in {"send_message", "followup_task", "send_input"}:
        return f"给 {_short(data.get('target'), 40)} 发消息"
    if name == "sleep":
        seconds = round((data.get("duration_ms") or 0) / 1000)
        return f"等待 {seconds} 秒" if seconds else "等待"
    if name == "update_plan":
        return "更新计划"
    if name == "web_search":
        return f"联网搜索 {_short(data.get('query'), 50)}" if data.get("query") else "联网搜索"
    if name == "view_image":
        return "查看图片"
    if name == "list_agents":
        return "查看子智能体"
    return f"调用 {name}"


class _ChildLog:
    """一个 Codex 子智能体 rollout 的增量读取状态。"""

    def __init__(self) -> None:
        self.offset = 0
        self.inode: int | None = None
        self.line_no = 0
        self.meta: dict = {}
        self.own_from = 0
        self.tool_calls = 0
        self.first_ts: float | None = None
        self.last_ts: float | None = None
        self.last_tool = ""
        self.phase = ""
        self.lifecycle = ""  # started / complete / aborted

    @property
    def activity(self) -> str:
        if self.lifecycle == "complete":
            return "已交差"
        if self.lifecycle == "aborted":
            return "已中断"
        if self.phase == "thinking":
            return f"思考中（上一步：{self.last_tool}）" if self.last_tool else "思考中"
        if self.phase == "writing":
            return "写回复"
        return self.last_tool

    def feed(self, path: Path) -> None:
        try:
            st = path.stat()
        except OSError:
            return
        if st.st_ino != self.inode or st.st_size < self.offset:
            self.__init__()
            self.inode = st.st_ino
        if st.st_size == self.offset:
            return
        with open(path, "rb") as fh:
            fh.seek(self.offset)
            data = fh.read(st.st_size - self.offset)
        end = data.rfind(b"\n")
        if end < 0:
            return
        self.offset += end + 1
        for raw in data[: end + 1].splitlines():
            index = self.line_no
            self.line_no += 1
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if isinstance(entry, dict):
                self._apply(index, entry)

    def _apply(self, index: int, entry: dict) -> None:
        kind = entry.get("type")
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        if kind == "session_meta" and not self.meta:
            self.meta = payload
            self.own_from = int(payload.get("subagent_history_start_ordinal") or 0)
            return
        if index < self.own_from:
            return  # 从父会话复制来的历史
        ts = _parse_ts(entry.get("timestamp"))
        if ts is not None:
            self.first_ts = ts if self.first_ts is None else min(self.first_ts, ts)
            self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)
        ptype = payload.get("type")
        if kind == "event_msg":
            if ptype == "task_started":
                self.lifecycle = "started"
            elif ptype == "task_complete":
                self.lifecycle = "complete"
            elif ptype == "turn_aborted":
                self.lifecycle = "aborted"
        elif kind == "response_item":
            if ptype in {"function_call", "custom_tool_call"}:
                self.tool_calls += 1
                self.last_tool = describe_codex_tool(payload.get("name"), payload.get("arguments", payload.get("input")))
                self.phase = "tool"
            elif ptype == "reasoning":
                self.phase = "thinking"
            elif ptype == "message" and payload.get("role") == "assistant":
                self.phase = "writing"


_LOGS: "OrderedDict[str, _ChildLog]" = OrderedDict()
_LOCK = threading.Lock()


def _log_for(path: Path) -> _ChildLog:
    key = str(path)
    with _LOCK:
        log = _LOGS.get(key)
        if log is None:
            if len(_LOGS) >= _MAX_CACHED:
                _LOGS.popitem(last=False)
            log = _LOGS[key] = _ChildLog()
        else:
            _LOGS.move_to_end(key)
        log.feed(path)
        return log


_HEADS: "OrderedDict[str, dict]" = OrderedDict()


def _read_head_meta(path: Path) -> dict:
    """只读第一行 session_meta（不变，按路径永久缓存）。"""
    key = str(path)
    with _LOCK:
        cached = _HEADS.get(key)
    if cached is not None:
        return cached
    try:
        with open(path, "rb") as fh:
            first = json.loads(fh.readline() or b"{}")
    except (OSError, ValueError):
        return {}
    payload = first.get("payload") if isinstance(first, dict) and first.get("type") == "session_meta" else None
    fields = ("id", "parent_thread_id", "agent_path", "agent_nickname", "agent_role", "thread_source")
    meta = {k: payload.get(k) for k in fields} if isinstance(payload, dict) else {}
    with _LOCK:
        if len(_HEADS) >= 4 * _MAX_CACHED:
            _HEADS.popitem(last=False)
        _HEADS[key] = meta
    return meta



def is_subagent_rollout(path: str | os.PathLike[str]) -> bool:
    return _read_head_meta(Path(path)).get("thread_source") == "subagent"


def child_state(path: str | os.PathLike[str], now: float | None = None) -> SubagentState | None:
    now = time.time() if now is None else now
    head = _read_head_meta(Path(path))
    if head.get("thread_source") != "subagent":
        return None
    log = _log_for(Path(path))
    updated = log.last_ts or log.first_ts or now
    if log.lifecycle == "complete":
        status = "completed"
    elif log.lifecycle == "aborted":
        status = "stopped"
    elif now - updated > STALE_SECONDS:
        status = "stale"
    else:
        status = "running"
    agent_path = str(head.get("agent_path") or "")
    name = agent_path.rsplit("/", 1)[-1] or agent_path
    nickname = str(head.get("agent_nickname") or "")
    return SubagentState(
        agent_id=str(head.get("id") or ""),
        tool_use_id=agent_path,
        agent_type=str(head.get("agent_role") or "codex"),
        description=f"{name}（{nickname}）" if nickname else name,
        background=True,
        status=status,
        activity=log.activity,
        tool_calls=log.tool_calls,
        started_at=log.first_ts,
        updated_at=updated,
    )


def sessions_root(rollout_path: str | os.PathLike[str]) -> Path | None:
    """rollout 所在的 ``<CODEX_HOME>/sessions`` 目录。"""
    for parent in Path(rollout_path).parents:
        if parent.name == "sessions":
            return parent
    return None


_LISTINGS: dict[str, tuple[float, list[Path]]] = {}
LISTING_TTL = 5.0
LISTING_DAYS = 3


def children_of(parent_rollout: str | os.PathLike[str], now: float | None = None) -> list[SubagentState]:
    """父会话派出过的子智能体（包括已经结束、进程不再打开的），按最近几天的 rollout 目录找。"""
    now = time.time() if now is None else now
    parent_id = _read_head_meta(Path(parent_rollout)).get("id")
    root = sessions_root(parent_rollout)
    if not parent_id or root is None:
        return []
    cached = _LISTINGS.get(str(root))
    if cached and now - cached[0] < LISTING_TTL:
        paths = cached[1]
    else:
        paths = []
        for days_ago in range(LISTING_DAYS):
            day = time.localtime(now - days_ago * 86400)
            day_dir = root / f"{day.tm_year:04d}" / f"{day.tm_mon:02d}" / f"{day.tm_mday:02d}"
            try:
                paths.extend(p for p in day_dir.iterdir() if p.name.startswith("rollout-") and p.suffix == ".jsonl")
            except OSError:
                continue
        _LISTINGS[str(root)] = (now, paths)
    states = []
    for path in paths:
        if _read_head_meta(path).get("parent_thread_id") != parent_id:
            continue
        state = child_state(path, now)
        if state:
            states.append(state)
    states.sort(key=lambda s: (s.started_at or 0, s.agent_id))
    return states
