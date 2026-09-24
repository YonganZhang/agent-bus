# Recovery

Recovery answers one question after a reboot, a tmux server crash, or a batch of
AI processes exiting: *which conversation belonged in which window, and is it
back?* It is implemented in `scripts/secretary_recovery.py` and exposed as
`agent-bus recovery ...`. It works on one tmux session
(`SECRETARY_TMUX_SESSION`, default `secretary_web`).

## Identity sources (live first)

1. **Live signals** — Claude: the session id that `claude agents --json`
   reports for a PID in the pane's process tree. Codex: the **root** rollout
   file the Codex process has open (sub-agent rollouts are excluded).
2. **Pane options** — `@ai_provider`, `@ai_session_id`, `@ai_transcript`,
   `@ai_codex_home`. Claude's are written by the `SessionStart` hook (below);
   Codex's are written at launch by `agent_window.sh` / `window_transition.py`
   and back-filled by snapshots from the open rollout.
3. **Command line** (`argv`) — only as a last resort. After `/clear`, `/resume`,
   or `/new` the argv is stale; a disagreement with (1) or (2) is recorded as
   `identity_conflict`, never silently resolved in favour of argv.

If none of these proves an identity, the pane is left alone. Nothing is matched
by cwd or file time.

`CODEX_HOME` is part of a Codex identity. It is derived from the open rollout
path (`<CODEX_HOME>/sessions/YYYY/MM/DD/rollout-*.jsonl`), then the process
environment, then `@ai_codex_home`. Restores resume a Codex session only in the
recorded home, and only if that home really contains the rollout. For old
snapshots without a recorded home, it is used only if exactly one of the default
home and `~/.codex-homes/*` has the session; two or more copies (a leftover from
a migration) is `blocked`, never a silent fallback to the shared `~/.codex`.

Snapshots, dashboard preferences, and the launch wrapper are located from
`AGENT_BUS_DEFAULT_CODEX_HOME` / `AGENT_BUS_DIR`, **not** from `CODEX_HOME`,
so running recovery inside an isolated Codex window reads the same state.

### Claude session stamp hook

`contrib/claude-hooks/tmux-session-stamp.sh` stamps the pane with the session id
and transcript path that Claude Code passes to hooks. Add it to
`~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command", "command": "/path/to/agent-bus/contrib/claude-hooks/tmux-session-stamp.sh"}]}],
    "SessionEnd":   [{"hooks": [{"type": "command", "command": "/path/to/agent-bus/contrib/claude-hooks/tmux-session-stamp.sh"}]}]
  }
}
```

The hook always exits 0, ignores calls whose cwd differs from the pane's, and
skips nested `claude` processes running inside the pane's main session so a
short-lived `claude -p` does not overwrite the real identity. On `SessionEnd` it
only records `@ai_ended_at`; the session id stays, because that is what
`recovery relaunch` needs.

For panes started before the hook was installed:
`python3 scripts/stamp_live_panes.py --apply` (or `--refresh-pane %ID`).

## Snapshots

`agent-bus recovery snapshot` (run it from a timer; see
`contrib/systemd/agent-bus-snapshot.*`) writes to `$AGENT_BUS_SNAPSHOT_DIR`
(default `~/.codex/tmux-snapshots`):

- `latest-good.json` — the default restore source.
- `latest-observed.json` — the most recent observation, even when unhealthy.
- `degraded-*.json` — evidence of a sudden drop in panes, or in resumable
  session ids (at least 5 lost or more than a quarter, from a base of 5 or
  more). These never replace `latest-good.json`.
- `daily-*.json`, `good-change-*.json` — history.
- `pinned-*.json` — a fixed copy used by one recovery run.

While a recovery run holds `.auto-restore.lock`, snapshots only update
`latest-observed.json`, so a half-restored state is never promoted to good.

If you really did close most windows on purpose, accept the new state with
`agent-bus recovery snapshot --force` (an empty session or unreadable dashboard
preferences are still refused).

A snapshot also stores the dashboard's categories, favourites, aliases, and a
derived manifest per card (`recovery cards-manifest`), so that metadata can be
replayed onto restored panes.

## Standard loop

