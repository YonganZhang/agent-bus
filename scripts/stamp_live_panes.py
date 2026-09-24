#!/usr/bin/env python3
"""给已经在跑的 Claude 窗口补上会话身份标记。

平时身份由 SessionStart hook 在会话启动时钉上(见 contrib/claude-hooks/tmux-session-stamp.sh)。
这个脚本管的是两种补救场景:hook 装上之前就已经开着的窗口,以及 hook 因故没跑成的窗口。

身份来源是 `claude agents --json` —— Claude 自己发布的 pid/sessionId,不做任何推断。
pid 通过进程树映射回 tmux pane;映射不到就跳过,不猜。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
PROJECTS = HOME / ".claude" / "projects"


def process_children() -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            ppid = int(Path(f"/proc/{entry}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        children.setdefault(ppid, []).append(int(entry))
    return children


def subtree(root: int, children: dict[int, list[int]]) -> list[int]:
    seen: set[int] = set()
    stack, out = [root], []
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        out.append(pid)
        stack.extend(children.get(pid, []))
    return out


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import secretary_recovery  # noqa: E402


def codex_identity(pids: list[int]) -> tuple[str, str, str]:
    """从进程自己身上读 Codex 的会话号、rollout 文件和 CODEX_HOME。

    Codex 没有 Claude 那样的 hook,只能读它此刻打开着的根 rollout: 复用
    secretary_recovery.codex_live_identity(即 provider_state.select_exact_rollout,
    并排除子智能体 rollout)。同一进程常同时开着根会话和几个子智能体的 rollout,
    取"第一个打开的 .jsonl"会把子智能体的号钉到窗口上。argv 里的号在 /new 之后就
    过期了,不再作为来源;读不出唯一的根 rollout 就跳过,不按 cwd 或时间凑。
    """
    identity = secretary_recovery.codex_live_identity(pids)
    return (
        str(identity.get("session_id") or ""),
        str(identity.get("rollout") or ""),
        str(identity.get("codex_home") or ""),
    )


def transcript_for(session_id: str) -> str:
    """按 session id 找 transcript 文件。

    直接 glob 而不是从 cwd 推目录名: Claude 对目录名的编码会把中文之类的字符折叠掉,
    自己拼一份规则出来就等于又造了一个会漂移的假设。
    """
    hits = sorted(PROJECTS.glob(f"*/{session_id}.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return str(hits[0]) if hits else ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", default=os.environ.get("SECRETARY_TMUX_SESSION", "secretary_web"))
    ap.add_argument("--apply", action="store_true", help="真正写入;默认只报告")
    ap.add_argument("--refresh-pane", metavar="PANE", help="重新核对并刷新一个已有标记的 pane（例如 %1967）")
    args = ap.parse_args()

    agents = json.loads(subprocess.run(["claude", "agents", "--json"],
                                       capture_output=True, text=True, timeout=60).stdout or "[]")
    by_pid = {int(a["pid"]): a for a in agents if a.get("pid")}
    children = process_children()

    fmt = "\t".join(["#{pane_id}", "#{pane_pid}", "#{@ai_session_id}", "#{window_name}"])
    rows = subprocess.run(["tmux", "list-panes", "-s", "-t", args.session, "-F", fmt],
                          capture_output=True, text=True, timeout=30).stdout.splitlines()
    planned, skipped, already, refreshed = [], [], 0, []
    homes: dict[str, str] = {}
    for row in rows:
        parts = row.split("\t")
        if len(parts) != 4:
            continue
        pane_id, pane_pid, stamped, window_name = parts
        if args.refresh_pane and args.refresh_pane != pane_id:
            continue
        if stamped.strip() and args.refresh_pane != pane_id:
            already += 1
            continue
        if not pane_pid.isdigit():
            continue
        pids = subtree(int(pane_pid), children)
        agent = next((by_pid[p] for p in pids if p in by_pid), None)
        if agent:
            entry = (pane_id, window_name, "claude", agent["sessionId"], transcript_for(agent["sessionId"]))
            planned.append(entry)
            if stamped.strip():
                refreshed.append({"pane": pane_id, "window": window_name, "old_session_id": stamped.strip(), "new_session_id": agent["sessionId"]})
            continue
        codex_id, rollout, codex_home = codex_identity(pids)
        if codex_id:
            entry = (pane_id, window_name, "codex", codex_id, rollout)
            homes[pane_id] = codex_home
            planned.append(entry)
            if stamped.strip():
                refreshed.append({"pane": pane_id, "window": window_name, "old_session_id": stamped.strip(), "new_session_id": codex_id})
            continue
        skipped.append((pane_id, window_name))

    for pane_id, name, kind, sid, path in planned:
        if args.apply:
            subprocess.run(["tmux", "set-option", "-p", "-t", pane_id, "@ai_session_id", sid], timeout=10)
            subprocess.run(["tmux", "set-option", "-p", "-t", pane_id, "@ai_provider", kind], timeout=10)
            if path:
                subprocess.run(["tmux", "set-option", "-p", "-t", pane_id, "@ai_transcript", path], timeout=10)
            if homes.get(pane_id):
                subprocess.run(["tmux", "set-option", "-p", "-t", pane_id, "@ai_codex_home", homes[pane_id]], timeout=10)
    print(json.dumps({
        "applied": bool(args.apply),
        "already_stamped": already,
        "refreshed": refreshed,
        "stamped": [{"pane": p, "window": n, "provider": k, "session_id": s, "transcript": bool(t),
                     **({"codex_home": homes[p]} if homes.get(p) else {})}
                    for p, n, k, s, t in planned],
        "no_live_agent": [{"pane": p, "window": n} for p, n in skipped],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
