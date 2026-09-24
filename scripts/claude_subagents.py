#!/usr/bin/env python3
"""Claude Code 子智能体（Agent 工具）的实时状态。

真源是 Claude 自己写下的结构化文件，不看屏幕：

- ``<project>/<session>.jsonl`` 同名目录下的 ``subagents/agent-<id>.meta.json``：
  子智能体类型、描述、以及发起它的那次 Agent 调用的 tool_use id；
- ``subagents/agent-<id>.jsonl``：子智能体自己的对话。最后一条是
  ``stop_reason == "end_turn"`` 的 assistant 消息，就说明它已经交差；
- 父会话里的 ``<task-notification>``：后台子智能体 failed / killed / stopped 这类
  不会写 end_turn 的终态，由调用方从父会话里解析后传进来。

终端底部那块 agents 面板只是这些事实的投影，而且会随 Claude Code 版本改样子，
所以这里不解析它。子智能体的日志动辄几 MB，这里按文件增量读取：每次只读上次之后
新写入的部分。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# 没交差、父会话也没报终态，却这么久没写过一行：多半是跟着父进程一起没了。
STALE_SECONDS = 20 * 60
# 不在当前时间线里、但最近还在写日志的子智能体也要显示（它的 Agent 调用可能已经
# 滚出了时间线的尾部窗口）。
RECENT_SECONDS = 15 * 60
TERMINAL_NOTIFICATION_STATUSES = {"completed", "failed", "killed", "stopped"}
WORKFLOW_RUN_RE = re.compile(r"^wf_[0-9a-f-]+$")
_MAX_CACHED_LOGS = 512


def subagents_dir(transcript_path: str | os.PathLike[str]) -> Path:
    path = Path(transcript_path)
    return path.parent / path.stem / "subagents"


def _parse_ts(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _short(value: object, limit: int = 72) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def describe_tool_use(name: object, inp: object) -> str:
    """把一次工具调用说成一句中文短语，用来回答"它现在在干什么"。"""
    name = str(name or "工具")
    data = inp if isinstance(inp, dict) else {}
    lower = name.lower()
    file_path = data.get("file_path") or data.get("notebook_path") or data.get("path")
    base = os.path.basename(str(file_path).rstrip("/")) if file_path else ""
    if lower == "read" and base:
        return f"读取 {base}"
    if lower in {"edit", "multiedit"} and base:
        return f"编辑 {base}"
    if lower == "write" and base:
        return f"写入 {base}"
    if lower == "notebookedit" and base:
        return f"编辑 {base}"
    if lower == "bash":
        desc = data.get("description")
        if desc:
            return f"运行命令：{_short(desc, 60)}"
        command = str(data.get("command") or "").strip().splitlines()
        return f"运行命令：{_short(command[0] if command else '', 60)}"
    if lower == "grep" and data.get("pattern"):
        return f"搜索 {_short(data['pattern'], 50)}"
    if lower == "glob" and data.get("pattern"):
        return f"查找文件 {_short(data['pattern'], 50)}"
    if lower == "webfetch" and data.get("url"):
        return f"抓取网页 {_short(data['url'], 60)}"
    if lower == "websearch" and data.get("query"):
        return f"联网搜索 {_short(data['query'], 50)}"
    if lower in {"agent", "task"}:
        return f"派出子智能体：{_short(data.get('description'), 50)}"
    if lower == "skill" and data.get("skill"):
        return f"加载技能 {_short(data['skill'], 40)}"
    if lower == "todowrite":
        return "更新待办清单"
    if lower.startswith("mcp__"):
        return f"调用 {name.split('__')[-1]}"
    return f"调用 {name}"


class _AgentLog:
    """一个子智能体日志文件的增量读取状态。"""

    def __init__(self) -> None:
        self.offset = 0
        self.inode: int | None = None
        self.tool_calls = 0
        self.first_ts: float | None = None
        self.last_ts: float | None = None
        self.last_tool = ""
        self.phase = ""  # tool / thinking / writing
        self.done = False

    @property
    def activity(self) -> str:
        """和终端 agents 面板同一个意思：最近一步在干什么。"""
        if self.done:
            return "已交差"
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
        # 只消费完整的行；写到一半的最后一行留到下次。
        end = data.rfind(b"\n")
        if end < 0:
            return
        self.offset += end + 1
        for raw in data[: end + 1].splitlines():
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if isinstance(entry, dict):
                self._apply(entry)

    def _apply(self, entry: dict) -> None:
        ts = _parse_ts(entry.get("timestamp"))
        if ts is not None:
            self.first_ts = ts if self.first_ts is None else min(self.first_ts, ts)
            self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)
        if entry.get("type") not in {"user", "assistant"}:
            return
        # 交差之后又有新消息，说明它被 SendMessage 续上了，重新算运行中。
        self.done = False
        msg = entry.get("message")
        if entry.get("type") != "assistant" or not isinstance(msg, dict):
            return
        content = msg.get("content")
        items = content if isinstance(content, list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "tool_use":
                self.tool_calls += 1
                self.last_tool = describe_tool_use(item.get("name"), item.get("input"))
                self.phase = "tool"
            elif kind == "thinking":
                self.phase = "thinking"
            elif kind == "text" and (item.get("text") or "").strip():
                self.phase = "writing"
        if msg.get("stop_reason") == "end_turn":
            self.done = True


# HTTP 服务是多线程的：两个标签页同时轮询同一窗口时会并发读同一个日志，
# 不加锁会重复计数、把读取位置推过文件末尾。按最近使用淘汰。
_LOGS: "OrderedDict[str, _AgentLog]" = OrderedDict()
_LOGS_LOCK = threading.Lock()


def _log_for(path: Path) -> _AgentLog:
    key = str(path)
    with _LOGS_LOCK:
        log = _LOGS.get(key)
        if log is None:
            if len(_LOGS) >= _MAX_CACHED_LOGS:
                _LOGS.popitem(last=False)
            log = _LOGS[key] = _AgentLog()
        else:
            _LOGS.move_to_end(key)
        log.feed(path)
        return log


@dataclass(frozen=True)
class SubagentState:
    agent_id: str
    tool_use_id: str
    agent_type: str
    description: str
    background: bool
    status: str  # running / completed / failed / killed / stopped / stale
    activity: str
    tool_calls: int
    started_at: float | None
    updated_at: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.agent_id,
            "tool_use_id": self.tool_use_id,
            "type": self.agent_type,
            "description": self.description,
            "background": self.background,
            "status": self.status,
            "activity": self.activity,
            "tool_calls": self.tool_calls,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
        }


_METAS: dict[str, tuple[float, dict]] = {}


def _read_meta(path: Path) -> dict:
    try:
        mtime = path.stat().st_mtime
        cached = _METAS.get(str(path))
        if cached and cached[0] == mtime:
            return cached[1]
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    data = data if isinstance(data, dict) else {}
    if len(_METAS) >= _MAX_CACHED_LOGS:
        _METAS.pop(next(iter(_METAS)))
    _METAS[str(path)] = (mtime, data)
    return data


def _status(done: bool, note: tuple[str, float | None] | None, updated: float, now: float) -> str:
    note_status, note_ts = note or ("", None)
    if done:
        return "completed"
    if note_status in TERMINAL_NOTIFICATION_STATUSES and (note_ts is None or note_ts >= updated - 1):
        return note_status
    if now - updated > STALE_SECONDS:
        return "stale"
    return "running"


def load_subagents(
    transcript_path: str | os.PathLike[str],
    tool_use_ids: set[str] | frozenset[str] = frozenset(),
    notifications: dict[str, tuple[str, float | None]] | None = None,
    now: float | None = None,
    finished_tool_uses: set[str] | frozenset[str] = frozenset(),
) -> list[SubagentState]:
    """返回这条会话里需要展示的子智能体：被 ``tool_use_ids`` 点名的，加上最近还在写日志的。

    ``notifications`` 是父会话里 ``<task-notification>`` 给出的 ``{agent_id: (status, ts)}``。
    ``finished_tool_uses``：父会话里已经拿到结果的前台 Agent 调用。前台子智能体被
    Esc 打断或出错时既不写 end_turn 也没有通知，父会话收到结果就是它结束的唯一记录。
    """
    now = time.time() if now is None else now
    notifications = notifications or {}
    directory = subagents_dir(transcript_path)
    try:
        metas = sorted(directory.glob("agent-*.meta.json"))
    except OSError:
        return []
    states: list[SubagentState] = []
    for meta_path in metas:
        agent_id = meta_path.name[len("agent-"): -len(".meta.json")]
        log_path = meta_path.with_name(f"agent-{agent_id}.jsonl")
        try:
            mtime = log_path.stat().st_mtime
        except OSError:
            continue
        meta = _read_meta(meta_path)
        tool_use_id = str(meta.get("toolUseId") or "")
        if tool_use_id not in tool_use_ids and now - mtime > RECENT_SECONDS:
            continue
        log = _log_for(log_path)
        updated = log.last_ts or mtime
        status = _status(log.done or tool_use_id in finished_tool_uses, notifications.get(agent_id), updated, now)
        states.append(
            SubagentState(
                agent_id=agent_id,
                tool_use_id=tool_use_id,
                agent_type=str(meta.get("agentType") or "agent"),
                description=str(meta.get("description") or ""),
                background=meta.get("requestShape") == "background",
                status=status,
                activity=log.activity,
                tool_calls=log.tool_calls,
                started_at=log.first_ts,
                updated_at=updated,
            )
        )
    states.sort(key=lambda s: (s.started_at or 0, s.agent_id))
    return states


def live_batch(states: list[SubagentState]) -> list[SubagentState]:
    """正在跑的这一批：所有运行中的，加上和它们同一批派出、已经结束的。

    没有运行中的就返回空列表 —— 实时面板只在有人干活时出现，结束后的状态留在
    时间线里各自的子智能体条目上。
    """
    running = [s for s in states if s.status == "running"]
    if not running:
        return []
    batch_start = min((s.started_at for s in running if s.started_at is not None), default=None)
    if batch_start is None:
        return running
    batch = [s for s in states if s.status == "running" or (s.started_at or 0) >= batch_start - 120]
    # 还在干活的排前面，一眼看到谁没做完。
    return sorted(batch, key=lambda s: (s.status != "running", s.started_at or 0, s.agent_id))


def load_workflow_run(
    transcript_path: str | os.PathLike[str],
    run_id: str,
    name: str = "",
    notification: tuple[str, float | None] | None = None,
    now: float | None = None,
) -> SubagentState | None:
    """一次 Workflow 运行汇总成一行：几个子任务做完了、最近一个在干什么。

    子任务日志在 ``subagents/workflows/<run_id>/agent-*.jsonl``。工作流分阶段派活，
    阶段之间所有子任务都可能刚好做完，所以"整个工作流结束"只认父会话的通知。
    """
    now = time.time() if now is None else now
    if not WORKFLOW_RUN_RE.match(run_id):
        return None
    run_dir = subagents_dir(transcript_path) / "workflows" / run_id
    try:
        paths = sorted(run_dir.glob("agent-*.jsonl"))
        run_mtime = run_dir.stat().st_mtime
    except OSError:
        return None
    logs = [(p, _log_for(p)) for p in paths]
    updated = max((log.last_ts for _p, log in logs if log.last_ts), default=run_mtime)
    started = min((log.first_ts for _p, log in logs if log.first_ts), default=None)
    done = sum(1 for _p, log in logs if log.done)
    # 工作流没有"被续上"这回事，父会话的终态通知直接算数，不比时间先后。
    status = _status(False, (notification[0], None) if notification else None, updated, now)
    activity = f"{done}/{len(logs)} 个子任务完成" if logs else "启动中"
    active = [log for _p, log in logs if not log.done and log.activity]
    if status == "running" and active:
        latest = max(active, key=lambda log: log.last_ts or 0)
        activity += f" · 最近：{latest.activity}"
    return SubagentState(
        agent_id=run_id,
        tool_use_id="",
        agent_type="工作流",
        description=name or run_id,
        background=True,
        status=status,
        activity=activity,
        tool_calls=sum(log.tool_calls for _p, log in logs),
        started_at=started,
        updated_at=updated,
    )


# 父会话尾部里的 <task-notification>：只读最后这么多字节，够覆盖正在跑的这一批。
NOTIFICATION_TAIL_BYTES = 1_000_000
_NOTIFICATION_RE = re.compile(r"<task-id>([^<]+)</task-id>.*?<status>([a-z_]+)</status>", re.S)
_NOTES: dict[str, tuple[float, int, dict[str, tuple[str, float | None]]]] = {}


def parent_notifications(transcript_path: str | os.PathLike[str]) -> dict[str, tuple[str, float | None]]:
    """父会话里各后台任务的最新终态 ``{task_id: (status, ts)}``，按文件变化缓存。"""
    key = str(transcript_path)
    try:
        st = os.stat(key)
    except OSError:
        return {}
    cached = _NOTES.get(key)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]
    notes: dict[str, tuple[str, float | None]] = {}
    with open(key, "rb") as fh:
        fh.seek(max(0, st.st_size - NOTIFICATION_TAIL_BYTES))
        data = fh.read()
    for raw in data.splitlines():
        if b"<task-notification>" not in raw:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        content = (entry.get("message") or {}).get("content") if isinstance(entry, dict) else None
        texts = [content] if isinstance(content, str) else [
            item.get("text", "") for item in content or [] if isinstance(item, dict)
        ]
        for text in texts:
            for task_id, status in _NOTIFICATION_RE.findall(str(text)):
                notes[task_id.strip()] = (status, _parse_ts(entry.get("timestamp")))
    if len(_NOTES) >= _MAX_CACHED_LOGS:
        _NOTES.pop(next(iter(_NOTES)))
    _NOTES[key] = (st.st_mtime, st.st_size, notes)
    return notes


def running_subagents(transcript_path: str | os.PathLike[str], now: float | None = None) -> list[SubagentState]:
    """这条会话此刻还在干活的子智能体（主对话可能已经回完话，但活没干完）。"""
    states = load_subagents(transcript_path, frozenset(), parent_notifications(transcript_path), now)
    return [s for s in states if s.status == "running"]
