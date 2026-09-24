# Cards dashboard

`dashboard/` is a single-page web app ("AI Session Cards") plus a stdlib HTTP
server. It shows one card per pane of one tmux session and lets you read and
drive those sessions from a browser, including a phone.

## Running

```bash
# credentials (required: every route returns 401 without them)
mkdir -p ~/.codex/agent-bus
printf 'WEBTERM_USER=%s\nWEBTERM_PASS=%s\n' me "$(openssl rand -hex 16)" > ~/.codex/agent-bus/webterm.env
chmod 600 ~/.codex/agent-bus/webterm.env

python3 dashboard/server.py            # http://127.0.0.1:7795/cards/
```

As a service: `contrib/systemd/agent-bus-dashboard.service`. Do not start a
second copy by hand while the unit is active; two servers race for the port.

`TMUX_CARD_USER` / `TMUX_CARD_PASS` in the environment take precedence over the
file. The file is re-read when it changes.

## What it shows

- **Cards grid / list** — one card per pane with provider (Claude, Codex, shell),
  project, status (running, idle, waiting, needs attention, quota limited), a
  short preview, and ledger job state.
- **Timeline** — the selected pane's conversation. Claude history comes from its
  transcript JSONL (Claude repaints in the alternate screen, so tmux has no
  scrollback for it); Codex history from its rollout JSONL; the live screen tail
  is merged in. Older history is paged on demand.
  Which Claude transcript: the session `claude agents --json` reports for the
  pane's process (matched by pid) wins over the pane stamp, so a reopened or
  `/resume`d window, or a session whose transcript moved into a worktree
  directory, still shows the right conversation; a background pass repairs the
  stale stamp. An authoritative transcript is shown whole — it is never cut at
  what the screen shows (the screen may be scrolled up, in copy mode, or behind
  a picker). While Claude's view is scrolled back ("Jump to bottom"), no screen
  content is merged, and text already recorded anywhere in the transcript is
  never appended as new output.
- **Sub-agent panel** — progress of Claude sub-agents and Codex sub-agent
  threads for the selected pane.
- **Workflow progress** — when a session runs a multi-agent workflow.
- **Choices and dialogs** — numbered pickers and unnumbered dialogs are rendered
  as clickable options. Clicking a numbered option sends that digit key;
  clicking an option of an unnumbered dialog uses the same verified,
  one-row-at-a-time driver (and per-pane lock) as the CLI.
- **Composer** — send text (with images/uploads) to the selected pane; queued
  messages are shown as pending. This is a person typing: when the request
  carries the pane pid and start time (the page sends them) it checks that the
  pane process is still the one the page showed; it does not apply the CLI's
  `needs_input` / AI-exited refusals. `/api/key` and `/api/choose` do no
  identity check.
- **Organisation** — categories, favourites, aliases, ordering. Stored
  server-side in `prefs.json` so they survive browser cache clears and sync
  across devices. The default fixed categories (`开发`, `论文`, `私人`, `待处理`,
  `其他`) cannot be deleted; `最近` and `全部` are views.
- **Trace view** — a tree of one session's tool calls, sub-agents, and timing,
  built on demand from local logs with secrets redacted. Export refuses output
  that still looks like it contains credentials.
- **Shared files** — a small upload/download area (`TMUX_CARD_SHARED_FILES_DIR`).
- **Local artefacts** — paths to documents, images, media, or archives under
  `TMUX_CARD_LOCAL_ARTIFACT_ROOT` mentioned in replies become authenticated
  download / preview links. Hidden paths and credential-looking names are
  refused.
- **Git row** — every card whose cwd is inside a Git repository shows a small
  line: branch, **待归档 N** (untracked + modified files, with 新 / 改 split on
  wide screens; amber from 50 files, 24 h since the last commit or no commit
  yet, red from 200 files or 7 days), **未推送 N** or 无远端, **上次归档** (age of the last commit), and
  with the plan integration **▶ task title** (click to open the plan). Git runs
  only on a background pool (2 s timeout per command, cached 60 s per
  repository, `GIT_OPTIONAL_LOCKS=0`); `/api/panes` never waits for it.
- **Detail header** — 计划 / 归档 / 终端 are always shown. A button that cannot
  be used is greyed out and says why on hover and on click (no plan, no live
  AI, archive running, integration not configured, ...). On narrow screens they
  move into the "⋯" menu.
- **Plan panel and one-click archive** (optional) — a drawer (full screen on a
  phone) with the plan file rendered as an outline tree plus 动态 / 分拣 / 原文
  tabs and whitelisted task edits; 归档 sends a configured prompt to the idle AI
  and reports what changed. Needs `CARDS_TOP_CLI` / `CARDS_ARCHIVE_PROMPT`; see
  [plan-integration.md](plan-integration.md).
