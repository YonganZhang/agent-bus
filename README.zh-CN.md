# Agent Bus

一个驾驭 tmux 里**已经在运行**的多个 Claude Code 与 Codex CLI 会话的监管工具：派活、纠偏、验收、恢复——判断依据是各提供方自己输出的结构化信号，而不是抓屏猜测。

[English](README.md)

## 为什么需要

在一台机器上同时开十几、几十个交互式 Claude Code / Codex 会话时，难点不在"怎么往窗口里打字"，而在：

- 知道哪个窗口真的在忙、空闲，还是卡在对话框上，而不是看 spinner 文字猜；
- 给一个会话派活，之后能**证明**它做完了，而不只是它自己说做完了；
- 窗口改了编号、进程重启过、突然弹出权限提示时，绝不把字打进错误的窗口；
- 重启或 tmux server 崩溃之后，把每个会话找回来——精确到那一条对话、正确的工作目录和 `CODEX_HOME`。

Agent Bus 是一组只用 Python 标准库的小工具加一个网页面板，在单台 Linux 机器上解决这些问题。它通过 tmux 驱动官方交互式 CLI，不替代它们，不代理它们的 API，也不碰它们的凭证。

## 能做什么

### 负责人 / 员工监管

- 任意 Claude 或 Codex 窗口都可以当**负责人**（leader），认领一组**员工**窗口（`leader create`），派发可审计的任务尝试（`leader assign`），发送有边界的纠偏（`leader steer`），并基于事件等待而不是轮询屏幕（`leader watch`，或后台 `leaderd` 循环：有相关变化时向负责人窗口发一条精简的 `LEADER_EVENT`）。
- 员工说"完成"只是声明。只有**每个**员工最新一次尝试都已用 `leader verify --evidence ...` 独立验收、或已 `leader abandon`（窗口已不在）、或带原因取消，`leader close --status completed` 才会通过。
- 进度截止（`--progress-deadline`，默认 1800 秒）与硬截止（`--hard-deadline`，默认 7200 秒）各只发一次 `leader_progress_stalled` / `leader_deadline_exceeded` 事件，只提醒，不自动杀任何东西。
- 同一员工同时只能被一个活跃负责人认领；第二个负责人必须显式 `--takeover`，并留下记录。

### 可靠投递

