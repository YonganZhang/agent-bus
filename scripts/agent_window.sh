#!/usr/bin/env bash
# agent 窗口流水线: 打开项目 / 新开 / 重启 / 关闭(批量) / 列出 claude·codex·bash 的 tmux 窗口。
# 一步到位,避免在 TUI 里 send-keys 发命令绕远路。
#
# 用法:
#   agent_window.sh open  [--codex] (--resume-id ID | --fresh) <关键词> [关键词...]   ← 已开则跳过
#   agent_window.sh new   <claude|codex|bash> (--resume-id ID | --fresh) [--name N] [--cwd DIR] [--session S]
#   agent_window.sh restart <win|name> [claude|codex|bash] [--fresh] [--cwd DIR] [--session S]
#     公共开关: --fresh 明确开新会话(claude 会自动钉一个 --session-id)
#               --resume-id <ID> 精确恢复某一条会话(claude --resume / codex resume)
#               --shared-home 让这个 codex 窗口回到共享默认 CODEX_HOME(默认是隔离的,见第 6 条)
#               --codex-home <路径> 强制用指定的 CODEX_HOME(内部用于 restart 沿用旧隔离目录)
#     open/new 起 claude/codex 时 --resume-id / --fresh 必须二选一;都没给就报错并列出可选会话
#     (见第 3 条)。restart 不给时按窗口里实时读到的精确会话号重来,读不到就拒绝。
#   agent_window.sh close <win|name> [更多...]            ← 批量关
#   agent_window.sh close-all [--keep <win|name关键词>]    ← 关所有(可保留一个,按 win 号或名字关键词)
#   agent_window.sh list  [--session S]
#
# 环境变量:
#   AGENT_BUS_PROJECTS_DIR        open 按关键词找项目的目录(默认 ~/projects)
#   AGENT_BUS_SESSION_SHELL       AI 启动 wrapper(默认 <repo>/bin/ai-session-shell)
#   AGENT_BUS_DEFAULT_CODEX_HOME  默认/共享 CODEX_HOME(默认 ~/.codex)
#   AGENT_BUS_CODEX_HOMES_ROOT    隔离 CODEX_HOME 的父目录(默认 ~/.codex-homes)
#
# 关键设计:
#   1. `bash -lc` 起 → 登录 shell 重新加载 ~/.bashrc 等环境(代理、PATH) → 新 claude/codex
#      拿到的是当前环境,而不是旧 shell 里可能已过期的变量。
#   2. 用 `<cmd>; exec bash` 而非 `exec <cmd>` → 程序退出后窗口不关,留个 bash 可复用。
#      (`exec codex` 时 codex 一退出,整个窗口就跟着关了。)
#   3. 没有隐式的"接最近一次": open/new 起 claude/codex 必须显式 --resume-id <ID>(精确恢复)
#      或 --fresh(明确新开),两者都没给就报错,并列出同 cwd 的候选会话和对应命令
#      (Claude: claude agents 里同 cwd 的活会话 + ~/.claude/projects/<cwd 编码>/ 最近 transcript;
#       Codex: 默认 ~/.codex 与 ~/.codex-homes/* 里同 cwd 的最近根 rollout)。
#      原因: claude --continue 可能接到后台 claude -p 或别的窗口退出前的会话;
#      codex 分到全新隔离 CODEX_HOME 时 resume --last 在空 home 里其实起的是新会话。
#      --continue / --resume 这两个旧开关已取消,传了直接报错。不要在 TUI 输入框里发送裸 continue。
#   4. restart = kill-window + new-window 一步 → 绝不在 TUI 里 send-keys 发 /exit、source。
#      (TUI 没真退出时,发的命令会全部打进输入框而不执行。)
#   5. 本脚本不调用 claude --continue / codex resume --last: 这两个入口按 cwd 取"最近一条",
#      同目录多个窗口一起用会全部落到同一条对话上。
#      新会话一律 claude --session-id <uuid> 显式钉号,退出时 wrapper 会打印恢复命令。
#   6. codex(不含 claude)新开/重启默认分配独立 CODEX_HOME(.codex-homes/<slug>/,
#      软链 auth.json/rules/skills,不拷贝),避免多个 codex 挤在同一个默认 home 里
#      抢 state/logs sqlite 的锁导致新会话卡死在 "model: loading"。
#      已隔离窗口 restart 会自动读回原来的 CODEX_HOME 继续用,不会被打回默认 home。
#      仍在共享默认 home 的既有窗口不会被主动重启/迁移;只有以后 restart 时才顺带转成隔离。
set -uo pipefail

