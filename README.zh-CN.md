<div align="center">

# 🚌 Agent Bus

**监管 tmux 里已经在运行的多个 Claude Code 与 Codex CLI 会话——派活、纠偏、验收、恢复——以各提供方自己输出的结构化信号为依据；另附一个手机可用的网页面板（AI Session Cards，卡片网站）。**

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg?logo=python&logoColor=white)](#-快速开始)
[![Platform: Linux](https://img.shields.io/badge/platform-Linux-lightgrey.svg?logo=linux&logoColor=white)](#-快速开始)
[![tmux 3.2+](https://img.shields.io/badge/tmux-3.2%2B-1BB91F.svg?logo=tmux&logoColor=white)](#-快速开始)
[![Tests: passing](https://img.shields.io/badge/tests-passing-brightgreen.svg)](#-测试)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-supported-D97757.svg)](#以提供方自己的信号判状态)
[![Codex CLI](https://img.shields.io/badge/Codex%20CLI-supported-412991.svg)](#以提供方自己的信号判状态)

[English](README.md) · [文档（英文）](docs/) · [卡片网站](docs/dashboard.md) · [安全模型](docs/safety-model.md)

</div>

---

## 📖 目录

- [✨ 特性](#-特性)
- [🖼️ 截图](#️-截图)
- [🚀 快速开始](#-快速开始)
- [🧭 架构](#-架构)
- [🗂️ 卡片网站功能一览](#️-卡片网站功能一览)
- [🔌 可选集成：计划与归档](#-可选集成计划与归档)
- [🛡️ 安全模型](#️-安全模型)
- [⚙️ 配置](#️-配置)
- [🧪 测试](#-测试)
- [⚖️ 与同类项目的比较](#️-与同类项目的比较)
- [⚠️ 局限与已知问题](#️-局限与已知问题)
- [📁 目录结构](#-目录结构)
- [🗺️ 路线图](#️-路线图)
- [🤝 贡献](#-贡献)
- [📄 许可](#-许可)

## ✨ 特性

在一台机器上同时开十几、几十个交互式 Claude Code / Codex 会话时，难点不在"怎么往窗口里打字"，而在：

- 知道哪个窗口真的在忙、空闲，还是卡在对话框上，而不是看 spinner 文字猜；
- 给一个会话派活，之后能**证明**它做完了，而不只是它自己说做完了；
- 窗口改了编号、进程重启过、突然弹出权限提示时，绝不把字打进错误的窗口；
- 重启或 tmux server 崩溃之后，把每个会话找回来——精确到那一条对话、正确的工作目录和 `CODEX_HOME`。

Agent Bus 是一组只用 Python 标准库的小工具加一个网页面板，在单台 Linux 机器上解决这些问题。它通过 tmux 驱动官方交互式 CLI，不替代它们，不代理它们的 API，也不碰它们的凭证。

### 负责人 / 员工监管

- 任意 Claude 或 Codex 窗口都可以当**负责人**（leader），认领一组**员工**窗口（`leader create`），派发可审计的任务尝试（`leader assign`），发送有边界的纠偏（`leader steer`），并基于事件等待而不是轮询屏幕（`leader watch`，或后台 `leaderd` 循环：有相关变化时向负责人窗口发一条精简的 `LEADER_EVENT`）。
- 员工说"完成"只是声明。只有**每个**员工最新一次尝试都已用 `leader verify --evidence ...` 独立验收、或已 `leader abandon`（窗口已不在）、或带原因取消，`leader close --status completed` 才会通过。
- 进度截止（`--progress-deadline`，默认 1800 秒）与硬截止（`--hard-deadline`，默认 7200 秒）各只发一次 `leader_progress_stalled` / `leader_deadline_exceeded` 事件，只提醒，不自动杀任何东西。
- 同一员工同时只能被一个活跃负责人认领；第二个负责人必须显式 `--takeover`，并留下记录。

### 可靠投递

- 所有输入走同一条路径：退出 copy mode → bracketed paste → 等粘贴落地 → 提交。只有观察到提供方进入忙碌或产生新回复，派活才报告 `verified=true`。
- 登记目标时冻结 tmux **pane id 与进程启动时间**。窗口里换成了别的进程，发送就 fail closed，而不是打到顶替它的东西上。
- 长任务或多行任务（`--task-file`、`--text-file`）自动写成任务文件，只粘贴一行指针；指针里的 SHA-256 前 16 位与接收方对文件跑 `sha256sum` 的结果一致。
- 提供方报 `needs_input`（权限提示或其他对话框开着）的窗口一律不打字：此时粘贴再回车会确认当前高亮的那个选项。
- 窗口里已经没有 Claude/Codex 进程时拒绝回车（AI 退出后 shell 接管，任务文字会被当成命令执行）。本来就是 shell 的窗口登记时加 `--shell`，单次发送可加 `--allow-shell`。
- 回答对话框时（自动审批、`leader approve`、网站上点选无编号对话框、开机脚本）持有每个窗口一把锁，两个对话框驱动方不会在同一个窗口里交错按键。普通文字发送不走这把锁，`leader approve --key`（你指定的原始按键）也不走。
- `steer` 带幂等键（`--action-key`），负责人重启后不会重复发送已经发过的纠偏。

### 以提供方自己的信号判状态

- **Claude Code**：`claude agents --json`（会话号、`busy` / `idle` / `waiting`），只有报告的 PID 在该窗口进程树里才绑定；会话 transcript JSONL 提供历史与轮次是否结束；Claude 子智能体日志提供子智能体进度。
- **Codex**：进程真正打开着的 rollout JSONL（读 `/proc/<pid>/fd`），以及通过 `parent_thread_id` 关联的子智能体 rollout。
- 屏幕只作兜底，并且遵守一条硬规则：提供方的输入框还在屏幕上，就不可能有对话框（真正的对话框会取代输入框）。
- 证明不了精确身份时，输出明确标为 `inferred`；绝不把"这个目录里最新的文件"当成某个会话的历史。

### 对话框处理（需主动开启）

- 开启后，权限类对话框——工具权限、目录信任、hooks 审阅、"设为 auto 模式"邀请、plan 模式的"执行这个计划？"/"Implement this plan?"确认——自动选择最宽松的选项。
- 与工作内容有关的提问（Claude `AskUserQuestion`、Codex `request_user_input`）、账号 / API key / 计费类选择、以及任何认不出的对话框，**永不**自动作答。工作提问通过只有提问框才会画出的行和页脚识别，交给负责人或留在面板上给人处理。
- 不盲按回车：只认真正的对话框（取代了输入框、带高亮行和按键提示）；高亮一格一格地移动，每按一次都重新读屏；只有高亮行正是选中的选项时才回车。对话框中途变化或消失就不再发送任何按键；回车后必须看到对话框关闭才记为"已答"。
- 处于 tmux 复制模式的窗口、最近 20 秒有人用过（网站或直接连着的终端）的窗口一律避让；别的发键方正在操作的窗口本轮跳过。
- 每次自动作答都写入事件账本（`worker_prompt_auto_answered`、`pane_prompt_auto_answered`）。
- **默认关闭**，见 [安全模型](#️-安全模型)。

### 统一事件账本

- 所有任务（tmux 派活、负责人尝试、Codex app-server 运行）都记在 `$AGENT_BUS_DIR/event-ledger/` 下同一个只追加账本里，每个任务一个精简 JSON。
- 终态不可复活：发给终态任务的活跃状态只会被记为 ignored，不会把它重新打开。
- `event-reap` 关闭窗口或进程已可证明消失的僵尸任务（记为 `interrupted`，绝不记 `completed`）；`event-prune` 把旧记录移到带恢复说明的 `_legacy/`，不删除。两者不加 `--yes` 都只预演。

### 崩溃恢复

- 定时快照记录每个窗口的窗口号、cwd、提供方、精确会话号，Codex 还记录该会话所在的 `CODEX_HOME`，以及面板的分类、收藏、别名。看起来像事故的快照（窗口数或会话号骤减）作为证据保存，绝不覆盖最后一份好快照。
- `recovery plan` 只读展示计划；`recovery restore` 按原窗口号重建消失的窗口；`recovery relaunch` 在掉回 shell 的窗口里就地拉起 AI（保留面板元数据）；`recovery verify` 独立核对每个窗口跑的是否是预期会话、是否在预期的 `CODEX_HOME`。`recovery auto` 基于同一份固定快照串起以上步骤（不加 `--yes` 只预演）。
- `agent_window.sh restart` 重启窗口后卡片保持原位（`agent-bus cards order-replace 旧 新` 把排序位置转给新 pane）；显式 `--resume-id <ID>` 时，即使旧窗口里的 AI 已退出也能按该会话号拉起。
- 可选的开机脚本只在高位窗口号（默认 999）建一个空 shell 占位，不起 AI，恢复出来的窗口因此拿回原来的编号。恢复后它会回答自己恢复的窗口上的启动对话框：目录信任与"设为 auto 模式"邀请始终处理，Codex 两种启动提示按编号逐格选择，其它权限类对话框只有自动审批开关打开时才处理；每次作答记为 `boot_prompt_auto_answered` 事件。

### Codex 一次性员工

- `agent-bus codex start --wait` 通过 `codex app-server` 运行一个无界面 Codex 员工，流式跟到结束，并记入账本（`status`、`wait`、`watch`、`doctor`、`steer`、`interrupt`、`diff`）。
- 每个并发员工从槽位池（`~/.codex-homes/app-worker-1..N`）取一个独立的 `CODEX_HOME`。登录（`auth.json`）软链共享；多个 Codex 进程会抢锁的 SQLite 状态不共享。

### 卡片网站

单页网页（`dashboard/`，标准库 HTTP 服务），每个 tmux 窗口一张卡片：实时对话、状态、Git 情况、卡片内终端、可选的任务计划面板，以及手机布局。见下方 [功能一览](#️-卡片网站功能一览) 与 [docs/dashboard.md](docs/dashboard.md)。

## 🖼️ 截图

所有截图**只含合成内容**——私有 tmux server、虚构项目和编造的对话，由 [`tests/e2e/readme_screenshots.py`](tests/e2e/readme_screenshots.py) 生成。

| 卡片与对话（桌面） | 任务计划面板（可选集成） |
|---|---|
| ![卡片网格、Git 行与选中窗口的 Claude 对话](docs/images/cards-desktop.png) | ![带进度条、大纲树和阶段表的计划面板](docs/images/plan-panel.png) |
| **卡片内终端视图** | **手机布局与 ⌁ 菜单** |
| ![在卡片里以 ANSI 配色显示窗口的真实终端](docs/images/terminal-view.png) | ![手机对话视图，圆形菜单已展开](docs/images/mobile-cards.png) |

## 🚀 快速开始

依赖：

- Linux（用到 `/proc`、`fcntl`、tmux）
- tmux 3.2 或更新（开发与测试用的是 tmux 3.7）
- Python 3.10+（只用标准库；测试需要 `pytest` 和 Node.js，浏览器测试可选 Playwright + Chromium）
- 已登录的 Claude Code 和/或 Codex CLI

```bash
git clone https://github.com/YonganZhang/agent-bus.git ~/src/agent-bus
export PATH="$HOME/src/agent-bus/bin:$PATH"     # 提供 agent-bus（及别名 secretary-bus）
agent-bus --help
```

不做任何系统级安装。运行状态写到 `$AGENT_BUS_DIR`（默认 `~/.codex/agent-bus`），见 [配置](#️-配置)。

先开一个 tmux 会话，里面有一个 Claude Code（或 Codex）窗口，然后：

```bash
# 1. 用稳定名字登记窗口（冻结 pane id + 进程启动时间）
agent-bus register --name worker-a --pane secretary_web:1.0 --expected-command claude

# 2. 看提供方自己怎么说
agent-bus provider-state worker-a --pretty

# 3. 派一个可审计的任务（不加 --yes 只预演）
agent-bus start --target worker-a --repo ~/src/my-project \
  --task "Fix the failing test in tests/test_parser.py. End with COMPLETION_STATUS: COMPLETE." --yes

# 4. 跟踪该任务的事件，直到终态或空闲
agent-bus event-watch --job-id <job-id> --until-terminal --timeout 3600

# 5. 收回回复与 diff 作为证据，再自己核对
agent-bus collect <job-id>
```

带负责人、多个员工和显式验收闸门的流程：

```bash
agent-bus register --name lead --pane secretary_web:0.0 --expected-command claude
agent-bus leader create --leader lead --worker worker-a --worker worker-b \
  --objective "Ship and verify the parser fix" --json
agent-bus leader assign <leader-id> --worker worker-a --repo ~/src/my-project --task-file task.md --yes
agent-bus leader watch <leader-id> --timeout 120 --json
agent-bus leader collect <leader-id> --worker worker-a
agent-bus leader verify <leader-id> --worker worker-a --evidence "pytest: 42 passed; diff reviewed"
agent-bus leader close <leader-id> --status completed --evidence "acceptance checks passed"
```

启动卡片网站（默认监听 127.0.0.1:7795）：

```bash
mkdir -p ~/.codex/agent-bus
printf 'WEBTERM_USER=%s\nWEBTERM_PASS=%s\n' me "$(openssl rand -hex 16)" > ~/.codex/agent-bus/webterm.env
chmod 600 ~/.codex/agent-bus/webterm.env
python3 dashboard/server.py            # --help 列出可配置项
# 打开 http://127.0.0.1:7795/cards/
```

（`agent-bus dashboard` 是另一回事：Codex app-server 线程与运行记录的小型 HTML/JSON 视图，不是卡片网站。）

systemd 用户单元示例（网站、tmux 守护、快照定时器、开机自动恢复）在 [contrib/systemd/](contrib/systemd/)；把会话号钉到窗口上的 Claude Code `SessionStart` / `SessionEnd` hook 在 [contrib/claude-hooks/](contrib/claude-hooks/)。

延伸阅读（英文）：[负责人流程](docs/leader-workflow.md)、[恢复](docs/recovery.md)、[卡片网站](docs/dashboard.md)、[计划集成](docs/plan-integration.md)、[安全模型](docs/safety-model.md)。

## 🧭 架构

```
            ┌──────────────── 你 / 负责人 AI ───────────────────┐
            │  bin/agent-bus (CLI)          卡片网站（浏览器）  │
            └──────┬───────────────────────────────┬────────────┘
                   │                               │ HTTP + Basic Auth
   ┌───────────────▼──────────────┐   ┌────────────▼────────────┐
   │ scripts/                     │   │ dashboard/server.py     │
   │  cli_bridge   supervisor     │◄──┤  （复用同一套           │
   │  leader       leader_daemon  │   │    提供方模块）         │
   │  codex_app    window_transition   └──────┬─────────┬──────┘
   │  secretary_recovery          │           │         │ 可选
   │  provider_state  dialogs     │           │         ▼
   │  claude_sessions / *_subagents / pane_detectors  计划 CLI
   └──────┬───────────────┬───────┴───────────┘  (CARDS_TOP_CLI)
          │               │ 只追加的事件 + 任务文件
          │        ┌──────▼──────────────────────────┐
          │        │ $AGENT_BUS_DIR/event-ledger/    │
          │        └─────────────────────────────────┘
          │ tmux（粘贴 / 按键 / 抓屏 / 调尺寸）      只读的提供方证据
   ┌──────▼──────────────────────────┐   ┌──────────────────────────────────┐
   │ 运行 `claude` 与 `codex` 交互式 │──►│ claude agents --json、transcript │
   │ CLI 的 tmux 窗口                │   │ Codex rollout（打开的 fd）、/proc │
   └─────────────────────────────────┘   └──────────────────────────────────┘
```

详见 [docs/architecture.md](docs/architecture.md)。

## 🗂️ 卡片网站功能一览

| 功能 | 说明 | 桌面 | 手机 |
|---|---|:---:|:---:|
| 🃏 卡片网格 / 列表 | 每个窗口一张卡片：提供方、项目、状态（处理中 / 空闲 / 等待 / 需要处理 / 额度受限）、预览、任务状态 | ✅ | ✅ |
| 💬 对话时间线 | Claude transcript / Codex rollout 历史，合并屏幕实时尾部，按需往前翻页；支持 Markdown 表格、嵌套列表、代码 | ✅ | ✅ |
| 🔘 选择题与对话框 | 带编号的选择题和无编号对话框渲染成按钮（逐格核对的驱动） | ✅ | ✅ |
| ⌨️ 输入框 | 发送文字与上传文件；收起输入框只收起（草稿按窗口保存），只有"发送"才发出去 | ✅ | ✅ |
| 🌿 Git 行 | 分支、**待归档 N**（新 / 改）、**未推送 N** 或"无远端"、**上次归档**时间、**▶ 当前任务**（需计划集成）；在请求路径之外计算 | ✅ | ✅（精简） |
| 🧰 标题栏三按钮 | 计划 / 归档 / 终端始终显示；不能用时置灰并说明原因 | ✅ | ✅（在"⋯"/⌁ 菜单里） |
| 🖥️ 终端视图 | 在卡片里显示窗口的真实终端：ANSI 配色、0.3 秒自适应刷新、不抖动、窗口按查看者尺寸调整（Claude 窗口最多 109 列），往上翻先接 scrollback 再接对话记录 | ✅ | ✅ |
| 🔗 卡片 ↔ 终端 | `?pane=%12` 深链；"打开完整终端页"把网页终端切到该窗口（需 `TMUX_CARD_TERMINAL_URL`）；`/api/terminal/status` 核对两边是否同一窗口 | ✅ | ✅ |
| 📋 计划面板（可选） | 计划文件的大纲树与进度、任务笔记、动态 / 分拣 / 原文标签页、白名单增删改 | ✅ 抽屉 | ✅ 全屏 |
| 📦 一键归档（可选） | 把你的归档提示发给空闲的 AI，完成后报告新增提交、剩余待归档和计划是否更新 | ✅ | ✅ |
| 🤖 子智能体与工作流 | Claude 子智能体、Codex 子智能体线程、多智能体工作流的进度 | ✅ | ✅ |
| 🔎 轨迹视图 | 单个会话的工具调用与子智能体树和耗时，已脱敏 | ✅ | ✅ |
| 🗃️ 组织 | 分类、收藏、别名、排序，存在服务端，多设备同步 | ✅ | ✅ |
| 📎 文件 | 共享上传区；回复里的本地产物路径变成带鉴权的下载 / 预览链接 | ✅ | ✅ |
| ⌁ 悬浮菜单 | 可拖动的圆形按钮，展开 计划 / 终端 / ESC；展开前后圆心不动，旋转屏幕后夹回可视区 | ✅ | ✅ |

界面文字为中文。所有写接口要求 JSON 并拒绝跨站请求；接口说明见 [docs/dashboard.md](docs/dashboard.md)。

## 🔌 可选集成：计划与归档

计划面板、卡片 Git 行上的任务标题、分拣和一键归档由一个**外部计划 CLI** 驱动，它只需遵守一份小的 JSON 约定（`plan show` / `plan list` / `plan add|edit|note|start|block|cancel|reopen` / `track`）。作者自己用的是 share-top 风格的计划 CLI；任何遵守这份约定的工具都可以。卡片网站自己从不解析或改写计划文件，只按窗口的实时 cwd 找项目，不经 shell、带超时调用 CLI，写操作只允许这七种。

```bash
CARDS_TOP_CLI=/path/to/plan-cli.py \
CARDS_ARCHIVE_PROMPT=/path/to/archive-prompt.md \
python3 dashboard/server.py
```

不配这两个变量时其余功能照常；"计划""归档"按钮仍显示但置灰（"需要配置 share-top 集成……"），`/api/plan*` 与 `/api/archive-request` 返回 `501`。约定细节、计划文件格式和笔记格式见 [docs/plan-integration.md](docs/plan-integration.md)；[`tests/dashboard/fixtures/fake_plan_cli.py`](tests/dashboard/fixtures/fake_plan_cli.py) 是一个最小参考实现。

## 🛡️ 安全模型

- **网站权限很大。** 能登录的人就能往你的 AI 会话里打字、发原始按键、回答对话框、调整和关闭窗口，而它只有 HTTP Basic Auth。请保持只绑定 `127.0.0.1`（默认），需要远程或手机访问时，放到带 TLS 和额外鉴权的反向代理后面。
- **自动审批需主动开启。** 除非你执行 `agent-bus leader config --auto-approve-permissions true`、设置 `AGENT_BUS_AUTO_APPROVE=1`，或以 `CARDS_AUTO_APPROVE=1` 启动网站，否则它是关闭的。开启后也只处理权限 / 信任 / hooks 审阅 / plan 执行确认几类对话框——永不回答工作内容相关的提问，也永不替你做账号或计费选择——每次作答都有记录。作答时持有该窗口的对话框锁，避开复制模式和最近 20 秒有人在用的窗口，回车后看到对话框关闭才记为已答。开启它意味着你接受这些对话框本来要问你的一切。
- **开机自动恢复是否作答，取决于你装不装它。** 装了 `contrib/boot/auto-restore` 后，即使自动审批关闭，它也会在自己恢复的窗口上回答目录信任与"设为 auto 模式"邀请（以及 Codex 两种启动提示）；其它权限类对话框要等开关打开。每次作答记为 `boot_prompt_auto_answered`。
- **trusted-owner 模式默认关闭。** `agent-bus leader config --trusted-owner true` 之后，负责人动作不需要 `--yes` 就会执行，并允许多行文本；需要预演时用 `--dry-run`。除非负责人窗口完全归你自己控制，否则不要打开。
- **不往裸 shell 里回车。** 窗口里的 AI 已退出时拒绝回车，除非目标登记时加了 `--shell` 或本次发送加了 `--allow-shell`。
- **身份校验 fail closed。** CLI 与负责人的发送、按键、重启、关闭前都会重新核对冻结的 pane id、进程启动时间和命令。（网站输入是人在打字：只有输入框发送（`/api/send`，请求里带窗口 pid 和启动时间，页面会带上）核对窗口进程仍是页面上显示的那个；`/api/key` 与 `/api/choose` 不核对。三者都不做 `needs_input` 或 AI 退出检查。）
- **CSRF 与跨站探测。** 写请求必须是 `application/json`；浏览器标记为跨站的请求一律拒绝，`/api/terminal/*` 与 `/api/plan/track` 的跨站读取也拒绝。
- **可选的计划 CLI 以你的身份运行。** 只配置你信任的 CLI；卡片网站传给它的是参数列表（绝不是 shell 字符串）和校验过的字段。
- **提供方条款。** Agent Bus 像你一样通过终端驱动官方交互式 CLI，不抽取、不保存、不转发凭证，自身也不调用提供方 API。请在各自的服务条款范围内使用 Claude Code 和 Codex。
- 不要把密码、token、API key 写进任务文本：任务预览会存进账本。

它不会做的事：不替你回答工作问题；不用 `--continue` / `resume --last` 接"最近一条"对话；负责人的控制动作（按键、打断、重启、关闭）只作用于它认领的窗口，此外窗口只会被你显式执行的命令关闭或重建（`agent_window.sh restart|close`、`recovery ... --yes`、网站上的关闭按钮）；没有记录在案的证据就不会把负责人会话关为完成。详见 [docs/safety-model.md](docs/safety-model.md)。

## ⚙️ 配置

所有路径的默认值都兼容已有的 `~/.codex` 布局。最常用的变量：

| 变量 | 默认值 | 用途 |
|---|---|---|
| `AGENT_BUS_DIR` | `~/.codex/agent-bus` | 总线状态：目标登记、事件账本、负责人会话、任务文件、Codex 运行记录 |
| `AGENT_BUS_DASHBOARD_STATE_DIR` | `$AGENT_BUS_DIR/card-dashboard` | 网站偏好、上传文件、transcript 映射 |
| `AGENT_BUS_SNAPSHOT_DIR` | `$AGENT_BUS_DEFAULT_CODEX_HOME/tmux-snapshots` | 恢复快照 |
| `AGENT_BUS_DEFAULT_CODEX_HOME` | `~/.codex` | 共享/默认 Codex home（从不取自 `CODEX_HOME`） |
| `AGENT_BUS_CODEX_HOMES_ROOT` | `~/.codex-homes` | 各窗口 / 各员工隔离 Codex home 的父目录 |
| `AGENT_BUS_AUTO_APPROVE` | 未设置 | `1`/`0` 强制开/关自动审批（覆盖配置开关） |
| `CARDS_AUTO_APPROVE` | 未设置 | `1` 启动网站后台自动审批循环；`0` 否决 |
| `AGENT_BUS_CLAUDE_PERMISSION_MODE` | 未设置 | `agent_window.sh` 开 Claude 窗口时附加的 `--permission-mode` |
| `SECRETARY_TMUX_SESSION` | `secretary_web` | 恢复与窗口工具使用的 tmux 会话 |
| `TMUX_CARD_HOST` / `TMUX_CARD_PORT` | `127.0.0.1` / `7795` | 网站监听地址 |
| `WEBTERM_ENV` | `$AGENT_BUS_DIR/webterm.env` | 网站 Basic Auth 文件（`WEBTERM_USER`、`WEBTERM_PASS`） |
| `TMUX_CARD_TERMINAL_URL` | 未设置 | "打开完整终端页"使用的网页终端地址 |
| `CARDS_TOP_CLI` / `CARDS_ARCHIVE_PROMPT` | 未设置 | 可选的计划 / 归档集成 |
| `PYTHON` | `python3` | `bin/agent-bus` 使用的解释器 |

完整列表（超时、缓存、leaderd 调参等）见 [docs/configuration.md](docs/configuration.md)。部分变量出于兼容保留历史前缀 `SECRETARY_` / `TMUX_CARD_`。

## 🧪 测试

```bash
python3 -m pytest -q
```

最近一次全量：**950 passed, 1 skipped, 1 xfailed**（Linux、Python 3.10、tmux 3.7、Node.js 22、Playwright Chromium；未安装任何计划 CLI）。目前还没有 CI，上面的测试徽章是静态的。

- 测试会在临时目录里起自己的 tmux server（设置 `TMUX_TMPDIR` 并去掉 `$TMUX`），并使用临时总线目录和临时 `HOME`，因此不会碰你真实的 tmux 会话、`~/.codex/agent-bus` 或 `~/.claude`。终端视图相关测试用私有 socket 驱动**真的** tmux。
- 前端测试用 Node.js 运行页面里的 JavaScript（需要安装 Node.js）。浏览器测试用 Playwright + Chromium，缺少任一时自动跳过（`python3 -m playwright install chromium`）。
- 计划面板测试使用合成的 [`fake_plan_cli.py`](tests/dashboard/fixtures/fake_plan_cli.py)，不需要任何外部计划工具。
- 需要 `tmux` 或 `codex` 二进制（一个 app-server schema 检查）的测试在缺少时自动跳过。
- `tests/e2e/*.py` 是手动 Playwright 脚本（针对运行中的网站，或自带环境的截图生成器），pytest 不收集。

## ⚖️ 与同类项目的比较

这个领域有不少好项目，取舍各不相同：

- [ntm](https://github.com/Dicklesworthstone/ntm)：多智能体的 tmux 控制面，能接管已有窗口，有派活账本和窗口身份；状态主要来自屏幕内容与输出速率。
- [agent-of-empires](https://github.com/agent-of-empires/agent-of-empires)：有 TUI、网页与手机端的会话管理器，用 hooks 与 ACP 判断状态，提供容器沙箱；已有会话是导入，而不是就地接管。
- [claude-squad](https://github.com/smtg-ai/claude-squad)：tmux 加每个智能体一个 git worktree；状态看屏幕。
- [Gas Town](https://github.com/gastownhall/gastown)：Mayor/Polecat 派活模型，配 Beads 账本与验证闸门。
- [Happy](https://github.com/slopus/happy)：Claude Code 与 Codex 的端到端加密手机 / 网页中继；会话需通过它的 wrapper 启动。
- 官方也在覆盖同类需求：Claude Code 的 agent view 与 agent teams，Codex 的原生子智能体。

Agent Bus 与它们互补：它监管许多独立的、已经在运行的交互式会话，Claude Code 与 Codex 混用，并以提供方自己的结构化信号做判断（屏幕只兜底）。如果你需要沙箱、每个智能体一个 worktree 的隔离，或不想自己配反向代理就能远程访问，上面某个项目可能更合适。

## ⚠️ 局限与已知问题

- 它读取 Claude Code 与 Codex CLI 的屏幕、日志和 JSONL 格式，这些都不是稳定的公开 API，会随上游版本变化。它依赖 `claude agents --json`（研究预览特性）和 Codex rollout 格式；上游变化时，状态判断会降级到屏幕兜底，对话框识别也可能需要跟进更新。
- 单机、单用户、单个 tmux server，没有多机协调。
- Codex `app-server` 协议仍在变化，`codex start` 可能需要随之更新。
- 仅支持 Linux（`/proc`、`fcntl`）。
- 网站界面、大量代码注释和部分脚本输出（例如 `agent_window.sh`、恢复相关提示）是中文；README、文档和大部分 CLI 帮助为英文。
- 对话框自动审批基于模式识别。新出现的对话框形态会被当成"不是权限对话框"留给人处理——这是安全的，但可能需要更新规则。
- 卡片内终端视图会把窗口的 tmux 尺寸调成查看者的尺寸（30 秒内有真实终端客户端在用该窗口时不调）；打开完整终端页时会把尺寸交还给真实客户端。Claude Code 全屏界面在 ≥110 列时会开代码改动侧栏，所以 Claude 窗口在卡片终端视图里最多 109 列。

## 📁 目录结构

```
bin/agent-bus            CLI 分发（bin/secretary-bus 为别名）
bin/ai-session-shell     启动 wrapper：AI 退出后保留窗口并打印恢复命令
scripts/                 总线模块（标准库 Python）与 shell 辅助脚本
  cards_control.py       `agent-bus cards ...`（收藏、分类、别名、排序）
  agent_window.sh        开 / 新建 / 重启 / 关闭窗口，必须写明会话选择
dashboard/               卡片网站（server.py、index.html、轨迹视图）
contrib/boot/            ensure-tmux-session、auto-restore
contrib/systemd/         systemd 用户单元示例
contrib/claude-hooks/    tmux-session-stamp.sh（Claude Code hook）
docs/                    架构、安全模型、负责人流程、恢复、网站、计划集成、配置（英文）；images/ 为 README 截图
tests/                   总线测试、tests/dashboard/（含 fixtures/fake_plan_cli.py）、
                         tests/e2e/（手动浏览器脚本、截图生成器）
```

## 🗺️ 路线图

只是计划，不是承诺：

- [ ] GitHub Actions CI（在此之前测试徽章是静态的）。
- [ ] 网站的英文界面选项（目前界面文字是中文）。
- [ ] 基于已公开的计划 CLI 约定，为其它计划工具写适配器。
- [ ] 更简单的安装方式（pipx 或一键安装脚本），代替 clone + `PATH`。
- [ ] 跟进上游 `claude agents --json`、transcript 与 Codex rollout 的格式变化。

## 🤝 贡献

欢迎提 issue 和 pull request。

- 提交改动前跑 `python3 -m pytest -q`（改到的文件再跑 `python3 -m py_compile`），并补一个没有这次改动就会失败的测试。
- 运行时保持只用标准库；测试工具（pytest、Node.js、Playwright）可以用。
- 测试夹具、截图和示例必须是**合成内容**：不放真实会话号、对话记录、项目名、路径或凭证。README 截图用 `python3 tests/e2e/readme_screenshots.py` 重新生成。
- 安全问题请先私下报告（开 issue 请求联系方式，不要附细节）。

## 📄 许可

[MIT](LICENSE) © 2026 Yongan Zhang
