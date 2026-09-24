# Optional plan and archive integration

**English** | [简体中文](plan-integration.zh-CN.md)

The Cards dashboard can show a project's task plan next to the conversation,
edit it through a small whitelist of actions, triage uncommitted files, and
send a one-click "archive" prompt to the AI in a pane. These features are
**optional**: they are driven by an external plan CLI that speaks the JSON
contract below (the author uses the share-top plan CLI; any tool that follows
the contract works). Cards never parses or rewrites a plan itself.

Without configuration everything else works (cards, Git row, terminal view,
composer, ...). The 计划 and 归档 buttons stay visible but greyed out with the
reason "需要配置 share-top 集成（设置 CARDS_TOP_CLI / CARDS_ARCHIVE_PROMPT …）",
and the endpoints answer `501` with the same message.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `CARDS_TOP_CLI` | unset (plan features off) | Plan CLI. A `*.py` file runs with the dashboard's Python, anything else is executed directly |
| `CARDS_ARCHIVE_PROMPT` | unset (归档 off) | Text file whose content is sent to the pane's AI when 归档 is pressed |
| `CARDS_TOP_PLAN_FILES` | `_wiki-methodology/_top/_task_plan.md:wiki-methodology/top/task_plan.md` | `:`-separated plan locations relative to a project directory; the first existing one wins |
| `TMUX_CARD_PROJECTS_DIR` | `~/projects` | Where the plan search stops for a pane outside any Git repository (`<projects dir>/<project>`) |

A configured path that does not exist (or an empty prompt file) is an error
(`500` with the variable name), not a silent fallback.

```bash
CARDS_TOP_CLI=/path/to/plan-cli.py \
CARDS_ARCHIVE_PROMPT=/path/to/archive-prompt.md \
python3 dashboard/server.py
```

`tests/dashboard/fixtures/fake_plan_cli.py` is a minimal implementation of the
contract (used by the test suite and the screenshots); read it next to this
page when you write an adapter for your own planning tool.

## Where the plan comes from

Cards resolves the project **only from the pane's live tmux cwd** (it never
accepts a path from the browser): it walks from the cwd up to the Git root —
or, without Git, up to `<TMUX_CARD_PROJECTS_DIR>/<project>` (else the home
directory) — and takes the first directory that contains one of
`CARDS_TOP_PLAN_FILES`. That directory is the `PROJECT` argument below.

## Calling convention

- Argument lists, never a shell; `stdin` is `/dev/null`; environment adds
  `GIT_OPTIONAL_LOCKS=0` and `GIT_TERMINAL_PROMPT=0`.
- Timeouts: 10 s per plan command, 60 s for `track` (answered `504`).
- Every command prints **one JSON object** on stdout (other output may go to
  stderr). Anything else is answered `502`.
- Values are passed as `--flag=value` and positionals after `--`, so a title
  starting with `-` is not an option.

## Commands

### `plan list PROJECT`

```json
{"items": [
  {"id": "P1.2", "title": "Write the parser", "state": "in_progress",
   "deps": ["P1.1"], "scopes": [], "gates": ["tests pass"], "evidence": [],
   "reason": null, "no_artifact_reason": null}
]}
```

`state` is one of `pending`, `in_progress`, `blocked`, `done`, `cancelled`.
Task ids match `P\d+(\.[A-Za-z0-9]+)+` (for example `P3.9.E1`). The card's Git
row uses the first `in_progress` item (title and first gate), refreshed in the
background whenever the plan file's mtime changes; a `task/<ID>` branch wins.

### `plan show PROJECT`

Fields read by the page (all optional except `task_plan`):

| Field | Use |
|---|---|
| `project`, `task_plan` | Absolute project dir and the plan path relative to it (the file whose raw text is shown) |
| `phase_id` | Current phase; new task ids are suggested under it |
| `current`, `next` | Task objects (as in `plan list`) or `null` |
| `blocker`, `next_hint` | Summary bar |
| `activity`, `notes` | Lists of strings for the 动态 tab |
| `unrecorded` | `{"commits": n, "files": n, "sample": [str]}` |
| `errors` | Plan validation messages (strings or `{code, message}`) |
| `error` | Present only on failure |

### Writes

