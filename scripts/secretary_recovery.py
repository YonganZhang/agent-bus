#!/usr/bin/env python3
"""Identity-safe snapshots and recovery for secretary_web plus Cards metadata."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import claude_sessions  # noqa: E402
import cli_bridge  # noqa: E402
import codex_subagents  # noqa: E402
import provider_state  # noqa: E402

REPO_ROOT = SCRIPT_DIR.parent
DASHBOARD_DIR = REPO_ROOT / "dashboard"

SESSION = os.environ.get("SECRETARY_TMUX_SESSION", "secretary_web")
HOME = Path.home()
# 快照与 Cards 偏好都在默认目录里,绝不能跟着环境变量 CODEX_HOME 走:
# 隔离 Codex 窗口里的 shell 继承的是 ~/.codex-homes/<slug>,负责人在那里跑 recovery
# 就会读写错目录(快照会因读不到 Cards 偏好被判 degraded)。
# 与 window_transition.py / create-isolated-codex-home.sh 用同一个覆盖变量。
DEFAULT_CODEX_HOME = Path(
    os.environ.get("AGENT_BUS_DEFAULT_CODEX_HOME", str(HOME / ".codex"))
).expanduser()
CODEX_HOMES_ROOT = Path(
    os.environ.get("AGENT_BUS_CODEX_HOMES_ROOT", str(HOME / ".codex-homes"))
).expanduser()
CODEX_HOME = DEFAULT_CODEX_HOME
SNAPSHOT_DIR = Path(
    os.environ.get("AGENT_BUS_SNAPSHOT_DIR", str(CODEX_HOME / "tmux-snapshots"))
).expanduser()
PREFS_PATH = cli_bridge.DASHBOARD_STATE_DIR / "prefs.json"
AI_SESSION_SHELL = Path(
    os.environ.get("AGENT_BUS_SESSION_SHELL", str(REPO_ROOT / "bin" / "ai-session-shell"))
).expanduser()
SECRETARY_BUS = REPO_ROOT / "bin" / "agent-bus"
SCHEMA_VERSION = 3
# 故意不含 ai-session-shell: wrapper 只在「AI 还没 exec 起来」的那一瞬间处于前台,
# AI 退出后它会 exec 回 /bin/bash。把它当 shell 会在窗口刚拉起时把正在启动的会话 -k 掉,
# 也会让"wrapper 还在 = AI 还在"这个判据失效。
SHELL_COMMANDS = {"bash", "sh", "zsh", "fish"}
# 上一版快照里的 session id 最多沿用多久(见 apply_sticky_providers)。
STICKY_MAX_AGE_SECONDS = int(os.environ.get("RECOVERY_STICKY_MAX_AGE", 7 * 24 * 3600))
UUID_RE = re.compile(
    r"(?<![0-9a-f])"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"(?![0-9a-f])",
    re.I,
)
CODEX_RESUME_RE = re.compile(
    r"\bcodex\s+resume\s+"
    + UUID_RE.pattern,
    re.I,
)
# --session-id 也算稳定身份: agent_window.sh 起新 claude 时会自己钉一个 uuid,
# 这样窗口从出生起就带号,不必等 dashboard 靠屏幕内容去猜。
CLAUDE_RESUME_RE = re.compile(
    r"\bclaude\s+(?:--resume|--session-id)\s+"
    + UUID_RE.pattern,
    re.I,
)


class RecoveryError(RuntimeError):
    """Expected operator-facing recovery failure."""


def run(
    args: list[str],
    *,
    timeout: float = 15,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def utcish_stamp(now: datetime | None = None) -> str:
    current = now or datetime.now().astimezone()
    return current.strftime("%Y%m%dT%H%M%S%z")


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode()


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(json_bytes(value))
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"cannot read JSON snapshot {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RecoveryError(f"JSON snapshot is not an object: {path}")
    return value


def read_proc_argv(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [item.decode(errors="replace") for item in raw.split(b"\0") if item]


def read_proc_env(pid: int, key: str) -> str:
    try:
        entries = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return ""
    prefix = key.encode() + b"="
    for item in entries:
        if item.startswith(prefix):
            return item[len(prefix):].decode(errors="replace")
    return ""


def process_tree() -> tuple[dict[int, list[int]], dict[int, str]]:
    children: dict[int, list[int]] = defaultdict(list)
    commands: dict[int, str] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
            tail = stat[stat.rfind(")") + 2:].split()
            ppid = int(tail[1])
            command = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
        except (OSError, ValueError, IndexError):
            continue
        children[ppid].append(pid)
        commands[pid] = command
    return dict(children), commands


def descendants(root_pid: int, children: dict[int, list[int]], max_depth: int = 5) -> list[int]:
    found: list[int] = []
    queue: deque[tuple[int, int]] = deque([(root_pid, 0)])
    seen: set[int] = set()
    while queue:
        pid, depth = queue.popleft()
        if pid in seen or depth > max_depth:
            continue
        seen.add(pid)
        found.append(pid)
        for child in children.get(pid, []):
            queue.append((child, depth + 1))
    return found


def _provider_from_text(text: str) -> tuple[str, str]:
    codex = CODEX_RESUME_RE.search(text)
    if codex:
        return "codex", codex.group(1).lower()
    claude = CLAUDE_RESUME_RE.search(text)
    if claude:
        return "claude", claude.group(1).lower()
    return "", ""


def _looks_like_provider(argv: list[str], command: str) -> str:
    tokens = [Path(item).name for item in argv]
    if command == "claude" or "claude" in tokens:
        return "claude"
    if command == "codex" or "codex" in tokens:
        return "codex"
    if any(item == "codex" or item.endswith("/bin/codex") for item in argv):
        return "codex"
    return ""


def _argv_identity_is_trustworthy(argv: list[str], command: str, kind: str) -> bool:
    """这个进程本身是不是它 argv 所声称的那个 provider。

    只判"不是 shell"或"argv 里某个 token 叫 claude"都不够: 任意进程(python3、别的
    wrapper)只要参数里抄了一句启动命令,就会顶替真身份,把一个空窗口压成 already-live,
    stale-shell 从此永远不成立。所以只认可执行位置:
      - 进程名就是 claude / codex;
      - ai-session-shell wrapper(它的 argv 就是真实启动命令,且 AI 退出后会 exec 回 bash);
      - node 起的 codex,要求 entry script 的 basename 是 codex/codex.js。
    """
    if not kind:
        return False
    name = Path(command or "").name
    if name == kind:
        return True
    if name.startswith("ai-session-shel"):
        return True
    if kind == "codex" and name in {"node", "npm", "npx"}:
        return any(Path(token).name in {"codex", "codex.js"} for token in argv[1:3])
    # wrapper 以 `bash /path/ai-session-shell ...` 形式出现时 command 是 bash,
    # 但 argv[1] 指向 wrapper 本身。
    if name in SHELL_COMMANDS and len(argv) > 1:
        return Path(argv[1]).name.startswith("ai-session-shel")
    return False


def _codex_home_from_rollout(path: str) -> str:
    """``<CODEX_HOME>/sessions/YYYY/MM/DD/rollout-*.jsonl`` -> ``<CODEX_HOME>``.

    这是 Codex 进程自己打开的文件,比任何环境变量都可靠: 隔离窗口的外层 wrapper 是
    ``ai-session-shell env CODEX_HOME=... codex``,只有子进程带着隔离目录,wrapper
    自己的 environ 里读不到。
    """
    rollout = Path(str(path or ""))
    parents = rollout.parents
    if not rollout.name.startswith("rollout-") or len(parents) < 5:
        return ""
    if parents[3].name != "sessions":
        return ""
    return str(parents[4])


def _codex_home_from_argv(argv: list[str]) -> str:
    """``env CODEX_HOME=<home> ... codex`` 形态的启动参数里写明的 home(只作兜底)。"""
    for index, token in enumerate(argv):
        if Path(token).name == "env":
            for item in argv[index + 1:]:
                if item.startswith("CODEX_HOME="):
                    return item[len("CODEX_HOME="):]
                if "=" not in item or item.startswith("-"):
                    break
    return ""


def _same_home(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return Path(left).expanduser().resolve(strict=False) == Path(right).expanduser().resolve(
        strict=False
    )


def _process_ai_kind(pid: int, command: str) -> str:
    name = Path(command or "").name
    if name in {"claude", "codex"}:
        return name
    if name in {"node", "npm", "npx"}:
        argv = read_proc_argv(pid)
        if any(Path(token).name in {"codex", "codex.js"} for token in argv[1:3]):
            return "codex"
    return ""


def _outermost_ai(
    pane_pid: int,
    children: dict[int, list[int]],
    commands: dict[int, str],
    max_depth: int = 6,
) -> tuple[int, str]:
    """离 pane 最近的那个 AI 进程。窗口的身份属于它,而不是它再派生出的 AI
    (Claude 在工具里跑 `codex exec`、Codex 在 shell 里再起一个 codex)。"""
    queue: deque[tuple[int, int]] = deque([(pane_pid, 0)])
    seen: set[int] = set()
    while queue:
        pid, depth = queue.popleft()
        if pid in seen or depth > max_depth:
            continue
        seen.add(pid)
        kind = _process_ai_kind(pid, commands.get(pid, ""))
        if kind:
            return pid, kind
        for child in children.get(pid, []):
            queue.append((child, depth + 1))
    return 0, ""


def _codex_scan_pids(
    root_pid: int,
    children: dict[int, list[int]],
    commands: dict[int, str],
) -> list[int]:
    """AI 子树里最外层的原生 codex 进程;它下面再嵌套的 codex 打开的 rollout 不算这个窗口的。"""
    found: list[int] = []
    stack: list[tuple[int, bool]] = [(root_pid, False)]
    seen: set[int] = set()
    while stack:
        pid, under_codex = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        is_codex = Path(commands.get(pid, "")).name == "codex"
        if is_codex and not under_codex:
            found.append(pid)
        for child in children.get(pid, []):
            stack.append((child, under_codex or is_codex))
    return sorted(found)


def _is_subagent_snapshot(snapshot: dict[str, Any]) -> bool:
    return bool(
        snapshot.get("source_kind") == "subagent"
        or snapshot.get("parent_thread_id")
        or codex_subagents.is_subagent_rollout(str(snapshot.get("path") or ""))
    )


def codex_live_identity(pids: list[int]) -> dict[str, Any]:
    """Codex 进程此刻打开着的根 rollout(排除子智能体)。

    返回 ``{session_id, history_record_id, codex_home, rollout}``;打开的根 rollout
    不止一条时返回 ``{"ambiguous": [...]}``;一条都没有时返回 ``{}``。/new 之后 argv
    里的号就过期了,只有这里读到的才是窗口当前那条对话。
    """
    snapshots: list[dict[str, Any]] = []
    for path in provider_state.rollout_paths_for_pids(pids):
        try:
            snapshot = provider_state.rollout_snapshot(path, 40)
        except OSError:
            continue
        if snapshot.get("thread_id"):
            snapshots.append({**snapshot, "path": str(path)})
    roots = [item for item in snapshots if not _is_subagent_snapshot(item)]
    chosen = provider_state.select_exact_rollout(roots) if len(roots) == 1 else None
    if chosen is None:
        if len(roots) > 1:
            return {"ambiguous": sorted({str(item["thread_id"]).lower() for item in roots})}
        return {}
    path = str(chosen["path"])
    return {
        "session_id": str(chosen["thread_id"]).lower(),
        "history_record_id": Path(path).stem,
        "codex_home": _codex_home_from_rollout(path),
        "rollout": path,
    }


def _codex_process_home(pids: list[int]) -> str:
    """原生 codex 进程 environ 里的 CODEX_HOME;没设就是它自己 HOME 下的 .codex。"""
    for pid in pids:
        try:
            entries = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except OSError:
            continue
        env = {}
        for item in entries:
            key, sep, value = item.partition(b"=")
            if sep:
                env[key.decode(errors="replace")] = value.decode(errors="replace")
        if env.get("CODEX_HOME"):
            return env["CODEX_HOME"]
        if env.get("HOME"):
            return str(Path(env["HOME"]) / ".codex")
    return ""


def _session_id_from_path(path: str) -> str:
    matches = UUID_RE.findall(Path(path).name)
    return matches[-1].lower() if matches else ""


def _history_identity_from_path(path: str) -> tuple[str, str]:
    path = path.strip()
    return _session_id_from_path(path), Path(path).stem if path else ""


def _dashboard_resolved_history(pane_id: str, kind: str) -> tuple[str, str]:
    """Use the dashboard's pane-scoped transcript resolver only as ambiguity fallback."""
    code = (
        "import sys;"
        f"sys.path.insert(0,{str(DASHBOARD_DIR)!r});"
        f"sys.path.insert(0,{str(SCRIPT_DIR)!r});"
        "import server;"
        f"kind={kind!r};pane={pane_id!r};"
        "path=(server.codex_rollout_path_for_pane_id(pane)[0] "
        "if kind=='codex' else server.transcript_path_for_pane_id(pane));"
        "print(path or '')"
    )
    try:
        result = run([sys.executable, "-c", code], timeout=12)
    except (subprocess.SubprocessError, OSError):
        return "", ""
    return _history_identity_from_path(result.stdout)