SESSION="${SECRETARY_TMUX_SESSION:-secretary_web}"
PROJECTS_DIR="${AGENT_BUS_PROJECTS_DIR:-$HOME/projects}"
NAME=""
CWD=""
RESUME_DEFAULT=1
EXPLICIT_FRESH=0
RESUME_ID=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# 默认 CODEX_HOME 不能跟着环境变量 CODEX_HOME 走: 隔离 Codex 窗口里的 shell 继承的是
# ~/.codex-homes/<slug>,在那里调用本脚本会把它误当成默认 home。
DEFAULT_CODEX_HOME="${AGENT_BUS_DEFAULT_CODEX_HOME:-$HOME/.codex}"
AI_SHELL="${AGENT_BUS_SESSION_SHELL:-$REPO_ROOT/bin/ai-session-shell}"
RECOVERY_PY="$SCRIPT_DIR/secretary_recovery.py"
BUS_CLI="$REPO_ROOT/bin/agent-bus"
# 多个 codex 进程共享同一个 CODEX_HOME 会在 state/logs sqlite 上抢锁,严重时新会话卡死在
# "model: loading"、Ctrl+C 都救不回来。默认给每个新开/重启的 codex 窗口分配独立
# CODEX_HOME;只有 --shared-home 才退回"大家挤一个默认 home"的行为。claude 不受影响。
CREATE_ISOLATED_HOME="$SCRIPT_DIR/create-isolated-codex-home.sh"
SHARED_HOME=0
CODEX_HOME_OVERRIDE=""
SOURCE_CODEX_HOME=""

new_uuid() { cat /proc/sys/kernel/random/uuid; }

