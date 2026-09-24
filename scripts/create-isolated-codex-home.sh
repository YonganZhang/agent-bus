#!/usr/bin/env bash
# 幂等地建好一个隔离 CODEX_HOME 目录: 独立 state/logs/sessions sqlite,
# 但共用默认 home 的登录态、rules、skills(软链,不拷贝)。
#
# 根因: 多个 codex 进程共享同一个 CODEX_HOME 时会在 state/logs sqlite 上产生锁竞争,
# 严重时新会话卡死在 "model: loading" 且 Ctrl+C 都杀不掉,只能 kill -9。
# 给每个窗口一个独立 CODEX_HOME 从根上避开这个锁竞争。
#
# 用法: create-isolated-codex-home.sh <slug> [project_dir]
#   slug 只用作 $AGENT_BUS_CODEX_HOMES_ROOT/<slug>/ 的目录名(默认 ~/.codex-homes/<slug>/),
#   调用方(agent_window.sh / codex_app.py)负责生成一个不冲突的 slug。
#   project_dir 可选:传入时会把这个目录预先标记为 trusted。新隔离 home 的 config.toml
#   不带共享 home 里已记录的项目信任状态,不预置的话每个隔离 home 第一次启动都会弹
#   "Do you trust the contents of this directory"确认框。
#
# config.toml 的来源:
#   - 设置了 AGENT_BUS_CODEX_HOME_INSTALLER(可执行文件)时,以 CODEX_HOME=<新 home> 调用它,
#     由它生成 config.toml / AGENTS.md 等(适合自己有一套 Codex 配置生成脚本的部署);
#   - 否则新 home 里还没有 config.toml 时,从默认 home 复制一份;默认 home 也没有就建空文件。
#     AGENTS.md 若默认 home 里有,则软链过去。
#
# 幂等: 已存在的软链会被 -f 刷新指向,不会重复创建或报错;已跑过一次的 home
# 再跑一次没有副作用,可以用来补齐历史上手工建的、漏做软链的 home。
set -euo pipefail

DEFAULT_HOME="${AGENT_BUS_DEFAULT_CODEX_HOME:-$HOME/.codex}"
HOMES_ROOT="${AGENT_BUS_CODEX_HOMES_ROOT:-$HOME/.codex-homes}"
INSTALLER="${AGENT_BUS_CODEX_HOME_INSTALLER:-}"

slug="${1:-}"
project_dir="${2:-}"
[ -n "$slug" ] || { echo "用法: create-isolated-codex-home.sh <slug> [project_dir]" >&2; exit 1; }
case "$slug" in
  */*|.*) echo "⛔ slug 不能含 / 或以 . 开头: $slug" >&2; exit 1 ;;
esac

home="$HOMES_ROOT/$slug"
mkdir -p "$home"

# auth.json 必须软链、不能拷贝: 拷贝的登录凭证会在下次刷新后失效,
# 软链才能一直跟着默认 home 的登录状态走。rules/skills 同理。
ln -sfn "$DEFAULT_HOME/auth.json" "$home/auth.json"
ln -sfn "$DEFAULT_HOME/rules" "$home/rules"
ln -sfn "$DEFAULT_HOME/skills" "$home/skills"

if [ -n "$INSTALLER" ]; then
  [ -x "$INSTALLER" ] || { echo "⛔ AGENT_BUS_CODEX_HOME_INSTALLER is not executable: $INSTALLER" >&2; exit 1; }
  CODEX_HOME="$home" "$INSTALLER" >/dev/null
else
  if [ ! -e "$home/config.toml" ]; then
    if [ -f "$DEFAULT_HOME/config.toml" ]; then
      cp "$DEFAULT_HOME/config.toml" "$home/config.toml"
    else
      : > "$home/config.toml"
    fi
  fi
  if [ -e "$DEFAULT_HOME/AGENTS.md" ] && [ ! -e "$home/AGENTS.md" ]; then
    ln -s "$DEFAULT_HOME/AGENTS.md" "$home/AGENTS.md"
  fi
fi

# Update exactly one trusted-project table, safely quoting unusual paths and
# keeping reruns idempotent (duplicate TOML tables are invalid, not overrides).
if [ -n "$project_dir" ]; then
  python3 - "$home/config.toml" "$project_dir" <<'PYCODE'
import json, re, sys
from pathlib import Path
path = Path(sys.argv[1])
header = '[projects.' + json.dumps(str(Path(sys.argv[2]).resolve()), ensure_ascii=False) + ']'
lines = path.read_text().splitlines()
result = []
in_project = False
seen = False
duplicate = False
for line in lines:
    if line.strip().startswith('['):
        in_project = line.strip() == header
        duplicate = in_project and seen
        if in_project and not seen:
            result.extend([line, 'trust_level = "trusted"'])
            seen = True
            continue
        if duplicate:
            continue
    if in_project and re.match(r'^\s*trust_level\s*=', line):
        continue
    if duplicate and line.strip() and not line.lstrip().startswith('#'):
        raise SystemExit('cannot merge duplicate project table with extra settings')
    if not duplicate:
        result.append(line)
if not seen:
    result += ['', header, 'trust_level = "trusted"']
tmp = path.with_suffix('.toml.tmp')
tmp.write_text('\n'.join(result) + '\n')
tmp.replace(path)
PYCODE
fi

echo "$home"
