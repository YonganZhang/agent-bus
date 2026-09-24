# Safety model

Agent Bus types into terminals that run AI agents with access to your files.
A wrong keystroke in the wrong pane can approve a command, confirm a dialog, or
start work in the wrong repository. The design goal is: when in doubt, do
nothing and say why.

## Fail-closed identity

- `register` freezes a target to its `%pane_id`, the PID of the pane process,
  that process's Linux start time (from `/proc/<pid>/stat`), the expected
  command, and the cwd.
- Every send, key, approve, interrupt, restart, and kill re-reads the pane and
  refuses if the pane is gone, the process start time changed (a respawn, even
  with a reused PID), or the command no longer matches.
- Window numbers and names are for humans only; actions are addressed by pane
  id. An invalid pane reference such as `.99` never falls back to `.0`.
- A leader can only control workers it has claimed, and a worker can be claimed
  by one active leader at a time.

## Never typing into a dialog

- Before a dispatch, the provider state is read. If it is `needs_input`, plain
  text is refused (including `continue` and `steer`): pasting and pressing Enter
  would confirm whichever option is highlighted.
- Enter is refused when no Claude/Codex process runs under the pane's
  foreground process any more: a launch wrapper keeps its PID when the AI exits
  and `exec`s a shell, so the frozen identity still matches, but the text would
  now run as shell commands. Targets that are shells on purpose are registered
  with `register --shell`; one send can pass `send --allow-shell`.
- Every dialog driver (automatic approval, `leader approve`, a dashboard click
  on an unnumbered option, the boot helper) takes the same per-pane lock under
  `$AGENT_BUS_DIR/locks/` for the whole answer. Ordinary text sends do not take
  this lock, and neither does `leader approve --key` (raw keys you chose).
- The delivery path is: leave copy mode, bracketed paste through a named tmux
  buffer, wait, then submit. If the paste is visible but the provider did not
  react within a bounded time, the result is `submitted-pending-confirmation`
  (`send` exits 75): not a failure, and it must not be retried blindly.

## Auto-approve (opt-in)

Off by default. It can be enabled by any of:

- `agent-bus leader config --auto-approve-permissions true` (stored in
  `$AGENT_BUS_DIR/config.json`),
- `AGENT_BUS_AUTO_APPROVE=1` in the environment (`0` forces it off),
- `CARDS_AUTO_APPROVE=1` for the dashboard process, which also starts the
  dashboard's background pass over waiting panes (every
  `CARDS_AUTO_APPROVE_INTERVAL` seconds, default 6; `CARDS_AUTO_APPROVE=0`
  keeps the dashboard pass off even when the bus switch is on). The dashboard
  loop is started only from its environment (`CARDS_AUTO_APPROVE=1` or
  `AGENT_BUS_AUTO_APPROVE=1`); the `leader config` switch alone does not start it.

What it answers (the most permissive option):

