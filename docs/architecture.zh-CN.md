# 架构

[English](architecture.md) | **简体中文**

Agent Bus 是一组相互配合的命令行模块加一个网站。没有中心守护进程：每条命令都读写 `$AGENT_BUS_DIR` 下的持久文件，常驻的部分（网站、每个负责人会话可选的 `leaderd`）也只是这些文件的其他读写方。

## 组件

| 模块 | 作用 |
|---|---|
| `bin/agent-bus` | Bash 分发器，把子命令映射到下面的 Python 模块。`bin/secretary-bus` 是别名。 |
| `scripts/cli_bridge.py` | 目标登记（`cli-targets.json`）、身份冻结、`send`、共用的投递路径。 |
| `scripts/tmux_delivery.py` | 退出复制模式、经命名 tmux buffer 的 bracketed paste、提交按键。 |
| `scripts/supervisor.py` | 单个派发任务的 `start` / `collect` / `continue` / `show` / `list` / `prune`，并收集 git diff。 |
| `scripts/leader.py` | 负责人会话：create、assign、tick/watch、steer、keys、approve、interrupt、restart、kill、collect、verify、abandon、close、doctor。 |
| `scripts/leader_daemon.py` | `leaderd`：每个负责人会话一个后台循环，运行 `tick`，有相关变化时向负责人窗口发一条有边界的 `LEADER_EVENT` 提示将其唤醒。 |
| `scripts/provider_state.py` | 单个目标的精确或推断的提供方状态（见下文）。 |
| `scripts/claude_sessions.py` | `claude agents --json` 的封装。 |
| `scripts/claude_subagents.py`, `scripts/codex_subagents.py` | 从 Claude 子智能体日志和 Codex 子智能体 rollout 读取子智能体进度。 |
| `scripts/pane_detectors.py` | 屏幕侧辅助：输入框在哪、哪些是界面装饰、哪些是内容。 |
| `scripts/dialogs.py` | 读取、分类并（开启时）回答对话框，每次只按一个经核对的键；持有每个窗口的对话框锁，负责复制模式 / 最近有人使用的检查。 |
| `scripts/event_ledger.py` | 统一的任务/事件账本；`jobs`、`status`、`events`、`watch`、`reap`、`prune`。 |
| `scripts/codex_app.py` | 基于 `codex app-server` 的一次性 Codex 员工，带 `CODEX_HOME` 槽位池。 |
| `scripts/secretary_recovery.py` | 快照、恢复计划、restore / relaunch / verify、卡片元数据回放。 |
| `scripts/stamp_live_panes.py` | 为 hook 出现之前启动的窗口补写窗口身份选项。 |
| `scripts/window_transition.py` | 窗口的 Claude ↔ Codex 交接，使用有边界、带校验和的交接文件。 |
| `scripts/cards_control.py` | `cards` 子命令：通过网站 API 管理收藏、分类、别名。 |
| `scripts/agent_window.sh` | 开 / 新建 / 重启 / 关闭窗口，必须显式选择会话（`--resume-id` 或 `--fresh`）。 |
| `scripts/create-isolated-codex-home.sh` | 幂等地准备一个隔离的 `CODEX_HOME`。 |
| `bin/ai-session-shell` | 启动 wrapper：AI 退出后保留窗口并打印恢复命令。 |
| `dashboard/server.py` | 卡片网站 HTTP 服务；从 `scripts/` 导入同一套提供方模块。 |

## 持久状态

```
$AGENT_BUS_DIR/                     default ~/.codex/agent-bus
  config.json                       trusted_owner, auto_approve_permissions
  cli-targets.json                  registered targets (+ .lock)
  event-ledger/
    events.jsonl                    append-only events
    jobs/<job-id>.json              compact current state per job
    next-event-id.txt, .lock
  secretary-jobs/                   supervisor job workspaces
  leader-sessions/                  leader compact snapshots
  leader-daemons/                   leaderd pid/start-token state and logs
  task-files/                       long task / steer texts (one-line pointers are pasted)
  locks/pane-*.lock                 per-pane lock held while a dialog is being answered
  codex-runs/, codex-runs.json      Codex app-server runs
  window-transitions/<id>/          hand-over plans and handoff files
  card-dashboard/                   dashboard prefs.json, uploads/, claude_map.json
  webterm.env                       dashboard Basic Auth credentials (you create it)
  _legacy/event-ledger-pruned/      where event-prune moves old records
$AGENT_BUS_SNAPSHOT_DIR/            default ~/.codex/tmux-snapshots
  latest-good.json, latest-observed.json, daily-*.json, degraded-*.json, pinned-*.json
```