def _claude_map_history(pane_id: str, cwd: str) -> tuple[str, str]:
    path = cli_bridge.DASHBOARD_STATE_DIR / "claude_map.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", ""
    item = data.get(pane_id) if isinstance(data, dict) else None
    if isinstance(item, str):
        transcript = item
        mapped_cwd = ""
    elif isinstance(item, dict):
        transcript = str(item.get("last_good") or item.get("path") or "")
        mapped_cwd = str(item.get("cwd") or "")
    else:
        return "", ""
    if mapped_cwd and cwd and mapped_cwd != cwd:
        return "", ""
    return _history_identity_from_path(transcript)


def _provider_record(
    kind: str,
    session_id: str,
    source: str,
    *,
    codex_home: str = "",
    history_record_id: str = "",
) -> dict[str, Any]:
    session_id = str(session_id or "").lower()
    return {
        "kind": kind,
        "session_id": session_id,
        # Kept for backward-compatible restore plans and launch commands.
        "resume_id": session_id,
        "history_record_id": str(history_record_id or ""),
        "history_kind": (
            "codex-rollout"
            if kind == "codex"
            else "claude-transcript"
            if kind == "claude"
            else ""
        ),
        "source": source,
        "codex_home": codex_home,
        "recoverable": bool(kind and session_id),
    }


def resolve_provider(
    pane_pid: int,
    pane_id: str,
    cwd: str,
    children: dict[int, list[int]],
    commands: dict[int, str],
    *,
    claude_records: Any = None,
    stamped_codex_home: str = "",
) -> dict[str, Any]:
    """窗口当前那条会话。优先级(SOP"实时优先"):

    1. 实时信号: Claude 用 ``claude agents`` 按进程树 pid 绑定的会话号;Codex 用它
       打开着的根 rollout(排除子智能体)。Claude 里 /clear、/resume,Codex 里 /new
       之后启动参数就过期了,只有这一层跟得上。
    2. 启动参数 argv 只作兜底;与实时信号不一致时记 ``identity_conflict``,
       绝不静默选 argv。
    Codex 的 CODEX_HOME 同样按"打开的 rollout 路径 > 进程 environ > pane 标记
    ``@ai_codex_home`` > argv 里的 env CODEX_HOME="取。

    ``claude_records`` 是返回 ``claude agents --json`` 记录的可调用对象(或列表);
    None 表示本次不查(测试和只读预览用)。
    """
    pids = descendants(pane_pid, children)
    detected_kind = ""
    argv_identity: tuple[str, str, int] | None = None
    argv_home = ""
    for pid in pids:
        argv = read_proc_argv(pid)
        text = " ".join(argv)
        kind, resume_id = _provider_from_text(text)
        # 只认"进程本身就是 provider"的 argv。shell 的 argv 里可能整条抄着启动命令
        # (`bash -lc 'claude --session-id X; exec bash'`),AI 早退了那串字符还在,
        # 照抄就会把一个空窗口报成活着的会话,relaunch 也就永远救不了它。
        command_name = commands.get(pid, "")
        if (
            kind
            and resume_id
            and argv_identity is None
            and _argv_identity_is_trustworthy(argv, command_name, kind)
        ):
            argv_identity = (kind, resume_id, pid)
            detected_kind = kind
            argv_home = _codex_home_from_argv(argv) if kind == "codex" else ""
        inferred = _looks_like_provider(argv, command_name)
        if inferred and not detected_kind:
            detected_kind = inferred

    ai_pid, ai_kind = _outermost_ai(pane_pid, children, commands)
    kind = ai_kind or detected_kind
    live_id = ""
    live_source = ""
    live_history = ""
    home = ""
    home_source = ""
    ambiguous: list[str] = []
    if ai_kind == "claude" and claude_records is not None:
        records = claude_records() if callable(claude_records) else claude_records
        record = claude_sessions.record_for(
            list(records or []), set(_all_descendants(ai_pid, children))
        )
        candidate = str((record or {}).get("sessionId") or "").lower()
        if UUID_RE.fullmatch(candidate):
            live_id, live_source, live_history = candidate, "claude-agents-pid", candidate
    elif ai_kind == "codex":
        scan = _codex_scan_pids(ai_pid, children, commands)
        live = codex_live_identity(scan)
        ambiguous = list(live.get("ambiguous") or [])
        if live.get("session_id"):
            live_id = str(live["session_id"])
            live_source = "open-rollout-fd"
            live_history = str(live.get("history_record_id") or "")
            if live.get("codex_home"):
                home, home_source = str(live["codex_home"]), "open-rollout"
        if not home:
            env_home = _codex_process_home(scan)
            if env_home:
                home, home_source = env_home, "process-environ"
    if kind == "codex" and not home and stamped_codex_home:
        home, home_source = stamped_codex_home, "tmux-pane-option"
    if kind == "codex" and not home and argv_home:
        home, home_source = argv_home, "argv-env"

    def record(session_id: str, source: str, history_record_id: str = "") -> dict[str, Any]:
        item = _provider_record(
            kind,
            session_id,
            source,
            codex_home=home if kind == "codex" else "",
            history_record_id=history_record_id,
        )
        if kind == "codex" and home_source:
            item["codex_home_source"] = home_source
        return item

    argv_same_kind = bool(argv_identity and (not ai_kind or argv_identity[0] == ai_kind))
    if live_id:
        item = record(live_id, live_source, live_history)
        if argv_same_kind and argv_identity and argv_identity[1] != live_id:
            item["identity_conflict"] = {
                "argv": argv_identity[1],
                "live": live_id,
                "resolution": "live",
            }
        return item

    if argv_same_kind and argv_identity:
        _, resume_id, _ = argv_identity
        if ambiguous and resume_id not in ambiguous:
            # 打开着好几条根 rollout,却没有一条是 argv 那条: argv 已经过期,
            # 又说不清是哪一条 —— 身份不可证,不能拿 argv 充数。
            item = record("", "identity-conflict")
            item["identity_conflict"] = {
                "argv": resume_id,
                "open_rollouts": ambiguous,
                "resolution": "unresolved",
            }
            return item
        history_record_id = ""
        if kind == "codex":
            resolved_session_id, resolved_record_id = _dashboard_resolved_history(
                pane_id, "codex"
            )
            if resolved_session_id == resume_id:
                history_record_id = resolved_record_id
        elif kind == "claude":
            # Claude transcript filenames are the provider session UUID.
            history_record_id = resume_id
        return record(resume_id, "process-argv", history_record_id)

    if kind == "codex" and not ambiguous:
        resume_id, history_record_id = _dashboard_resolved_history(pane_id, "codex")
        if resume_id:
            return record(resume_id, "dashboard-pane-resolver", history_record_id)
    if kind == "claude":
        resume_id, history_record_id = _claude_map_history(pane_id, cwd)
        if not resume_id:
            resume_id, history_record_id = _dashboard_resolved_history(pane_id, "claude")
        if resume_id:
            return record(resume_id, "claude-map-or-dashboard", history_record_id)
    item = record("", "provider-without-stable-session" if kind else "shell-only")
    if ambiguous:
        item["identity_conflict"] = {"open_rollouts": ambiguous, "resolution": "unresolved"}
    return item