- tool permission prompts,
- folder-trust prompts,
- hooks review prompts,
- the "make auto mode your default" offer,
- plan-mode execution confirmations (Claude's "ready to execute" approval,
  Codex's "Implement this plan?").

What it never answers:

- questions about the work: Claude `AskUserQuestion`, Codex
  `request_user_input`. These are recognised by the rows and footers only those
  prompts draw (for example "Type something", "Chat about this", "None of the
  above", "Question 1/1", "tab to add notes") and are classified as questions
  before any permission rule is considered;
- account, login, API-key, and billing choices;
- anything it cannot classify.

How it answers:

1. Only a screen whose input box is gone, and which shows a highlighted row
   and key hints, is read as a dialog (an unsent draft in the input box is not
   a dialog).
2. For Codex (which draws dialogs without a frame) only the trailing run of
   numbered options `1..n` counts as the option list, and only the few lines
   above it as the question, so numbered lists in earlier conversation are not
   mistaken for options.
3. The highlight is moved one row at a time; the screen is re-read after every
   key; Enter is pressed only when the highlighted row is the chosen option.
4. If the dialog changes or disappears midway, nothing more is sent and a
   `*_auto_answer_failed` event is recorded. After Enter, the dialog must be
   seen to close; if the same dialog is still open the answer counts as failed.
5. The per-pane dialog lock is held for the whole answer; a pane another sender
   is driving is skipped. A pane in tmux copy mode is refused. Automatic answers
   also skip a pane a person typed into within `AGENT_BUS_HUMAN_ACTIVE_SECONDS`
   (default 20; judged from attached tmux clients, and the dashboard also counts
   its own web input). Explicit choices (`leader approve`, a click in the
   dashboard) are not blocked by recent typing.
6. The same dialog (pane + question) is not retried within 30 seconds.
7. Every answer is recorded: `worker_prompt_auto_answered` (leader tick /
   leaderd) or `pane_prompt_auto_answered` (dashboard), with the dialog kind,
   the chosen option, the headline that decided the category, and the prompt
   text. Questions left for a human are recorded once as
   `worker_prompt_needs_user`.

Enabling it means accepting whatever those dialogs would have asked you. If the
agents you drive can run destructive commands, auto-approving tool permissions
lets them.

`leader approve --worker W` without `--key` applies the same policy once and
refuses questions; `--key` sends raw keys you chose.

### Boot auto-restore

`contrib/boot/auto-restore` is opt-in by installation. On the panes it restored
it answers, even with auto-approve off: folder-trust dialogs, the auto-mode
offer, and the two Codex start-up notices ("Skip until next version", "Resume
paused goal", option 2 chosen row by row). Other permission dialogs (tool
permission, hooks review, plan approval) are answered only when the
auto-approve switch is on. Every answer is recorded as a
`boot_prompt_auto_answered` event.

## Trusted-owner mode

Off by default. `agent-bus leader config --trusted-owner true` makes leader
actions (`assign`, `steer`, `keys`, `approve`, `interrupt`, `restart`, `kill`)
execute without `--yes` and allows multi-line text; `--dry-run` still previews.
It is meant for a leader pane you fully control. Turn it off with
`--trusted-owner false`.

## Destructive operations

- `recovery restore|relaunch|auto`, `event-reap`, and `event-prune` are dry-run
  unless `--yes` is passed. `event-prune` moves records to `_legacy/` with a
  restore README; it does not delete.
- `recovery relaunch` only respawns a pane in the same tmux server when the
  whole process tree of the pane is an idle shell; a user's training job or
  build in that shell is never killed.
- `agent_window.sh restart` writes a recovery snapshot first and refuses to
  replace the pane if that fails; it refuses split windows and panes whose
  session id cannot be proven.
- Snapshots that look like a disaster (a sudden drop in panes or resumable
  session ids) are stored as `degraded-*.json` and never overwrite
  `latest-good.json` unless you pass `--force`.
- Deadlines only notify. Nothing is killed or closed automatically.

## Sessions

- There is no implicit "continue the latest conversation". Opening a Claude or
  Codex window requires `--resume-id <id>` or `--fresh`.
- Codex sessions are resumed only in the `CODEX_HOME` that holds them; when
  that is ambiguous, recovery reports `blocked` instead of guessing.
- Cross-provider hand-over never passes one provider's session id to the other.

## Dashboard exposure

- Basic Auth on every route. With no credentials configured every request gets
  401.
- Cross-site writes are refused: a browser caches Basic Auth, so without this
  any web page could post a form to `/api/send`. Write requests the browser
  marks as cross-site (`Sec-Fetch-Site`), JSON writes without
  `Content-Type: application/json`, and uploads without the `X-Cards-Upload`
  header get 403.
- Dashboard input is a person typing. Only the send box (`/api/send`, when the
  request carries the pane pid and start time, as the page does) checks that
  the pane process is still the one the page showed; `/api/key` and
  `/api/choose` do not. None of them apply the CLI's `needs_input` or
  AI-exited refusals. A click on a numbered option sends that digit key; a
  click on an unnumbered dialog option uses the verified row-by-row driver.
- Read endpoints that expose process / tmux state or run a subprocess
  (`/api/terminal/capture`, `/api/terminal/status`, `/api/plan/track`) also
  refuse cross-site requests.
- `/api/terminal/resize` sizes a pane's tmux window for the in-card terminal
  view (bounded 40–250 × 12–120, skipped while a real terminal client used that
  window in the last 30 s); `/api/terminal/focus` switches the session's
  current window (every client on a shared session follows) after checking the
  pane instance.
- The optional plan integration runs the program named by `CARDS_TOP_CLI` as
  your user, with argument lists (no shell), `stdin=/dev/null`, timeouts, a
  whitelist of seven write actions and validated fields; the project is only
  derived from the pane's live cwd. `/api/archive-request` sends the
  `CARDS_ARCHIVE_PROMPT` text through the normal send path, only to an idle
  live Claude / Codex. Both are off (`501`) unless configured; see
  [plan-integration.md](plan-integration.md).
- Binds to `127.0.0.1` by default. Logged-in users can send text and raw keys to
  panes, answer dialogs, upload files, resize and close panes, so treat the password
  like a shell password. For remote access, use a reverse proxy with TLS and an
  extra authentication layer.
- Local file downloads are limited to `TMUX_CARD_LOCAL_ARTIFACT_ROOT`
  (default: your home directory), to a fixed list of document/media/archive
  extensions, and exclude hidden path components and names that look like
  credentials (`secret`, `token`, `password`, `api_key`, `id_rsa`, ...).
  Narrow the root if your home directory holds files you would not want
  downloadable by a dashboard user.

## Secrets

- Do not put passwords, tokens, or API keys in task text; task previews are
  stored in the ledger.
- Agent Bus never reads or copies provider credentials. Isolated Codex homes
  link to the default `auth.json` instead of copying it.
- The trace export refuses to produce a file that still looks like it contains
  credentials after redaction.
