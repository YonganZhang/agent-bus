# Configuration

Everything is configured with environment variables; there is no config file to
write except the optional `$AGENT_BUS_DIR/config.json` (managed by
`agent-bus leader config`) and the dashboard credentials file.

Defaults are chosen so the tools work with an existing `~/.codex` layout. Many
variables keep historical `SECRETARY_` / `TMUX_CARD_` prefixes.

## Paths and state

| Variable | Default | Used by | Meaning |
|---|---|---|---|
| `AGENT_BUS_DIR` | `~/.codex/agent-bus` | all | Bus state: `config.json`, targets, event ledger, leader sessions, task files, Codex runs, window transitions |
| `AGENT_CLI_TARGETS` | `$AGENT_BUS_DIR/cli-targets.json` | cli_bridge | Target registry file |
| `AGENT_EVENT_LEDGER_DIR` | `$AGENT_BUS_DIR/event-ledger` | event_ledger | Ledger directory |
| `AGENT_BUS_DASHBOARD_STATE_DIR` | `$AGENT_BUS_DIR/card-dashboard` | dashboard, recovery | `prefs.json`, `uploads/`, `claude_map.json` |
| `AGENT_BUS_SNAPSHOT_DIR` | `$AGENT_BUS_DEFAULT_CODEX_HOME/tmux-snapshots` | recovery, auto-restore | Snapshots and the recovery lock |
| `AGENT_BUS_DEFAULT_CODEX_HOME` | `~/.codex` | recovery, windows, isolated homes | Default / shared Codex home. Deliberately independent of `CODEX_HOME`, which differs inside isolated windows |
| `AGENT_BUS_CODEX_HOMES_ROOT` | `~/.codex-homes` | windows, codex_app, recovery | Parent of isolated Codex homes |
| `AGENT_BUS_CODEX_HOME_INSTALLER` | unset | create-isolated-codex-home.sh | Executable run with `CODEX_HOME=<new home>` to generate its config. Unset: copy `config.toml` from the default home and link its `AGENTS.md` |
| `AGENT_BUS_SESSION_SHELL` | `<repo>/bin/ai-session-shell` | windows, recovery | Launch wrapper for new AI panes |
| `AGENT_BUS_PROJECTS_DIR` | `~/projects` | agent_window.sh `open` | Where project directories are looked up by keyword |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | provider_state | Claude config directory (transcripts under `projects/`) |
| `CODEX_HOME` | `~/.codex` | codex_app | Caller's Codex home for `--shared-home` workers and thread lookups |
| `AI_SESSION_TMPDIR` | `~/.claude/tmp` | ai-session-shell | `TMPDIR` for launched AI sessions |
| `PYTHON` | `python3` | bin/agent-bus | Interpreter |

## tmux

| Variable | Default | Meaning |
|---|---|---|
| `SECRETARY_TMUX_SESSION` | `secretary_web` | Session used by recovery, `agent_window.sh`, `stamp_live_panes.py`, and the boot scripts |
| `SECRETARY_TMUX_SOCKET` | `$TMUX_TMPDIR/tmux-<uid>/default` | Socket used by `window_transition.py` |
| `SECRETARY_BUS_TMUX_SOCKET` | unset (tmux default) | Socket used by `event-reap` when checking panes |
| `SECRETARY_ENSURE_TMUX` | `<repo>/contrib/boot/ensure-tmux-session` | Script `recovery auto --yes` calls to close the boot placeholder |
| `SECRETARY_PLACEHOLDER_INDEX` | `999` | Window number of the boot placeholder |
| `TMUX_BIN` | `tmux` | tmux binary for `ensure-tmux-session` |
| `AGENT_BUS_ENV_FILES` | unset | Colon-separated env files sourced by `ensure-tmux-session` before creating the session |
| `AUTO_RESTORE_SETTLE` | `150` | Seconds `auto-restore` waits before acting |
| `AUTO_RESTORE_RATIO` | `0.5` | Restore only when live panes < ratio × resumable panes in the snapshot |
| `RECOVERY_STICKY_MAX_AGE` | `604800` | Max age (seconds) of a previous snapshot's session id that may be carried over for a pane whose AI exited |

