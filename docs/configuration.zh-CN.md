# 配置

[English](configuration.md) | **简体中文**

所有配置都通过环境变量完成；除了可选的 `$AGENT_BUS_DIR/config.json`（由
`agent-bus leader config` 管理）和卡片网站凭据文件之外，不需要编写任何配置文件。

默认值的选取使这些工具能直接配合现有的 `~/.codex` 目录布局使用。许多变量沿用了历史上的
`SECRETARY_` / `TMUX_CARD_` 前缀。

## 路径与状态

| 变量 | 默认值 | 使用方 | 含义 |
|---|---|---|---|
| `AGENT_BUS_DIR` | `~/.codex/agent-bus` | 全部 | 总线状态：`config.json`、目标、事件账本、负责人会话、任务文件、Codex 运行记录、窗口切换 |
| `AGENT_CLI_TARGETS` | `$AGENT_BUS_DIR/cli-targets.json` | cli_bridge | 目标登记文件 |
| `AGENT_EVENT_LEDGER_DIR` | `$AGENT_BUS_DIR/event-ledger` | event_ledger | 账本目录 |
| `AGENT_BUS_DASHBOARD_STATE_DIR` | `$AGENT_BUS_DIR/card-dashboard` | dashboard, recovery | `prefs.json`、`uploads/`、`claude_map.json` |
| `AGENT_BUS_SNAPSHOT_DIR` | `$AGENT_BUS_DEFAULT_CODEX_HOME/tmux-snapshots` | recovery, auto-restore | 快照与恢复锁 |
| `AGENT_BUS_DEFAULT_CODEX_HOME` | `~/.codex` | recovery、windows、隔离 home | 默认 / 共享的 Codex home。刻意与 `CODEX_HOME` 无关，因为后者在隔离窗口内取值不同 |
| `AGENT_BUS_CODEX_HOMES_ROOT` | `~/.codex-homes` | windows, codex_app, recovery | 各隔离 Codex home 的父目录 |
| `AGENT_BUS_CODEX_HOME_INSTALLER` | 未设置 | create-isolated-codex-home.sh | 以 `CODEX_HOME=<new home>` 运行、用于生成其配置的可执行文件。未设置时：从默认 home 复制 `config.toml` 并链接其 `AGENTS.md` |
| `AGENT_BUS_SESSION_SHELL` | `<repo>/bin/ai-session-shell` | windows, recovery | 新 AI 窗口的启动包装脚本 |
| `AGENT_BUS_PROJECTS_DIR` | `~/projects` | agent_window.sh `open` | 按关键词查找项目目录的位置 |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | provider_state | Claude 配置目录（transcript 位于 `projects/` 下） |
| `CODEX_HOME` | `~/.codex` | codex_app | 调用方的 Codex home，供 `--shared-home` 员工和线程查找使用 |
| `AI_SESSION_TMPDIR` | `~/.claude/tmp` | ai-session-shell | 所启动 AI 会话的 `TMPDIR` |
| `PYTHON` | `python3` | bin/agent-bus | 解释器 |

## tmux

| 变量 | 默认值 | 含义 |
|---|---|---|
| `SECRETARY_TMUX_SESSION` | `secretary_web` | recovery、`agent_window.sh`、`stamp_live_panes.py` 和开机脚本使用的会话 |
| `SECRETARY_TMUX_SOCKET` | `$TMUX_TMPDIR/tmux-<uid>/default` | `window_transition.py` 使用的 socket |
| `SECRETARY_BUS_TMUX_SOCKET` | 未设置（tmux 默认） | `event-reap` 检查窗口时使用的 socket |
| `SECRETARY_ENSURE_TMUX` | `<repo>/contrib/boot/ensure-tmux-session` | `recovery auto --yes` 调用以关闭开机占位窗口的脚本 |
| `SECRETARY_PLACEHOLDER_INDEX` | `999` | 开机占位窗口的窗口编号 |
| `TMUX_BIN` | `tmux` | `ensure-tmux-session` 使用的 tmux 可执行文件 |
| `AGENT_BUS_ENV_FILES` | 未设置 | `ensure-tmux-session` 在创建会话前 source 的环境文件，以冒号分隔 |
| `AUTO_RESTORE_SETTLE` | `150` | `auto-restore` 动作前等待的秒数 |
| `AUTO_RESTORE_RATIO` | `0.5` | 仅当存活窗口数 < 比例 × 快照中可恢复窗口数时才恢复 |
| `RECOVERY_STICKY_MAX_AGE` | `604800` | 对于 AI 已退出的窗口，可沿用上一份快照中会话 id 的最长时限（秒） |

