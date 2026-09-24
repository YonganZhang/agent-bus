# Architecture

Agent Bus is a set of cooperating command-line modules plus a dashboard. There is
no central daemon: every command reads and writes durable files under
`$AGENT_BUS_DIR`, and the long-running pieces (the dashboard, an optional
`leaderd` per leader session) are just other readers/writers of the same files.

## Components

| Module | Role |
|---|---|
| `bin/agent-bus` | Bash dispatcher. Maps sub-commands to the Python modules below. `bin/secretary-bus` is an alias. |
| `scripts/cli_bridge.py` | Target registry (`cli-targets.json`), identity freezing, `send`, the shared delivery path. |
| `scripts/tmux_delivery.py` | Copy-mode exit, bracketed paste via a named tmux buffer, submit key. |
| `scripts/supervisor.py` | `start` / `collect` / `continue` / `show` / `list` / `prune` for single dispatched jobs, with git diff collection. |
| `scripts/leader.py` | Leader sessions: create, assign, tick/watch, steer, keys, approve, interrupt, restart, kill, collect, verify, abandon, close, doctor. |
| `scripts/leader_daemon.py` | `leaderd`: a background loop per leader session that runs `tick` and wakes the leader pane with one bounded `LEADER_EVENT` prompt when something relevant changed. |
| `scripts/provider_state.py` | Exact or inferred provider state for one target (see below). |
| `scripts/claude_sessions.py` | Wrapper around `claude agents --json`. |
| `scripts/claude_subagents.py`, `scripts/codex_subagents.py` | Sub-agent progress from Claude sub-agent logs and Codex sub-agent rollouts. |
| `scripts/pane_detectors.py` | Screen-side helpers: where the input box is, what is chrome, what is content. |
| `scripts/dialogs.py` | Read, classify, and (when enabled) answer dialogs, one verified key at a time; owns the per-pane dialog lock and the copy-mode / recent-human checks. |
| `scripts/event_ledger.py` | The unified job/event ledger; `jobs`, `status`, `events`, `watch`, `reap`, `prune`. |
| `scripts/codex_app.py` | One-shot Codex workers over `codex app-server`, with a `CODEX_HOME` slot pool. |
| `scripts/secretary_recovery.py` | Snapshots, recovery planning, restore / relaunch / verify, Cards metadata replay. |
| `scripts/stamp_live_panes.py` | Back-fills pane identity options for panes that were started before the hook existed. |
| `scripts/window_transition.py` | Claude ↔ Codex hand-over for a window with a bounded, checksummed handoff file. |
| `scripts/cards_control.py` | `cards` sub-commands: favourites, categories, aliases through the dashboard API. |
| `scripts/agent_window.sh` | Open / new / restart / close windows with an explicit session choice (`--resume-id` or `--fresh`). |
| `scripts/create-isolated-codex-home.sh` | Idempotently prepares an isolated `CODEX_HOME`. |
| `bin/ai-session-shell` | Launch wrapper: keeps a pane open after the AI exits and prints the resume command. |
| `dashboard/server.py` | Cards HTTP server; imports the same provider modules from `scripts/`. |

## Durable state

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

## Job lifecycle

Active statuses: `created`, `sent`, `queued`, `leased`, `starting`, `running`,
`waiting_user`, `stale`. Terminal statuses: `completed`, `blocked`, `failed`,
`cancelled`, `interrupted`.

- A tmux paste without provider acknowledgement is only `sent`. It becomes
  `running` when the provider is observed busy (or a new reply appears).
- A worker can claim a result by ending its reply with
  `COMPLETION_STATUS: COMPLETE|BLOCKED|FAILED|CANCELLED`. `collect` reads only
  text after the prompt it typed and takes the last marker. A claim is not
  acceptance.
- `collect` itself is an evidence event, not a status change. Without a claim,
  the job stays active as `waiting_user`.
- Leader sessions move to `verifying` when a worker attempt is terminal; only
  `leader verify --evidence` records acceptance, and `leader close --status
  completed` requires it for every worker.
- A terminal job never becomes active again. `upsert_job` refuses the
  transition unless explicitly reopened, and an active status appended to a
  terminal job is stored as `data.ignored_status`.
- Ledger failures are best-effort around the core send path: a broken ledger
  does not make tmux panes unusable.

## Provider state

`provider-state TARGET` returns `fidelity: exact` only when the session can be
bound to the pane without guessing:

- Claude: a `claude agents --json` record whose PID is in the pane's process
  tree. Its `status` (`busy` / `idle` / `waiting`, with `waitingFor`) is used
  directly; the transcript JSONL gives history and whether the last turn ended.
- Codex: the root rollout JSONL file the Codex process currently has open
  (`/proc/<pid>/fd`), excluding sub-agent rollouts. The rollout's own events give
  turn state.

Otherwise the result is `inferred` / live-tail and says so. Nothing is ever
chosen by cwd, mtime, or "latest file".

The screen is read only as a fallback and for dialogs. One invariant drives
dialog detection: a real dialog replaces the provider's input box, so a screen
that still shows the input box has no open dialog, whatever text is above it.

## Identity

A registered target stores the display reference (`session:window.pane`), the
stable `%pane_id`, the pane process PID and its Linux start time, the command,
and the cwd. Every control action re-reads these and fails closed on any
mismatch (a respawned process gets a new start time even if the PID is reused).

For recovery, panes also carry tmux options: `@ai_provider`, `@ai_session_id`,
`@ai_transcript` (Claude, set by the `SessionStart` hook in
`contrib/claude-hooks/`), and `@ai_codex_home` (Codex, set at launch and by
snapshots). These survive the AI process exiting, which is exactly when
recovery needs them.

## Dashboard

`dashboard/server.py` is a `ThreadingHTTPServer` serving `index.html` and a JSON
API under `/cards`. It lists panes of one tmux session, captures and parses
their screens, reads Claude transcripts and Codex rollouts for history, merges
ledger jobs into card status, stores organisation preferences, and forwards
input (`/api/send`, `/api/key`, `/api/choose`) through the same delivery code
as the CLI. See [dashboard.md](dashboard.md).
