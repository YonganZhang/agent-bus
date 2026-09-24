# 可选的计划与归档集成

[English](plan-integration.md) | **简体中文**

卡片网站可以在对话旁边显示项目的任务计划，通过一小组白名单动作编辑它，分拣未提交的文件，并一键向窗口里的 AI 发送"归档"提示。这些功能是**可选的**：它们由一个遵守下文 JSON 约定的外部计划 CLI 驱动（作者自己用的是 share-top 的计划 CLI；任何遵守这份约定的工具都可以）。卡片网站自己从不解析或改写计划。

不做配置时其余一切照常（卡片、Git 行、终端视图、输入框……）。"计划"和"归档"按钮仍然显示，但置灰并注明原因"需要配置 share-top 集成（设置 CARDS_TOP_CLI / CARDS_ARCHIVE_PROMPT …）"，相关接口返回 `501` 和同样的说明。

## 配置

| 变量 | 默认值 | 含义 |
|---|---|---|
| `CARDS_TOP_CLI` | 未设置（计划功能关闭） | 计划 CLI。`*.py` 文件用网站自己的 Python 运行，其它文件直接执行 |
| `CARDS_ARCHIVE_PROMPT` | 未设置（归档关闭） | 文本文件，按下"归档"时把其内容发给窗口里的 AI |
| `CARDS_TOP_PLAN_FILES` | `_wiki-methodology/_top/_task_plan.md:wiki-methodology/top/task_plan.md` | 相对项目目录的计划文件位置，`:` 分隔；取第一个存在的 |
| `TMUX_CARD_PROJECTS_DIR` | `~/projects` | 窗口不在任何 Git 仓库里时，计划查找的上界（`<projects dir>/<project>`） |

配置了但不存在的路径（或内容为空的提示文件）会报错（`500`，并写明变量名），不会静默回退。

```bash
CARDS_TOP_CLI=/path/to/plan-cli.py \
CARDS_ARCHIVE_PROMPT=/path/to/archive-prompt.md \
python3 dashboard/server.py
```

`tests/dashboard/fixtures/fake_plan_cli.py` 是这份约定的最小实现（测试和截图都用它）；给自己的计划工具写适配器时，可以对照本页阅读它。

## 计划从哪里来

卡片网站**只按窗口的实时 tmux cwd** 找项目（从不接受浏览器传来的路径）：从 cwd 往上找到 Git 根——没有 Git 时找到 `<TMUX_CARD_PROJECTS_DIR>/<project>`（否则到家目录）——取第一个包含 `CARDS_TOP_PLAN_FILES` 之一的目录。这个目录就是下文命令里的 `PROJECT` 参数。

## 调用约定

- 参数列表，绝不经 shell；`stdin` 为 `/dev/null`；环境变量额外设置 `GIT_OPTIONAL_LOCKS=0` 和 `GIT_TERMINAL_PROMPT=0`。
- 超时：每条计划命令 10 秒，`track` 60 秒（超时返回 `504`）。
- 每条命令在 stdout 输出**一个 JSON 对象**（其它输出可以走 stderr）。否则返回 `502`。
- 取值用 `--flag=value` 形式传，位置参数放在 `--` 之后，所以以 `-` 开头的标题不会被当成选项。

## 命令

### `plan list PROJECT`

```json
{"items": [
  {"id": "P1.2", "title": "Write the parser", "state": "in_progress",
   "deps": ["P1.1"], "scopes": [], "gates": ["tests pass"], "evidence": [],
   "reason": null, "no_artifact_reason": null}
]}
```

`state` 取值为 `pending`、`in_progress`、`blocked`、`done`、`cancelled` 之一。任务 ID 匹配 `P\d+(\.[A-Za-z0-9]+)+`（例如 `P3.9.E1`）。卡片的 Git 行使用第一个 `in_progress` 项（标题和第一条验收），计划文件 mtime 变化时在后台刷新；`task/<ID>` 分支优先。

### `plan show PROJECT`

页面读取的字段（除 `task_plan` 外都可缺省）：

| 字段 | 用途 |
|---|---|
| `project`, `task_plan` | 项目的绝对路径，以及相对它的计划文件路径（页面显示这个文件的原文） |
| `phase_id` | 当前阶段；建议的新任务 ID 挂在它下面 |
| `current`, `next` | 任务对象（与 `plan list` 相同）或 `null` |
| `blocker`, `next_hint` | 摘要条 |
| `activity`, `notes` | "动态"标签页用的字符串列表 |
| `unrecorded` | `{"commits": n, "files": n, "sample": [str]}` |
| `errors` | 计划校验消息（字符串或 `{code, message}`） |
| `error` | 只在失败时出现 |

### 写操作