## 行为开关

| 变量 | 默认值 | 含义 |
|---|---|---|
| `AGENT_BUS_AUTO_APPROVE` | 未设置 | `1` / `0` 强制开启 / 关闭权限类对话框的自动审批。未设置时：取 `config.json` 的 `auto_approve_permissions`（默认 `false`） |
| `CARDS_AUTO_APPROVE` | 未设置 | 仅作用于卡片网站。`1` 启动后台循环并启用其各轮处理；`0` 即使总线开关已打开也禁用它。卡片网站进程带有 `AGENT_BUS_AUTO_APPROVE=1` 时，该循环同样会启动；仅通过 `leader config` 打开开关**不会**启动卡片网站的循环 |
| `CARDS_AUTO_APPROVE_INTERVAL` | `6` | 卡片网站相邻两轮自动审批之间的秒数 |
| `CARDS_IDENTITY_RECONCILE_INTERVAL` | `60` | 相邻两轮之间的秒数，每轮依据 `claude agents --json`（按 pid 匹配）重写 Claude 窗口过期的 `@ai_session_id` / `@ai_transcript`；每次修复记录为 `pane_identity_restamped`。`0` 表示禁用 |
| `AGENT_BUS_CLAUDE_PERMISSION_MODE` | 未设置 | 设置后，`agent_window.sh` 以 `--permission-mode <value>` 启动 Claude（例如 `bypassPermissions`；请先了解其风险） |
| `AGENT_BUS_HUMAN_ACTIVE_SECONDS` | `20` | 后台自动审批会跳过在这么多秒内被人操作过的窗口 |
| `AGENT_BUS_CARDS_CHECKPOINT` | `1` | `agent_window.sh restart` 在替换窗口前先做快照，替换后再对账卡片网站元数据；`0` 跳过（测试用） |
| `AGENT_BUS_ID` | 未设置 | 记录为 supervisor 作业的发起方 |

## 投递与提供方状态

| 变量 | 默认值 | 含义 |
|---|---|---|
| `SECRETARY_BUS_COMMAND_TIMEOUT` | `10`（bridge）、`20`（supervisor） | tmux / git 命令超时，单位秒 |
| `SECRETARY_BUS_SUBMIT_DELAY` | `0.18` | 粘贴与提交按键之间的停顿 |
| `SECRETARY_BUS_VERIFY_TIMEOUT` | `6` | 等待提供方接受派发的观察时长 |
| `SECRETARY_BUS_VERIFY_INTERVAL` | `0.2` | 该检查的轮询间隔 |
| `SECRETARY_BUS_PROVIDER_TIMEOUT` | `4` | 提供方探测（`claude agents --json`, …）的超时 |
| `SECRETARY_BUS_PROVIDER_TAIL_BYTES` | `131072` | 每次探测读取的 transcript / rollout 尾部长度 |
| `SECRETARY_BUS_PROVIDER_HEAD_BYTES` | `65536` | 为读取会话元数据而读的头部长度 |
| `SECRETARY_BUS_PROVIDER_MAX_CHARS` | `1200` | 提供方状态输出中回复文本的最大字符数 |
| `SECRETARY_BUS_LIFECYCLE_SCAN_BYTES` | `33554432` | 扫描轮次生命周期事件的最大字节数 |
| `SECRETARY_BUS_MAX_UNTRACKED_DIFF_BYTES` | `1000000` | 收集的 diff 中所含未跟踪文件内容的上限 |

## 负责人与 leaderd