is_uuid() {
  [[ "$1" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]
}

# 走 ai-session-shell: AI 退出后它会打印 [Shell is ready. Resume with:] <原命令>,
# 窗口里就永远留着这一条可复制的恢复命令,不用事后去猜是哪条会话。
wrap() {
  if [ -x "$AI_SHELL" ]; then echo "$AI_SHELL $*"; else echo "$*"; fi
}

# 和 wrap() 一样,但如果拿到了隔离 CODEX_HOME 就把 `env CODEX_HOME=...` 前缀塞进去。
# home 必须 %q: 隔离目录名取自 cwd 的项目名,带空格或单引号时裸拼会把命令拆坏。
wrap_env() {
  local home="$1"; shift
  if [ -n "$home" ]; then wrap "env CODEX_HOME=$(printf '%q' "$home") $*"; else wrap "$*"; fi
}

same_path() {
  [ -n "$1" ] && [ -n "$2" ] && [ "$(realpath -m -- "$1")" = "$(realpath -m -- "$2")" ]
}

# 给一个新的/要转成隔离的 codex 窗口起一个 slug:优先用 cwd 的项目名(去掉尾部 hash)
# 做前缀,可读;总是加一段随机后缀保证不同窗口不会撞到同一个隔离目录——同 cwd 开
# 多个协作窗口(比如同一个项目的多个 git worktree)是本机常态,这些窗口互相之间也要
# 各自独立,不能共用一个隔离 home,否则原样重现"共享 db 抢锁"的问题。
new_codex_home_slug() {
  local cwd="$1" kind="$2" base
  if [ -n "$cwd" ]; then base=$(basename "$cwd" | sed 's/-[0-9a-fA-F]\{6,\}$//'); fi
  [ -n "${base:-}" ] || base="${kind:-agent}"
  printf '%s-%s' "$base" "$(new_uuid | cut -c1-6)"
}

# prog_cmd 被要求"恢复"却没有精确会话号: open/new 在更早处已挡住并列出候选,
# restart 只在读到实时会话号时才走恢复;走到这里说明调用方漏了检查。
no_implicit_resume() {
  echo "⛔ $1: 要恢复却没有 --resume-id,本脚本不再做隐式的\"接最近一次\"(--continue / resume --last)。" >&2
  echo "   精确恢复用 --resume-id <会话ID>,明确新开用 --fresh。" >&2
}

# 列出某个 cwd 下可选的会话(只读,写到 stderr)。$1=claude|codex $2=cwd
#   Claude: `claude agents --json` 里同 cwd 的活会话(再接一次会让两个窗口写同一条对话)
#           + ~/.claude/projects/<cwd 编码>/ 下最近的交互式 transcript(sdk-cli 即后台
#           claude -p / SDK 会话只计数不列出)。
#   Codex:  默认 CODEX_HOME、~/.codex-homes/* 与 --codex-home 指定目录里同 cwd 的最近根 rollout
#           (子智能体 rollout 不列)。
list_session_candidates() {
  python3 - "$1" "$2" "$SCRIPT_DIR" "$DEFAULT_CODEX_HOME" "$CODEX_HOME_OVERRIDE" <<'PY' >&2
import glob, json, os, re, sys, time
from pathlib import Path

kind, cwd, script_dir, default_home, override_home = sys.argv[1:6]
LIMIT = 5
real = os.path.realpath(cwd)


def stamp(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def newest_first(paths):
    rows = []
    for path in paths:
        try:
            rows.append((os.stat(path).st_mtime, path))
        except OSError:
            continue
    rows.sort(reverse=True)
    return rows


UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

if kind == "claude":
    sys.path.insert(0, script_dir)
    try:
        import claude_sessions
        records, error = claude_sessions.agent_records()
    except Exception as exc:  # 列候选失败要看得见,但不影响"拒绝隐式恢复"本身
        records, error = [], f"{type(exc).__name__}: {exc}"
    live = [r for r in records
            if r.get("sessionId") and os.path.realpath(str(r.get("cwd") or "")) == real]
    live_ids = {str(r["sessionId"]) for r in live}
    print("  同 cwd 正在运行的 claude 会话(已在别处运行,再 --resume-id 接会变成两个窗口写同一条对话):")
    if error:
        print(f"    (claude agents 读取失败: {error})")
    for r in live:
        print(f"    {r['sessionId']}  pid={r.get('pid')} status={r.get('status')} "
              f"kind={r.get('kind')} name={r.get('name') or ''}")
    if not live and not error:
        print("    (无)")
    dirs = []
    for path in dict.fromkeys([cwd, real]):
        d = Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", path)
        if d.is_dir() and d not in dirs:
            dirs.append(d)
    print(f"  最近的 claude transcript({', '.join(str(d) for d in dirs) or '无 ~/.claude/projects/<cwd 编码>/ 目录'}):")
    shown = sdk = 0
    for mtime, path in newest_first([f for d in dirs for f in d.glob("*.jsonl")])[:300]:
        if shown >= LIMIT:
            break
        sid = path.stem
        if not UUID.fullmatch(sid):
            continue
        entrypoint = ""
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for index, line in enumerate(fh):
                    if index >= 40:
                        break
                    try:
                        entrypoint = str(json.loads(line).get("entrypoint") or "")
                    except (ValueError, AttributeError):
                        continue
                    if entrypoint:
                        break
        except OSError:
            continue
        if entrypoint == "sdk-cli":
            sdk += 1
            continue
        note = "  (正在运行)" if sid in live_ids else ""
        print(f"    {sid}  {stamp(mtime)}  entrypoint={entrypoint or '?'}{note}")
        shown += 1
    if not shown:
        print("    (无)")
    if sdk:
        print(f"    另有 {sdk} 条 sdk-cli(后台 claude -p / SDK)会话未列出")
else:
    homes = []
    candidates = [override_home, default_home, *sorted(glob.glob(str(Path.home() / ".codex-homes" / "*")))]
    for home in candidates:
        if home and os.path.isdir(os.path.join(home, "sessions")):
            key = os.path.realpath(home)
            if key not in [os.path.realpath(h) for h in homes]:
                homes.append(home)
    files = [f for home in homes
             for f in glob.glob(os.path.join(home, "sessions", "*", "*", "*", "rollout-*.jsonl"))]
    print(f"  最近的 codex 根 rollout(查了 {len(homes)} 个 CODEX_HOME):")
    rows = []
    # 全部扫一遍(只读第一行,1000+ 个文件约 0.1s): 同一会话号在多个 home 里都有时要能提示。
    for mtime, path in newest_first(files):
        try:
            with open(path, "rb") as fh:
                first = json.loads(fh.readline() or b"{}")
        except (OSError, ValueError):
            continue
        payload = first.get("payload") if isinstance(first, dict) and first.get("type") == "session_meta" else None
        if not isinstance(payload, dict) or payload.get("thread_source") == "subagent":
            continue
        if os.path.realpath(str(payload.get("cwd") or "")) != real:
            continue
        sid = str(payload.get("id") or "")
        home = path.split(os.sep + "sessions" + os.sep, 1)[0]
        rows.append((sid, mtime, home))
    seen = {}
    for sid, _, _ in rows:
        seen[sid] = seen.get(sid, 0) + 1
    for sid, mtime, home in rows[:LIMIT]:
        note = "  (多个 CODEX_HOME 都有这条,恢复时加 --codex-home <目录> 指定)" if seen[sid] > 1 else ""
        print(f"    {sid}  {stamp(mtime)}  CODEX_HOME={home}{note}")
    if not rows:
        print("    (无)")
PY
}

# open/new 起 claude/codex 前: 必须显式 --resume-id 或 --fresh,否则报错并列出候选。
# $1=kind $2=cwd $3=给用户照抄的命令前缀(不含 --resume-id/--fresh)
require_session_choice() {
  local kind="$1" cwd="$2" cmd="$3"
  case "$kind" in claude|codex|node) ;; *) return 0 ;; esac
  [ "$kind" = "node" ] && kind="codex"
  [ -n "$RESUME_ID" ] || [ "$EXPLICIT_FRESH" = "1" ] && return 0
  echo "⛔ 起 $kind 必须显式二选一: --resume-id <会话ID>(精确恢复)或 --fresh(明确新开)。" >&2
  echo "   不再有隐式的\"接最近一次\": claude --continue 会接到后台 claude -p 或别的窗口的会话," >&2
  echo "   codex 分到新隔离 CODEX_HOME 时 resume --last 实际起的是新会话。未开任何窗口。" >&2
  if [ -n "$cwd" ]; then
    echo "   可选会话(cwd=$cwd):" >&2
    list_session_candidates "$kind" "$cwd" || echo "    (列候选失败,见上方报错)" >&2
  else
    echo "   (没给 --cwd,无法列出同目录的会话;加 --cwd <目录> 再跑一次即可看到候选)" >&2
  fi
  echo "   精确恢复: $cmd --resume-id <会话ID>" >&2
  echo "   明确新开: $cmd --fresh" >&2
  return 1
}