- 所有输入走同一条路径：退出 copy mode → bracketed paste → 等粘贴落地 → 提交。只有观察到提供方进入忙碌或产生新回复，派活才报告 `verified=true`。
- 登记目标时冻结 tmux **pane id 与进程启动时间**。窗口里换成了别的进程，发送就 fail closed，而不是打到顶替它的东西上。
- 长任务或多行任务（`--task-file`、`--text-file`）自动写成任务文件，只粘贴一行指针。
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
- **默认关闭**，见 [安全与负责任使用](#安全与负责任使用)。

### 统一事件账本

- 所有任务（tmux 派活、负责人尝试、Codex app-server 运行）都记在 `$AGENT_BUS_DIR/event-ledger/` 下同一个只追加账本里，每个任务一个精简 JSON。
- 终态不可复活：发给终态任务的活跃状态只会被记为 ignored，不会把它重新打开。
- `event-reap` 关闭窗口或进程已可证明消失的僵尸任务（记为 `interrupted`，绝不记 `completed`）；`event-prune` 把旧记录移到带恢复说明的 `_legacy/`，不删除。两者不加 `--yes` 都只预演。

### 崩溃恢复

- 定时快照记录每个窗口的窗口号、cwd、提供方、精确会话号，Codex 还记录该会话所在的 `CODEX_HOME`，以及面板的分类、收藏、别名。看起来像事故的快照（窗口数或会话号骤减）作为证据保存，绝不覆盖最后一份好快照。
- `recovery plan` 只读展示计划；`recovery restore` 按原窗口号重建消失的窗口；`recovery relaunch` 在掉回 shell 的窗口里就地拉起 AI（保留面板元数据）；`recovery verify` 独立核对每个窗口跑的是否是预期会话、是否在预期的 `CODEX_HOME`。`recovery auto` 基于同一份固定快照串起以上步骤（不加 `--yes` 只预演）。
- 可选的开机脚本只在高位窗口号（默认 999）建一个空 shell 占位，不起 AI，恢复出来的窗口因此拿回原来的编号。恢复后它会回答自己恢复的窗口上的启动对话框：目录信任与"设为 auto 模式"邀请始终处理，Codex 两种启动提示按编号逐格选择，其它权限类对话框只有自动审批开关打开时才处理；每次作答记为 `boot_prompt_auto_answered` 事件。

### Codex 一次性员工

- `agent-bus codex start --wait` 通过 `codex app-server` 运行一个无界面 Codex 员工，流式跟到结束，并记入账本（`status`、`wait`、`watch`、`doctor`、`steer`、`interrupt`、`diff`）。
- 每个并发员工从槽位池（`~/.codex-homes/app-worker-1..N`）取一个独立的 `CODEX_HOME`。登录（`auth.json`）软链共享；多个 Codex 进程会抢锁的 SQLite 状态不共享。

### 卡片网站

- 单页网页（`dashboard/`），每个 tmux 窗口一张卡片：从 transcript / rollout 解析的实时时间线、忙碌 / 空闲 / 等待状态、子智能体进度面板、工作流进度、可点选的选择题与对话框。
- 分类、收藏、别名；手机可用（移动端布局与输入框）。
- 单个会话的工具调用与子智能体轨迹视图，从本地日志构建并脱敏。
- 所有路由都有 HTTP Basic Auth；未配置凭证时拒绝一切请求。浏览器标记为跨站的写请求、不是 `Content-Type: application/json` 的 JSON 写请求、不带 `X-Cards-Upload` 头的上传一律拒绝（防 CSRF）。
- 网站是给人用的远程终端：输入框发送就是粘贴加回车，不做 CLI 那种 `needs_input` / AI 已退出的拒绝。点选带编号的选项就是发那个数字键；点选无编号对话框的选项走逐格核对的驱动。
- 分类名默认是一组固定中文分类（`开发`、`论文`、`私人`、`待处理`、`其他`）；`最近`、`全部` 是视图。

## 架构

见 [README.md 的架构图](README.md#architecture) 与 [docs/architecture.md](docs/architecture.md)。简述：`bin/agent-bus` 分发到 `scripts/` 下各模块；所有模块读写 `$AGENT_BUS_DIR` 下的持久文件（目标登记、事件账本、负责人会话等）；卡片网站 `dashboard/server.py` 复用同一套提供方模块；证据来源是 `claude agents --json`、Claude transcript、Codex 打开着的 rollout 和 `/proc`，屏幕只兜底。

## 安装

依赖：

- Linux（用到 `/proc`、`fcntl`、tmux）
- tmux 3.x
- Python 3.10+（只用标准库；测试需要 `pytest` 和 Node.js）
- 已登录的 Claude Code 和/或 Codex CLI

```bash
git clone <本仓库> ~/src/agent-bus
export PATH="$HOME/src/agent-bus/bin:$PATH"     # 提供 agent-bus（及别名 secretary-bus）
agent-bus --help
```

不做任何系统级安装。运行状态写到 `$AGENT_BUS_DIR`（默认 `~/.codex/agent-bus`），见 [配置](#配置)。

## 快速开始

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

延伸阅读（英文）：[负责人流程](docs/leader-workflow.md)、[恢复](docs/recovery.md)、[卡片网站](docs/dashboard.md)、[安全模型](docs/safety-model.md)。

## 安全与负责任使用

- **网站权限很大。** 能登录的人就能往你的 AI 会话里打字、发原始按键、回答对话框、关闭窗口，而它只有 HTTP Basic Auth。请保持只绑定 `127.0.0.1`（默认），需要远程或手机访问时，放到带 TLS 和额外鉴权的反向代理后面。
- **自动审批需主动开启。** 除非你执行 `agent-bus leader config --auto-approve-permissions true`、设置 `AGENT_BUS_AUTO_APPROVE=1`，或以 `CARDS_AUTO_APPROVE=1` 启动网站，否则它是关闭的。开启后也只处理权限 / 信任 / hooks 审阅 / plan 执行确认几类对话框——永不回答工作内容相关的提问，也永不替你做账号或计费选择——每次作答都有记录。作答时持有该窗口的对话框锁，避开复制模式和最近 20 秒有人在用的窗口，回车后看到对话框关闭才记为已答。开启它意味着你接受这些对话框本来要问你的一切。
- **开机自动恢复是否作答，取决于你装不装它。** 装了 `contrib/boot/auto-restore` 后，即使自动审批关闭，它也会在自己恢复的窗口上回答目录信任与"设为 auto 模式"邀请（以及 Codex 两种启动提示）；其它权限类对话框要等开关打开。每次作答记为 `boot_prompt_auto_answered`。
- **trusted-owner 模式默认关闭。** `agent-bus leader config --trusted-owner true` 之后，负责人动作不需要 `--yes` 就会执行，并允许多行文本；需要预演时用 `--dry-run`。除非负责人窗口完全归你自己控制，否则不要打开。
- **不往裸 shell 里回车。** 窗口里的 AI 已退出时拒绝回车，除非目标登记时加了 `--shell` 或本次发送加了 `--allow-shell`。
- **身份校验 fail closed。** CLI 与负责人的发送、按键、重启、关闭前都会重新核对冻结的 pane id、进程启动时间和命令。（网站输入是人在打字：只有输入框发送（`/api/send`，请求里带窗口 pid 和启动时间，页面会带上）核对窗口进程仍是页面上显示的那个；`/api/key` 与 `/api/choose` 不核对。三者都不做 `needs_input` 或 AI 退出检查。）
- **提供方条款。** Agent Bus 像你一样通过终端驱动官方交互式 CLI，不抽取、不保存、不转发凭证，自身也不调用提供方 API。请在各自的服务条款范围内使用 Claude Code 和 Codex。
- 不要把密码、token、API key 写进任务文本：任务预览会存进账本。

它不会做的事：不替你回答工作问题；不用 `--continue` / `resume --last` 接"最近一条"对话；负责人的控制动作（按键、打断、重启、关闭）只作用于它认领的窗口，此外窗口只会被你显式执行的命令关闭或重建（`agent_window.sh restart|close`、`recovery ... --yes`、网站上的关闭按钮）；没有记录在案的证据就不会把负责人会话关为完成。详见 [docs/safety-model.md](docs/safety-model.md)。

## 与同类项目的比较

这个领域有不少好项目，取舍各不相同：

- [ntm](https://github.com/Dicklesworthstone/ntm)：多智能体的 tmux 控制面，能接管已有窗口，有派活账本和窗口身份；状态主要来自屏幕内容与输出速率。
- [agent-of-empires](https://github.com/agent-of-empires/agent-of-empires)：有 TUI、网页与手机端的会话管理器，用 hooks 与 ACP 判断状态，提供容器沙箱；已有会话是导入，而不是就地接管。
- [claude-squad](https://github.com/smtg-ai/claude-squad)：tmux 加每个智能体一个 git worktree；状态看屏幕。
- [Gas Town](https://github.com/gastownhall/gastown)：Mayor/Polecat 派活模型，配 Beads 账本与验证闸门。
- [Happy](https://github.com/slopus/happy)：Claude Code 与 Codex 的端到端加密手机 / 网页中继；会话需通过它的 wrapper 启动。
- 官方也在覆盖同类需求：Claude Code 的 agent view 与 agent teams，Codex 的原生子智能体。

Agent Bus 与它们互补：它监管许多独立的、已经在运行的交互式会话，Claude Code 与 Codex 混用，并以提供方自己的结构化信号做判断（屏幕只兜底）。如果你需要沙箱、每个智能体一个 worktree 的隔离，或不想自己配反向代理就能远程访问，上面某个项目可能更合适。

## 配置

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
| `PYTHON` | `python3` | `bin/agent-bus` 使用的解释器 |

完整列表（超时、缓存、leaderd 调参等）见 [docs/configuration.md](docs/configuration.md)。部分变量出于兼容保留历史前缀 `SECRETARY_` / `TMUX_CARD_`。

## 局限与已知问题

- 它读取 Claude Code 与 Codex CLI 的屏幕、日志和 JSONL 格式，这些都不是稳定的公开 API，会随上游版本变化。它依赖 `claude agents --json`（研究预览特性）和 Codex rollout 格式；上游变化时，状态判断会降级到屏幕兜底，对话框识别也可能需要跟进更新。
- 单机、单用户、单个 tmux server，没有多机协调。
- Codex `app-server` 协议仍在变化，`codex start` 可能需要随之更新。
- 仅支持 Linux（`/proc`、`fcntl`）。
- 网站界面、大量代码注释和部分脚本输出（例如 `agent_window.sh`、恢复相关提示）是中文；README、文档和大部分 CLI 帮助为英文。
- 对话框自动审批基于模式识别。新出现的对话框形态会被当成"不是权限对话框"留给人处理——这是安全的，但可能需要更新规则。

## 目录结构

```
bin/agent-bus            CLI 分发（bin/secretary-bus 为别名）
bin/ai-session-shell     启动 wrapper：AI 退出后保留窗口并打印恢复命令
scripts/                 总线模块（标准库 Python）与 shell 辅助脚本
dashboard/               卡片网站（server.py、index.html、轨迹视图）
contrib/boot/            ensure-tmux-session、auto-restore
contrib/systemd/         systemd 用户单元示例
contrib/claude-hooks/    tmux-session-stamp.sh（Claude Code hook）
docs/                    架构、安全模型、负责人流程、恢复、网站、配置（英文）
tests/                   总线测试、tests/dashboard/、tests/e2e/（手动浏览器脚本）
```

## 测试

```bash
python3 -m pytest -q
```

测试会在临时目录里起自己的 tmux server（设置 `TMUX_TMPDIR` 并去掉 `$TMUX`），并使用临时总线目录，因此不会碰你真实的 tmux 会话或 `~/.codex/agent-bus`。前端测试用 Node.js 运行页面里的 JavaScript（需要安装 Node.js）。需要 `tmux` 或 `codex` 二进制（一个 app-server schema 检查）的测试在缺少时自动跳过。`tests/e2e/*.py` 是针对运行中网站的手动 Playwright 脚本，pytest 不收集。

## 许可证

[MIT](LICENSE) © 2026 Yongan Zhang