```text
plan add    --id=ID --title=T [--gate=G] [--depends-on=A,B]  -- PROJECT
plan edit   [--title=T] [--add-gate=G] [--add-dep=ID]...      -- PROJECT ID
plan note   [--id=ID] --kind=progress|fail|learn               -- PROJECT TEXT
plan start                                                     -- PROJECT ID
plan block  --reason=R                                         -- PROJECT ID
plan cancel --reason=R                                         -- PROJECT ID
plan reopen                                                    -- PROJECT ID
```

成功：一个不含 `error`、且 `verdict` 不是 `block` / `broken` 的对象（有 `item_id` 和 `next_hint` 时会显示）。失败：带 `error`，或 `verdict: "block" | "broken"` 加 `errors: [...]`。以下消息会翻译给页面：`changed concurrently`（→ `409`，"刷新后重试"）、`duplicate task id: X`、`unknown dependency: X`、`unknown task id: X`、`only done/cancelled/blocked task can reopen: X is S`。

网站自身只允许这七种动作，限制字段长度（标题 200，验收/原因/内容/前置任务 300），拒绝控制字符、单行字段里的换行以及 ` · ` 分隔符。CLI 的 `reopen` 不收原因，所以原因作为随后的一条 `plan note` 记录。每次写入记一行不含文本内容的日志（`plan action pane=… action=… project=… result=…`）。

### `track PROJECT`（"分拣"标签页）

```json
{"verdict": "warn", "counts": {"commit": 2, "ignore": 1, "review": 3, "tracked_should_ignore": 0},
 "next_hint": "2 files to commit", "note": "", "elapsed_s": 0.4,
 "structure": {"root_loose": {"count": 1, "sample": ["x.txt"]},
               "dirs_without_readme": {"count": 0, "dirs": []}}}
```

按仓库缓存 60 秒；没有 Git 的项目拒绝（`409`）。

## 计划文件格式（"计划"标签页的渲染）

页面渲染计划原文，并按 ID 把任务行对到 `plan list` 的结果上：

```markdown
# Demo · Task Plan

🧭 当前 [P1] task=[P1.2] build the parser
> Done Criteria: all tests pass

## Current Coordinate

- Goal: ship the parser
- Current task: P1.2
- Next task: P1.3
- Latest learning: none
- Blocker: none

## Active Work

- [x] P1.1 Read the spec
- [ ] P1.2 Write the parser · state=in_progress · gate=tests pass
  - [ ] P1.2.1 Tokenizer
- [ ] P1.3 Wire it up · dep=P1.2
```

其余 `##` 段落按 Markdown 块渲染（`Phase 进展`、`决策表`、`错误表` 默认展开）。解析不成任务的行原样显示。

## 任务笔记

每个任务显示的笔记来自计划旁边的每日日志：计划所在目录名为 `_top` 时是 `<plan dir>/_logs/YYYY-MM-DD-plan.md`，否则是 `<plan dir>/logs/`。行格式：

```text
- 09:30 note:progress P1.2 second pass green · git=main@abc123
```

（` · git=…` 后缀可省略。）扫描最近 60 天，从新到旧，总计最多 2 MB；每个任务保留最新 5 条，每条最多 400 字。

## 一键归档

"归档"把 `CARDS_ARCHIVE_PROMPT` 的文本经与输入框相同的投递路径（`/api/send`）发给窗口，前提是窗口里有一个活着且空闲的 Claude 或 Codex（否则返回 `409`）。发送前，卡片网站在 `prefs.json` 里记录一份基线（HEAD、待归档文件数、未推送提交数、计划哈希）（`archiveRuns`，由服务端独占，保留 24 小时）。窗口回到空闲且距发送已满 30 秒后，计算结果——基线之后的新提交、仍待归档的文件、计划是否变化——并显示在详情标题栏（"归档完成：待归档 50→3，新增 4 个提交，计划已更新"）。没有 Git 的窗口也可以归档；如果 AI 建了仓库，会统计它的全部提交。

"归档"具体做什么，完全由你的提示文件决定。

## HTTP 接口

| 接口 | 说明 |
|---|---|
| `GET /api/plan?pane=%12[&if_mtime=…&if_notes=…]` | `plan show` + `plan list` + 原文（≤ 200 KB）、`mtime_ns`、`sha256`、`notes_by_task`、Git 摘要、`suggested_id`；未变化的轮询不运行 CLI |
| `GET /api/plan/track?pane=%12` | `track`；拒绝跨站请求（`403`） |
| `POST /api/plan/action` | `{"pane", "action", "id", "title", "gate", "depends_on", "kind", "text", "reason"}` |
| `POST /api/archive-request` | `{"pane", "pane_pid", "pane_start_time"}` |

未配置集成时这四个接口都返回 `501`。`/api/panes` 为每个窗口带上：`git`（含 `task`、`task_title`、`task_gate`、`has_plan`）、`plan_available`、`plan_reason`、`archive_blocker`，以及归档进行中 / 结束后的 `archive`。
