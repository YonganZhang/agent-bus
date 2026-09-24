#!/usr/bin/env python3
"""Secretary Bus tmux bridge: safe direct input to a registered tmux CLI pane.

Conservative by design:
- only tmux panes are supported;
- targets must be registered before use;
- actual input requires --yes (default is dry-run);
- the pane's current command is checked before sending keys;
- multiline / control characters are blocked unless explicitly allowed.

Inter-conversation work goes through this direct bridge plus supervisor.py,
leader.py and codex_app.py; there is no message board or relay queue.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import dialogs  # noqa: E402
import pane_detectors  # noqa: E402
import tmux_delivery  # noqa: E402


def default_home() -> Path:
    script = Path(__file__).resolve()
    if ".claude" in script.parts:
        return Path.home() / ".claude"
    return Path.home() / ".codex"


BUS = Path(os.environ.get("AGENT_BUS_DIR", str(default_home() / "agent-bus")))
# Runtime state of the Cards dashboard (prefs.json, uploads, claude_map.json).
# Shared by dashboard/server.py and secretary_recovery.py so both agree.
DASHBOARD_STATE_DIR = Path(
    os.environ.get("AGENT_BUS_DASHBOARD_STATE_DIR", str(BUS / "card-dashboard"))
).expanduser()
TARGETS_FILE = Path(os.environ.get("AGENT_CLI_TARGETS", str(BUS / "cli-targets.json")))
TARGETS_LOCK_FILE = TARGETS_FILE.with_suffix(TARGETS_FILE.suffix + ".lock")
COMMAND_TIMEOUT = float(os.environ.get("SECRETARY_BUS_COMMAND_TIMEOUT", "10"))
SUBMIT_DELAY = float(os.environ.get("SECRETARY_BUS_SUBMIT_DELAY", "0.18"))
VERIFY_TIMEOUT = float(os.environ.get("SECRETARY_BUS_VERIFY_TIMEOUT", "6"))
VERIFY_INTERVAL = float(os.environ.get("SECRETARY_BUS_VERIFY_INTERVAL", "0.2"))
PROVIDER_STATE_SCRIPT = SCRIPT_DIR / "provider_state.py"
CONNECTIVITY_PATTERNS = (
    "tls handshake eof",
    "error sending request",
    "backend-api/codex/responses",
    "connection reset",
    "connection refused",
)


@contextlib.contextmanager
def targets_locked() -> Iterator[None]:
    """Serialize read-modify-write access to TARGETS_FILE across processes.

    Without this, two `register` calls racing (e.g. two independent AI agents
    naming the same or different panes at nearly the same moment) can each
    load the file, add their own entry in memory, then write back - whichever
    writes last silently wins and the other's registration is lost. This
    mirrors event_ledger.py's own fcntl-based `locked()`."""
    TARGETS_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with TARGETS_LOCK_FILE.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass
class Target:
    name: str
    pane: str
    expected_command: str
    note: str = ""
    pane_id: str = ""
    pane_pid: int = 0
    pane_start_time: str = ""
    foreground_pid: int = 0
    foreground_start_time: str = ""
    # Registered as a plain shell on purpose: typed text is run as commands.
    shell: bool = False


def run(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, check=check, text=True, capture_output=True, timeout=COMMAND_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(f"command timed out after {COMMAND_TIMEOUT}s: {' '.join(args)}") from exc


def tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["tmux", *args], check=check)


def load_targets() -> dict[str, Target]:
    if not TARGETS_FILE.exists():
        return {}
    import json

    raw = json.loads(TARGETS_FILE.read_text(encoding="utf-8"))
    return {name: Target(name=name, **data) for name, data in raw.items()}