| 变量 | 默认值 | 含义 |
|---|---|---|
| `SECRETARY_BUS_LEADER_LEASE_SECONDS` | `7200` | 员工认领租约 |
| `SECRETARY_BUS_LEADER_PROBE_LINES` | `80` | 每次定向探测读取的窗口行数 |
| `SECRETARY_BUS_LEADERD_INTERVAL` | `1.5` | `leaderd --interval` 的默认值（循环间隔，秒） |
| `SECRETARY_BUS_LEADERD_PROBE_INTERVAL` | `-1` | `leaderd --probe-interval` 的默认值 |
| `SECRETARY_BUS_LEADERD_PROVIDER_POLL_SECONDS` | `5` | `leaderd --provider-poll-seconds` 的默认值 |
| `SECRETARY_BUS_LEADERD_MAX_NOTIFICATION` | `3800` | 单条 `LEADER_EVENT` 的最大字符数 |
| `SECRETARY_BUS_LEADERD_REDRAW_COOLDOWN` | `30` | `leaderd --redraw-cooldown` 的默认值 |
| `SECRETARY_BUS_LEADERD_RETRY_SECONDS` | `5` | `leaderd --delivery-retry-seconds` 的默认值 |
| `SECRETARY_BUS_LEADERD_RECEIPT_TIMEOUT` | `120` | `leaderd --receipt-timeout` 的默认值 |
| `SECRETARY_BUS_LEADERD_HEARTBEAT_SECONDS` | `30` | 守护进程状态未变化时两次写入之间的最小间隔 |
| `SECRETARY_BUS_LEADERD_LOG_MAX_BYTES` | `2097152` | 日志轮转大小 |
| `SECRETARY_BUS_LEADERD_LOG_BACKUPS` | `2` | 保留的轮转日志份数 |

## Codex app-server 员工

| 变量 | 默认值 | 含义 |
|---|---|---|
| `SECRETARY_BUS_CODEX_TIMEOUT` | `20` | JSON-RPC 请求超时，单位秒 |
| `SECRETARY_BUS_CODEX_WORKER_SLOTS` | `6` | 隔离员工 home 的数量（最大并发员工数） |
| `SECRETARY_BUS_CODEX_WORKER_HOME_SLUG` | `app-worker` | 员工 home 为 `$AGENT_BUS_CODEX_HOMES_ROOT/<slug>-1..N` |

## 事件账本

| 变量 | 默认值 | 含义 |
|---|---|---|
| `AGENT_EVENT_LEDGER_MAX_OFFSETS` | `256` | 兼容性 offset 索引的大小 |

## 卡片网站

