# 安全模型

[English](safety-model.md) | **简体中文**

Agent Bus 会往运行着 AI 智能体的终端里打字，而这些智能体能访问你的文件。在错误的窗口里按错一个键，就可能批准一条命令、确认一个对话框，或在错误的仓库里开工。设计目标是：拿不准时什么都不做，并说明原因。

## Fail closed 的身份核验

- `register` 把目标冻结为它的 `%pane_id`、窗口进程的 PID、该进程的 Linux 启动时间（取自 `/proc/<pid>/stat`）、预期命令以及 cwd。
- 每次 send、key、approve、interrupt、restart 和 kill 都会重新读取窗口；若窗口已不存在、进程启动时间变了（进程被重新拉起，即使 PID 被复用），或命令不再匹配，就拒绝执行。
- 窗口号和名称只给人看；操作按 pane id 寻址。像 `.99` 这样无效的窗口引用绝不会回退到 `.0`。
- 负责人只能控制自己认领的员工，一个员工同一时间只能被一个活跃的负责人认领。

## 绝不往对话框里打字

- 派活前先读取提供方状态。若为 `needs_input`，纯文本一律拒绝（包括 `continue` 和 `steer`）：此时粘贴再回车会确认当前高亮的那个选项。
- 当窗口前台进程下已没有 Claude/Codex 进程在运行时，拒绝回车：启动包装器在 AI 退出后 `exec` 成 shell 时保留自己的 PID，所以冻结的身份仍然匹配，但文字此时会被当作 shell 命令执行。本来就是 shell 的目标用 `register --shell` 登记；单次发送可以传 `send --allow-shell`。
- 每个对话框驱动方（自动审批、`leader approve`、卡片网站上点击无编号选项、开机辅助脚本）在整个作答过程中都持有 `$AGENT_BUS_DIR/locks/` 下同一把每窗口锁。普通文字发送不取这把锁，`leader approve --key`（你自己选的原始按键）也不取。
- 投递路径是：退出复制模式，通过一个具名 tmux buffer 做 bracketed paste，等待，然后提交。如果粘贴已可见但提供方在限定时间内没有反应，结果为 `submitted-pending-confirmation`（`send` 退出码 75）：这不是失败，且绝不能盲目重试。

## 自动审批（需主动开启）

默认关闭。以下任一方式均可开启：

- `agent-bus leader config --auto-approve-permissions true`（存于 `$AGENT_BUS_DIR/config.json`），
- 环境变量 `AGENT_BUS_AUTO_APPROVE=1`（`0` 强制关闭），
- 为卡片网站进程设置 `CARDS_AUTO_APPROVE=1`，这还会启动卡片网站对等待中窗口的后台巡检（每 `CARDS_AUTO_APPROVE_INTERVAL` 秒一次，默认 6；即使总线开关打开，`CARDS_AUTO_APPROVE=0` 也会让卡片网站的巡检保持关闭）。卡片网站的巡检循环只由它自己的环境变量启动（`CARDS_AUTO_APPROVE=1` 或 `AGENT_BUS_AUTO_APPROVE=1`）；单靠 `leader config` 开关不会启动它。

它会回答以下对话框（选择最宽松的选项）：

- 工具权限提示，
- 目录信任提示，
- hooks 审阅提示，
- "把 auto 模式设为默认"的邀请，
- plan 模式的执行确认（Claude 的 "ready to execute" 批准、Codex 的 "Implement this plan?"）。

它永不回答：

- 关于工作内容的提问：Claude `AskUserQuestion`、Codex `request_user_input`。这类提问通过只有它们才会画出的行和页脚来识别（例如 "Type something"、"Chat about this"、"None of the above"、"Question 1/1"、"tab to add notes"），并在考虑任何权限规则之前就被归类为提问；
- 账号、登录、API key 和计费类选择；
- 任何它无法归类的对话框。

它如何作答：

1. 只有输入框已消失、且显示高亮行和按键提示的屏幕才被当作对话框（输入框里未发送的草稿不是对话框）。
2. 对 Codex（它画对话框不带边框），只有末尾那一串连续的编号选项 `1..n` 算作选项列表，只有其上方几行算作问题，因此更早对话里的编号列表不会被误认为选项。
3. 高亮一次移动一行；每按一次键都重新读屏；只有高亮行正是所选选项时才按回车。
4. 如果对话框中途变化或消失，就不再发送任何按键，并记录一个 `*_auto_answer_failed` 事件。回车后必须看到对话框关闭；如果同一个对话框仍然开着，这次作答算失败。
5. 整个作答过程持有每窗口对话框锁；另一个发送方正在操作的窗口会被跳过。处于 tmux 复制模式的窗口一律拒绝。自动作答还会跳过在 `AGENT_BUS_HUMAN_ACTIVE_SECONDS` 秒内有人打过字的窗口（默认 20；依据已连接的 tmux 客户端判断，卡片网站还会计入自己的网页输入）。明确的选择（`leader approve`、在卡片网站里点击）不受最近打字的阻挡。
6. 同一个对话框（窗口 + 问题）30 秒内不重试。
7. 每次作答都有记录：`worker_prompt_auto_answered`（leader tick / leaderd）或 `pane_prompt_auto_answered`（卡片网站），内容包括对话框类型、所选选项、决定类别的那行标题以及提示文本。留给人处理的提问只记录一次，事件为 `worker_prompt_needs_user`。

开启它意味着接受这些对话框本来要问你的一切。如果你驱动的智能体能执行破坏性命令，自动批准工具权限就等于放行它们。