def save_targets(targets: dict[str, Target]) -> None:
    import json

    TARGETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        name: {
            "pane": target.pane,
            "expected_command": target.expected_command,
            "note": target.note,
            "pane_id": target.pane_id,
            "pane_pid": target.pane_pid,
            "pane_start_time": target.pane_start_time,
            "foreground_pid": target.foreground_pid,
            "foreground_start_time": target.foreground_start_time,
            **({"shell": True} if target.shell else {}),
        }
        for name, target in sorted(targets.items())
    }
    tmp = TARGETS_FILE.with_suffix(TARGETS_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, TARGETS_FILE)


def process_start_time(pid: int) -> str:
    """Return Linux's process start token, or an empty string if unavailable."""
    try:
        # /proc/<pid>/stat field 22. The command field may contain spaces and
        # parentheses, so split only after its final closing parenthesis.
        tail = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        return tail[19]
    except (FileNotFoundError, IndexError, OSError, ValueError):
        return ""


def foreground_pid(pane_pid: int) -> int:
    """Return the pane tty's foreground process-group leader.

    tmux ``pane_pid`` is commonly the long-lived shell, so binding only that
    PID fails to notice a Claude/Codex child restart. Linux `/proc/<pid>/stat`
    field 8 is the terminal foreground process group (tpgid); its leader is
    the best dependency-free runtime handle available here.
    """
    try:
        tail = Path(f"/proc/{pane_pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        tpgid = int(tail[5])
        return tpgid if tpgid > 0 else pane_pid
    except (FileNotFoundError, IndexError, OSError, ValueError):
        return pane_pid


def pane_info(pane: str) -> dict[str, str | int]:
    fmt = (
        "#{session_name}:#{window_index}.#{pane_index}\t#{pane_id}\t#{pane_pid}\t"
        "#{pane_current_command}\t#{pane_current_path}\t#{pane_title}"
    )
    cp = tmux("display-message", "-p", "-t", pane, fmt, check=False)
    if cp.returncode != 0:
        raise SystemExit(f"target pane not found: {pane}\n{cp.stderr.strip()}")
    target, pane_id, raw_pid, command, cwd, title = cp.stdout.rstrip("\n").split("\t", 5)
    if not pane_id or not raw_pid.strip():
        # `display-message -t %N` for a pane that no longer exists can exit 0
        # with empty fields; that is a missing pane, not a tmux error.
        raise SystemExit(f"target pane not found: {pane}")
    try:
        pane_pid = int(raw_pid)
    except ValueError as exc:
        raise SystemExit(f"tmux returned an invalid pane pid for {pane}: {raw_pid}") from exc

    # `tmux display-message -t session:window.99` can silently resolve the
    # window and report its active pane. Registration and command injection
    # require an exact identity, never that permissive fallback.
    if pane.startswith("%"):
        exact = pane == pane_id
    elif re.fullmatch(r"[^:]+:\d+\.\d+", pane):
        exact = pane == target
    else:
        exact = True  # compatibility for explicit tmux aliases/window names
    if not exact:
        raise SystemExit(f"requested pane {pane} resolved to a different pane {target} ({pane_id})")
    active_pid = foreground_pid(pane_pid)
    return {
        "pane": target,
        "pane_id": pane_id,
        "pane_pid": pane_pid,
        "pane_start_time": process_start_time(pane_pid),
        "foreground_pid": active_pid,
        "foreground_start_time": process_start_time(active_pid),
        "command": command,
        "cwd": cwd,
        "title": title,
    }


def reregister_hint(name: str, pane: str) -> str:
    return (
        f"\nnext: if {pane} now runs the AI you intend, re-register it: "
        f"secretary-bus register --name {name} --pane {pane}"
        " (or pick another live pane: secretary-bus leader discover --json)"
    )


def target_info(target: Target) -> dict[str, str | int]:
    """Resolve a registered target and fail closed if its process changed."""
    try:
        return _target_info(target)
    except SystemExit as exc:
        message = str(exc.code if exc.code is not None else "")
        if "next: " in message:
            raise
        raise SystemExit(message + reregister_hint(target.name, target.pane_id or target.pane)) from None


def _target_info(target: Target) -> dict[str, str | int]:
    info = pane_info(target.pane_id or target.pane)
    if target.pane_id and info["pane_id"] != target.pane_id:
        raise SystemExit(
            f"target runtime changed: {target.name} expected pane_id={target.pane_id} actual={info['pane_id']}"
        )
    if target.pane_start_time and info["pane_start_time"] != target.pane_start_time:
        raise SystemExit(
            "target runtime changed: "
            f"{target.name} pane_id={info['pane_id']} process start token no longer matches"
        )
    if target.foreground_pid and info["foreground_pid"] != target.foreground_pid:
        raise SystemExit(
            "target runtime changed: "
            f"{target.name} pane_id={info['pane_id']} foreground pid was "
            f"{target.foreground_pid} now {info['foreground_pid']}"
        )
    if target.foreground_start_time and info["foreground_start_time"] != target.foreground_start_time:
        raise SystemExit(
            "target runtime changed: "
            f"{target.name} pane_id={info['pane_id']} foreground process start token no longer matches"
        )
    return info


def require_safe_text(text: str, allow_newline: bool) -> None:
    if not text:
        raise SystemExit("empty input text")
    if "\x00" in text:
        raise SystemExit("NUL byte is not allowed")
    if "\n" in text and not allow_newline:
        raise SystemExit("newline input is blocked; pass --allow-newline if this is intentional")
    for char in text:
        code = ord(char)
        if code < 32 and not (allow_newline and char == "\n"):
            raise SystemExit(f"control character U+{code:04X} is blocked")
        if code == 127:
            raise SystemExit("DEL control character is blocked")
    if len(text) > INLINE_TEXT_LIMIT:
        raise SystemExit(
            "input text too long for direct injection; pass it with --text-file (it is written to a "
            "task file and one pointer line is sent)"
        )


INLINE_TEXT_LIMIT = 4000
TASK_SPILL_DIR = BUS / "task-files"


def inline_or_spill(text: str, *, spill_dir: Path | None = None, label: str = "task") -> str:
    """Text to inject into a pane: the text itself when it is short and one line,
    otherwise a one-line pointer to a file holding the full text.

    Pasting long or multi-line prompts into a TUI is fragile (placeholders,
    accidental submits, 4000-char guard).  Writing the task to a file and
    injecting a single pointer line lets callers dispatch any size of task in
    one command.  The file name carries the content hash so the receiving AI
    and the caller can confirm it is the same text.
    """
    text = text.strip("\n")
    if len(text) <= INLINE_TEXT_LIMIT and "\n" not in text and "\t" not in text:
        return text
    body = (text + "\n").encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()[:16]   # of the exact bytes on disk: `sha256sum` agrees
    directory = spill_dir or (TASK_SPILL_DIR / time.strftime("%Y-%m-%d"))
    directory.mkdir(parents=True, exist_ok=True)
    safe_label = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-") or "task"
    path = directory / f"{safe_label}-{digest}.md"
    path.write_bytes(body)
    return (
        f"任务内容较长，已写入文件 {path} （sha256 前 16 位 {digest}）。"
        "请完整读取这个文件，文件内容就是本次任务，按其中要求执行。"
        f" / The full task is in {path}; read all of it and carry it out."
    )


def read_text_source(text: str | None, text_file: str | None) -> str:
    if text_file:
        if text_file == "-":
            value = sys.stdin.read()
        else:
            try:
                value = Path(text_file).read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise SystemExit(f"cannot read input text file: {text_file}: {exc}") from exc
        # A file source is how callers hand over long, multi-line tasks: spill
        # it and inject one pointer line instead of refusing.
        return inline_or_spill(value.rstrip("\n"), label=Path(text_file).stem if text_file != "-" else "stdin")
    return str(text or "")


def provider_snapshot(target_name: str) -> dict[str, object]:
    """Read exact/inferred provider state without importing a circular module."""
    try:
        cp = subprocess.run(
            [sys.executable, str(PROVIDER_STATE_SCRIPT), target_name, "--max-chars", "1600"],
            text=True,
            capture_output=True,
            timeout=min(COMMAND_TIMEOUT, 6.0),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if cp.returncode != 0:
        return {}
    try:
        payload = json.loads(cp.stdout)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _state_value(snapshot: dict[str, object]) -> str:
    state = snapshot.get("state")
    return str(state.get("value") or "unknown") if isinstance(state, dict) else "unknown"


def _new_connectivity_error(before: dict[str, object], after: dict[str, object], sent_text: str = "") -> str:
    old_output = str(before.get("last_output") or "").lower()
    new_output = str(after.get("last_output") or "").lower()
    if not new_output or new_output == old_output:
        return ""
    # A debugging task that mentions "connection refused" is echoed on screen;
    # that echo is not a provider error.
    sent = sent_text.lower()
    return next(
        (pattern for pattern in CONNECTIVITY_PATTERNS if pattern in new_output and pattern not in sent),
        "",
    )


def wait_for_provider_acceptance(
    target_name: str,
    before: dict[str, object],
    *,
    timeout: float,
    sent_text: str = "",
) -> dict[str, object] | None:
    deadline = time.monotonic() + max(0.2, timeout)
    before_assistant = str(before.get("last_assistant") or "")
    while time.monotonic() < deadline:
        current = provider_snapshot(target_name)
        if current:
            # A provider that is already working has accepted the task; only
            # then look for connection errors that appeared after submit.
            state = _state_value(current)
            if state in {"busy", "needs_input"}:
                return current
            connectivity_error = _new_connectivity_error(before, current, sent_text)
            if connectivity_error:
                raise SystemExit(
                    f"provider connectivity error after submit: {connectivity_error}; task start not confirmed"
                )
            current_assistant = str(current.get("last_assistant") or "")
            if current_assistant and current_assistant != before_assistant:
                return current  # A very short turn may complete between polls.
        time.sleep(max(0.05, min(VERIFY_INTERVAL, 1.0)))
    return None


def composer_contains_prompt(pane_id: str, text: str) -> bool:
    """Use only the live screen tail to decide whether one extra submit is safe."""
    captured = tmux("capture-pane", "-p", "-t", pane_id, check=False)
    if captured.returncode != 0:
        return False
    normalized_text = re.sub(r"\s+", "", text)
    if not normalized_text:
        return False
    # Look inside the provider's input box (not "the last 10 lines": Claude's
    # subagent panel below the footer can push the box out of that window).
    lines = pane_detectors.strip_agent_panel(captured.stdout.splitlines())
    split = pane_detectors.split_screen(lines)
    region = split.input_lines if split.has_input_region else lines[-10:]
    # A long paste is shown as a placeholder instead of its text.
    if any("[Pasted text #" in line for line in region):
        return True
    needle = normalized_text[-min(24, len(normalized_text)):]
    screen_tail = "".join(re.sub(r"\s+", "", line) for line in region)
    return len(needle) >= 8 and needle in screen_tail


def auto_approve_enabled() -> bool:
    """Opt-in auto-answer of permission-type dialogs (see dialogs.py).

    ``AGENT_BUS_AUTO_APPROVE=1|0`` overrides; otherwise the bus config key
    ``auto_approve_permissions`` decides (default off).
    """
    override = os.environ.get("AGENT_BUS_AUTO_APPROVE", "").strip()
    if override in {"0", "1"}:
        return override == "1"
    try:
        import bus_config  # imported lazily: bus_config imports this module

        return bool(bus_config.load_config().get("auto_approve_permissions", False))
    except (OSError, ValueError, SystemExit) as exc:
        print(f"warning: cannot read bus config ({exc}); not auto-answering prompts", file=sys.stderr)
        return False


def tmux_query(args: list[str]) -> str:
    """stdout of one tmux command ('' when it fails); the query hook of dialogs."""
    cp = tmux(*args, check=False)
    return cp.stdout if cp.returncode == 0 else ""


def auto_answer_prompt(pane_id: str) -> dict[str, str] | None:
    """Answer a permission-type dialog on ``pane_id`` by the standing policy (see dialogs)."""
    try:
        return dialogs.auto_approve(
            capture=lambda: tmux("capture-pane", "-p", "-t", pane_id, check=False).stdout,
            send=lambda key: tmux("send-keys", "-t", pane_id, key),
            pane_id=pane_id,
            tmux=tmux_query,
        )
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(f"could not auto-answer the open dialog on {pane_id}: {exc}") from None


def pane_runs_ai(foreground_pid: object) -> bool:
    """Whether a Claude/Codex process runs under the pane's foreground process.

    A wrapper such as ai-session-shell keeps its pid when the AI exits and
    ``exec``s a login shell, so the frozen identity still matches; only the
    process tree shows that text would now be run by bash."""
    import provider_state  # imported lazily: provider_state imports this module

    try:
        root = int(str(foreground_pid or "0"))
    except ValueError:
        return False
    return any(
        provider_state._provider_of(provider_state.process_cmdline([pid])) != "unknown"
        for pid in provider_state.process_tree_pids(root)
    )


def send_to_target(
    target: Target,
    text: str,
    enter: bool,
    yes: bool,
    allow_newline: bool,
    show_text: bool = False,
    *,
    delivery_mode: str = "dispatch",
    allow_shell: bool = False,
) -> dict[str, object]:
    require_safe_text(text, allow_newline=allow_newline)
    info = target_info(target)
    if info["command"] != target.expected_command:
        raise SystemExit(
            "target command mismatch: "
            f"{target.name} pane={info['pane']} expected={target.expected_command} actual={info['command']}"
        )
    print(f"target: {target.name} pane={info['pane']} command={info['command']} cwd={info['cwd']}")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    if (not yes) or show_text:
        print("input:")
        print(text)
        print("input_repr:")
        print(repr(text))
    else:
        print(f"input: <redacted on send; chars={len(text)} sha256={digest}>")
    print(f"enter: {str(enter).lower()}")
    if not yes:
        print("dry-run only; add --yes to send")
        return {"sent": False, "verified": False, "delivery": "dry-run"}

    before = provider_snapshot(target.name) if enter else {}
    provider = str(before.get("provider") or "unknown")
    before_state = _state_value(before)
    if (
        enter
        and provider not in {"codex", "claude"}
        and not (allow_shell or target.shell)
        and not pane_runs_ai(info.get("foreground_pid"))
    ):
        raise SystemExit(
            f"no Claude/Codex process runs in {target.name} ({info['pane_id']}); the text would be executed by a shell. "
            "Restart the AI in that window (scripts/agent_window.sh restart / recovery relaunch), "
            "pass --allow-shell for this send, or register the pane with --shell if it is a shell on purpose."
        )
    if provider in {"codex", "claude"} and before_state == "needs_input" and auto_approve_enabled():
        # The owner pre-authorised permission-type prompts (dialogs.py); work
        # questions are left for the user and still refused below.
        answered = auto_answer_prompt(str(info["pane_id"]))
        if answered:
            print(f"auto_answered: kind={answered['kind']} answer={answered['answer']!r}")
            time.sleep(0.5)
            before = provider_snapshot(target.name) or before
            before_state = _state_value(before)
    if provider in {"codex", "claude"} and before_state == "needs_input":
        # Pasting text and pressing Enter here would confirm whatever option is
        # highlighted in the open prompt (e.g. change a global permission mode).
        raise SystemExit(
            "target provider is needs_input (a permission prompt or dialog is open); refusing to type into it. "
            "Answer it deliberately (secretary-bus leader approve|keys, or ask the user), then retry."
        )
    if delivery_mode == "dispatch" and provider in {"codex", "claude"} and before_state == "busy":
        raise SystemExit(
            "target provider is busy; refusing a new dispatch that would be queued. "
            "Use leader steer/continue for an existing job, or pass send --queue only when queuing is intentional."
        )

    tmux_delivery.paste_and_submit(
        str(info["pane_id"]),
        text,
        enter,
        timeout=COMMAND_TIMEOUT,
        submit_delay=SUBMIT_DELAY,
    )
    if not enter:
        print("sent")
        print("delivery: terminal verified=false")
        return {"sent": True, "verified": False, "delivery": "terminal", "provider": provider}

    if provider not in {"codex", "claude"}:
        print("sent")
        print("delivery: terminal verified=false provider=unknown")
        return {"sent": True, "verified": False, "delivery": "terminal", "provider": provider}

    if delivery_mode == "followup" and before_state == "busy":
        print("sent")
        print(f"delivery: queued-or-steered verified=false provider={provider}")
        return {"sent": True, "verified": False, "delivery": "queued-or-steered", "provider": provider}

    accepted = wait_for_provider_acceptance(target.name, before, timeout=VERIFY_TIMEOUT, sent_text=text)
    retried = False
    if accepted is None and composer_contains_prompt(str(info["pane_id"]), text):
        retried = True
        print("submit_retry: 1 reason=prompt-still-in-composer")
        tmux("send-keys", "-t", str(info["pane_id"]), "C-m")
        accepted = wait_for_provider_acceptance(
            target.name, before, timeout=max(2.0, VERIFY_TIMEOUT / 2), sent_text=text
        )
    if accepted is None:
        # Transport succeeded, but a bounded observation window did not see a
        # provider transition.  This is deliberately *not* an error: Codex
        # can be wrapped by bash and provider-state can be inferred/stale.
        # Reporting it as a failure invites callers to resend the same prompt.
        # Keep the durable job in its non-terminal ``sent`` state and let the
        # event-first watcher observe the next real transition.
        print("sent")
        print(
            "delivery: submitted-pending-confirmation verified=false "
            f"provider={provider}; do not resend; inspect provider-state or event-watch"
        )
        return {
            "sent": True,
            "verified": False,
            "delivery": "submitted-pending-confirmation",
            "provider": provider,
            "state": "pending_confirmation",
            "submit_retry": int(retried),
        }
    state = _state_value(accepted)
    print("sent")
    print(f"delivery: accepted verified=true provider={provider} state={state} retry={int(retried)}")
    return {
        "sent": True,
        "verified": True,
        "delivery": "accepted",
        "provider": provider,
        "state": state,
        "submit_retry": int(retried),
    }


def cmd_tmux_list(_args: argparse.Namespace) -> None:
    fmt = "#{session_name}:#{window_index}.#{pane_index}\t#{pane_current_command}\t#{pane_current_path}\t#{pane_title}"
    cp = tmux("list-panes", "-a", "-F", fmt, check=False)
    if cp.returncode != 0:
        raise SystemExit(cp.stderr.strip() or "tmux list-panes failed")
    print(cp.stdout.rstrip())


def cmd_register(args: argparse.Namespace) -> None:
    info = pane_info(args.pane)
    expected = args.expected_command or info["command"]
    if args.expected_command and args.expected_command != info["command"]:
        print(
            f"warning: {args.pane} currently runs {info['command']!r}, not {args.expected_command!r}; "
            "sends will be refused until they match (omit --expected-command to record the live command)",
            file=sys.stderr,
        )
    with targets_locked():
        targets = load_targets()
        targets[args.name] = Target(
            name=args.name,
            pane=str(info["pane"]),
            expected_command=str(expected),
            note=args.note or "",
            pane_id=str(info["pane_id"]),
            pane_pid=int(info["pane_pid"]),
            pane_start_time=str(info["pane_start_time"]),
            foreground_pid=int(info["foreground_pid"]),
            foreground_start_time=str(info["foreground_start_time"]),
            shell=bool(args.shell),
        )
        save_targets(targets)
    print(f"registered {args.name}: pane={info['pane']} expected_command={expected}" + (" shell=true" if args.shell else ""))


def target_status(target: Target) -> str:
    try:
        info = _target_info(target)
    except SystemExit as exc:
        message = str(exc.code if exc.code is not None else "")
        if "runtime changed" in message:
            return "process-changed"
        if "pane not found" in message:
            return "missing"
        return "error"
    return "ok" if info["command"] == target.expected_command else f"command-mismatch:{info['command']}"


def cmd_targets(args: argparse.Namespace) -> None:
    targets = load_targets()
    if not targets:
        print(f"(no targets; register one with --pane. target file: {TARGETS_FILE})")
        return
    statuses = {name: target_status(target) for name, target in targets.items()}
    for name, target in targets.items():
        print(f"{target.name}\tpane={target.pane}\texpected={target.expected_command}\tstatus={statuses[name]}\tnote={target.note}")
    if not getattr(args, "prune", False):
        stale = sum(1 for value in statuses.values() if value == "missing")
        if stale:
            print(f"({stale} target(s) point at panes that no longer exist; `secretary-bus targets --prune --yes` removes them)")
        return
    # Only targets whose pane is gone are pruned; a changed process may be a
    # legitimate restart the owner wants to re-register under the same name.
    doomed = [name for name, value in statuses.items() if value == "missing"]
    if not args.yes:
        print(f"prune dry-run: would remove {len(doomed)} missing target(s); add --yes to apply")
        return
    with targets_locked():
        current = load_targets()
        backup = TARGETS_FILE.with_name(f"{TARGETS_FILE.name}.bak.{time.strftime('%Y%m%d-%H%M%S')}")
        if TARGETS_FILE.exists():
            backup.write_text(TARGETS_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        for name in doomed:
            current.pop(name, None)
        save_targets(current)
    print(f"pruned {len(doomed)} missing target(s); backup: {backup}")


def cmd_send(args: argparse.Namespace) -> int:
    targets = load_targets()
    if args.target not in targets:
        raise SystemExit(
            f"unknown target: {args.target}\nnext: secretary-bus register --name {args.target} --pane %ID "
            "(find the pane with secretary-bus leader discover --json)"
        )
    text = read_text_source(args.text, args.text_file)
    receipt = send_to_target(
        targets[args.target],
        text,
        enter=args.enter,
        yes=args.yes,
        allow_newline=args.allow_newline,
        show_text=args.show_text,
        delivery_mode="followup" if args.queue else "dispatch",
        allow_shell=bool(getattr(args, "allow_shell", False)),
    )
    # A pending observation is neither a transport failure nor proof that the
    # task started.  Give shell callers a distinct retry-later code while the
    # in-process supervisor records the same receipt as a durable ``sent``
    # job instead of turning it into a false failure.
    return 75 if receipt.get("delivery") == "submitted-pending-confirmation" else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Secretary Bus: safe direct tmux CLI input bridge.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("tmux-list").set_defaults(fn=cmd_tmux_list)
    p = sub.add_parser("targets")
    p.add_argument("--prune", action="store_true", help="Remove targets whose pane no longer exists")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_targets)

    p = sub.add_parser("register")
    p.add_argument("--name", required=True)
    p.add_argument("--pane", required=True)
    p.add_argument("--expected-command", default="")
    p.add_argument("--note", default="")
    p.add_argument("--shell", action="store_true", help="The pane is a plain shell on purpose; sent text runs as commands")
    p.set_defaults(fn=cmd_register)

    p = sub.add_parser("send")
    p.add_argument("--target", required=True)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--text")
    source.add_argument("--text-file", help="UTF-8 file, or - to read stdin")
    p.add_argument("--enter", action="store_true")
    p.add_argument("--allow-newline", action="store_true")
    p.add_argument("--show-text", action="store_true", help="Show full input even when --yes sends it")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--queue", action="store_true", help="Explicitly allow a follow-up to a busy AI target")
    p.add_argument(
        "--allow-shell", action="store_true", help="Allow Enter when no Claude/Codex runs in the pane (text goes to a shell)"
    )
    p.set_defaults(fn=cmd_send)

    args = parser.parse_args()
    result = args.fn(args)
    return int(result) if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
