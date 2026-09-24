# 卡片网站

[English](dashboard.md) | **简体中文**

`dashboard/` 是一个单页网页应用（"AI Session Cards"）加一个标准库 HTTP 服务。它为一个 tmux 会话里的每个窗口显示一张卡片，让你在浏览器（包括手机）里阅读和驱动这些会话。

## 运行

```bash
# credentials (required: every route returns 401 without them)
mkdir -p ~/.codex/agent-bus
printf 'WEBTERM_USER=%s\nWEBTERM_PASS=%s\n' me "$(openssl rand -hex 16)" > ~/.codex/agent-bus/webterm.env
chmod 600 ~/.codex/agent-bus/webterm.env

python3 dashboard/server.py            # http://127.0.0.1:7795/cards/
```

作为服务运行：`contrib/systemd/agent-bus-dashboard.service`。该单元在运行时不要再手动起第二份，两个服务会抢同一个端口。

环境变量里的 `TMUX_CARD_USER` / `TMUX_CARD_PASS` 优先于文件。文件变化后会重新读取。

## 显示什么

- **卡片网格 / 列表**——每个窗口一张卡片，显示提供方（Claude、Codex、shell）、项目、状态（处理中、空闲、等待、需要处理、额度受限）、简短预览和账本里的任务状态。
- **时间线**——选中窗口的对话。Claude 历史来自它的 transcript JSONL（Claude 在备用屏幕里重绘，tmux 没有它的 scrollback）；Codex 历史来自它的 rollout JSONL；再合并屏幕的实时尾部。更早的历史按需分页。
  选哪份 Claude transcript：`claude agents --json` 为该窗口进程报告的会话（按 pid 匹配）优先于窗口上的标记，所以重新打开或 `/resume` 过的窗口、transcript 挪进 worktree 目录的会话，都能显示正确的对话；后台会修复过期的标记。权威 transcript 完整显示——从不按屏幕上看到的内容截断（屏幕可能往上翻着、处于复制模式或被选择框挡住）。Claude 视图往回翻着（"Jump to bottom"）时不合并任何屏幕内容，transcript 里任何位置已记录的文字也绝不会作为新输出追加。
- **子智能体面板**——选中窗口的 Claude 子智能体和 Codex 子智能体线程的进度。
- **工作流进度**——会话运行多智能体工作流时显示。
- **选择题与对话框**——带编号的选择框和无编号对话框渲染成可点的选项。点带编号的选项就发那个数字键；点无编号对话框的选项使用与 CLI 相同的、逐行核对的驱动（以及每个窗口一把的锁）。
- **输入框**——向选中窗口发送文字（可带图片/上传）；排队中的消息显示为待发送。这是人在打字：请求带有窗口 pid 和启动时间时（页面会带上），会核对窗口进程仍是页面上显示的那个；不做 CLI 的 `needs_input` / AI 已退出拒绝。`/api/key` 与 `/api/choose` 不做身份核对。
- **组织**——分类、收藏、别名、排序。存在服务端的 `prefs.json` 里，清浏览器缓存也不丢，并在多设备间同步。默认固定分类（`开发`、`论文`、`私人`、`待处理`、`其他`）不能删除；`最近` 和 `全部` 是视图。
- **轨迹视图**——单个会话的工具调用、子智能体和耗时组成的树，按需从本地日志构建并脱敏。看起来仍含凭证的内容拒绝导出。
- **共享文件**——一个小的上传/下载区（`TMUX_CARD_SHARED_FILES_DIR`）。
- **本地产物**——回复里提到的、位于 `TMUX_CARD_LOCAL_ARTIFACT_ROOT` 下的文档、图片、媒体或压缩包路径，会变成带鉴权的下载 / 预览链接。隐藏路径和看起来像凭证的文件名一律拒绝。
- **Git 行**——cwd 在 Git 仓库里的每张卡片都显示一行小字：分支、**待归档 N**（未跟踪 + 已修改文件，宽屏上拆成 新 / 改；50 个文件起、距上次提交超过 24 小时或还没有提交时为琥珀色，200 个文件起或超过 7 天为红色）、**未推送 N** 或"无远端"、**上次归档**（最近一次提交距今多久），启用计划集成时还有 **▶ 任务标题**（点击打开计划）。Git 只在后台线程池里运行（每条命令超时 2 秒，按仓库缓存 60 秒，`GIT_OPTIONAL_LOCKS=0`）；`/api/panes` 从不等它。
- **详情标题栏**——计划 / 归档 / 终端 三个按钮始终显示。不能用的按钮置灰，悬停和点击时都说明原因（没有计划、没有活着的 AI、归档进行中、未配置集成……）。窄屏上收进"⋯"菜单。
- **计划面板与一键归档**（可选）——一个抽屉（手机上全屏），把计划文件渲染成大纲树，另有 动态 / 分拣 / 原文 标签页和白名单内的任务编辑；"归档"把配置好的提示发给空闲的 AI，并报告改变了什么。需要 `CARDS_TOP_CLI` / `CARDS_ARCHIVE_PROMPT`；见 [plan-integration.zh-CN.md](plan-integration.zh-CN.md)。
- **终端视图**——"终端"把对话换成窗口的真实终端（`/api/terminal/capture`，ANSI 颜色用 Dracula 调色板显示、最低对比度 4.5，保留粗体 / 下划线 / 反显）。AI 在工作或最近 3 秒内输出有变化时每 0.3 秒刷新，连续十次没变后每 1 秒，页面隐藏时每 5 秒，你发送或按键后立即刷新；只重绘变化的行，打字时滚动位置不会跳。窗口会按查看者尺寸调整（`/api/terminal/resize`，像一个 tmux 客户端；30 秒内有真实终端客户端用过该窗口时不调整，此时视图改为按浏览器宽度重新换行）。往上滚先读 tmux scrollback，再接对话记录（Claude JSONL / Codex rollout）一直到会话开头。双击或按 End 跳到最新一行。选中的视图（对话 / 终端）对所有窗口生效，并按浏览器记住。
- **卡片 ↔ 终端**——`?pane=%12`（或 `?pane=12`）打开该窗口的详情。设置了 `TMUX_CARD_TERMINAL_URL` 时，"⋯ → 打开完整终端页"会把你的网页终端（例如 attach 到同一会话的 ttyd）切到这个窗口（`/api/terminal/focus`）并打开它。`/api/terminal/status` 报告终端客户端显示的是哪个窗口，以及是否与卡片一致。
- **手机布局**——圆形 ⌁ 按钮展开一排 计划 / 终端 / ESC；它可以拖动，展开时圆心不动，旋转屏幕后会夹回可视区。收起输入框只是收起（草稿按窗口保存）；只有发送按钮或发送快捷键才会发送。