# 简称 → 启动命令。$2=1 表示要恢复(必须已有 RESUME_ID),0 表示新开;$3=目标 cwd;
# bash/sh/空 → 纯 shell。起不了时返回非 0 并把原因写到 stderr,调用方必须中止。
prog_cmd() {
  local kind="$1" resume="${2:-$RESUME_DEFAULT}" target_cwd="${3:-}" uuid
  case "$kind" in
    claude)
      # 可选的 --permission-mode(AGENT_BUS_CLAUDE_PERMISSION_MODE,默认不加,沿用 Claude 自己的
      # settings)。设成 bypassPermissions 可以让 Plan 模式收尾时不再弹模式选择菜单(那个菜单
      # 不是普通权限请求,hook 拦不住;只有会话从启动起就有 bypass 资格才不出现)。
      # ⚠️ 代价:bypassPermissions 关掉了 Claude 的权限确认与 auto 模式的安全分类器,官方只建议
      # 在隔离环境里用。是否开启由部署者自己决定,本仓库默认不开。
      local mode_flag=""
      [ -n "${AGENT_BUS_CLAUDE_PERMISSION_MODE:-}" ] && mode_flag=" --permission-mode $AGENT_BUS_CLAUDE_PERMISSION_MODE"
      if [ -n "$RESUME_ID" ]; then wrap "claude --resume $RESUME_ID$mode_flag"; return 0; fi
      # 没有隐式的"接最近一次"(claude --continue 会接到后台 claude -p 或别的窗口的会话)。
      [ "$resume" = "1" ] && { no_implicit_resume claude; return 1; }
      uuid="$(new_uuid)"
      wrap "claude --session-id $uuid$mode_flag"
      ;;
    codex|node)   # pane_current_command 里 codex 显示为 node
      local home="" is_new_home=0 slug
      # 先挡在建隔离 home 之前: 要恢复却没有精确会话号时,不能落到 resume --last
      # (新隔离 home 里没有历史,resume --last 实际起的是新会话)。
      if [ "$resume" = "1" ] && [ -z "$RESUME_ID" ]; then no_implicit_resume codex; return 1; fi
      if [ -n "$CODEX_HOME_OVERRIDE" ]; then
        home="$CODEX_HOME_OVERRIDE"
      elif [ "$SHARED_HOME" != "1" ]; then
        slug="$(new_codex_home_slug "$target_cwd" "$kind")"
        home="$("$CREATE_ISOLATED_HOME" "$slug" "$target_cwd")" || { echo "⛔ 无法创建隔离 CODEX_HOME(slug=$slug)" >&2; return 1; }
        is_new_home=1
      fi
      # 新隔离 home 没有历史。显式/已证明的会话先验证并复制单条 rollout，
      # 失败保持旧窗口；只有显式 --fresh 的窗口才开全新会话。
      if [ "$is_new_home" = "1" ]; then
        if [ -n "$RESUME_ID" ]; then
          local source_home="${SOURCE_CODEX_HOME:-}"
          # 源 home 必须是真有这条 rollout 的那一个。默认回落到共享 ~/.codex 会拷走迁移前
          # 留下的旧副本(隔离窗口的会话在共享目录里往往只剩更短的旧分叉,或根本不存在)。
          if [ -z "$source_home" ]; then
            source_home="$(python3 "$RECOVERY_PY" codex-home --session-id "$RESUME_ID" --path-only)" || {
              echo "⛔ 找不到会话 ${RESUME_ID:0:8}… 唯一所在的 CODEX_HOME；用 --codex-home <目录> 显式指定。" >&2
              return 1
            }
          fi
          python3 "$SCRIPT_DIR/window_transition.py" copy-session \
            "$source_home" "$home" "$RESUME_ID" "$target_cwd" >&2 || return 1
          wrap_env "$home" "codex resume $RESUME_ID"
          return 0
        fi
        wrap_env "$home" "codex"
        return 0
      fi
      if [ -n "$RESUME_ID" ]; then wrap_env "$home" "codex resume $RESUME_ID"; return 0; fi
      wrap_env "$home" "codex"
      ;;
    bash|sh|"") echo "" ;;
    *) echo "$kind" ;;        # 其它当自定义命令原样跑
  esac
}

# 起一个窗口: $1=window名 $2=程序命令(可空) $3=cwd $4=可选目标窗口号(restart专用)
spawn_window() {
  local wname="$1" prog="$2" cwd="$3" idx="${4:-}" inner="exec bash" target="$SESSION"
  # 走 wrapper 时用 exec: wrapper 自己会在 AI 退出后 exec 回 bash,窗口不会关,
  # 而外层 bash 被替换掉,argv 里就不会永久留着 "claude --session-id X" 这串字符
  # ——快照按 argv 认身份,残留会让空窗口一直被当成活会话。
  if [ -n "$prog" ]; then
    case "$prog" in
      "$AI_SHELL "*) inner="exec $prog" ;;
      *) inner="$prog; exec bash" ;;
    esac
  fi
  # restart 传入 idx 时钉死同一个窗口号:不钉的话 tmux new-window 会捡当前最小
  # 的空闲号,批量重启多个窗口时号码会级联漂移。Cards 本身按 pane id 认身份不受影响,
  # 但人看的窗口号会全部对不上。
  [ -n "$idx" ] && target="$SESSION:$idx"
  # cwd 交给 tmux -c,不拼进命令串:带空格或引号的目录名会把命令拼坏。
  # inner 放进单引号前把其中的 ' 写成 '\'' —— 这个外层串由 tmux 的 default-shell
  # 解析(可能是 sh),所以只用 POSIX 引号,不用 bash 专有的 $'...'。
  local pane quoted="'${inner//\'/\'\\\'\'}'"
  if [ -n "$cwd" ]; then
    pane="$(tmux new-window -P -F '#{pane_id}' -t "$target" -n "$wname" -c "$cwd" "bash -lc $quoted")" || return 1
  else
    pane="$(tmux new-window -P -F '#{pane_id}' -t "$target" -n "$wname" "bash -lc $quoted")" || return 1
  fi
  stamp_pane_identity "$pane" "$prog"
}