```bash
agent-bus recovery plan --summary          # one line per window; without --summary: full JSON
agent-bus recovery cards-manifest
agent-bus recovery restore --limit 5 --yes # a small trial batch first
agent-bus recovery verify
agent-bus recovery restore --all --yes
agent-bus recovery relaunch --all --yes    # windows that are alive but dropped back to a shell
agent-bus recovery reconcile-cards --yes
agent-bus recovery verify
```

Single window: `recovery restore|relaunch --target N` (window number, `%pane`,
name, or dashboard alias). Whole chain against one pinned snapshot:
`recovery auto` (dry-run) / `recovery auto --yes`.

Plan statuses:

| Status | Meaning |
|---|---|
| `already-live` | identity matches; nothing to do |
| `already-live-key` | the window exists but neither side knows its session id; needs a human |
| `stale-shell` | window alive, AI exited, session known → `relaunch` in place |
| `unresolved-pane` | the session is known but the pane is not the same one (or the tmux server changed); counted as missing |
| `restore` | the window is gone; it will be recreated (at a new number if the original is taken) |
| `metadata-only` | no exact session id; not guessed |
| `blocked` | cwd missing or Codex home ambiguous / missing the rollout |
| `home-mismatch` | the session runs in a different `CODEX_HOME` (likely an old fork); counted as missing |

Rules the implementation enforces:

- Restores never overwrite an occupied window.
- `relaunch` (`respawn-window -k`) only runs on a pane in the same tmux server
  whose entire process tree is an idle shell.
- Pane ids are only unique within one tmux server lifetime, so snapshots record
  the server identity; if it cannot be read, nothing is changed.
- Launching many Codex sessions at once can hit `database is locked`; restore and
  relaunch retry (`--retries`, `--retry-backoff`) instead of failing the batch.
- A command that returned 0 is not a recovered window. `verify` fails when any
  resumable session is missing, any pane is a `stale-shell`, or dashboard
  metadata drifted.

## After a restore

New TUIs often stop at a one-time dialog (an update notice, folder trust, "resume
paused goal?"). The boot script in `contrib/boot/auto-restore` handles these
only on panes it restored, addressed by pane id (not window number), and only
while the pane still has a live `claude`/`codex` process (the dialog text may be
a leftover on a shell). Folder trust and the auto-mode offer go through
`dialogs.auto_approve` even with auto-approve off; other permission dialogs
(tool permission, hooks review, plan approval) only when the auto-approve switch
is on. The two Codex start-up notices ("Skip until next version", "Resume paused
goal") are answered with option 2 through the same row-by-row driver. Nothing is
sent when no dialog is read, and work questions are left alone. Every answer is
recorded as a `boot_prompt_auto_answered` ledger event.

Check at least one Claude and one Codex window by hand: they should be in their
original conversation, not a new one. Some large Codex sessions do not repaint
their history after resume; ask a question about earlier content instead of
judging by the screen.

## Boot automation (optional)

- `contrib/boot/ensure-tmux-session` (`agent-bus-tmux.service`) keeps the
  session alive. When it is missing it creates one idle shell window named
  `boot-placeholder`, marked `@boot_placeholder=1`, at window 999 — no AI — so
  restored windows get their original numbers and the placeholder is never
  recorded as a resumable session.
- `contrib/boot/auto-restore` (`agent-bus-autorestore.service`) waits for the
  system to settle, pins `latest-good.json`, runs `restore --all` only when live
  panes are below half of the snapshot's resumable count and the snapshot is
  healthy, always runs `relaunch --all`, clears known start-up dialogs on panes
  it touched, runs an independent `verify`, closes the placeholder, and takes a
  new snapshot **only** if everything verified. Manual use:
  `auto-restore --now --dry-run`, or `--now --force` to skip the gates.

## Changing windows

- New windows: `scripts/agent_window.sh new|open <claude|codex> (--resume-id ID | --fresh)`.
  Without either flag it refuses and lists candidate sessions for that cwd.
- New and restarted Codex windows get an isolated `CODEX_HOME` by default
  (`--shared-home` opts out). When a session moves from the shared home to an
  isolated one, only that session's rollout and index entry are copied —
  never credentials or SQLite files.
- `agent_window.sh restart` takes a snapshot first, pins the original window
  number, and reconciles dashboard metadata afterwards.
- Claude ↔ Codex hand-over for one window: `agent-bus window prepare|launch|verify|promote`
  (a bounded, checksummed handoff file; the source window is kept as a backup).