| 变量 | 默认值 | 含义 |
|---|---|---|
| `TMUX_CARD_HOST` | `127.0.0.1` | 绑定地址 |
| `TMUX_CARD_PORT` | `7795` | 端口 |
| `TMUX_CARD_URL_PREFIX` | `/cards` | URL 前缀 |
| `TMUX_CARD_SESSION` | `secretary_web` | 展示的 tmux 会话 |
| `TMUX_CARD_TMUX_SOCKET` / `TMUX_CARD_TMUX_LABEL` | 未设置 | 使用指定的 tmux server |
| `TMUX_CARD_USER` / `TMUX_CARD_PASS` | 未设置 | Basic Auth（优先于凭据文件） |
| `WEBTERM_ENV` | `$AGENT_BUS_DIR/webterm.env` | 含 `WEBTERM_USER=` / `WEBTERM_PASS=` 的凭据文件（`agent-bus cards` 也会读取） |
| `TMUX_CARD_LOCAL_ARTIFACT_ROOT` | `~` | 经认证的本地产物下载的根目录 |
| `TMUX_CARD_PUBLIC_SHARE_HOST` | 未设置 | 可选的公开静态主机，其路径映射到 `<artifact root>/<share dir>/` |
| `TMUX_CARD_PUBLIC_SHARE_DIR` | `share-public` | 该映射在产物根目录下对应的目录 |
| `TMUX_CARD_TERMINAL_URL` | 未设置 | 完整网页终端页面（例如连到同一会话的 ttyd）；启用"⋯ → 打开完整终端页" |
| `TMUX_CARD_PROJECTS_DIR` | `~/projects` | Git 之外窗口的计划搜索边界（`<dir>/<project>`） |
| `CARDS_TOP_CLI` | 未设置 | 可选的计划 CLI；启用计划面板、卡片上的任务标题和分拣（[plan-integration.zh-CN.md](plan-integration.zh-CN.md)） |
| `CARDS_ARCHIVE_PROMPT` | 未设置 | 可选的提示词文件；启用「归档」按钮 |
| `CARDS_TOP_PLAN_FILES` | `_wiki-methodology/_top/_task_plan.md:wiki-methodology/top/task_plan.md` | 相对于项目目录的计划文件位置 |
| `TMUX_CARD_SHARED_FILES_DIR` | `$AGENT_BUS_DASHBOARD_STATE_DIR/shared_files` | 共享文件区 |
| `TMUX_CARD_MAX_UPLOAD_BYTES` | `8388608` | 输入框上传上限 |
| `TMUX_CARD_MAX_SHARED_FILE_BYTES` | `0` | 共享文件大小上限（`0`：不限） |
| `TMUX_CARD_SHARED_UPLOAD_CHUNK_BYTES` | `33554432` | 大文件共享上传的分块大小 |
| `TMUX_CARD_SHARED_CHUNK_TTL_SECONDS` | `86400` | 废弃分块的清理时限 |
| `TMUX_CARD_SHARED_FILES_LIST_LIMIT` | `500` | 最多列出的文件数 |
| `TMUX_CARD_TIMEOUT` | `3` | tmux 命令超时 |
| `TMUX_CARD_SUBMIT_DELAY` | `0.18` | 粘贴与提交之间的停顿 |
| `TMUX_CARD_CAPTURE_LOCK` | `/run/user/<uid>/tmux-card-preview-capture*.lock` | 串行化预览抓取的锁 |
| `TMUX_CARD_CAPTURE_LOCK_TIMEOUT` | `5` | 等待该锁的时长 |
| `TMUX_CARD_CAPTURE_WORKERS` | `8` | 并行预览抓取数 |
| `TMUX_CARD_ACTIVITY_GRACE` | `1.8` | 状态判定调参（见 `dashboard/server.py`） |
| `TMUX_CARD_RESPONSE_ECHO_GRACE_SECONDS` | `3.0` | 状态判定调参（见 `dashboard/server.py`） |
| `TMUX_CARD_JOB_CACHE_TTL_SECONDS` | `1.0` | 账本作业缓存 TTL |
| `TMUX_CARD_JOB_COMPLETION_IDLE_SECONDS` | `6.0` | 卡片网站发出的作业需连续观察到空闲达此时长才判为完成 |
| `TMUX_CARD_JOB_STALE_SECONDS` | `21600` | 活动作业超过此时长后显示为过期 |
| `TMUX_CARD_PANES_FAILURE_BACKOFF_SECONDS` | `1.5` | 窗口列表调参（见 `dashboard/server.py`） |
| `TMUX_CARD_PANES_MAX_STALE_SECONDS` | `30` | 窗口列表调参（见 `dashboard/server.py`） |
| `TMUX_CARD_PROVIDER_RUNTIME_TTL_SECONDS` | `2.0` | 提供方运行时缓存 TTL |
| `TMUX_CARD_BLOB_CACHE_MAX_BYTES` / `_MAX_ENTRIES` / `_MAX_ITEM_BYTES` | `200 MiB` / `512` / `2 MiB` | transcript blob 缓存 |
| `TMUX_CARD_HISTORY_CACHE_MAX_BYTES` / `_MAX_ENTRIES` / `_MAX_ITEM_BYTES` | `48 MiB` / `6` / `16 MiB` | 历史页缓存 |
| `TMUX_CARD_SUMMARY_HEADINGS` | `Summary` | 以竖线分隔的标题词；仅能看到屏幕内容时，用它们判定回复已完成 |
| `TMUX_CARD_TRACE_MAX_SPANS` | `2000` | trace 大小上限 |
| `TMUX_CARD_TRACE_MAX_RESPONSE_BYTES` | `2097152` | trace 响应上限 |
| `TMUX_CARD_TRACE_CACHE_ITEMS` | `8` | trace 缓存条目数 |
| `TMUX_CARD_TRACE_BUILD_CONCURRENCY` | `1` | trace 并发构建数 |
| `CARDS_BASE_URL` | `http://127.0.0.1:7795/cards` | `agent-bus cards` 使用的卡片网站 URL |
| `CARDS_CONTROL_TIMEOUT` | `8` | `agent-bus cards` 的 HTTP 超时 |

仅测试用：`TMUX_CARD_TEST_URL`（`tests/e2e/` 中的手动 E2E 脚本）。