页面每次轮询都会检查所提供资源的版本，磁盘上的 `index.html` 变了就提示你刷新，旧标签页不会悄悄运行旧代码。

## 轮次结束

transcript 表明结束（`end_turn`、Codex 的 assistant 消息）时，一条回复算作结束的轮次。只有屏幕可用时，以标题文字为 `TMUX_CARD_SUMMARY_HEADINGS` 之一（默认 `Summary`，`|` 分隔）的 Markdown 标题作为兜底信号。

## 状态

状态按可信度依次来自：账本里的任务状态；提供方自己的状态（`claude agents --json`、transcript 的 `stop_reason`、Codex rollout 事件）；然后是屏幕启发式（spinner、"esc to interrupt"、对话框）。轮次结束后若还挂着后台监控，Claude 会报 `busy`；当 transcript 表明本轮已结束、且只剩监控（没有后台 shell、子智能体或工作流）时，卡片显示为空闲，并提示"answered · background monitor"。空闲的屏幕永远不会让负责人或 supervisor 任务完成；只有任务所有者的 `verify` / `close` / `collect` 才会。

## 接口（都在 `TMUX_CARD_URL_PREFIX` 下，默认 `/cards`）

读：`/api/panes`、`/api/capture`、`/api/history_before`、`/api/jobs`、`/api/events`、`/api/trace`、`/api/files`、`/api/active-pane`、`/api/terminal/capture`、`/api/terminal/status`、`/api/plan`、`/api/plan/track`。

`/api/terminal/capture?pane=%12[&lines=N][&before=ROW][&if_hash=H][&join=1]` 返回窗口带 ANSI 颜色的各行，从最老的历史行（0）编号到屏幕最后一行，外加几何信息、光标和哈希（`if_hash` 相同时返回 `unchanged`）；`join=1` 把软换行拼回一行。它从不切换窗口。`/api/terminal/status[?pane=%12]` 列出挂在该会话上的 tmux 客户端，带 `pane` 时说明终端显示的是否与卡片是同一个窗口。两者都拒绝跨站请求（`403`）。

写：`/api/send`、`/api/key`、`/api/choose`、`/api/pane/close`、`/api/prefs*`、`/api/upload`、`/api/files/*`、`/api/terminal/focus`（`{pane, pane_pid, pane_start_time}`）、`/api/terminal/resize`（`{pane, cols 40–250, rows 12–120}`）、`/api/plan/action`、`/api/archive-request`。写请求必须是 JSON（`Content-Type: application/json`），上传必须带 `X-Cards-Upload: 1`，浏览器标记为跨站的请求一律拒绝（防 CSRF）。`/api/key` 只接受数字、`Escape`、`C-c` 和 `C-m`。

`python3 dashboard/server.py --help` 打印主要设置后退出。`agent-bus dashboard` 与此无关：它显示 Codex app-server 的线程/运行记录。

`agent-bus cards ...`（`scripts/cards_control.py`）通过 prefs 接口原子地修改收藏 / 分类 / 别名；请用它，不要直接编辑 `prefs.json`。

## 自动审批循环

默认关闭。`CARDS_AUTO_APPROVE=1`（或 `AGENT_BUS_AUTO_APPROVE=1`）启动一个后台循环，每 `CARDS_AUTO_APPROVE_INTERVAL` 秒检查一遍处于 `waiting` 状态的窗口；`CARDS_AUTO_APPROVE=0` 保持关闭。只用 `agent-bus leader config --auto-approve-permissions true` 打开开关不会启动这个循环（是否启动由网站启动时的环境决定）。策略与审计事件见 [safety-model.zh-CN.md](safety-model.zh-CN.md)。

## 设置

见 [configuration.zh-CN.md](configuration.zh-CN.md)。最常用的：`TMUX_CARD_HOST`、`TMUX_CARD_PORT`、`TMUX_CARD_SESSION`、`TMUX_CARD_URL_PREFIX`、`TMUX_CARD_TMUX_SOCKET`、`TMUX_CARD_LOCAL_ARTIFACT_ROOT`、`TMUX_CARD_PUBLIC_SHARE_HOST` / `TMUX_CARD_PUBLIC_SHARE_DIR`（把指向你自己的公开静态站的链接映射回本地文件，经鉴权预览）、`TMUX_CARD_TERMINAL_URL`（完整网页终端页），以及可选的计划集成 `CARDS_TOP_CLI` / `CARDS_ARCHIVE_PROMPT`。
