#!/usr/bin/env bash
# 把 Claude 会话身份钉在它所在的 tmux pane 上(Claude Code SessionStart / SessionEnd hook)。
# 安装方式见 docs/recovery.md。
#
# 为什么需要: 否则"这个窗口对应哪条对话"只能靠扫进程 argv、拿屏幕文字去比对 transcript
# 来猜。同一个目录开两个窗口时内容指纹分不开,两个 pane 会被指向同一条 transcript。
# 而 hook 的 payload 里 session_id 和 transcript_path 是 Claude 自己给的,不用猜。
#
# 钉在 pane option 上而不是别处,因为 tmux 会替我们保管它: 进程被杀、窗口 respawn 之后
# 这个值仍然在,只有 pane 本身消失才跟着消失 —— 正好是我们想要的生命周期。
#
# 硬约束: 任何情况下都 exit 0。hook 失败绝不能拖累 Claude 启动。
set -uo pipefail

payload=$(cat 2>/dev/null || true)
pane="${TMUX_PANE:-}"
[ -z "$pane" ] && exit 0
command -v tmux >/dev/null 2>&1 || exit 0

# Claude can launch short-lived SDK/sidecar sessions from /tmp while the
# interactive Claude process remains in the project pane.  Those hooks inherit
# TMUX_PANE but are not the pane's primary conversation; allowing them to write
# here silently replaces the real session identity (Cards then shows another
# conversation).  Only the hook whose working directory matches the pane's
# current project directory may stamp it.  Keep the hook fail-open when either
# path is unavailable so startup is never blocked.
pane_cwd=$(tmux display-message -p -t "$pane" '#{pane_current_path}' 2>/dev/null || true)
hook_cwd=$(pwd -P 2>/dev/null || true)
if [ -n "$pane_cwd" ] && [ -n "$hook_cwd" ] && [ "$pane_cwd" != "$hook_cwd" ]; then
  exit 0
fi

# A nested `claude -p ...` (or any Claude sub-process launched, e.g. via the
# Bash tool, from *inside* an already-running interactive Claude in this same
# pane/cwd) inherits TMUX_PANE and passes the cwd check above, then would
# stamp the pane with its own short-lived session -- clobbering the real
# conversation's identity even though the real process is still alive and
# working (for example a `claude -p "reply OK"` self-check run from inside the
# pane's own session). Detect this by walking
# the process ancestry from the hook's invoking Claude process up toward the
# pane's leader process: if we hit ANOTHER `claude` process before reaching
# the pane leader, this call is nested inside it -- skip the write. Fail open
# on any lookup failure so a real top-level hook is never blocked.
invoker="${PPID:-}"
if [ -n "$invoker" ] && [ -e "/proc/$invoker" ]; then
  pane_pid=$(tmux display-message -p -t "$pane" '#{pane_pid}' 2>/dev/null || true)
  if [ -n "$pane_pid" ]; then
    walk="$invoker"
    depth=0
    while [ "$depth" -lt 25 ]; do
      if [ "$walk" = "$pane_pid" ]; then
        break
      fi
      if [ "$walk" != "$invoker" ]; then
        comm=$(cat "/proc/$walk/comm" 2>/dev/null || true)
        if [ "$comm" = "claude" ]; then
          exit 0
        fi
      fi
      next=$(awk '{print $4}' "/proc/$walk/stat" 2>/dev/null || true)
      if [ -z "$next" ] || [ "$next" = "0" ] || [ "$next" = "1" ]; then
        break
      fi
      walk="$next"
      depth=$((depth + 1))
    done
  fi
fi

read -r sid tpath event <<<"$(printf '%s' "$payload" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("- - -"); raise SystemExit
def clean(v):
    v = str(v or "").strip()
    # 只接受不带空白的普通标量,避免把任意内容塞进 tmux 选项
    return v if v and not any(c.isspace() for c in v) else "-"
print(clean(d.get("session_id")), clean(d.get("transcript_path")), clean(d.get("hook_event_name")))
' 2>/dev/null || echo "- - -")"

[ "${sid:--}" != "-" ] && tmux set-option -p -t "$pane" @ai_session_id "$sid" 2>/dev/null
[ "${tpath:--}" != "-" ] && tmux set-option -p -t "$pane" @ai_transcript "$tpath" 2>/dev/null
if [ "${sid:--}" != "-" ]; then
  tmux set-option -p -t "$pane" @ai_provider "claude" 2>/dev/null
  tmux set-option -p -t "$pane" @ai_stamped_at "$(date +%s)" 2>/dev/null
fi

# 会话结束只记一个时间戳,**不清除** session_id / transcript —— 那两个正是
# `recovery relaunch` 用来把一个空壳窗口拉回原对话的依据,清掉等于把还救得回来的
# 窗口变成"知道它空着但不知道该恢复什么"。是否还活着由进程树判断,不看这个标记。
case "${event:--}" in
  SessionEnd) tmux set-option -p -t "$pane" @ai_ended_at "$(date +%s)" 2>/dev/null ;;
  *)          tmux set-option -pu -t "$pane" @ai_ended_at 2>/dev/null ;;
esac
exit 0