## Behaviour switches

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_BUS_AUTO_APPROVE` | unset | `1` / `0` forces auto-approve of permission-type dialogs on / off. Unset: `config.json` `auto_approve_permissions` (default `false`) |
| `CARDS_AUTO_APPROVE` | unset | Dashboard only. `1` starts the background loop and enables its passes; `0` disables it even if the bus switch is on. The loop also starts when the dashboard process has `AGENT_BUS_AUTO_APPROVE=1`; turning the switch on only with `leader config` does **not** start the dashboard loop |
| `CARDS_AUTO_APPROVE_INTERVAL` | `6` | Seconds between dashboard auto-approve passes |
| `CARDS_IDENTITY_RECONCILE_INTERVAL` | `60` | Seconds between passes that rewrite a Claude pane's stale `@ai_session_id` / `@ai_transcript` from `claude agents --json` (matched by pid); each repair is logged as `pane_identity_restamped`. `0` disables |
| `AGENT_BUS_CLAUDE_PERMISSION_MODE` | unset | If set, `agent_window.sh` launches Claude with `--permission-mode <value>` (for example `bypassPermissions`; understand the risk first) |
| `AGENT_BUS_HUMAN_ACTIVE_SECONDS` | `20` | Background auto-approve skips a pane a person used within this many seconds |
| `AGENT_BUS_CARDS_CHECKPOINT` | `1` | `agent_window.sh restart` snapshots before and reconciles dashboard metadata after replacing a pane; `0` skips (tests) |
| `AGENT_BUS_ID` | unset | Recorded as the initiator of supervisor jobs |

## Delivery and provider state

| Variable | Default | Meaning |
|---|---|---|
| `SECRETARY_BUS_COMMAND_TIMEOUT` | `10` (bridge), `20` (supervisor) | tmux / git command timeout, seconds |
| `SECRETARY_BUS_SUBMIT_DELAY` | `0.18` | Pause between paste and submit key |
| `SECRETARY_BUS_VERIFY_TIMEOUT` | `6` | How long to watch for the provider accepting a dispatch |
| `SECRETARY_BUS_VERIFY_INTERVAL` | `0.2` | Poll interval for that check |
| `SECRETARY_BUS_PROVIDER_TIMEOUT` | `4` | Timeout for provider probes (`claude agents --json`, …) |
| `SECRETARY_BUS_PROVIDER_TAIL_BYTES` | `131072` | Transcript / rollout tail read per probe |
| `SECRETARY_BUS_PROVIDER_HEAD_BYTES` | `65536` | Head read for session metadata |
| `SECRETARY_BUS_PROVIDER_MAX_CHARS` | `1200` | Max characters of reply text in provider-state output |
| `SECRETARY_BUS_LIFECYCLE_SCAN_BYTES` | `33554432` | Max bytes scanned for turn lifecycle events |
| `SECRETARY_BUS_MAX_UNTRACKED_DIFF_BYTES` | `1000000` | Cap on untracked-file content included in collected diffs |

## Leader and leaderd

| Variable | Default | Meaning |
|---|---|---|
| `SECRETARY_BUS_LEADER_LEASE_SECONDS` | `7200` | Worker claim lease |
| `SECRETARY_BUS_LEADER_PROBE_LINES` | `80` | Pane lines read per targeted probe |
| `SECRETARY_BUS_LEADERD_INTERVAL` | `1.5` | Default of `leaderd --interval` (loop interval, seconds) |
| `SECRETARY_BUS_LEADERD_PROBE_INTERVAL` | `-1` | Default of `leaderd --probe-interval` |
| `SECRETARY_BUS_LEADERD_PROVIDER_POLL_SECONDS` | `5` | Default of `leaderd --provider-poll-seconds` |
| `SECRETARY_BUS_LEADERD_MAX_NOTIFICATION` | `3800` | Max characters of one `LEADER_EVENT` |
| `SECRETARY_BUS_LEADERD_REDRAW_COOLDOWN` | `30` | Default of `leaderd --redraw-cooldown` |
| `SECRETARY_BUS_LEADERD_RETRY_SECONDS` | `5` | Default of `leaderd --delivery-retry-seconds` |
| `SECRETARY_BUS_LEADERD_RECEIPT_TIMEOUT` | `120` | Default of `leaderd --receipt-timeout` |
| `SECRETARY_BUS_LEADERD_HEARTBEAT_SECONDS` | `30` | Min interval between unchanged daemon state writes |
| `SECRETARY_BUS_LEADERD_LOG_MAX_BYTES` | `2097152` | Log rotation size |
| `SECRETARY_BUS_LEADERD_LOG_BACKUPS` | `2` | Rotated logs kept |

## Codex app-server workers

| Variable | Default | Meaning |
|---|---|---|
| `SECRETARY_BUS_CODEX_TIMEOUT` | `20` | JSON-RPC request timeout, seconds |
| `SECRETARY_BUS_CODEX_WORKER_SLOTS` | `6` | Number of isolated worker homes (max concurrent workers) |
| `SECRETARY_BUS_CODEX_WORKER_HOME_SLUG` | `app-worker` | Worker homes are `$AGENT_BUS_CODEX_HOMES_ROOT/<slug>-1..N` |

## Event ledger

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_EVENT_LEDGER_MAX_OFFSETS` | `256` | Size of the compatibility offset index |

## Dashboard