不带 `--key` 的 `leader approve --worker W` 按同一策略作答一次，并拒绝回答提问；`--key` 发送你自己选的原始按键。

### 开机自动恢复

`contrib/boot/auto-restore` 是否启用取决于你是否安装它。在它恢复出来的窗口上，即使自动审批关闭，它也会回答：目录信任对话框、auto 模式邀请，以及 Codex 的两种启动提示（"Skip until next version"、"Resume paused goal"，逐行移动选中选项 2）。其它权限类对话框（工具权限、hooks 审阅、plan 批准）只有自动审批开关打开时才回答。每次作答都记录为 `boot_prompt_auto_answered` 事件。

## 受信所有者模式

默认关闭。`agent-bus leader config --trusted-owner true` 让负责人操作（`assign`、`steer`、`keys`、`approve`、`interrupt`、`restart`、`kill`）无需 `--yes` 即可执行，并允许多行文本；`--dry-run` 仍然只做预览。它面向你完全掌控的负责人窗口。用 `--trusted-owner false` 关闭。

## 破坏性操作

- `recovery restore|relaunch|auto`、`event-reap` 和 `event-prune` 默认只做 dry-run，除非传入 `--yes`。`event-prune` 把记录移到 `_legacy/` 并附一份恢复用 README；它不删除。
- 只有当窗口的整棵进程树都是空闲 shell 时，`recovery relaunch` 才会在同一个 tmux server 里重新拉起该窗口；那个 shell 里用户的训练任务或构建绝不会被杀掉。
- `agent_window.sh restart` 先写一份恢复快照，写失败就拒绝替换窗口；它拒绝处理已分屏的窗口，以及无法证实会话 id 的窗口。
- 看起来像事故的快照（窗口数或可恢复会话 id 骤减）存为 `degraded-*.json`，绝不覆盖 `latest-good.json`，除非你传入 `--force`。
- 截止时间只发通知。不会自动杀掉或关闭任何东西。

## 会话

- 不存在隐式的"接着最近一次对话"。打开 Claude 或 Codex 窗口必须指定 `--resume-id <id>` 或 `--fresh`。
- Codex 会话只在保存它的那个 `CODEX_HOME` 中恢复；无法确定是哪一个时，恢复流程报告 `blocked`，而不是去猜。
- 跨提供方交接绝不把一个提供方的会话 id 传给另一个提供方。

## 卡片网站的暴露面

- 每个路由都有 Basic Auth。未配置凭证时，每个请求都得到 401。
- 拒绝跨站写入：浏览器会缓存 Basic Auth，没有这一条的话，任何网页都能向 `/api/send` 提交表单。浏览器标记为跨站（`Sec-Fetch-Site`）的写请求、不带 `Content-Type: application/json` 的 JSON 写请求，以及不带 `X-Cards-Upload` 请求头的上传，都得到 403。
- 卡片网站的输入视为有人在打字。只有发送框（`/api/send`，且请求像页面那样带上窗口 pid 和启动时间时）会检查窗口进程是否仍是页面显示的那个；`/api/key` 和 `/api/choose` 不检查。它们都不套用 CLI 的 `needs_input` 拒绝和 AI 已退出拒绝。点击带编号的选项会发送对应的数字键；点击无编号对话框选项则使用经过核验的逐行驱动。
- 暴露进程 / tmux 状态或会运行子进程的读端点（`/api/terminal/capture`、`/api/terminal/status`、`/api/plan/track`）同样拒绝跨站请求。
- `/api/terminal/resize` 为卡片内终端视图调整该窗口所在 tmux 窗口的尺寸（限定在 40–250 × 12–120；若最近 30 s 内有真实终端客户端用过该 tmux 窗口则跳过）；`/api/terminal/focus` 在核对窗口实例后切换会话的当前 tmux 窗口（共享同一会话的所有客户端都会跟着切换）。
- 可选的计划集成以你的用户身份运行 `CARDS_TOP_CLI` 指定的程序，使用参数列表（不经 shell）、`stdin=/dev/null`、超时、七种写操作的白名单以及经过校验的字段；项目只从窗口的实时 cwd 推导。`/api/archive-request` 通过常规发送路径发送 `CARDS_ARCHIVE_PROMPT` 文本，且只发给空闲、存活的 Claude / Codex。两者未配置时都处于关闭状态（`501`）；见 [plan-integration.zh-CN.md](plan-integration.zh-CN.md)。
- 默认绑定 `127.0.0.1`。已登录用户可以向窗口发送文本和原始按键、回答对话框、上传文件、调整和关闭窗口，所以请像对待 shell 密码一样对待这个密码。远程访问时，请使用带 TLS 和额外鉴权层的反向代理。
- 本地文件下载限于 `TMUX_CARD_LOCAL_ARTIFACT_ROOT` 之内（默认：你的家目录），限于一组固定的文档 / 媒体 / 压缩包扩展名，并排除隐藏的路径分量和看起来像凭证的名称（`secret`、`token`、`password`、`api_key`、`id_rsa`、...）。如果你的家目录里有不希望被卡片网站用户下载的文件，请收窄这个根目录。

## 机密信息

- 不要把密码、token 或 API key 写进任务文本；任务预览会存入事件账本。
- Agent Bus 从不读取或复制提供方凭证。隔离的 Codex home 链接到默认的 `auth.json`，而不是复制它。
- 如果脱敏后的文件看起来仍含凭证，trace 导出会拒绝生成该文件。