# 启动时就把身份钉在 pane 上(@ai_provider / @ai_session_id / @ai_codex_home)。
# AI 退出后进程信号全没了,只剩这些标记告诉 restart / 快照"这是哪个 provider、在哪个
# CODEX_HOME";不钉的话 Codex 窗口一退出就退化成普通 bash,身份无从恢复。
stamp_pane_identity() {
  local pane="$1" prog="$2" rest kind="" home="" sid=""
  [ -n "$pane" ] && [ -n "$prog" ] || return 0
  rest="${prog#"$AI_SHELL "}"
  case "$rest" in
    "env CODEX_HOME="*)
      # home 是 %q 过的。需要转义的少见路径不在这里还原(不 eval),
      # 留给 stamp_live_panes.py 从运行中的进程读出真实 CODEX_HOME。
      kind=codex; home="${rest#env CODEX_HOME=}"; home="${home%% *}"
      case "$home" in *\\*|\$\'*) home="" ;; esac
      ;;
    codex|"codex "*) kind=codex ;;
    claude|"claude "*) kind=claude ;;
  esac
  [ -n "$kind" ] || return 0
  tmux set-option -p -t "$pane" @ai_provider "$kind" 2>/dev/null
  [ -n "$home" ] && tmux set-option -p -t "$pane" @ai_codex_home "$home" 2>/dev/null
  sid="$(printf '%s' "$rest" | grep -oE '(--session-id|--resume|resume) [0-9a-fA-F-]{36}' | grep -oE '[0-9a-fA-F-]{36}$' | head -1)"
  if [ -n "$sid" ] && is_uuid "$sid"; then
    tmux set-option -p -t "$pane" @ai_session_id "$sid" 2>/dev/null
  fi
  return 0
}

# A restart replaces the tmux pane, so Cards keys based on pane id become
# stale. Take a fresh identity snapshot before the destructive step and
# reconcile metadata immediately after the replacement. Fail closed before
# killing the old window if the checkpoint cannot be written.
cards_checkpoint() {
  [ "${AGENT_BUS_CARDS_CHECKPOINT:-1}" = "1" ] || return 0
  [ -x "$BUS_CLI" ] || {
    echo "⛔ agent-bus CLI not found at $BUS_CLI; refusing pane replacement without Cards checkpoint" >&2
    return 1
  }
  local out rc
  out="$("$BUS_CLI" recovery snapshot 2>&1)"; rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "⛔ Cards/recovery 快照没通过(exit=$rc)，拒绝替换窗口。快照输出:" >&2
    printf '%s\n' "$out" | tail -5 >&2
    if [ "$rc" -eq 3 ]; then
      echo "   快照被判 degraded(原因见上面的 reasons)。若窗口数/会话号的变化是你有意为之，" >&2
      echo "   确认后运行: secretary-bus recovery snapshot --force ，再重试 restart。" >&2
    fi
    return 1
  fi
}

cards_reconcile_after_restart() {
  [ "${AGENT_BUS_CARDS_CHECKPOINT:-1}" = "1" ] || return 0
  "$BUS_CLI" recovery reconcile-cards --yes >/dev/null 2>&1 || {
    echo "⚠️ pane restarted, but Cards reconciliation needs verification: secretary-bus recovery reconcile-cards --yes" >&2
    return 1
  }
}