def read_cards_prefs() -> tuple[dict[str, Any], str]:
    try:
        prefs = json.loads(PREFS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, str(exc)
    if not isinstance(prefs, dict):
        return {}, "prefs root is not an object"
    return prefs, ""


def preference_key(session: str, window: int, pane_index: int, cwd: str) -> str:
    target = f"{session}:{window}.{pane_index}"
    return f"{target}|{cwd}" if cwd else target


def _pref_value(mapping: Any, keys: list[str], default: Any = "") -> Any:
    if not isinstance(mapping, dict):
        return default
    for key in keys:
        if key in mapping:
            return mapping[key]
    return default


def _provider_session_id(provider: dict[str, Any]) -> str:
    return str(provider.get("session_id") or provider.get("resume_id") or "")


def _provider_is_live(provider: dict[str, Any]) -> bool:
    """Return whether the identity is backed by a currently running provider.

    Pane options and sticky snapshot inheritance preserve the identity needed
    for recovery after the process exits.  They are deliberately *not* proof
    that the provider process is still alive.
    """
    if not _provider_session_id(provider):
        return False
    source = str(provider.get("source") or "")
    return source != "tmux-pane-option" and not source.startswith("sticky:")


def _cards_window_record(session: str, pane: dict[str, Any]) -> dict[str, Any]:
    provider = pane.get("provider") if isinstance(pane.get("provider"), dict) else {}
    cards = pane.get("cards") if isinstance(pane.get("cards"), dict) else {}
    window_index = int(pane.get("window_index") or 0)
    pane_index = int(pane.get("pane_index") or 0)
    window_name = str(pane.get("window_name") or "")
    alias = str(cards.get("alias") or "")
    return {
        "target": f"{session}:{window_index}.{pane_index}",
        "preference_key": str(pane.get("preference_key") or ""),
        "pane_id": str(pane.get("pane_id") or ""),
        "window_index": window_index,
        "pane_index": pane_index,
        "window_name": window_name,
        "cards_alias": alias,
        "display_name": alias or window_name,
        "provider": str(provider.get("kind") or ""),
        "session_id": _provider_session_id(provider),
        "history_record_id": str(provider.get("history_record_id") or ""),
        "history_kind": str(provider.get("history_kind") or ""),
        "recoverable": bool(provider.get("recoverable")),
        "category": str(cards.get("category") or ""),
        "favorite": bool(cards.get("favorite")),
        "order_rank": cards.get("order_rank"),
        "cwd": str(pane.get("cwd") or ""),
    }


def _cards_window_reference(window: dict[str, Any]) -> dict[str, Any]:
    """Keep category/favorite membership readable without duplicating every field."""
    return {
        "target": window["target"],
        "window_name": window["window_name"],
        "display_name": window["display_name"],
        "session_id": window["session_id"],
        "history_record_id": window["history_record_id"],
    }


def build_cards_manifest(
    session: str,
    panes: list[dict[str, Any]],
    prefs: dict[str, Any],
) -> dict[str, Any]:
    """Derive a Cards-centered audit view from pane records and raw preferences."""
    windows = [_cards_window_record(session, pane) for pane in panes]
    windows.sort(key=lambda item: (item["window_index"], item["pane_index"]))

    category_names = [
        str(value)
        for value in prefs.get("categories", [])
        if str(value) and str(value) not in {"最近", "全部"}
    ]
    for window in windows:
        category = str(window["category"])
        if category and category not in category_names:
            category_names.append(category)

    categories = []
    for category in category_names:
        members = [
            _cards_window_reference(window)
            for window in windows
            if window["category"] == category
        ]
        categories.append(
            {
                "name": category,
                "window_count": len(members),
                "windows": members,
            }
        )

    favorites = [
        _cards_window_reference(window)
        for window in windows
        if window["favorite"]
    ]
    uncategorized = [
        _cards_window_reference(window)
        for window in windows
        if not window["category"]
    ]
    return {
        "summary": {
            "window_count": len(windows),
            "favorite_count": len(favorites),
            "categorized_count": len(windows) - len(uncategorized),
            "uncategorized_count": len(uncategorized),
            "session_id_count": sum(bool(window["session_id"]) for window in windows),
            "history_record_id_count": sum(
                bool(window["history_record_id"]) for window in windows
            ),
        },
        "windows": windows,
        "favorites": favorites,
        "categories": categories,
        "uncategorized": uncategorized,
    }


def cards_manifest_errors(snapshot: dict[str, Any]) -> list[str]:
    """Validate the schema-v3 manifest against its canonical panes/raw prefs."""
    if int(snapshot.get("schema_version") or 0) < 3:
        return []
    cards = snapshot.get("cards") if isinstance(snapshot.get("cards"), dict) else {}
    manifest = cards.get("manifest")
    if not isinstance(manifest, dict):
        return ["cards-manifest-missing"]
    panes = snapshot.get("panes") if isinstance(snapshot.get("panes"), list) else []
    prefs = cards.get("prefs") if isinstance(cards.get("prefs"), dict) else {}
    expected = build_cards_manifest(str(snapshot.get("session") or SESSION), panes, prefs)
    if manifest != expected:
        return ["cards-manifest-out-of-sync"]
    expected_sha = hashlib.sha256(
        json.dumps(expected, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    if str(cards.get("manifest_sha256") or "") != expected_sha:
        return ["cards-manifest-hash-mismatch"]
    return []


def tmux_server_id() -> str:
    """标识一次 tmux server 生命周期。

    pane id(%N)只在一次 server 生命周期内唯一: server 重启后又从 %0 开始发号,
    于是"同一个 pane id"跨重启就不再能证明"同一个 pane"。凡是拿 pane id 当身份用的
    地方(sticky 继承、stale-shell 判定),都必须先确认还是同一个 server。
    """
    try:
        result = run(["tmux", "display-message", "-p", "#{pid}"], timeout=10)
    except (subprocess.SubprocessError, OSError):
        return ""
    pid = result.stdout.strip()
    if not pid.isdigit():
        return ""
    try:
        # /proc/<pid>/stat 的 starttime 字段,防的是 pid 本身被复用。
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[-1].split()
        start = stat[19]
    except (OSError, IndexError):
        start = ""
    return f"{pid}:{start}" if start else ""


def _parse_iso_ts(value: Any) -> float | None:
    """解析失败返回 None,调用方必须 fail closed —— 返回 0 会让记录永不过期。"""
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def apply_sticky_providers(
    panes: list[dict[str, Any]],
    baseline: dict[str, Any] | None,
    *,
    now: float | None = None,
    server_id: str = "",
) -> int:
    """给「AI 进程已退出、当前解析不出 session id」的 pane 补回上一版快照里的 id。

    为什么需要: dashboard 只对活着的 Claude/Codex pane 解析历史,进程一死 provider
    立刻变空,快照跟着丢号,下一轮恢复就只能按 cwd 猜 —— 同一工作区有多个窗口时必然
    串到同一条对话,或者掉成 already-live-key、再也认不回自己的会话。

    安全边界: 只补空值; 要求 preference_key 与 cwd 都一致; 从最初一次 sticky 起算
    超过 STICKY_MAX_AGE_SECONDS 就不再继承; 继承来的记录始终带 sticky 标记,plan /
    verify 会把它和真正实时解析出来的身份区分开。
    """
    if not isinstance(baseline, dict):
        return 0
    # 跨 tmux server 生命周期时 pane id 会被重新发号,继承等于给新窗口贴旧会话。
    # 读不出 server 身份也一并 fail closed。
    baseline_server = str(baseline.get("tmux_server") or "")
    if not server_id or not baseline_server or server_id != baseline_server:
        return 0
    now = time.time() if now is None else now
    # 按 pane_id 索引: window:pane|cwd 这种 key 在窗口号被复用时会重合(而 cwd 本来就
    # 是 key 的一部分,不构成第二重约束),只有 tmux 的 pane id 能证明"还是同一个 pane"。
    previous: dict[str, dict[str, Any]] = {}
    for pane in baseline.get("panes") or []:
        if isinstance(pane, dict) and str(pane.get("pane_id") or ""):
            previous[str(pane["pane_id"])] = pane
    applied = 0
    for pane in panes:
        provider = pane.get("provider") if isinstance(pane.get("provider"), dict) else {}
        if _provider_session_id(provider):
            continue
        old = previous.get(str(pane.get("pane_id") or ""))
        if not isinstance(old, dict):
            continue
        if str(old.get("cwd") or "") != str(pane.get("cwd") or ""):
            continue
        if str(old.get("preference_key") or "") != str(pane.get("preference_key") or ""):
            continue
        old_provider = old.get("provider") if isinstance(old.get("provider"), dict) else {}
        old_id = _provider_session_id(old_provider)
        old_kind = str(old_provider.get("kind") or "")
        if not old_id or not old_kind:
            continue
        since = str(old_provider.get("sticky_since") or baseline.get("captured_at") or "")
        since_ts = _parse_iso_ts(since)
        if since_ts is None or now - since_ts > STICKY_MAX_AGE_SECONDS:
            continue  # 读不出时间就当过期,宁可丢号也不要绑一个不知多旧的会话
        source = str(old_provider.get("source") or "unknown")
        if not source.startswith("sticky:"):
            source = f"sticky:{source}"
        pane["provider"] = {
            **old_provider,
            "sticky": True,
            "sticky_since": since,
            "source": source,
            "recoverable": True,
        }
        applied += 1
    return applied


def _lazy_claude_records():
    """``claude agents --json`` 只在真的遇到 Claude 进程时才调用一次(最多 4 秒)。"""
    cache: dict[str, Any] = {}

    def records() -> list[dict[str, Any]]:
        if "records" not in cache:
            cache["records"], cache["error"] = claude_sessions.agent_records()
        return cache["records"]

    def error() -> str:
        return str(cache.get("error") or "")

    return records, error


def _pane_provider(
    pane_pid: int,
    pane_id: str,
    cwd: str,
    children: dict[int, list[int]],
    commands: dict[int, str],
    *,
    stamped_kind: str = "",
    stamped_session: str = "",
    stamped_home: str = "",
    claude_records: Any = None,
) -> dict[str, Any]:
    """实时解析 + pane 标记合并。capture_state 与 identity 子命令共用这一份。"""
    stamped_kind = stamped_kind.strip().lower()
    stamped_session = stamped_session.strip().lower()
    stamped_home = stamped_home.strip()
    stamp_ok = stamped_kind in {"claude", "codex"} and bool(UUID_RE.fullmatch(stamped_session))
    provider = resolve_provider(
        pane_pid,
        pane_id,
        cwd,
        children,
        commands,
        claude_records=claude_records,
        stamped_codex_home=stamped_home,
    )
    detected_kind = str(provider.get("kind") or "")
    session_id = _provider_session_id(provider)
    conflict = provider.get("identity_conflict") if isinstance(provider.get("identity_conflict"), dict) else {}
    # Pane 上的标记在进程退出后仍保留,所以单独出现时只证明"可恢复"。若现场同时
    # 观测到同 provider 的活进程但解析不出唯一会话号,二者合并才构成活身份。
    # provider kind 不同则标记可能陈旧,不能覆盖。
    if not session_id and stamp_ok:
        open_rollouts = conflict.get("open_rollouts") if conflict else None
        if open_rollouts and stamped_session not in open_rollouts:
            return provider  # 标记不在打开的根 rollout 里:它自己也可能过期,不拿来充数
        if detected_kind == stamped_kind:
            # The stamped session id was recorded together with its home; pair
            # them.  If the live process runs under a different home, the stamp
            # belongs to another run and cannot confirm this one.
            live_home = str(provider.get("codex_home") or "")
            if (
                stamped_kind == "codex"
                and stamped_home
                and live_home
                and os.path.realpath(stamped_home) != os.path.realpath(live_home)
            ):
                return provider
            merged = _provider_record(
                stamped_kind,
                stamped_session,
                "live-process+tmux-pane-option",
                codex_home=(stamped_home or live_home) if stamped_kind == "codex" else "",
                history_record_id=stamped_session if stamped_kind == "claude" else "",
            )
            if conflict:
                merged["identity_conflict"] = {**conflict, "resolution": "pane-option"}
            return merged
        if not detected_kind:
            record = _provider_record(
                stamped_kind,
                stamped_session,
                "tmux-pane-option",
                codex_home=stamped_home if stamped_kind == "codex" else "",
                history_record_id=stamped_session if stamped_kind == "claude" else "",
            )
            if stamped_kind == "codex" and stamped_home:
                record["codex_home_source"] = "tmux-pane-option"
            return record
        return provider
    # 实时信号缺席、只剩 argv 兜底时,若 pane 标记(Claude 的 SessionStart hook 会在
    # /clear、/resume 时更新它;Codex 的由快照按打开的 rollout 补打)说的是另一条,
    # argv 已经过期: 用标记并记冲突,不静默选 argv。
    if (
        stamp_ok
        and session_id
        and provider.get("source") == "process-argv"
        and detected_kind == stamped_kind
        and stamped_session != session_id
    ):
        merged = _provider_record(
            stamped_kind,
            stamped_session,
            "live-process+tmux-pane-option",
            codex_home=str(provider.get("codex_home") or ""),
            history_record_id=stamped_session if stamped_kind == "claude" else "",
        )
        if provider.get("codex_home_source"):
            merged["codex_home_source"] = provider["codex_home_source"]
        merged["identity_conflict"] = {
            "argv": session_id,
            "pane_option": stamped_session,
            "resolution": "pane-option",
        }
        return merged
    return provider


def _stamp_live_codex_pane(
    pane_id: str,
    provider: dict[str, Any],
    stamped_kind: str,
    stamped_session: str,
    stamped_home: str,
) -> list[dict[str, str]]:
    """按 Codex 打开的根 rollout 给 pane 补打 @ai_provider / @ai_session_id / @ai_codex_home。

    Codex 没有 Claude 那样的 SessionStart hook;不补打的话 AI 一退出,pane 上什么都不剩,
    `agent_window.sh restart` 只能把窗口变成普通 bash。
    只认 open-rollout-fd 这一种精确来源;值没变就不写。
    """
    if provider.get("kind") != "codex" or provider.get("source") != "open-rollout-fd":
        return []
    wanted = {
        "@ai_provider": "codex",
        "@ai_session_id": _provider_session_id(provider),
        "@ai_codex_home": str(provider.get("codex_home") or ""),
    }
    current = {
        "@ai_provider": stamped_kind.strip().lower(),
        "@ai_session_id": stamped_session.strip().lower(),
        "@ai_codex_home": stamped_home.strip(),
    }
    applied: list[dict[str, str]] = []
    for option, value in wanted.items():
        if not value or current[option] == value:
            continue
        try:
            result = run(
                ["tmux", "set-option", "-p", "-t", pane_id, option, value],
                timeout=5,
                check=False,
            )
            error = (result.stderr or "").strip()[:200] if result.returncode else ""
        except (subprocess.SubprocessError, OSError) as exc:
            error = str(exc)[:200]
        applied.append({"pane_id": pane_id, "option": option, "value": value, "error": error})
    return applied


def capture_state(
    session: str = SESSION,
    sticky_baseline: dict[str, Any] | None = None,
    *,
    stamp_pane_options: bool = False,
) -> dict[str, Any]:
    """读现场。默认纯只读;只有 snapshot 传 stamp_pane_options=True 才补打 Codex pane 标记。"""
    fmt = "\t".join(
        [
            "#{session_name}",
            "#{window_index}",
            "#{window_name}",
            "#{pane_index}",
            "#{pane_id}",
            "#{pane_pid}",
            "#{pane_current_path}",
            "#{pane_current_command}",
            "#{pane_dead}",
            "#{@ai_provider}",
            "#{@ai_session_id}",
            "#{@ai_codex_home}",
        ]
    )
    try:
        result = run(["tmux", "list-panes", "-a", "-F", fmt], timeout=20)
        lines = result.stdout.splitlines()
        tmux_error = ""
    except (subprocess.SubprocessError, OSError) as exc:
        lines = []
        tmux_error = str(exc)

    prefs, prefs_error = read_cards_prefs()
    pane_order = prefs.get("paneOrder") if isinstance(prefs.get("paneOrder"), list) else []
    order_rank = {str(pane_id): index for index, pane_id in enumerate(pane_order)}
    children, commands = process_tree()
    claude_records, claude_error = _lazy_claude_records()
    stamps: list[dict[str, str]] = []
    panes: list[dict[str, Any]] = []
    for line in lines:
        fields = line.split("\t")
        if len(fields) == 11:
            fields.append("")  # 旧格式 / 测试数据没有 @ai_codex_home 列
        if len(fields) != 12 or fields[0] != session:
            continue
        (
            _, win, window_name, pane_index, pane_id, pane_pid, cwd, command, dead,
            stamped_kind, stamped_session, stamped_home,
        ) = fields
        try:
            win_num = int(win)
            pane_num = int(pane_index)
            pid_num = int(pane_pid)
        except ValueError:
            continue
        key = preference_key(session, win_num, pane_num, cwd)
        target = f"{session}:{win_num}.{pane_num}"
        keys = [key, target, pane_id]
        provider = _pane_provider(
            pid_num,
            pane_id,
            cwd,
            children,
            commands,
            stamped_kind=stamped_kind,
            stamped_session=stamped_session,
            stamped_home=stamped_home,
            claude_records=claude_records,
        )
        if stamp_pane_options:
            stamps.extend(
                _stamp_live_codex_pane(
                    pane_id, provider, stamped_kind, stamped_session, stamped_home
                )
            )
        panes.append(
            {
                "window_index": win_num,
                "window_name": window_name,
                "pane_index": pane_num,
                "pane_id": pane_id,
                "pane_pid": pid_num,
                "cwd": cwd,
                "command": command,
                "dead": dead == "1",
                "preference_key": key,
                "provider": provider,
                "cards": {
                    "category": str(_pref_value(prefs.get("paneCategories"), keys, "") or ""),
                    "favorite": bool(_pref_value(prefs.get("paneFavorites"), keys, False)),
                    "alias": str(_pref_value(prefs.get("paneAliases"), keys, "") or ""),
                    "order_rank": order_rank.get(pane_id),
                },
            }
        )
    panes.sort(key=lambda item: (item["window_index"], item["pane_index"]))
    server_id = tmux_server_id()
    apply_sticky_providers(panes, sticky_baseline, server_id=server_id)

    logical = {
        "session": session,
        "panes": [
            {
                "window_index": pane["window_index"],
                "window_name": pane["window_name"],
                "pane_index": pane["pane_index"],
                "cwd": pane["cwd"],
                "provider": pane["provider"],
                "cards": pane["cards"],
            }
            for pane in panes
        ],
        "categories": prefs.get("categories", []),
    }
    logical_sha = hashlib.sha256(
        json.dumps(logical, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    prefs_sha = hashlib.sha256(
        json.dumps(prefs, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest() if prefs else ""
    captured_at = datetime.now().astimezone().isoformat()
    recoverable = sum(
        1
        for pane in panes
        if pane.get("provider", {}).get("recoverable")
    )
    manifest = build_cards_manifest(session, panes, prefs)
    manifest_sha = hashlib.sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "captured_at": captured_at,
        "session": session,
        "pane_count": len(panes),
        "recoverable_pane_count": recoverable,
        "logical_sha256": logical_sha,
        "tmux_error": tmux_error,
        "tmux_server": server_id,
        # 实时身份信号读不到时要看得见(否则只会表现为 Claude 身份悄悄退回 argv)。
        "provider_errors": {"claude_agents": claude_error()} if claude_error() else {},
        "pane_option_stamps": stamps,
        "cards": {
            "prefs": prefs,
            "prefs_sha256": prefs_sha,
            "manifest": manifest,
            "manifest_sha256": manifest_sha,
            "error": prefs_error,
        },
        "panes": panes,
    }


def _recoverable_identities(state: dict[str, Any]) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for pane in state.get("panes") or []:
        if not isinstance(pane, dict):
            continue
        provider = pane.get("provider") if isinstance(pane.get("provider"), dict) else {}
        session_id = _provider_session_id(provider)
        if session_id:
            out.add((str(provider.get("kind") or ""), session_id))
    return out


def degradation_reasons(current: dict[str, Any], baseline: dict[str, Any] | None) -> list[str]:
    reasons: list[str] = []
    current_count = int(current.get("pane_count") or 0)
    if current.get("tmux_error"):
        reasons.append("tmux-inventory-error")
    if current_count == 0:
        reasons.append("no-live-panes")
    cards = current.get("cards") if isinstance(current.get("cards"), dict) else {}
    if cards.get("error"):
        reasons.append("cards-prefs-unreadable")
    baseline_count = int((baseline or {}).get("pane_count") or 0)
    if baseline_count >= 5:
        minimum = max(2, math.ceil(baseline_count * 0.5))
        if current_count < minimum:
            reasons.append(f"sudden-pane-drop:{baseline_count}->{current_count}")
    # 窗口数不掉、会话号却成片消失,同样是灾难:把这种状态提升成 last-known-good,
    # 等于把还能精确恢复的号永久抹掉,下一轮就只剩"窗口在但不知道是哪条对话"。
    if isinstance(baseline, dict):
        before = _recoverable_identities(baseline)
        after = _recoverable_identities(current)
        lost = before - after
        if len(before) >= 5 and (len(lost) >= 5 or len(lost) * 4 > len(before)):
            reasons.append(f"recoverable-identity-loss:{len(before)}->{len(after)}")
    return reasons


def _load_baseline() -> tuple[dict[str, Any] | None, Path | None]:
    for path in (SNAPSHOT_DIR / "latest-good.json", SNAPSHOT_DIR / "latest.json"):
        if not path.is_file():
            continue
        try:
            return load_json(path), path
        except RecoveryError:
            continue
    return None, None


def _trim_files(pattern: str, keep: int) -> None:
    for old in sorted(SNAPSHOT_DIR.glob(pattern))[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass


def auto_restore_lock_path() -> Path:
    """开机自动恢复(contrib/boot/auto-restore)与 `recovery auto --yes` 共用的锁。"""
    return SNAPSHOT_DIR / ".auto-restore.lock"


def recovery_in_progress() -> bool:
    """有恢复流程持有 .auto-restore.lock 时返回 True。只探测、立刻释放,不持有。"""
    path = auto_restore_lock_path()
    if not path.exists():
        return False
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return False


def snapshot_command(args: argparse.Namespace) -> int:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = SNAPSHOT_DIR / ".snapshot.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        baseline, baseline_path = _load_baseline()
        current = capture_state(args.session, sticky_baseline=baseline, stamp_pane_options=True)
        if recovery_in_progress():
            # 恢复进行到一半时,现场是"部分救回"的中间态。每 2 分钟的定时快照若照常判
            # good,就会覆盖 latest-good 与当天 daily,没救回的会话号从基线里消失,最终
            # 验收还是绿的。所以恢复锁被占用期间只写 observed,绝不升级为 good。
            current["health"] = {
                "status": "observed-only",
                "reasons": ["recovery-in-progress"],
                "baseline": str(baseline_path or ""),
                "baseline_pane_count": int((baseline or {}).get("pane_count") or 0),
            }
            atomic_write_json(SNAPSHOT_DIR / "latest-observed.json", current)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "status": "observed-only",
                        "reasons": ["recovery-in-progress"],
                        "pane_count": current["pane_count"],
                        "last_good": str(baseline_path or ""),
                        "observed": str(SNAPSHOT_DIR / "latest-observed.json"),
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        reasons = degradation_reasons(current, baseline)
        if args.force and int(current.get("pane_count") or 0) > 0 and not current.get("tmux_error"):
            reasons = [reason for reason in reasons if reason in {"cards-prefs-unreadable"}]
        current["health"] = {
            "status": "degraded" if reasons else "good",
            "reasons": reasons,
            "baseline": str(baseline_path or ""),
            "baseline_pane_count": int((baseline or {}).get("pane_count") or 0),
        }
        atomic_write_json(SNAPSHOT_DIR / "latest-observed.json", current)
        stamp = utcish_stamp()
        if reasons:
            incident = SNAPSHOT_DIR / f"degraded-{stamp}.json"
            atomic_write_json(incident, current)
            _trim_files("degraded-*.json", 30)
            print(
                json.dumps(
                    {
                        "ok": False,
                        "status": "degraded",
                        "reasons": reasons,
                        "pane_count": current["pane_count"],
                        "last_good": str(baseline_path or ""),
                        "incident": str(incident),
                    },
                    ensure_ascii=False,
                )
            )
            return 3

        previous_sha = str((baseline or {}).get("logical_sha256") or "")
        atomic_write_json(SNAPSHOT_DIR / "latest-good.json", current)
        atomic_write_json(SNAPSHOT_DIR / "latest.json", current)
        day = datetime.now().astimezone().strftime("%Y%m%d")
        atomic_write_json(SNAPSHOT_DIR / f"daily-{day}.json", current)
        if current.get("logical_sha256") != previous_sha:
            atomic_write_json(SNAPSHOT_DIR / f"good-change-{stamp}.json", current)
        _trim_files("daily-*.json", 14)
        _trim_files("good-change-*.json", 60)
        print(
            json.dumps(
                {
                    "ok": True,
                    "status": "good",
                    "pane_count": current["pane_count"],
                    "recoverable_pane_count": current["recoverable_pane_count"],
                    "categories": len(current.get("cards", {}).get("prefs", {}).get("categories", [])),
                    "favorites": int(
                        current.get("cards", {})
                        .get("manifest", {})
                        .get("summary", {})
                        .get("favorite_count", 0)
                    ),
                    "history_record_ids": int(
                        current.get("cards", {})
                        .get("manifest", {})
                        .get("summary", {})
                        .get("history_record_id_count", 0)
                    ),
                    "snapshot": str(SNAPSHOT_DIR / "latest-good.json"),
                },
                ensure_ascii=False,
            )
        )
        return 0


def default_snapshot_path() -> Path:
    for path in (SNAPSHOT_DIR / "latest-good.json", SNAPSHOT_DIR / "latest.json"):
        if path.is_file():
            return path
    raise RecoveryError("no recovery snapshot found")


def _rollout_in_home(home: Path, session_id: str) -> bool:
    sessions = home / "sessions"
    if not sessions.is_dir():
        return False
    return any(sessions.glob(f"*/*/*/rollout-*-{session_id}.jsonl"))


def resolve_codex_home(session_id: str, recorded_home: str = "") -> tuple[str, str, str]:
    """Codex 会话该在哪个 CODEX_HOME 里 resume: ``(home, source, error)``。

    迁移到隔离目录时,共享 ~/.codex/sessions 里往往还留着一份更短的旧副本。在错的
    home 里 `codex resume` 会静默接上旧分叉、丢掉迁移后的内容(或者直接失败),而验收
    只比 provider 和会话号照样通过。所以:
      - 快照记了 home: 该 home 里必须真有这条 rollout,否则拒绝;
      - 没记(旧快照): 只在默认 home 与 ~/.codex-homes/* 里恰好一处有这条时才用它,
        两处以上(新旧副本并存)就说不清是哪份,拒绝,绝不退回共享目录。
    """
    if not UUID_RE.fullmatch(str(session_id or "")):
        return "", "", "codex-session-id-invalid"
    if recorded_home:
        if _rollout_in_home(Path(recorded_home), session_id):
            return recorded_home, "recorded", ""
        return "", "", "codex-rollout-missing-in-recorded-home"
    homes = [DEFAULT_CODEX_HOME]
    try:
        homes.extend(sorted(path for path in CODEX_HOMES_ROOT.iterdir() if path.is_dir()))
    except OSError:
        pass
    found = [home for home in homes if _rollout_in_home(home, session_id)]
    if len(found) == 1:
        return str(found[0]), "located", ""
    if not found:
        return "", "", "codex-rollout-not-found"
    return "", "", f"codex-home-ambiguous:{len(found)}"


def _apply_codex_home(item: dict[str, Any], resolver: Any) -> None:
    if item.get("provider") != "codex" or resolver is None:
        return
    home, source, error = resolver(str(item.get("resume_id") or ""), str(item.get("codex_home") or ""))
    if error:
        item.update({"status": "blocked", "reason": error})
        return
    item["codex_home"] = home
    item["codex_home_source"] = source


def build_recovery_plan(
    snapshot: dict[str, Any],
    live: dict[str, Any],
    session: str = SESSION,
    *,
    codex_home_resolver: Any = None,
) -> dict[str, Any]:
    """session 必须由调用方透传: 用模块常量拼 target,会在 --session 非默认时
    检查一个 session 却杀掉另一个 session 的同号窗口。

    ``codex_home_resolver``(通常是 resolve_codex_home)给要 restore / relaunch 的
    Codex 条目定 CODEX_HOME;定不下来的条目改判 blocked,不会带着错的 home 去拉起。
    """
    live_panes = live.get("panes") if isinstance(live.get("panes"), list) else []
    snapshot_panes = snapshot.get("panes") if isinstance(snapshot.get("panes"), list) else []
    same_server = bool(
        str(snapshot.get("tmux_server") or "")
        and str(snapshot.get("tmux_server") or "") == str(live.get("tmux_server") or "")
    )
    # 同一个 (kind, session_id) 可以同时属于多个 pane —— 一条会话被 `claude --resume`
    # 开了两次。单值 dict 会让后来的覆盖先来的,两个窗口于是被
    # 当成一个: 真正掉了的那个永远不会被恢复,verify 还报告一切正常。存成列表,匹配一个
    # 消耗一个,配不上的必须自己走 restore。
    live_sessions: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    live_keys: dict[str, dict[str, Any]] = {}
    used_indices: set[int] = set()
    for pane in live_panes:
        provider = pane.get("provider") if isinstance(pane.get("provider"), dict) else {}
        identity = (str(provider.get("kind") or ""), _provider_session_id(provider))
        if all(identity) and _provider_is_live(provider):
            live_sessions[identity].append(pane)
        key = str(pane.get("preference_key") or "")
        if key:
            live_keys[key] = pane
        used_indices.add(int(pane.get("window_index") or 0))

    next_index = max(used_indices, default=-1) + 1
    items: list[dict[str, Any]] = []
    counts: dict[str, int] = defaultdict(int)
    for pane in snapshot_panes:
        provider = pane.get("provider") if isinstance(pane.get("provider"), dict) else {}
        kind = str(provider.get("kind") or "")
        resume_id = _provider_session_id(provider)
        identity = (kind, resume_id)
        key = str(pane.get("preference_key") or "")
        item = {
            "source_window": int(pane.get("window_index") or 0),
            "source_pane": int(pane.get("pane_index") or 0),
            "window_name": str(pane.get("window_name") or kind or "recovered"),
            "cwd": str(pane.get("cwd") or ""),
            "provider": kind,
            "session_id": resume_id,
            "resume_id": resume_id,
            "history_record_id": str(provider.get("history_record_id") or ""),
            "codex_home": str(provider.get("codex_home") or ""),
            "cards": pane.get("cards") if isinstance(pane.get("cards"), dict) else {},
            "preference_key": key,
            "source_pane_id": str(pane.get("pane_id") or ""),
        }
        if all(identity) and live_sessions.get(identity):
            candidates = live_sessions[identity]
            # 同 server 时优先认 pane id 相同的那个,否则先到先得;认下就从池子里拿走,
            # 下一个共用同一会话号的快照条目就配不上了,只能自己走 restore。
            live_pane = next(
                (candidate for candidate in candidates
                 if same_server
                 and str(candidate.get("pane_id") or "") == str(pane.get("pane_id") or "")),
                candidates[0],
            )
            candidates.remove(live_pane)
            live_provider = live_pane.get("provider") if isinstance(live_pane.get("provider"), dict) else {}
            live_home = str(live_provider.get("codex_home") or "")
            item.update(
                {
                    "status": "already-live",
                    "target_window": int(live_pane.get("window_index") or 0),
                    "target": f"{session}:{int(live_pane.get('window_index') or 0)}.0",
                    "target_pane_id": str(live_pane.get("pane_id") or ""),
                }
            )
            if kind == "codex" and item["codex_home"] and not _same_home(item["codex_home"], live_home):
                # 会话号对上了,但跑在另一个 CODEX_HOME 里: 多半是在共享目录接上了迁移前
                # 的旧分叉。只比 provider+会话号的验收会把它当成功,必须变红。
                item.update(
                    {
                        "status": "home-mismatch",
                        "reason": "codex-home-mismatch",
                        "live_codex_home": live_home,
                    }
                )
        elif (
            key
            and key in live_keys
            and not _provider_is_live(live_keys[key].get("provider", {}))
            and (
                not all(identity)
                or str(live_keys[key].get("provider", {}).get("kind") or "") in ("", kind)
            )
        ):
            # 窗口还在,但里面没有可识别身份的 AI 进程。绝不能当成 missing 去 new-window,
            # 那会凭空多出一个重复窗口。已知该恢复哪条会话时标成 stale-shell,
            # 交给 `recovery relaunch` 在原窗口内就地拉起。
            live_pane = live_keys[key]
            target_window = int(live_pane.get("window_index") or 0)
            # pane id 只在同一个 tmux server 生命周期内可比;server 换过就当身份不可证。
            same_pane = bool(
                same_server
                and str(live_pane.get("pane_id") or "")
                and str(live_pane.get("pane_id") or "") == str(pane.get("pane_id") or "")
            )
            if not all(identity):
                # 两边都没有会话号:窗口在、但没人知道它该是哪条对话,只能等人处理。
                status = "already-live-key"
            elif same_pane:
                status = "stale-shell"
            else:
                # 快照知道该恢复哪条,但这个 pane 已经不是当初那个了。既不能就地 -k
                # 重来(可能是别人的新窗口),也不能装作没事 —— 必须让 verify 变红。
                status = "unresolved-pane"
            item.update(
                {
                    "status": status,
                    "target_window": target_window,
                    "target": f"{session}:{target_window}.0",
                    "target_pane_id": str(live_pane.get("pane_id") or ""),
                    "target_pane_server": str(live.get("tmux_server") or "") if same_pane else "",
                }
            )
            if status == "stale-shell":
                _apply_codex_home(item, codex_home_resolver)
            if item["status"] == "stale-shell":
                item["relaunch_command"] = shlex.join(_launch_argv(item))
            elif status == "unresolved-pane":
                item["reason"] = "pane-identity-unprovable"
        elif not all(identity):
            item.update({"status": "metadata-only", "reason": provider.get("source") or "no-session-id"})
        elif not item["cwd"] or not Path(item["cwd"]).is_dir():
            item.update({"status": "blocked", "reason": "cwd-missing"})
        else:
            desired = item["source_window"]
            if desired in used_indices:
                while next_index in used_indices:
                    next_index += 1
                target_window = next_index
                next_index += 1
                item["remapped_from"] = desired
            else:
                target_window = desired
            used_indices.add(target_window)
            item.update(
                {
                    "status": "restore",
                    "target_window": target_window,
                    "target": f"{session}:{target_window}.0",
                }
            )
            _apply_codex_home(item, codex_home_resolver)
        counts[item["status"]] += 1
        items.append(item)

    return {
        "snapshot_at": snapshot.get("captured_at", ""),
        "snapshot_panes": len(snapshot_panes),
        "live_panes": len(live_panes),
        "counts": dict(sorted(counts.items())),
        "items": items,
    }


def load_source(path_arg: str) -> tuple[dict[str, Any], Path]:
    path = Path(path_arg).expanduser() if path_arg else default_snapshot_path()
    return load_json(path), path


def _summary_line(item: dict[str, Any]) -> str:
    window = item.get("target_window", item.get("source_window"))
    session_id = str(item.get("session_id") or "")
    home = str(item.get("codex_home") or "")
    name = str((item.get("cards") or {}).get("alias") or item.get("window_name") or "")
    line = (
        f"win{window!s:<4} {item.get('status', ''):<16} {item.get('provider') or '-':<6} "
        f"{session_id[:8] or '-':<8} home={Path(home).name if home else '-'} {name}"
    )
    extra = [str(item[key]) for key in ("reason",) if item.get(key)]
    if item.get("remapped_from") is not None:
        extra.append(f"remapped-from:{item['remapped_from']}")
    if item.get("live_codex_home"):
        extra.append(f"live-home={Path(str(item['live_codex_home'])).name}")
    return line + (f"  ({', '.join(extra)})" if extra else "")


def plan_summary_text(plan: dict[str, Any]) -> str:
    counts = " ".join(f"{key}={value}" for key, value in plan.get("counts", {}).items())
    lines = [f"source={plan.get('source', '')} snapshot_panes={plan.get('snapshot_panes')} "
             f"live_panes={plan.get('live_panes')} {counts}"]
    lines.extend(_summary_line(item) for item in plan.get("items", []))
    return "\n".join(lines)


def plan_command(args: argparse.Namespace) -> int:
    snapshot, source = load_source(args.snapshot)
    live = capture_state(args.session)
    plan = build_recovery_plan(
        snapshot, live, args.session, codex_home_resolver=resolve_codex_home
    )
    plan["source"] = str(source)
    if getattr(args, "summary", False):
        print(plan_summary_text(plan))
    else:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0 if not plan["counts"].get("blocked") else 2


def cards_manifest_command(args: argparse.Namespace) -> int:
    snapshot, source = load_source(args.snapshot)
    cards = snapshot.get("cards") if isinstance(snapshot.get("cards"), dict) else {}
    panes = snapshot.get("panes") if isinstance(snapshot.get("panes"), list) else []
    prefs = cards.get("prefs") if isinstance(cards.get("prefs"), dict) else {}
    manifest = cards.get("manifest")
    if not isinstance(manifest, dict):
        manifest = build_cards_manifest(
            str(snapshot.get("session") or SESSION),
            panes,
            prefs,
        )
    print(
        json.dumps(
            {
                "source": str(source),
                "captured_at": snapshot.get("captured_at", ""),
                "manifest": manifest,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _launch_argv(item: dict[str, Any]) -> list[str]:
    provider = str(item["provider"])
    resume_id = str(item["resume_id"])
    if provider == "codex":
        command = ["codex", "resume", resume_id]
    elif provider == "claude":
        command = ["claude", "--resume", resume_id]
    else:
        raise RecoveryError(f"unsupported provider: {provider}")
    wrapper: list[str] = []
    codex_home = str(item.get("codex_home") or "")
    if provider == "codex" and codex_home:
        wrapper.extend(["env", f"CODEX_HOME={codex_home}"])
    wrapper.extend([str(AI_SESSION_SHELL), *command])
    return wrapper


def spawn_item(item: dict[str, Any], session: str) -> None:
    target = f"{session}:{int(item['target_window'])}"
    inner = "exec " + shlex.join(_launch_argv(item))
    result = run(
        [
            "tmux",
            "new-window",
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-t",
            target,
            "-n",
            str(item["window_name"]),
            "-c",
            str(item["cwd"]),
            "/bin/bash",
            "-lc",
            inner,
        ],
        timeout=20,
    )
    # 记下"这个窗口是本次创建的"这一事实。重试时只认这个 pane id: 窗口号可以被别人
    # 占用,号相同不等于所有权相同,而 respawn 带 -k 会直接杀掉里面的东西。
    pane_id = result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""
    if pane_id:
        item["target_pane_id"] = pane_id
        # pane id 只在一次 server 生命周期内唯一,所有权必须连 server 一起记:
        # server 重启后新 pane 同样从 %0 开始,只比 id 就会把别人的窗口当成自己的。
        item["target_pane_server"] = tmux_server_id()
    run(["tmux", "set-option", "-w", "-t", pane_id or target, "remain-on-exit", "on"], timeout=5)
    _stamp_item_identity(pane_id or target, item)


def _stamp_item_identity(target: str, item: dict[str, Any]) -> None:
    """拉起时就把身份钉到 pane 上: AI 之后退出,pane 上仍知道它是谁、在哪个 CODEX_HOME。"""
    provider = str(item.get("provider") or "")
    options = [("@ai_provider", provider), ("@ai_session_id", str(item.get("resume_id") or ""))]
    for option, value in options:
        if value:
            run(["tmux", "set-option", "-p", "-t", target, option, value], timeout=5, check=False)
    home = str(item.get("codex_home") or "")
    if provider == "codex" and home:
        run(["tmux", "set-option", "-p", "-t", target, "@ai_codex_home", home], timeout=5, check=False)
    else:
        run(["tmux", "set-option", "-p", "-u", "-t", target, "@ai_codex_home"], timeout=5, check=False)




def _pane_exists(pane_id: str) -> bool:
    if not pane_id:
        return False
    try:
        result = run(["tmux", "list-panes", "-a", "-F", "#{pane_id}"], timeout=10, check=False)
    except (subprocess.SubprocessError, OSError):
        return False
    return pane_id in result.stdout.split()


def _item_owns_pane(item: dict[str, Any], current_server: str) -> bool:
    """这个 item 现在是否真的拥有一个 pane。

    所有权 = 同一个 tmux server 生命周期 + 那个 pane 还在。缺了 server 这一半,
    server 重启后复用的同号 pane 会被当成自己的,respawn 的 -k 就杀到别人头上;
    读不出 server 身份时一律判"没有所有权"(fail closed)。
    """
    pane_id = str(item.get("target_pane_id") or "")
    owner_server = str(item.get("target_pane_server") or "")
    if not (pane_id and owner_server and current_server and owner_server == current_server):
        return False
    return _pane_exists(pane_id)


def _window_exists(session: str, window_index: int) -> bool:
    try:
        result = run(
            ["tmux", "list-windows", "-t", session, "-F", "#{window_index}"],
            timeout=10,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return str(window_index) in result.stdout.split()


def _all_descendants(root: int, children: dict[int, list[int]]) -> list[int]:
    """完整子树,不做深度截断 —— descendants() 的 max_depth 会漏掉更深的后台任务,
    而安全判据一旦漏看,respawn 的 -k 就会杀到用户自己跑的东西。"""
    seen: set[int] = set()
    stack = [root]
    out: list[int] = []
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        out.append(pid)
        stack.extend(children.get(pid, []))
    return out


def _pane_is_idle_shell(target: str, expected_pane_id: str = "") -> bool:
    """窗口里只剩一个真正空闲的 shell 时,才允许就地 respawn(它会 -k 掉现有进程)。

    三道判据缺一不可,而且必须在每次 respawn 前现查 —— 提前一次性预检等于把整批窗口
    的安全性冻结在几十秒前的状态:
      1. pane id 与计划里冻结的一致(窗口号会被新 pane 复用,号相同不等于还是同一个 pane);
      2. 前台命令是 shell —— 故意不含 ai-session-shell,它只在 AI 还没 exec 的那一瞬间
         在前台,把它当空闲会杀掉正在启动的会话;
      3. 整棵子树里没有任何非 shell 进程。只排除 claude/codex 是不够的: 用户在 bash 里
         跑的训练、编译、下载同样不能被 -k 掉。
    """
    try:
        result = run(
            [
                "tmux",
                "display-message",
                "-p",
                "-t",
                target,
                "#{pane_current_command}\t#{pane_pid}\t#{pane_id}",
            ],
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    fields = result.stdout.strip().split("\t")
    if len(fields) != 3:
        return False
    command, pane_pid, pane_id = (field.strip() for field in fields)
    if expected_pane_id and pane_id != expected_pane_id:
        return False
    if command not in SHELL_COMMANDS:
        return False
    try:
        pid_num = int(pane_pid)
    except ValueError:
        return False
    children, commands = process_tree()
    for pid in _all_descendants(pid_num, children):
        if commands.get(pid, "") not in SHELL_COMMANDS:
            return False
    return True


def respawn_item(item: dict[str, Any], session: str) -> None:
    """在该 item 已有的窗口里就地重新拉起同一条会话,不新建窗口。

    目标优先用冻结的 pane id: 安全检查查的是那个 pane,如果动作却按窗口号执行,
    这中间 pane 被换掉就会杀到一个从没检查过的窗口上。

    必须用 respawn-pane 而不是 respawn-window: 后者即使 -t 给的是 pane id 也作用于整个
    窗口,分屏里没被检查过的其他 pane 会被一起销毁;空闲判据只查了目标 pane。
    """
    target = str(item.get("target_pane_id") or "") or f"{session}:{int(item['target_window'])}"
    run(
        [
            "tmux",
            "respawn-pane",
            "-k",
            "-t",
            target,
            "-c",
            str(item["cwd"]),
            "/bin/bash",
            "-lc",
            "exec " + shlex.join(_launch_argv(item)),
        ],
        timeout=20,
    )
    run(["tmux", "rename-window", "-t", target, str(item["window_name"])], timeout=10, check=False)
    run(["tmux", "set-option", "-w", "-t", target, "remain-on-exit", "on"], timeout=5, check=False)
    _stamp_item_identity(target, item)


def _live_pane_identities(session: str) -> tuple[str, dict[str, tuple[str, ...]]]:
    """Return active provider identity (kind, session id, CODEX_HOME) bound to its
    exact pane and tmux server."""
    live = capture_state(session)
    by_pane: dict[str, tuple[str, ...]] = {}
    for pane in live.get("panes", []):
        provider = pane.get("provider") if isinstance(pane.get("provider"), dict) else {}
        pane_id = str(pane.get("pane_id") or "")
        if pane_id and _provider_is_live(provider):
            by_pane[pane_id] = (
                str(provider.get("kind") or ""),
                _provider_session_id(provider),
                str(provider.get("codex_home") or ""),
            )
    return str(live.get("tmux_server") or ""), by_pane


def _item_identity(item: dict[str, Any]) -> tuple[str, str]:
    return (str(item["provider"]), str(item["resume_id"]))


def _observed_matches(item: dict[str, Any], observed: tuple[str, ...] | None) -> bool:
    """会话号对上还不够: Codex 条目记了 CODEX_HOME 时,活进程必须跑在同一个 home 里。"""
    if not observed or tuple(observed[:2]) != _item_identity(item):
        return False
    home = str(item.get("codex_home") or "")
    if item.get("provider") == "codex" and home:
        return _same_home(home, str(observed[2]) if len(observed) > 2 else "")
    return True


def drive_with_retries(
    items: list[dict[str, Any]],
    session: str,
    launch,
    *,
    pause_seconds: float,
    settle_seconds: float,
    retries: int,
    retry_backoff: float,
    stability_seconds: float = 0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    """拉起 items,每轮结束后重新观测,只对没验证通过的重试。

    为什么要重试而不是纯串行: Codex 的本机 sqlite(state_5 / logs_2)是全机器单例,
    并发拉起会 `database is locked` 直接退出 status 1;但实测重试一两次就能抢到锁,
    而全串行会把开机恢复从一分钟拖到十几分钟。大批量拉起时首轮常有一部分失败,
    补一两轮通常全部成功。

    返回 (verified, pending, failures)。
    """
    pending = list(items)
    verified: list[dict[str, Any]] = []
    last_error: dict[int, tuple[int, str]] = {}
    for attempt in range(1, max(1, int(retries)) + 1):
        if not pending:
            break
        if attempt > 1 and retry_backoff:
            time.sleep(retry_backoff)
        launched: list[dict[str, Any]] = []
        errored: list[dict[str, Any]] = []
        for item in pending:
            try:
                launch(item, session, attempt)
                launched.append(item)
                last_error.pop(id(item), None)
            except (RecoveryError, subprocess.SubprocessError, OSError) as exc:
                # 抛异常的项留在 pending 里继续重试: tmux/OS 的瞬时错误和"pane 这一刻
                # 正忙"都可能下一轮就好了,一次出局会让 retries 名不副实。
                errored.append(item)
                last_error[id(item)] = (attempt, str(exc)[:500])
            if pause_seconds:
                time.sleep(pause_seconds)
        still: list[dict[str, Any]] = list(errored)
        if launched:
            if settle_seconds:
                time.sleep(settle_seconds)
            observed_server, live = _live_pane_identities(session)
            if stability_seconds:
                time.sleep(stability_seconds)
                second_server, second = _live_pane_identities(session)
                if not observed_server or second_server != observed_server:
                    live = {}
                else:
                    live = {
                        pane_id: identity
                        for pane_id, identity in live.items()
                        if second.get(pane_id) == identity
                    }
            for item in launched:
                pane_id = str(item.get("target_pane_id") or "")
                owner_server = str(item.get("target_pane_server") or "")
                if (
                    pane_id
                    and owner_server
                    and owner_server == observed_server
                    and _observed_matches(item, live.get(pane_id))
                ):
                    verified.append(item)
                else:
                    still.append(item)
        pending = still
    failures = [
        {
            "target": str(item.get("target") or ""),
            "attempt": str(last_error[id(item)][0]),
            "error": last_error[id(item)][1],
        }
        for item in pending
        if id(item) in last_error
    ]
    return verified, pending, failures


def restore_cards(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    categories: dict[str, list[str]] = defaultdict(list)
    favorites: list[str] = []
    aliases: list[tuple[str, str]] = []
    for item in items:
        target = str(item.get("target") or "")
        cards = item.get("cards") if isinstance(item.get("cards"), dict) else {}
        category = str(cards.get("category") or "")
        if category:
            categories[category].append(target)
        if cards.get("favorite"):
            favorites.append(target)
        alias = str(cards.get("alias") or "")
        if alias:
            aliases.append((target, alias))
    for category, targets in categories.items():
        result = run(
            [str(SECRETARY_BUS), "cards", "group", category, *targets, "--create", "--json"],
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            failures.append({"scope": f"category:{category}", "error": result.stderr.strip()[:500]})
    for target in favorites:
        result = run(
            [str(SECRETARY_BUS), "cards", "favorite", target, "--json"],
            timeout=20,
            check=False,
        )
        if result.returncode != 0:
            failures.append({"scope": f"favorite:{target}", "error": result.stderr.strip()[:500]})
    for target, alias in aliases:
        result = run(
            [str(SECRETARY_BUS), "cards", "alias", target, alias, "--json"],
            timeout=20,
            check=False,
        )
        if result.returncode != 0:
            failures.append({"scope": f"alias:{target}", "error": result.stderr.strip()[:500]})
    return failures


def live_cards() -> tuple[list[dict[str, Any]], str]:
    result = run(
        [str(SECRETARY_BUS), "cards", "list", "--json"],
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        return [], result.stderr.strip()[:500] or f"cards list exited {result.returncode}"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return [], f"invalid cards JSON: {exc}"
    panes = payload.get("panes") if isinstance(payload, dict) else None
    if not isinstance(panes, list):
        return [], "cards JSON has no panes list"
    return panes, ""


def cards_mismatches(
    plan: dict[str, Any],
    cards: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_target = {str(item.get("target") or ""): item for item in cards}
    mismatches: list[dict[str, Any]] = []
    for item in plan.get("items", []):
        if item.get("status") not in {"already-live", "already-live-key"}:
            continue
        target = str(item.get("target") or "")
        actual = by_target.get(target)
        expected = item.get("cards") if isinstance(item.get("cards"), dict) else {}
        if actual is None:
            mismatches.append({"target": target, "field": "pane", "expected": "present", "actual": "missing"})
            continue
        expected_category = str(expected.get("category") or "")
        actual_category = str(actual.get("category") or "")
        if expected_category != actual_category:
            mismatches.append(
                {
                    "target": target,
                    "field": "category",
                    "expected": expected_category,
                    "actual": actual_category,
                }
            )
        expected_favorite = bool(expected.get("favorite"))
        actual_favorite = bool(actual.get("favorite"))
        if expected_favorite != actual_favorite:
            mismatches.append(
                {
                    "target": target,
                    "field": "favorite",
                    "expected": expected_favorite,
                    "actual": actual_favorite,
                }
            )
        expected_alias = str(expected.get("alias") or "")
        actual_alias = str(actual.get("alias") or "")
        if expected_alias != actual_alias:
            mismatches.append(
                {
                    "target": target,
                    "field": "alias",
                    "expected": expected_alias,
                    "actual": actual_alias,
                }
            )
    return mismatches


def reconcile_cards_command(args: argparse.Namespace) -> int:
    if not args.yes:
        raise RecoveryError("Cards reconciliation changes metadata; pass --yes after reviewing recovery verify")
    snapshot, source = load_source(args.snapshot)
    plan = build_recovery_plan(snapshot, capture_state(args.session), args.session)
    before, cards_error = live_cards()
    if cards_error:
        raise RecoveryError(cards_error)
    mismatches = cards_mismatches(plan, before)
    failures: list[dict[str, str]] = []
    category_groups: dict[str, list[str]] = defaultdict(list)
    for mismatch in mismatches:
        target = str(mismatch["target"])
        if mismatch["field"] == "category":
            category = str(mismatch["expected"] or "")
            if category:
                category_groups[category].append(target)
            else:
                result = run(
                    [str(SECRETARY_BUS), "cards", "uncategorize", target, "--json"],
                    timeout=20,
                    check=False,
                )
                if result.returncode != 0:
                    failures.append({"scope": f"category:{target}", "error": result.stderr.strip()[:500]})
        elif mismatch["field"] == "favorite":
            action = "favorite" if mismatch["expected"] else "unfavorite"
            result = run(
                [str(SECRETARY_BUS), "cards", action, target, "--json"],
                timeout=20,
                check=False,
            )
            if result.returncode != 0:
                failures.append({"scope": f"{action}:{target}", "error": result.stderr.strip()[:500]})
        elif mismatch["field"] == "alias":
            result = run(
                [
                    str(SECRETARY_BUS),
                    "cards",
                    "alias",
                    target,
                    str(mismatch["expected"] or ""),
                    "--json",
                ],
                timeout=20,
                check=False,
            )
            if result.returncode != 0:
                failures.append({"scope": f"alias:{target}", "error": result.stderr.strip()[:500]})
    for category, targets in category_groups.items():
        result = run(
            [str(SECRETARY_BUS), "cards", "group", category, *targets, "--create", "--json"],
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            failures.append({"scope": f"category:{category}", "error": result.stderr.strip()[:500]})
    after, after_error = live_cards()
    remaining = cards_mismatches(plan, after) if not after_error else mismatches
    payload = {
        "ok": not failures and not after_error and not remaining,
        "source": str(source),
        "before": len(mismatches),
        "remaining": remaining,
        "failures": failures,
        "error": after_error,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 2


def _item_matches_target(item: dict[str, Any], selector: str) -> bool:
    selector = selector.strip()
    if not selector:
        return False
    if selector.isdigit():
        windows = (item.get("target_window"), item.get("source_window"))
        return int(selector) in {int(value) for value in windows if value is not None}
    if selector.startswith("%"):
        return selector in {
            str(item.get("target_pane_id") or ""),
            str(item.get("source_pane_id") or ""),
        }
    cards = item.get("cards") if isinstance(item.get("cards"), dict) else {}
    return selector in {str(item.get("window_name") or ""), str(cards.get("alias") or "")}


def select_target_item(plan: dict[str, Any], selector: str, status: str) -> list[dict[str, Any]]:
    """--target: 按窗口号 / pane id / 窗口名(或 Cards 别名)只挑一个;不唯一就拒绝。"""
    matches = [item for item in plan.get("items", []) if _item_matches_target(item, selector)]
    if not matches:
        raise RecoveryError(f"no plan item matches --target {selector!r}")
    if len(matches) > 1:
        described = ", ".join(
            f"win{item.get('target_window', item.get('source_window'))}:{item.get('status')}"
            for item in matches
        )
        raise RecoveryError(f"--target {selector!r} is ambiguous: {described}")
    item = matches[0]
    if item.get("status") != status:
        reason = f" ({item['reason']})" if item.get("reason") else ""
        raise RecoveryError(
            f"--target {selector!r} is {item.get('status')}{reason}, not {status}; nothing to do"
        )
    return matches


def restore_command(args: argparse.Namespace) -> int:
    if not args.yes:
        raise RecoveryError("restore is write-capable; pass --yes after reviewing `recovery plan`")
    target = str(getattr(args, "target", "") or "")
    if not args.all and args.limit is None and not target:
        raise RecoveryError("choose --target T, --limit N for a trial batch, or --all")
    snapshot, source = load_source(args.snapshot)
    plan = build_recovery_plan(
        snapshot, capture_state(args.session), args.session,
        codex_home_resolver=resolve_codex_home,
    )
    if target:
        candidates = select_target_item(plan, target, "restore")
    else:
        candidates = [item for item in plan["items"] if item["status"] == "restore"]
    if args.limit is not None:
        candidates = candidates[: max(0, args.limit)]
    def launch(item: dict[str, Any], session: str, attempt: int) -> None:
        # 三分法,不能只问"窗口号在不在":
        #   本次建的 pane 还在 -> 就地重来(首轮 new-window 建成后失败的情况);
        #   窗口号被别的 pane 占了 -> 撞号,直接判死,绝不 -k 别人的窗口;
        #   两者都不是 -> 还没建成,重新 new-window(首轮在创建前就失败的情况)。
        if _item_owns_pane(item, tmux_server_id()):
            respawn_item(item, session)
        elif _window_exists(session, int(item["target_window"])):
            raise RecoveryError(
                f"target window {session}:{item['target_window']} is held by another pane"
            )
        else:
            spawn_item(item, session)

    verified, pending, failures = drive_with_retries(
        candidates,
        args.session,
        launch,
        pause_seconds=args.pause_seconds,
        settle_seconds=args.settle_seconds,
        retries=args.retries,
        retry_backoff=args.retry_backoff,
        stability_seconds=getattr(args, "stability_seconds", 0),
    )
    # 只有"本轮真正拥有的 pane"才算起过的窗口。曾经建过但现在已经被别人占掉的,
    # 交给 Cards 会改写别人的分类,交给对话框清理会往别人窗口发按键。
    current_server = tmux_server_id()
    spawned = [item for item in verified + pending if _item_owns_pane(item, current_server)]
    # 已经验证过、但到这一步已经不再拥有 pane 的: 窗口多半被换掉了,不能算成功。
    lost_ownership = [
        item for item in verified if not _item_owns_pane(item, current_server)
    ]
    card_failures = restore_cards(spawned) if spawned and not args.skip_cards else []
    payload = {
        "ok": not failures and not card_failures and not pending and not lost_ownership,
        "lost_ownership": [item["target"] for item in lost_ownership],
        "source": str(source),
        "planned": len(candidates),
        "spawned": len(spawned),
        "verified": len(verified),
        "unverified": [item["target"] for item in pending],
        "attempts_allowed": int(args.retries),
        "failures": failures,
        "card_failures": card_failures,
        "remaining": max(0, int(plan["counts"].get("restore", 0)) - len(spawned)),
        "targets": [item["target"] for item in spawned],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 2


def relaunch_command(args: argparse.Namespace) -> int:
    """把「窗口还在、AI 已经退出」的 stale-shell 窗口按各自的 session id 就地拉起。

    和 restore 的分工: restore 处理窗口整个没了(tmux 重启、掉电)的情况,会 new-window;
    relaunch 处理窗口还在、里面掉回 bash 的情况,只在原窗口里重来,不新建、不改窗口号,
    因此也保住了 Cards 上的分类和收藏。
    """
    if not args.yes:
        raise RecoveryError("relaunch is write-capable; pass --yes after reviewing `recovery plan`")
    target = str(getattr(args, "target", "") or "")
    if not args.all and args.limit is None and not target:
        raise RecoveryError("choose --target T, --limit N for a trial batch, or --all")
    snapshot, source = load_source(args.snapshot)
    live = capture_state(args.session)
    plan = build_recovery_plan(
        snapshot, live, args.session, codex_home_resolver=resolve_codex_home
    )
    if target:
        candidates = select_target_item(plan, target, "stale-shell")
    else:
        candidates = [item for item in plan["items"] if item["status"] == "stale-shell"]
    if args.limit is not None:
        candidates = candidates[: max(0, args.limit)]
    # 必须取"算这份计划时看到的" server,不能事后再问一次: capture 与冻结之间若 tmux
    # server 换过,事后问到的是新 server,guard 就会拿新 server 跟自己比而永远通过。
    plan_server = str(live.get("tmux_server") or "")
    skipped: list[dict[str, str]] = []

    def guarded_respawn(item: dict[str, Any], session: str, attempt: int) -> None:
        # respawn 会 -k 掉窗口里现有的进程,所以判据必须贴着动作现查:提前批量预检,
        # 到真正动手时状态早就可能变了(用户回到窗口敲了命令、pane 被换掉)。
        current_server = tmux_server_id()
        # 读不出 server 身份 = 无法证明计划里的 pane id 还指向同一个 pane,一律不动。
        if not plan_server or not current_server or plan_server != current_server:
            raise RecoveryError(f"tmux server identity unprovable since planning: {item['target']}")
        pane_id = str(item.get("target_pane_id") or "")
        if not pane_id:
            raise RecoveryError(f"no frozen pane id to act on: {item['target']}")
        # 检查和 respawn 用同一个 pane id 定位。按窗口号检查、按 pane id 动手(或反过来)
        # 都留着"查了 A 杀了 B"的缝: 窗口号会被新 pane 复用,pane id 不会。
        if not _pane_is_idle_shell(pane_id, pane_id):
            raise RecoveryError(f"pane no longer an idle shell: {item['target']}")
        respawn_item(item, session)

    verified, pending, failures = drive_with_retries(
        candidates,
        args.session,
        guarded_respawn,
        pause_seconds=args.pause_seconds,
        settle_seconds=args.settle_seconds,
        retries=args.retries,
        retry_backoff=args.retry_backoff,
        stability_seconds=getattr(args, "stability_seconds", 0),
    )
    # 被判定为"pane 不再空闲"而没动的窗口: 它们仍然是空的,必须让 ok 变红。
    # 把跳过当成功,会造成最要命的可观测性缺口(systemd 绿灯 / 窗口照旧空着)。
    skipped = [failure for failure in failures if "idle shell" in failure["error"]]
    payload = {
        "ok": not failures and not pending,
        "source": str(source),
        "planned": len(candidates),
        "skipped": skipped,
        "relaunched": len(verified),
        "unverified": [item["target"] for item in pending],
        "attempts_allowed": int(args.retries),
        "failures": failures,
        "targets": [item["target"] for item in verified],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 2


def verify_command(args: argparse.Namespace) -> int:
    snapshot, source = load_source(args.snapshot)
    live = capture_state(args.session)
    plan = build_recovery_plan(
        snapshot, live, args.session, codex_home_resolver=resolve_codex_home
    )
    missing = [
        item
        for item in plan["items"]
        if item["status"] in {"restore", "blocked", "unresolved-pane", "home-mismatch"}
    ]
    cards, cards_error = live_cards()
    card_mismatches = cards_mismatches(plan, cards) if not cards_error else []
    manifest_errors = cards_manifest_errors(snapshot)
    manifest = (
        snapshot.get("cards", {}).get("manifest", {})
        if isinstance(snapshot.get("cards"), dict)
        else {}
    )
    stale_shells = [
        {
            "target": str(item.get("target") or ""),
            "window_name": str(item.get("window_name") or ""),
            "provider": str(item.get("provider") or ""),
            "session_id": str(item.get("session_id") or ""),
            "relaunch_command": str(item.get("relaunch_command") or ""),
        }
        for item in plan["items"]
        if item["status"] == "stale-shell"
    ]
    payload = {
        "ok": (
            not missing
            and not stale_shells
            and not cards_error
            and not card_mismatches
            and not manifest_errors
        ),
        "source": str(source),
        "snapshot_panes": plan["snapshot_panes"],
        "live_panes": plan["live_panes"],
        "counts": plan["counts"],
        "missing": missing,
        # 窗口还在但 AI 退了也属于恢复未完成，必须让 ok 变红。
        "stale_shells": stale_shells,
        "cards_error": cards_error,
        "cards_mismatches": card_mismatches,
        "manifest_errors": manifest_errors,
        "manifest_summary": (
            manifest.get("summary", {}) if isinstance(manifest, dict) else {}
        ),
    }
    if getattr(args, "summary", False):
        plan["source"] = str(source)
        print(plan_summary_text(plan))
        print(
            f"verify ok={payload['ok']} missing={len(missing)} stale_shells={len(stale_shells)} "
            f"cards_mismatches={len(card_mismatches)} cards_error={bool(cards_error)} "
            f"manifest_errors={len(manifest_errors)}"
        )
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 2


def _run_step(argv: list[str]) -> tuple[int, Any]:
    """在进程内跑一个子命令,收回它打印的 JSON(auto 用)。"""
    namespace = parser().parse_args(argv)
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            code = int(namespace.func(namespace))
    except RecoveryError as exc:
        return 2, {"ok": False, "error": str(exc)}
    text = buffer.getvalue()
    try:
        return code, json.loads(text)
    except json.JSONDecodeError:
        return code, text


def pin_baseline(source_arg: str, label: str) -> tuple[Path, Path]:
    """把本次恢复要对照的基线固定成一份副本。

    恢复要跑好几分钟,期间 latest-good 可能被换掉;restore / relaunch / verify 各读
    各的"最新"基线,没救回的会话号就会从后读的那份里消失,最终验收反而是绿的。
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+", label):
        raise RecoveryError(f"invalid pin label: {label!r}")
    snapshot, source = load_source(source_arg)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    pinned = SNAPSHOT_DIR / f"pinned-{label}-{utcish_stamp()}.json"
    atomic_write_json(pinned, snapshot)
    _trim_files(f"pinned-{label}-*.json", 20)
    return pinned, source


def pin_baseline_command(args: argparse.Namespace) -> int:
    pinned, source = pin_baseline(args.snapshot, args.label)
    if args.path_only:
        print(pinned)
    else:
        print(json.dumps({"ok": True, "pinned": str(pinned), "source": str(source)}, ensure_ascii=False))
    return 0


def _auto_steps(session: str, pinned: str) -> list[tuple[str, list[str]]]:
    common = ["--session", session, "--snapshot", pinned]
    return [
        ("restore", ["restore", *common, "--all", "--yes"]),
        ("relaunch", ["relaunch", *common, "--all", "--yes"]),
        ("reconcile-cards", ["reconcile-cards", *common, "--yes"]),
        ("verify", ["verify", *common]),
    ]


def auto_command(args: argparse.Namespace) -> int:
    """固定基线 -> restore -> relaunch -> reconcile-cards -> verify,四步共用同一份 --snapshot。

    默认只打印计划(dry-run);--yes 才执行,执行期间持有恢复锁,定时快照只写 observed。
    """
    if args.dry_run or not args.yes:
        snapshot, source = load_source(args.snapshot)
        plan = build_recovery_plan(
            snapshot, capture_state(args.session), args.session,
            codex_home_resolver=resolve_codex_home,
        )
        plan["source"] = str(source)
        would_pin = str(SNAPSHOT_DIR / "pinned-auto-<stamp>.json")
        steps = [
            "secretary-bus recovery " + shlex.join(argv)
            for _, argv in _auto_steps(args.session, would_pin)
        ]
        if args.summary:
            print(plan_summary_text(plan))
            print(f"dry-run: would pin {source} -> {would_pin}")
            print("\n".join(f"  {step}" for step in steps))
            print("add --yes to run")
        else:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "mode": "dry-run",
                        "source": str(source),
                        "would_pin": would_pin,
                        "recovery_in_progress": recovery_in_progress(),
                        "counts": plan["counts"],
                        "steps": steps,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return 0 if not plan["counts"].get("blocked") else 2

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    with auto_restore_lock_path().open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RecoveryError("another recovery holds .auto-restore.lock; not starting a second one") from exc
        pinned, source = pin_baseline(args.snapshot, "auto")
        results: dict[str, Any] = {}
        for name, argv in _auto_steps(args.session, str(pinned)):
            code, output = _run_step(argv)
            results[name] = {"rc": code, "result": output}
        # The boot placeholder (window 999, see contrib/boot/ensure-tmux-session)
        # is only needed until the real windows are back; the script closes it
        # only when it is marked, idle, and not the last window.
        placeholder = Path(os.environ.get(
            "SECRETARY_ENSURE_TMUX", str(REPO_ROOT / "contrib" / "boot" / "ensure-tmux-session")
        ))
        if all(step["rc"] == 0 for step in results.values()) and placeholder.exists():
            try:
                closed = subprocess.run(
                    [str(placeholder), "--close-placeholder"], capture_output=True, text=True,
                    timeout=30, stdin=subprocess.DEVNULL,
                )
                results["close-placeholder"] = {"rc": closed.returncode, "result": (closed.stdout or closed.stderr).strip()}
            except (OSError, subprocess.SubprocessError) as exc:
                results["close-placeholder"] = {"rc": 2, "result": str(exc)}
    ok = all(step["rc"] == 0 for step in results.values())
    print(
        json.dumps(
            {"ok": ok, "pinned": str(pinned), "source": str(source), "steps": results},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if ok else 2


def codex_home_command(args: argparse.Namespace) -> int:
    """只读: 一条 Codex 会话该在哪个 CODEX_HOME 里 resume(agent_window.sh new --resume-id 用)。"""
    home, source, error = resolve_codex_home(str(args.session_id).lower(), args.recorded)
    if args.path_only:
        if error:
            print(error, file=sys.stderr)
            return 2
        print(home)
        return 0
    print(json.dumps({"ok": not error, "codex_home": home, "source": source, "error": error}))
    return 0 if not error else 2


def identity_command(args: argparse.Namespace) -> int:
    """只读: 一个 pane 当前的会话身份与 CODEX_HOME(agent_window.sh restart 用)。"""
    target = str(args.target)
    if target.isdigit():
        target = f"{args.session}:{target}"
    fmt = "\t".join(
        [
            "#{pane_id}", "#{pane_pid}", "#{pane_current_path}", "#{window_index}",
            "#{window_panes}", "#{@ai_provider}", "#{@ai_session_id}", "#{@ai_codex_home}",
        ]
    )
    try:
        result = run(["tmux", "display-message", "-p", "-t", target, fmt], timeout=10)
    except (subprocess.SubprocessError, OSError) as exc:
        raise RecoveryError(f"cannot read pane {target}: {exc}") from exc
    fields = result.stdout.rstrip("\n").split("\t")
    if len(fields) != 8 or not fields[1].isdigit():
        raise RecoveryError(f"unexpected tmux pane fields for {target}: {result.stdout!r}")
    pane_id, pane_pid, cwd, window_index, window_panes, stamped_kind, stamped_session, stamped_home = fields
    children, commands = process_tree()
    records, claude_error = _lazy_claude_records()
    provider = _pane_provider(
        int(pane_pid), pane_id, cwd, children, commands,
        stamped_kind=stamped_kind, stamped_session=stamped_session,
        stamped_home=stamped_home, claude_records=records,
    )
    kind = str(provider.get("kind") or "")
    session_id = _provider_session_id(provider)
    payload: dict[str, Any] = {
        "pane_id": pane_id,
        "window_index": window_index,
        "window_panes": window_panes,
        "cwd": cwd,
        "kind": kind,
        "session_id": session_id,
        "source": str(provider.get("source") or ""),
        "ai_alive": bool(_outermost_ai(int(pane_pid), children, commands)[1]),
        "codex_home": str(provider.get("codex_home") or ""),
        "codex_home_source": str(provider.get("codex_home_source") or ""),
        "identity_conflict": provider.get("identity_conflict") or {},
        "claude_agents_error": claude_error(),
        "resume_codex_home": "",
        "resume_codex_home_source": "",
        "resume_codex_home_error": "",
    }
    wanted = str(args.resume_id or session_id)
    if (args.kind or kind) == "codex" and wanted:
        recorded = payload["codex_home"] if wanted == session_id else ""
        home, source, error = resolve_codex_home(wanted, recorded)
        payload.update(
            resume_codex_home=home, resume_codex_home_source=source, resume_codex_home_error=error
        )
    if args.format == "shell":
        values = {
            "ID_PANE": payload["pane_id"],
            "ID_WINDOW_PANES": payload["window_panes"],
            "ID_KIND": kind,
            "ID_SESSION": session_id,
            "ID_SOURCE": payload["source"],
            "ID_ALIVE": "1" if payload["ai_alive"] else "0",
            "ID_CODEX_HOME": payload["codex_home"],
            "ID_RESUME_HOME": payload["resume_codex_home"],
            "ID_RESUME_HOME_ERROR": payload["resume_codex_home_error"],
            "ID_CONFLICT": json.dumps(payload["identity_conflict"], ensure_ascii=False)
            if payload["identity_conflict"] else "",
        }
        print("\n".join(f"{key}={shlex.quote(str(value))}" for key, value in values.items()))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0



def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        description="Snapshot, plan, restore, and verify secretary_web plus Cards metadata."
    )
    sub = top.add_subparsers(dest="command", required=True)

    snapshot = sub.add_parser("snapshot", help="Capture an atomic last-known-good state bundle")
    snapshot.add_argument("--session", default=SESSION)
    snapshot.add_argument(
        "--force",
        action="store_true",
        help="Accept an intentional non-empty large pane-count drop as the new baseline",
    )
    snapshot.set_defaults(func=snapshot_command)

    plan = sub.add_parser("plan", help="Read-only recovery plan; never changes tmux or Cards")
    plan.add_argument("--session", default=SESSION)
    plan.add_argument("--snapshot", default="")
    plan.add_argument("--summary", action="store_true", help="One line per window instead of JSON")
    plan.set_defaults(func=plan_command)

    manifest = sub.add_parser(
        "cards-manifest",
        help="Print the saved Cards favorites, categories, names, and history identities",
    )
    manifest.add_argument("--snapshot", default="")
    manifest.set_defaults(func=cards_manifest_command)

    restore = sub.add_parser("restore", help="Restore only missing resumable AI sessions")
    restore.add_argument("--session", default=SESSION)
    restore.add_argument("--snapshot", default="")
    choice = restore.add_mutually_exclusive_group()
    choice.add_argument("--limit", type=int, help="Restore at most N missing sessions")
    choice.add_argument("--all", action="store_true", help="Restore every missing resumable session")
    choice.add_argument(
        "--target", default="",
        help="Restore exactly one item: snapshot window number, pane id (%%N), window name or Cards alias",
    )
    restore.add_argument("--yes", action="store_true", help="Confirm the write-capable restore")
    restore.add_argument("--skip-cards", action="store_true")
    restore.add_argument("--pause-seconds", type=float, default=0.15)
    restore.add_argument("--settle-seconds", type=float, default=12.0)
    restore.add_argument(
        "--stability-seconds",
        type=float,
        default=3.0,
        help="Require the provider identity to survive a second observation",
    )
    restore.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Attempts per session; retries reuse the same window (sqlite lock contention)",
    )
    restore.add_argument("--retry-backoff", type=float, default=12.0)
    restore.set_defaults(func=restore_command)

    relaunch = sub.add_parser(
        "relaunch",
        help="Re-launch stale-shell windows in place (window alive, AI process gone)",
    )
    relaunch.add_argument("--session", default=SESSION)
    relaunch.add_argument("--snapshot", default="")
    relaunch_choice = relaunch.add_mutually_exclusive_group()
    relaunch_choice.add_argument("--limit", type=int, help="Relaunch at most N stale shells")
    relaunch_choice.add_argument("--all", action="store_true", help="Relaunch every stale shell")
    relaunch_choice.add_argument(
        "--target", default="",
        help="Relaunch exactly one window: window number, pane id (%%N), window name or Cards alias",
    )
    relaunch.add_argument("--yes", action="store_true", help="Confirm the write-capable relaunch")
    relaunch.add_argument("--pause-seconds", type=float, default=1.0)
    relaunch.add_argument("--settle-seconds", type=float, default=12.0)
    relaunch.add_argument(
        "--stability-seconds",
        type=float,
        default=3.0,
        help="Require the provider identity to survive a second observation",
    )
    relaunch.add_argument("--retries", type=int, default=3)
    relaunch.add_argument("--retry-backoff", type=float, default=12.0)
    relaunch.set_defaults(func=relaunch_command)

    reconcile = sub.add_parser(
        "reconcile-cards",
        help="Replay snapshot categories/favorites onto already-restored identities",
    )
    reconcile.add_argument("--session", default=SESSION)
    reconcile.add_argument("--snapshot", default="")
    reconcile.add_argument("--yes", action="store_true")
    reconcile.set_defaults(func=reconcile_cards_command)

    verify = sub.add_parser("verify", help="Compare a snapshot with live provider identities")
    verify.add_argument("--session", default=SESSION)
    verify.add_argument("--snapshot", default="")
    verify.add_argument("--summary", action="store_true", help="One line per window plus a verdict line")
    verify.set_defaults(func=verify_command)

    pin = sub.add_parser(
        "pin-baseline",
        help="Copy the recovery baseline to a fixed pinned-<label>-<stamp>.json for one recovery run",
    )
    pin.add_argument("--snapshot", default="")
    pin.add_argument("--label", default="manual")
    pin.add_argument("--path-only", action="store_true", help="Print only the pinned file path")
    pin.set_defaults(func=pin_baseline_command)

    auto = sub.add_parser(
        "auto",
        help="Pin baseline, then restore -> relaunch -> reconcile-cards -> verify (dry-run unless --yes)",
    )
    auto.add_argument("--session", default=SESSION)
    auto.add_argument("--snapshot", default="")
    auto.add_argument("--yes", action="store_true", help="Actually run the four steps")
    auto.add_argument("--dry-run", action="store_true", help="Only print the plan (default)")
    auto.add_argument("--summary", action="store_true", help="Dry-run output one line per window")
    auto.set_defaults(func=auto_command)

    identity = sub.add_parser(
        "identity",
        help="Read-only: live session identity and CODEX_HOME of one pane (used by agent_window.sh)",
    )
    identity.add_argument("--session", default=SESSION)
    identity.add_argument("--target", required=True, help="pane id (%%N), window number or tmux target")
    identity.add_argument("--resume-id", default="", help="Resolve the CODEX_HOME for this session id")
    identity.add_argument("--kind", default="", choices=["", "claude", "codex"])
    identity.add_argument("--format", default="json", choices=["json", "shell"])
    identity.set_defaults(func=identity_command)

    home = sub.add_parser(
        "codex-home",
        help="Read-only: which CODEX_HOME holds this Codex session (refuses when ambiguous)",
    )
    home.add_argument("--session-id", required=True)
    home.add_argument("--recorded", default="", help="Home recorded in a snapshot; verified, not trusted")
    home.add_argument("--path-only", action="store_true")
    home.set_defaults(func=codex_home_command)
    return top


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.func(args))
    except RecoveryError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