- **Terminal view** — 终端 replaces the conversation with the pane's real
  terminal (`/api/terminal/capture`, ANSI colours in a Dracula palette with a
  minimum contrast of 4.5, bold / underline / reverse kept). It refreshes every
  0.3 s while the AI works or output changed in the last 3 s, 1 s after ten
  unchanged polls, 5 s while the page is hidden, and immediately after you send
  or press a key; only changed rows are redrawn and the scroll position never
  jumps while you type. The pane's window is resized to the viewer
  (`/api/terminal/resize`, like a tmux client; skipped while a real terminal
  client used that window in the last 30 s, in which case the view re-wraps
  lines to the browser width instead). Claude windows get at most 109 columns:
  Claude Code's fullscreen UI opens a code-changes side panel at 110 columns or
  more. Scrolling up reads tmux scrollback, then
  continues with the conversation records (Claude JSONL / Codex rollout) up to
  the start of the session. Double-click or End jumps to the latest line. The
  chosen view (对话 / 终端) applies to every window and is remembered per
  browser.
- **Card ↔ terminal** — `?pane=%12` (or `?pane=12`) opens that window's detail
  view. With `TMUX_CARD_TERMINAL_URL` set, "⋯ → 打开完整终端页" points your web
  terminal (for example ttyd attached to the same session) at the window
  (`/api/terminal/focus`) and opens it. `/api/terminal/status` reports which
  pane the terminal clients show and whether it matches a card.
- **Phone layout** — the round ⌁ button opens a row with 计划 / 终端 / ESC; it
  can be dragged, keeps its centre when opened, and is clamped back into view
  after rotation. Collapsing the composer only collapses it (the draft is kept
  per window); only the send button or shortcut sends.

The page checks the served asset version on every poll and asks you to reload
when `index.html` changed on disk, so an old tab does not silently run old code.

## Turn completion

A reply counts as a finished turn when the transcript says so (`end_turn`,
Codex assistant message). When only the screen is available, a Markdown
heading whose text is one of `TMUX_CARD_SUMMARY_HEADINGS` (default `Summary`,
`|`-separated) is used as the fallback signal.

## Status

Status is built from, in order of trust: the ledger job state; the provider's
own state (`claude agents --json`, transcript `stop_reason`, Codex rollout
events); then screen heuristics (spinners, "esc to interrupt", dialogs).
Claude reports `busy` while a background monitor is armed after the turn ended;
when the transcript says the turn is over and only monitors remain (no
background shells, sub-agents or workflows), the card shows idle with an
"answered · background monitor" hint. An idle
screen never completes a leader or supervisor job; only the owner's
`verify` / `close` / `collect` does.

## API (all under `TMUX_CARD_URL_PREFIX`, default `/cards`)

Read: `/api/panes`, `/api/capture`, `/api/history_before`, `/api/jobs`,
`/api/events`, `/api/trace`, `/api/files`, `/api/active-pane`,
`/api/terminal/capture`, `/api/terminal/status`, `/api/plan`, `/api/plan/track`.

`/api/terminal/capture?pane=%12[&lines=N][&before=ROW][&if_hash=H][&join=1]`
returns the pane's rows with ANSI colours, numbered from the oldest history
row (0) to the last screen row, plus geometry, cursor and a hash (`unchanged`
when `if_hash` matches); `join=1` joins soft-wrapped rows. It never selects a
window. `/api/terminal/status[?pane=%12]` lists the tmux clients on the
session and, with `pane`, whether the terminal shows the same pane as the card.
Both refuse cross-site requests (`403`).

Write: `/api/send`, `/api/key`, `/api/choose`, `/api/pane/close`, `/api/prefs*`,
`/api/upload`, `/api/files/*`, `/api/terminal/focus` (`{pane, pane_pid,
pane_start_time}`), `/api/terminal/resize` (`{pane, cols 40–250, rows
12–120}`), `/api/plan/action`, `/api/archive-request`. Writes must be JSON (`Content-Type:
application/json`), uploads must carry `X-Cards-Upload: 1`, and requests a
browser marks as cross-site are refused (CSRF protection). `/api/key` only
accepts digits, `Escape`, `C-c`, and `C-m`.

`python3 dashboard/server.py --help` prints the main settings and exits.
`agent-bus dashboard` is unrelated: it renders Codex app-server threads/runs.

`agent-bus cards ...` (`scripts/cards_control.py`) uses the prefs endpoints for
atomic favourite / category / alias changes; use it instead of editing
`prefs.json`.

## Auto-approve loop

Off by default. `CARDS_AUTO_APPROVE=1` (or `AGENT_BUS_AUTO_APPROVE=1`) starts a
background pass every `CARDS_AUTO_APPROVE_INTERVAL` seconds over panes in the
`waiting` state; `CARDS_AUTO_APPROVE=0` keeps it off. Enabling the switch only
with `agent-bus leader config --auto-approve-permissions true` does not start
this loop (it is decided from the dashboard's environment at start-up). Policy and audit events are
described in [safety-model.md](safety-model.md).

## Settings

See [configuration.md](configuration.md#dashboard). The most common ones:
`TMUX_CARD_HOST`, `TMUX_CARD_PORT`, `TMUX_CARD_SESSION`, `TMUX_CARD_URL_PREFIX`,
`TMUX_CARD_TMUX_SOCKET`, `TMUX_CARD_LOCAL_ARTIFACT_ROOT`,
`TMUX_CARD_PUBLIC_SHARE_HOST` / `TMUX_CARD_PUBLIC_SHARE_DIR` (map links to a
public static host of yours back to local files for authenticated preview),
`TMUX_CARD_TERMINAL_URL` (full web terminal page), and the optional plan
integration `CARDS_TOP_CLI` / `CARDS_ARCHIVE_PROMPT`.