# 打开单个项目(关键词模糊匹配 projects/): $1=关键词 $2=程序(claude/codex) $3=是否恢复
open_one() {
  local kw="$1" kind="$2" resume="${3:-$RESUME_DEFAULT}" prog matches n name existing
  existing=$(tmux list-windows -t "$SESSION" -F "#{window_index} #{pane_current_path}" 2>/dev/null | grep -iE "projects/.*$kw" | head -1 | awk '{print $1}')
  if [ -n "$existing" ]; then echo "↗️ [$kw] 已开 win$existing,跳过"; return 0; fi
  matches=$(ls -d "$PROJECTS_DIR"/*"$kw"* 2>/dev/null)
  n=$(printf '%s\n' "$matches" | grep -c .)
  [ "$n" -eq 0 ] && { echo "⚠️ [$kw] 没找到项目"; return 1; }
  [ "$n" -gt 1 ] && { echo "⚠️ [$kw] 匹配多个,请更精确:"; printf '%s\n' "$matches" | sed 's|.*/projects/|     |'; return 1; }
  name=$(basename "$matches" | sed 's/-[0-9a-f]\{6\}$//')
  local open_cmd="$SCRIPT_DIR/agent_window.sh open"
  [ "$SESSION" != "secretary_web" ] && open_cmd="$open_cmd --session $(printf '%q' "$SESSION")"
  [ "$kind" = "codex" ] && open_cmd="$open_cmd --codex"
  require_session_choice "$kind" "$matches" "$open_cmd $(printf '%q' "$kw")" || return 1
  prog="$(prog_cmd "$kind" "$resume" "$matches")" || return 1
  spawn_window "$name" "$prog" "$matches"
  echo "✅ 打开 [$name] 起 $prog @ $matches"
}

# 抽出 --name / --cwd / --session,剩下的位置参数放回 REST
REST=()
ACTION="${1:-list}"; shift || true
while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --cwd)  CWD="$2"; shift 2 ;;
    --session) SESSION="$2"; shift 2 ;;
    --fresh|--no-resume) RESUME_DEFAULT=0; EXPLICIT_FRESH=1; shift ;;
    --resume|--continue)
      echo "⛔ $1 已取消: 不再有隐式的\"接最近一次\"。用 --resume-id <会话ID> 精确恢复,或 --fresh 明确新开。" >&2
      exit 1 ;;
    --resume-id|--session-id)
      RESUME_ID="$2"
      is_uuid "$RESUME_ID" || { echo "⛔ --resume-id 需要一个 UUID,收到: $RESUME_ID" >&2; exit 1; }
      shift 2 ;;
    --shared-home) SHARED_HOME=1; shift ;;
    --codex-home) CODEX_HOME_OVERRIDE="$2"; shift 2 ;;
    *) REST+=("$1"); shift ;;
  esac
done
set -- ${REST[@]+"${REST[@]}"}   # 空数组不要退化成一个空参数(会让 close 静默成功)
if [ -n "$RESUME_ID" ] && [ "$EXPLICIT_FRESH" = "1" ]; then
  echo "⛔ --resume-id 和 --fresh 只能二选一。" >&2
  exit 1
fi