| Variable | Default | Meaning |
|---|---|---|
| `TMUX_CARD_HOST` | `127.0.0.1` | Bind address |
| `TMUX_CARD_PORT` | `7795` | Port |
| `TMUX_CARD_URL_PREFIX` | `/cards` | URL prefix |
| `TMUX_CARD_SESSION` | `secretary_web` | tmux session shown |
| `TMUX_CARD_TMUX_SOCKET` / `TMUX_CARD_TMUX_LABEL` | unset | Use a specific tmux server |
| `TMUX_CARD_USER` / `TMUX_CARD_PASS` | unset | Basic Auth (take precedence over the file) |
| `WEBTERM_ENV` | `$AGENT_BUS_DIR/webterm.env` | Credentials file with `WEBTERM_USER=` / `WEBTERM_PASS=` (also read by `agent-bus cards`) |
| `TMUX_CARD_LOCAL_ARTIFACT_ROOT` | `~` | Root for authenticated local artefact downloads |
| `TMUX_CARD_PUBLIC_SHARE_HOST` | unset | Optional public static host whose paths map to `<artifact root>/<share dir>/` |
| `TMUX_CARD_PUBLIC_SHARE_DIR` | `share-public` | Directory under the artefact root for that mapping |
| `TMUX_CARD_TERMINAL_URL` | unset | Full web terminal page (e.g. ttyd on the same session); enables "⋯ → 打开完整终端页" |
| `TMUX_CARD_PROJECTS_DIR` | `~/projects` | Plan search bound for panes outside Git (`<dir>/<project>`) |
| `CARDS_TOP_CLI` | unset | Optional plan CLI; enables the plan panel, task title on cards and triage ([plan-integration.md](plan-integration.md)) |
| `CARDS_ARCHIVE_PROMPT` | unset | Optional prompt file; enables the 归档 button |
| `CARDS_TOP_PLAN_FILES` | `_wiki-methodology/_top/_task_plan.md:wiki-methodology/top/task_plan.md` | Plan locations relative to a project directory |
| `TMUX_CARD_SHARED_FILES_DIR` | `$AGENT_BUS_DASHBOARD_STATE_DIR/shared_files` | Shared files area |
| `TMUX_CARD_MAX_UPLOAD_BYTES` | `8388608` | Composer upload limit |
| `TMUX_CARD_MAX_SHARED_FILE_BYTES` | `0` | Shared file size cap (`0`: none) |
| `TMUX_CARD_SHARED_UPLOAD_CHUNK_BYTES` | `33554432` | Chunk size for large shared uploads |
| `TMUX_CARD_SHARED_CHUNK_TTL_SECONDS` | `86400` | Abandoned chunk cleanup |
| `TMUX_CARD_SHARED_FILES_LIST_LIMIT` | `500` | Max files listed |
| `TMUX_CARD_TIMEOUT` | `3` | tmux command timeout |
| `TMUX_CARD_SUBMIT_DELAY` | `0.18` | Pause between paste and submit |
| `TMUX_CARD_CAPTURE_LOCK` | `/run/user/<uid>/tmux-card-preview-capture*.lock` | Lock serialising preview captures |
| `TMUX_CARD_CAPTURE_LOCK_TIMEOUT` | `5` | Wait for that lock |
| `TMUX_CARD_CAPTURE_WORKERS` | `8` | Parallel preview captures |
| `TMUX_CARD_ACTIVITY_GRACE` | `1.8` | Status tuning (see `dashboard/server.py`) |
| `TMUX_CARD_RESPONSE_ECHO_GRACE_SECONDS` | `3.0` | Status tuning (see `dashboard/server.py`) |
| `TMUX_CARD_JOB_CACHE_TTL_SECONDS` | `1.0` | Ledger job cache TTL |
| `TMUX_CARD_JOB_COMPLETION_IDLE_SECONDS` | `6.0` | Idle must be observed continuously this long before a dashboard-sent job is completed |
| `TMUX_CARD_JOB_STALE_SECONDS` | `21600` | Age after which an active job is shown as stale |
| `TMUX_CARD_PANES_FAILURE_BACKOFF_SECONDS` | `1.5` | Pane-listing tuning (see `dashboard/server.py`) |
| `TMUX_CARD_PANES_MAX_STALE_SECONDS` | `30` | Pane-listing tuning (see `dashboard/server.py`) |
| `TMUX_CARD_PROVIDER_RUNTIME_TTL_SECONDS` | `2.0` | Provider runtime cache TTL |
| `TMUX_CARD_BLOB_CACHE_MAX_BYTES` / `_MAX_ENTRIES` / `_MAX_ITEM_BYTES` | `200 MiB` / `512` / `2 MiB` | Transcript blob cache |
| `TMUX_CARD_HISTORY_CACHE_MAX_BYTES` / `_MAX_ENTRIES` / `_MAX_ITEM_BYTES` | `48 MiB` / `6` / `16 MiB` | History page cache |
| `TMUX_CARD_SUMMARY_HEADINGS` | `Summary` | Pipe-separated heading words that mark a finished reply when only the screen is available |
| `TMUX_CARD_TRACE_MAX_SPANS` | `2000` | Trace size cap |
| `TMUX_CARD_TRACE_MAX_RESPONSE_BYTES` | `2097152` | Trace response cap |
| `TMUX_CARD_TRACE_CACHE_ITEMS` | `8` | Trace cache entries |
| `TMUX_CARD_TRACE_BUILD_CONCURRENCY` | `1` | Concurrent trace builds |
| `CARDS_BASE_URL` | `http://127.0.0.1:7795/cards` | Dashboard URL used by `agent-bus cards` |
| `CARDS_CONTROL_TIMEOUT` | `8` | HTTP timeout for `agent-bus cards` |

Test-only: `TMUX_CARD_TEST_URL` (manual E2E scripts in `tests/e2e/`).