## 任务生命周期

活跃状态：`created`、`sent`、`queued`、`leased`、`starting`、`running`、`waiting_user`、`stale`。终态：`completed`、`blocked`、`failed`、`cancelled`、`interrupted`。

- 没有提供方确认的 tmux 粘贴只算 `sent`。观察到提供方忙碌（或出现新回复）后才变成 `running`。
- 员工可以在回复末尾写 `COMPLETION_STATUS: COMPLETE|BLOCKED|FAILED|CANCELLED` 来声明结果。`collect` 只读它自己输入的提示之后的文字，并取最后一个标记。声明不等于验收。
- `collect` 本身是一个证据事件，不改变状态。没有声明时，任务保持活跃，状态为 `waiting_user`。
- 员工的某次尝试进入终态后，负责人会话进入 `verifying`；只有 `leader verify --evidence` 记录验收，`leader close --status completed` 要求每个员工都已验收。
- 终态任务永远不会再变回活跃。除非显式重开，`upsert_job` 拒绝这种转换；追加到终态任务上的活跃状态存为 `data.ignored_status`。
- 账本失败在核心发送路径周围是尽力而为的：账本坏了不会让 tmux 窗口不可用。

## 提供方状态

只有会话能不靠猜测地绑定到窗口时，`provider-state TARGET` 才返回 `fidelity: exact`：

- Claude：一条 PID 在窗口进程树里的 `claude agents --json` 记录。直接使用它的 `status`（`busy` / `idle` / `waiting`，以及 `waitingFor`）；transcript JSONL 提供历史以及最后一轮是否结束。
- Codex：Codex 进程当前打开着的根 rollout JSONL 文件（`/proc/<pid>/fd`），不含子智能体 rollout。轮次状态来自 rollout 自身的事件。

否则结果是 `inferred` / live-tail，并如实标明。绝不按 cwd、mtime 或"最新文件"选择。

屏幕只作兜底以及用于对话框。对话框检测由一条不变式驱动：真正的对话框会取代提供方的输入框，所以屏幕上仍显示输入框时就没有打开的对话框，不管它上面写着什么。

## 身份

登记的目标保存显示引用（`session:window.pane`）、稳定的 `%pane_id`、窗口进程 PID 及其 Linux 启动时间、命令和 cwd。每个控制动作都会重新读取这些值，任何不一致都 fail closed（重新拉起的进程即使复用了 PID，启动时间也不同）。

为了恢复，窗口还带有 tmux 选项：`@ai_provider`、`@ai_session_id`、`@ai_transcript`（Claude，由 `contrib/claude-hooks/` 里的 `SessionStart` hook 设置），以及 `@ai_codex_home`（Codex，在启动时和快照时设置）。AI 进程退出后这些选项仍然保留，而那正是恢复需要它们的时候。

## 卡片网站

`dashboard/server.py` 是一个 `ThreadingHTTPServer`，在 `/cards` 下提供 `index.html` 和 JSON API。它列出一个 tmux 会话的窗口，抓取并解析它们的屏幕，读取 Claude transcript 和 Codex rollout 作为历史，把账本任务合并进卡片状态，保存组织偏好，并通过与 CLI 相同的投递代码转发输入（`/api/send`、`/api/key`、`/api/choose`）。每张卡片的 Git 状态和可选的计划摘要在一个小的后台线程池里计算，从不在请求路径上。卡片内终端视图用 `capture-pane -e` 读取窗口（`/api/terminal/capture`）。可选的计划面板和归档按钮调用外部计划 CLI（[plan-integration.zh-CN.md](plan-integration.zh-CN.md)）。见 [dashboard.zh-CN.md](dashboard.zh-CN.md)。