```text
plan add    --id=ID --title=T [--gate=G] [--depends-on=A,B]  -- PROJECT
plan edit   [--title=T] [--add-gate=G] [--add-dep=ID]...      -- PROJECT ID
plan note   [--id=ID] --kind=progress|fail|learn               -- PROJECT TEXT
plan start                                                     -- PROJECT ID
plan block  --reason=R                                         -- PROJECT ID
plan cancel --reason=R                                         -- PROJECT ID
plan reopen                                                    -- PROJECT ID
```

Success: an object without `error` whose `verdict` is not `block` / `broken`
(`item_id` and `next_hint` are shown if present). Failure: `error`, or
`verdict: "block" | "broken"` with `errors: [...]`. These messages are
translated for the page: `changed concurrently` (→ `409`, "refresh and retry"),
`duplicate task id: X`, `unknown dependency: X`, `unknown task id: X`,
`only done/cancelled/blocked task can reopen: X is S`.

The dashboard itself only allows these seven actions, caps field lengths
(title 200, gate/reason/text/deps 300), rejects control characters, newlines in
one-line fields and the ` · ` separator. `reopen` takes no reason in the CLI,
so the reason is recorded as a follow-up `plan note`. Each write logs one line
without the text (`plan action pane=… action=… project=… result=…`).

### `track PROJECT` (分拣 tab)

```json
{"verdict": "warn", "counts": {"commit": 2, "ignore": 1, "review": 3, "tracked_should_ignore": 0},
 "next_hint": "2 files to commit", "note": "", "elapsed_s": 0.4,
 "structure": {"root_loose": {"count": 1, "sample": ["x.txt"]},
               "dirs_without_readme": {"count": 0, "dirs": []}}}
```

Cached per repository for 60 s; refused for projects without Git (`409`).

## Plan file shape (rendered by the 计划 tab)

The page renders the raw plan text and matches task lines to `plan list` by id:

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

Other `##` sections are rendered as Markdown blocks (`Phase 进展`, `决策表`,
`错误表` open by default). Lines that do not parse as tasks are shown as-is.

## Task notes

Notes shown per task are read from daily logs next to the plan:
`<plan dir>/_logs/YYYY-MM-DD-plan.md` when the plan directory is named `_top`,
else `<plan dir>/logs/`. Line format:

```text
- 09:30 note:progress P1.2 second pass green · git=main@abc123
```

(the ` · git=…` suffix is optional). The last 60 days are scanned, newest first,
at most 2 MB in total; each task keeps its latest 5 notes of up to 400 chars.

## One-click archive

归档 sends the text of `CARDS_ARCHIVE_PROMPT` to the pane through the same
delivery path as the composer (`/api/send`), only when the pane runs a live
Claude or Codex that is idle (`409` otherwise). Just before sending, Cards
records a baseline (HEAD, pending file count, unpushed commits, plan hash) in
`prefs.json` (`archiveRuns`, server-owned, kept 24 h). Once the pane is idle
again and at least 30 s have passed, it computes the result — new commits since
the baseline, files still pending, whether the plan changed — and shows it in
the detail header ("归档完成：待归档 50→3，新增 4 个提交，计划已更新"). A pane
without Git can be archived too; if the AI creates a repository, all its
commits are counted.

What "archive" means is entirely up to your prompt file.

## HTTP endpoints

| Endpoint | Notes |
|---|---|
| `GET /api/plan?pane=%12[&if_mtime=…&if_notes=…]` | `plan show` + `plan list` + raw text (≤ 200 KB), `mtime_ns`, `sha256`, `notes_by_task`, Git summary, `suggested_id`; unchanged polls skip the CLI |
| `GET /api/plan/track?pane=%12` | `track`; cross-site requests refused (`403`) |
| `POST /api/plan/action` | `{"pane", "action", "id", "title", "gate", "depends_on", "kind", "text", "reason"}` |
| `POST /api/archive-request` | `{"pane", "pane_pid", "pane_start_time"}` |

All four answer `501` while the integration is not configured. `/api/panes`
carries, per pane: `git` (with `task`, `task_title`, `task_gate`, `has_plan`),
`plan_available`, `plan_reason`, `archive_blocker` and, during/after an
archive, `archive`.
