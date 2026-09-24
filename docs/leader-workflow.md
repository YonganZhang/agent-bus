# Leader workflow

A *leader* is any Claude Code or Codex pane that takes responsibility for a
result and drives a set of *worker* panes until the result is independently
accepted. The leader is a role, not a daemon: durable state lives in the bus, so
a leader can be resumed, handed over, or woken by `leaderd`, a provider's own
scheduler, or a human.

The CLI evolves; always check `agent-bus leader --help` and the sub-command's
`--help` before relying on a flag.

## 1. Contract first

Before creating a leader session, write down:

- **Objective** — one sentence.
- **Acceptance** — checks that can be run or observed.
- **Authority** — what the leader may do on its own and what needs a human.
- **Members** — leader and worker targets (registered, identity frozen).
- **Task map** — scope, deliverable, dependencies, and write ownership per worker.
  Two workers must not own overlapping files in the same repository.
- **Monitor policy** — expected silence, progress / hard deadlines, max attempts.
- **Evidence** — diff, tests, build output, artefact paths.

By default a leader may dispatch, read state, collect evidence, run
non-destructive checks, and make small in-contract corrections. It should not
widen the scope, run destructive git, kill or respawn panes, send anything
outside the machine, or duplicate a running task without authorisation.

## 2. Create

```bash
agent-bus register --name lead     --pane secretary_web:0.0 --expected-command claude
agent-bus register --name worker-a --pane secretary_web:3.0 --expected-command codex
agent-bus leader create --leader lead --worker worker-a \
  --objective "Parser fix shipped with tests" \
  --acceptance "pytest passes; no unrelated files changed" \
  --scope worker-a="fix tests/test_parser.py failure" \
  --write-owner worker-a="src/parser/" \
  --progress-deadline 1800 --hard-deadline 7200 --max-attempts 3 --json
agent-bus leader doctor <leader-id> --json
```

- A worker already claimed by another active leader is refused (a paused
  leader keeps its expired claims until its next tick renews them); `--takeover`
  supersedes the old leader explicitly (it can no longer send, and its active
  attempts are cancelled).
- Visible mode (default) keeps members in the dashboard and only fills in a
  category for members that have none; existing categories are left alone.
  `--hidden` keeps a one-off session out of the dashboard.
- `leader discover --json` lists every pane of the tmux server when you first
  need to find the right one.

## 3. Assign

```bash
agent-bus leader assign <leader-id> --worker worker-a --repo ~/src/my-project \
  --task-file task.md --yes
```

- One active attempt per worker. For a correction to a running attempt use
  `steer`, not a second `assign`.
- Long or multi-line task text is written to a task file under
  `$AGENT_BUS_DIR/task-files/`; only a one-line pointer is pasted. Text is
  validated before the job is created, so a rejected text does not consume an
  attempt.
- Ask the worker to end with `COMPLETION_STATUS: COMPLETE` (or `BLOCKED`,
  `FAILED`, `CANCELLED`) so `collect` can read a claim.

A good task states: outcome, repository and baseline, in/out of scope, write
ownership, acceptance checks, required evidence, deadline, and what to do when
blocked. Corrections should be deltas:

```text
Observed: <smallest fact>
Expected: <the acceptance condition it violates>
Action: <one concrete correction>
Preserve: <what not to redo or touch>
Return evidence: <command or artefact>
```

```bash
agent-bus leader steer <leader-id> --worker worker-a --text-file correction.md \
  --action-key fix-1 --yes
```

`--action-key` makes the correction idempotent across leader restarts.

## 4. Monitor

Event first, bounded back-off, targeted probes:

```bash
agent-bus leader watch <leader-id> --timeout 120 --json   # blocks on relevant events
agent-bus leaderd start <leader-id>                       # optional background loop
agent-bus leaderd status <leader-id>; agent-bus leaderd stop <leader-id>
```

- `watch` polls the local ledger, not the model; it returns on a relevant job
  event, an identity or claim anomaly, or a due pane probe.
- `leaderd` runs the same reconcile step in the background and pastes one
  compact `LEADER_EVENT` line into the frozen leader pane when something
  changed.
- Busy / idle comes from `agent-bus provider-state TARGET --pretty` and the
  ledger. Do not grep the screen for "Working".
- When a worker's provider reports a dialog, `tick` / `leaderd` handle it by the
  auto-approve policy if enabled (see [safety-model.md](safety-model.md));
  questions about the work are reported as `worker_prompt_needs_user` once.
- An idle worker is not a finished worker. Collect and check.

## 5. Collect and verify

```bash
agent-bus leader collect <leader-id> --worker worker-a
# run your own acceptance checks
agent-bus leader verify <leader-id> --worker worker-a --evidence "pytest: 42 passed; diff limited to src/parser/"
```

Evidence strength, weakest first: spinner / goal status → worker's own report →
ledger lifecycle → collected diff and files → the leader's independent tests,
build, or artefact check. `verify` requires evidence, is idempotent, and does not
send anything to the worker. It refuses while the worker is still busy or
waiting for input; `--force` accepts anyway when you are sure. Do not use `interrupt` (Ctrl-C) to "finish" an idle
worker: on an idle Codex prompt it can exit the TUI.

If acceptance fails, do not verify that attempt. `steer` it if it is still
active, or `assign` a new attempt if it is terminal (counts toward
`--max-attempts`).

## 6. Deadlines and trouble

- **Progress deadline**: no non-leader event or worker state change on the
  active attempt for the configured time → one `leader_progress_stalled` event
  (again only after new progress and a new stall).
- **Hard deadline**: time since the leader was created → one
  `leader_deadline_exceeded` event.
- Both only notify. The leader decides: `interrupt` a stuck busy turn, `restart`
  the pane (rotates the frozen identity), or `kill` and retire the worker.
- Identity mismatch → fail closed; re-check with `leader doctor`.
- Worker pane gone or replaced by another process →
  `leader abandon <leader-id> --worker W --reason "..."`.

## 7. Close

```bash
agent-bus leader close <leader-id> --status completed \
  --evidence "pytest: 42 passed" --evidence "artefact checked: dist/parser.whl"
```

`--status completed` is refused while any worker's latest attempt is neither
verified, abandoned, nor cancelled with a reason; the error lists the exact
`verify` commands still needed. `blocked`, `failed`, and `cancelled` are always
allowed. Closing releases claims and records still-active attempts as cancelled,
without interrupting the worker panes.

## Resuming a leader

A new leader session (after a restart or a handover) reads
`leader status <id> --json` and `leader doctor <id> --json`, then continues
`watch` from the saved cursor. It should not re-read the ledger from event 0 or
whole transcripts.