case "$ACTION" in
  open)
    kind="claude"; resume="$RESUME_DEFAULT"; kws=()
    for a in "$@"; do
      case "$a" in
        --codex) kind="codex" ;;
        --claude) kind="claude" ;;
        "") ;;
        *) kws+=("$a") ;;
      esac
    done
    [ ${#kws[@]} -eq 0 ] && { echo "用法: open [--codex] (--resume-id ID | --fresh) <关键词> [关键词...]"; exit 1; }
    if [ -n "$RESUME_ID" ] && [ ${#kws[@]} -gt 1 ]; then
      echo "⛔ --resume-id 只能对应一个项目,收到 ${#kws[@]} 个关键词。" >&2
      exit 1
    fi
    for kw in "${kws[@]}"; do open_one "$kw" "$kind" "$resume"; done
    ;;
  new)
    kind="${1:-bash}"
    new_cmd="$SCRIPT_DIR/agent_window.sh new $kind"
    [ -n "$CWD" ] && new_cmd="$new_cmd --cwd $(printf '%q' "$CWD")"
    [ -n "$NAME" ] && new_cmd="$new_cmd --name $(printf '%q' "$NAME")"
    [ "$SESSION" != "secretary_web" ] && new_cmd="$new_cmd --session $(printf '%q' "$SESSION")"
    [ "$SHARED_HOME" = "1" ] && new_cmd="$new_cmd --shared-home"
    [ -n "$CODEX_HOME_OVERRIDE" ] && new_cmd="$new_cmd --codex-home $(printf '%q' "$CODEX_HOME_OVERRIDE")"
    require_session_choice "$kind" "$CWD" "$new_cmd" || exit 1
    prog="$(prog_cmd "$kind" "$RESUME_DEFAULT" "$CWD")" || exit 1
    [ -z "$NAME" ] && NAME="$kind"
    spawn_window "$NAME" "$prog" "$CWD"
    echo "✅ 新窗口 [$NAME] 起 ${prog:-bash} @ ${CWD:-继承} (session=$SESSION)"
    ;;
  restart)
    win="${1:-}"
    [ -z "$win" ] && { echo "用法: restart <win|name> [claude|codex|bash]"; exit 1; }
    oldname="$(tmux display-message -p -t "$SESSION:$win" "#{window_name}" 2>/dev/null || echo "$win")"
    oldcwd="$(tmux display-message -p -t "$SESSION:$win" "#{pane_current_path}" 2>/dev/null || true)"
    oldcmd="$(tmux display-message -p -t "$SESSION:$win" "#{pane_current_command}" 2>/dev/null || true)"
    # 撞车检查按 window_index 比对,而 $win 可能是窗口名 —— 先解析成号,否则排除不掉自己。
    oldidx="$(tmux display-message -p -t "$SESSION:$win" "#{window_index}" 2>/dev/null || echo "$win")"
    oldpane="$(tmux display-message -p -t "$SESSION:$win" '#{pane_id}')" || exit 1
    oldidentity="$(tmux display-message -p -t "$oldpane" '#{pid}:#{pane_pid}:#{window_id}:#{pane_current_path}')" || exit 1
    # restart 是整窗 kill + new-window: 有分屏时会连带关掉同窗口的其它 pane。
    oldpanes="$(tmux display-message -p -t "$oldpane" '#{window_panes}')" || exit 1
    if [ "$oldpanes" != "1" ]; then
      echo "⛔ 窗口 #$oldidx 有 $oldpanes 个分屏；restart 会整窗 kill，连带关掉其它分屏。" >&2
      echo "   先把要保留的分屏 break-pane 成独立窗口，或手动处理后再 restart。" >&2
      exit 1
    fi
    # 身份按"实时优先": Claude 用 claude agents 按 pid 取会话号，Codex 用它打开着的根
    # rollout(同时给出 CODEX_HOME)；启动参数 argv 只作兜底，与实时信号不一致时记冲突。
    # /clear、/resume、/new 之后 argv 就过期了，按 argv 重启会回到切换前的对话。
    id_args=(identity --session "$SESSION" --target "$oldpane" --format shell)
    [ -n "$RESUME_ID" ] && id_args+=(--resume-id "$RESUME_ID")
    case "${2:-}" in
      codex|node) id_args+=(--kind codex) ;;
      claude) id_args+=(--kind claude) ;;
    esac
    ident="$(python3 "$RECOVERY_PY" "${id_args[@]}")" || {
      echo "⛔ 读不出窗口 #$oldidx 的会话身份(secretary_recovery.py identity 失败)，保持原窗口。" >&2
      exit 1
    }
    ID_KIND=""; ID_SESSION=""; ID_SOURCE=""; ID_ALIVE="0"; ID_CODEX_HOME=""
    ID_RESUME_HOME=""; ID_RESUME_HOME_ERROR=""; ID_CONFLICT=""
    eval "$ident"
    oldkind="$ID_KIND"
    kind="${2:-$oldcmd}"
    [ -z "${2:-}" ] && [ -n "$oldkind" ] && kind="$oldkind"
    [ "$kind" = "node" ] && kind="codex"
    is_ai=0
    { [ "$kind" = "codex" ] || [ "$kind" = "claude" ]; } && is_ai=1
    # AI 已经退出: 窗口里没有实时身份可读。继续 restart 要么把窗口变成普通 bash，
    # 要么在错的 CODEX_HOME 里接上旧分叉。就地恢复是 recovery relaunch 的活
    # (它按快照里记下的会话号和 CODEX_HOME 拉起，并且只动空闲 shell)。
    if [ "$RESUME_DEFAULT" = "1" ] && [ -z "$RESUME_ID" ] && [ "$ID_ALIVE" != "1" ] && [ "$is_ai" = "1" ]; then
      echo "⛔ 窗口 #$oldidx 里的 AI 已退出(pane 标记: ${oldkind:-无})，restart 读不到实时会话。" >&2
      echo "   改用就地恢复: secretary-bus recovery plan --summary      # 确认 win$oldidx 是 stale-shell" >&2
      echo "                 secretary-bus recovery relaunch --target $oldidx --yes" >&2
      echo "   (确实要换成新会话才用 restart --fresh；要接指定会话用 --resume-id <ID>)" >&2
      exit 1
    fi
    # 显式 --resume-id 时调用方已指明会话：窗口里 AI 已退出、认不出原 AI 也可以按该会话号拉起
    # (跨模型复用会话号仍然拒绝)。
    if [ "$RESUME_DEFAULT" = "1" ] && [ "$is_ai" = "1" ] && [ "$oldkind" != "$kind" ] \
       && { [ -n "$oldkind" ] || [ -z "$RESUME_ID" ]; }; then
      if [ -z "$oldkind" ]; then
        echo "⛔ 认不出窗口 #$oldidx 原来跑的是哪个 AI，不能按会话号重启为 $kind；保持原窗口。" >&2
        echo "   要接指定会话用 --resume-id <ID>，要开新会话用 --fresh。" >&2
      else
        echo "⛔ 跨模型不能复用会话号。先用 secretary-bus window prepare '#$oldidx' --to $kind，再 launch/verify/promote；原窗口保留。" >&2
      fi
      exit 1
    fi
    [ -z "$CWD" ] && CWD="$oldcwd"
    # restart 的语义是"原样重来": 用实时读到的那条会话号，cwd 级的 --continue / --last
    # 并不保证回到同一条。
    if [ -z "$RESUME_ID" ] && [ "$RESUME_DEFAULT" = "1" ] && [ -n "$ID_SESSION" ]; then
      RESUME_ID="$ID_SESSION"
      echo "↩️ 读到当前会话号 ${RESUME_ID:0:8}…(来源 $ID_SOURCE)，按精确会话重启"
    fi
    if [ -n "$ID_CONFLICT" ]; then
      echo "⚠️ 会话身份信号不一致(已按实时信号处理，未采用过期的启动参数): $ID_CONFLICT" >&2
    fi
    if [ "$RESUME_DEFAULT" = "1" ] && [ "$is_ai" = "1" ] && [ -z "$RESUME_ID" ]; then
      echo "⛔ 无法证明当前精确会话号；保持原窗口，先恢复身份，或显式 --fresh。" >&2
      exit 1
    fi
    if [ -n "$RESUME_ID" ] && ! is_uuid "$RESUME_ID"; then
      echo "⛔ 会话标记不是合法 UUID，保持原窗口。" >&2
      exit 1
    fi
    # CODEX_HOME 用记录下来的那个(打开的 rollout 路径 > 进程 environ > pane 标记)，
    # 绝不默认回落共享 ~/.codex: 迁移后共享目录里多半只剩更短的旧副本，在那里 resume
    # 会静默接上旧分叉并重新抢锁。
    #   - 隔离窗口: 原样沿用同一个隔离目录;
    #   - 共享默认 home 的窗口: 按"以后新开/重启都用隔离"的策略转成隔离，从共享目录拷这一条;
    #   - 定不下来(两处都有、哪都没有): 拒绝，保持原窗口。
    if [ "$kind" = "codex" ] && [ "$SHARED_HOME" != "1" ] && [ -z "$CODEX_HOME_OVERRIDE" ]; then
      if [ -n "$RESUME_ID" ]; then
        if [ -z "$ID_RESUME_HOME" ]; then
          echo "⛔ 定不下会话 ${RESUME_ID:0:8}… 该在哪个 CODEX_HOME 里恢复(${ID_RESUME_HOME_ERROR:-unknown})；保持原窗口。" >&2
          echo "   确认后用 --codex-home <目录> 显式指定。" >&2
          exit 1
        fi
        resume_home="$ID_RESUME_HOME"
      else
        resume_home="$ID_CODEX_HOME"
      fi
      if [ -n "$resume_home" ] && ! same_path "$resume_home" "$DEFAULT_CODEX_HOME"; then
        CODEX_HOME_OVERRIDE="$resume_home"
        echo "↩️ 沿用原有隔离 CODEX_HOME: $CODEX_HOME_OVERRIDE"
      elif [ -n "$resume_home" ]; then
        SOURCE_CODEX_HOME="$resume_home"
      fi
    fi
    # 撞车检查要在 kill 之前做完:窗口都杀了才发现不能起,等于白白丢一个窗口。
    prog="$(prog_cmd "$kind" "$RESUME_DEFAULT" "$CWD")" || exit 1
    cards_checkpoint || exit 1
    currentidentity="$(tmux display-message -p -t "$oldpane" '#{pid}:#{pane_pid}:#{window_id}:#{pane_current_path}')" || exit 1
    [ "$currentidentity" = "$oldidentity" ] || { echo "⛔ 窗口身份变化，取消重启。" >&2; exit 1; }
    tmux kill-window -t "$oldpane" || exit 1
    sleep 1
    spawn_window "$oldname" "$prog" "$CWD" "$oldidx" || exit 1
    # 卡片顺序按 pane id 记；新 pane 接替旧 pane 的位置，不掉到网格末尾。
    newpane="$(tmux display-message -p -t "$SESSION:$oldidx" '#{pane_id}' 2>/dev/null || true)"
    if [ -n "$newpane" ] && [ "${AGENT_BUS_CARDS_CHECKPOINT:-1}" = "1" ]; then
      "$BUS_CLI" cards order-replace "$oldpane" "$newpane" >/dev/null 2>&1 \
        || echo "⚠️ 卡片顺序没能转到新 pane（不影响窗口本身）: agent-bus cards order-replace $oldpane $newpane" >&2
    fi
    cards_reconcile_after_restart || exit 1
    echo "✅ 重启窗口 [$oldname] 起 ${prog:-bash} @ ${CWD:-继承}"
    ;;
  close)
    [ $# -eq 0 ] && { echo "用法: close <win|name> [更多...]  或  close-all"; exit 1; }
    for win in "$@"; do
      [ -z "$win" ] && continue
      tmux kill-window -t "$SESSION:$win" 2>/dev/null && echo "✅ 关了 [$win]" || echo "⚠️ 没找到 [$win]"
    done
    ;;
  close-all)
    keep=""
    [ "${1:-}" = "--keep" ] && keep="${2:-}"
    tmux list-windows -t "$SESSION" -F "#{window_index}|#{window_name}" 2>/dev/null | while IFS='|' read -r idx wname; do
      if [ -n "$keep" ] && { [ "$idx" = "$keep" ] || printf '%s' "$wname" | grep -qi "$keep"; }; then
        echo "  保留 win$idx [$wname]"; continue
      fi
      tmux kill-window -t "$SESSION:$idx" 2>/dev/null && echo "  ✅ 关 win$idx [$wname]"
    done
    ;;
  list)
    tmux list-windows -t "$SESSION" \
      -F "  win#{window_index} [#{window_name}] = #{pane_current_command} @ #{pane_current_path}" 2>/dev/null
    ;;
  *)
    echo "用法: agent_window.sh open|new|restart|close|close-all|list ...  (见脚本头注释)"
    exit 1
    ;;
esac
