#!/usr/bin/env python3
"""Read, classify and answer the dialogs Claude Code / Codex draw over their input box.

A real dialog (permission prompt, folder-trust question, settings offer,
AskUserQuestion) replaces the provider's input box, so a dialog is only read
from a screen whose input box is gone (``pane_detectors``).  Options may be
numbered (``❯ 1. Yes``) or not (``❯ Yes, …`` / ``  No, …``).

Opt-in policy (off by default; see ``cli_bridge.auto_approve_enabled``): when
it is enabled, in every Claude/Codex window that Agent Bus drives or the Cards
dashboard shows, *permission-type* dialogs (tool permission, folder trust,
hooks review, auto-mode offer, and the plan-mode "execute this plan?" approval
of Claude and Codex) are answered automatically with the most permissive
option.  Questions about the work itself (Claude AskUserQuestion, Codex
request_user_input), account/billing choices and anything unrecognised are
never auto-answered.

Codex draws its dialogs without a frame, so the lines above the options are
conversation history.  Only the trailing run of numbered options (1..n) is an
option list, and only the few lines right above it are the question.

Answering never presses a blind Enter: the highlight is moved one row at a
time and the screen is re-read after every step; Enter is pressed only when
the highlighted row is the chosen option, and the dialog must be gone (or
replaced by the next one) afterwards.  The dialog is pinned when driving
starts: if its question or options change, driving stops.

Several senders may drive the same pane (leader tick, delivery, the Cards
background loop, a click on the Cards page).  They serialise on one
non-blocking per-pane lock; the automatic senders also stay away from a pane
in tmux copy mode or one a person typed into in the last few seconds.
"""
from __future__ import annotations

import fcntl
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator

import event_ledger
import pane_detectors

NUMBERED_RE = re.compile(r"^\s*(?:[│|]\s*)?([❯›]?)\s*(?:(?:☐|☑|☒|\[[ xX✓]\])\s*)?(\d+)[.)]\s+(.+?)\s*[│|]?\s*$")
HIGHLIGHT_RE = re.compile(r"^(\s*)[❯›]\s+(\S.*?)\s*$")
RULE_RE = re.compile(r"^\s*[─━═▔]{8,}")
HINT_RE = re.compile(
    r"(enter to|esc to|tab to|to select|to confirm|to cancel|to navigate|↑|↓|space to|press \d|ctrl\+"
    r"|enter select|esc back)",
    re.I,
)

KIND_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("mode-offer", re.compile(r"make auto mode your default permission mode", re.I)),
    # Claude plan mode: "Claude has written up a plan and is ready to execute.
    # Would you like to proceed?" (a permission-type approval of the plan).
    # Codex plan mode: "Implement this plan?".
    (
        "plan-approval",
        re.compile(r"written up a plan|ready to execute|approve (?:this|the) plan|exit plan mode|implement this plan\?", re.I),
    ),
    ("hooks-review", re.compile(r"hooks? (?:need|needs|require|requires) review", re.I)),
    (
        "trust",
        re.compile(
            r"do you trust|one you trust|trust (?:the |this )?(?:files|contents|folder|directory|workspace)"
            r"|accessing workspace",
            re.I,
        ),
    ),
    (
        "permission",
        re.compile(
            r"do you want to (?:proceed|make (?:this|these) edits?|create|run|allow|delete|overwrite|fetch)"
            r"|would you like to (?:run|make|apply|allow|grant|send input)|allow (?:this|the following|command)"
            r"|requires? (?:approval|permission)|approve (?:this|the following)|permission to",
            re.I,
        ),
    ),
)

# Preferred answers per kind, most permissive first.  Each is matched against
# the option text; the first pattern with a match wins.
ANSWER_POLICY: dict[str, tuple[re.Pattern[str], ...]] = {
    "mode-offer": (re.compile(r"keep bypass", re.I),),
    "plan-approval": (
        re.compile(r"bypass|auto-accept|don.t ask", re.I),
        re.compile(r"^yes", re.I),
    ),
    "hooks-review": (re.compile(r"trust all|approve all|always", re.I), re.compile(r"^(?:trust|yes|approve)", re.I)),
    "trust": (re.compile(r"^(?:yes|i trust|trust)", re.I), re.compile(r"proceed|continue", re.I)),
    "permission": (
        re.compile(r"don.t ask again|always allow|allow all|for (?:this|the) session|yes, and", re.I),
        re.compile(r"^(?:yes|allow|approve|proceed|run)", re.I),
    ),
}
NEGATIVE_RE = re.compile(r"^(?:no\b|deny|reject|cancel|don.t allow|abort)", re.I)
# Rows and footers only drawn by work questions (Claude AskUserQuestion, Codex
# request_user_input).  Such a dialog is never a permission prompt, whatever
# its question says ("Do you want to proceed with the migration?").
WORK_QUESTION_OPTION_RE = re.compile(r"^(?:type something|chat about this|none of the above)\b", re.I)
WORK_QUESTION_TEXT_RE = re.compile(
    r"question \d+/\d+|navigate questions|type your answer|to add notes|ready to submit your answers", re.I
)
# Account, login and billing choices ("Do you want to use this API key?") are
# not permissions; the user decides them.
ACCOUNT_RE = re.compile(r"api key|log ?in\b|sign in|billing|subscription|payment|credits?\b|telemetry", re.I)
QUESTION_LINES = 8


@dataclass
class Dialog:
    question: str
    options: list[str]
    selected: int = -1
    numbers: list[int] = field(default_factory=list)
    kind: str = "question"
    footer: str = ""

    @property
    def numbered(self) -> bool:
        return len(self.numbers) == len(self.options) and bool(self.numbers)

    @property
    def headline(self) -> str:
        """The question line that decided the kind, else the line nearest the options."""
        lines = self.question.splitlines()
        pattern = dict(KIND_PATTERNS).get(self.kind)
        hit = next((line for line in lines if pattern and pattern.search(line)), None)
        return hit or (lines[-1] if lines else "")


def _trailing_region(lines: list[str]) -> list[str]:
    region: list[str] = []
    for line in reversed(lines):
        if RULE_RE.match(line):
            break
        region.insert(0, line)
        if len(region) >= 30:
            break
    return region


def read_dialog(screen: str) -> Dialog | None:
    """The dialog that owns the bottom of this screen, or None."""
    lines = pane_detectors.strip_agent_panel(screen.splitlines())
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or pane_detectors.split_screen(lines).has_input_region:
        return None
    region = _trailing_region(lines)
    numbered = [(i, NUMBERED_RE.match(line)) for i, line in enumerate(region)]
    numbered = [(i, m) for i, m in numbered if m]
    if numbered:
        # The option list is the trailing run numbered 1..n; numbered lines
        # further up (a plan's steps, earlier answers) are history.
        block = [numbered[-1]]
        for i, m in reversed(numbered[:-1]):
            if int(block[0][1].group(2)) <= 1 or int(m.group(2)) != int(block[0][1].group(2)) - 1:
                break
            block.insert(0, (i, m))
        numbered = block
    if len(numbered) >= 2:
        first = numbered[0][0]
        options = [m.group(3).strip() for _i, m in numbered]
        numbers = [int(m.group(2)) for _i, m in numbered]
        selected = next((k for k, (_i, m) in enumerate(numbered) if m.group(1)), -1)
        question_lines = region[:first]
        footer_lines = region[numbered[-1][0] + 1:]
        # A dialog always highlights one row and ends with key hints (or the
        # last option's own description, indented deeper).  A numbered list in
        # a reply, or a draft "› 1. …" typed into Codex's input box, does not.
        column = numbered[-1][1].start(2)
        if selected < 0 or any(
            line.strip() and not HINT_RE.search(line) and len(line) - len(line.lstrip()) <= column
            for line in footer_lines
        ):
            return None
    else:
        hi = next((i for i in range(len(region) - 1, -1, -1) if HIGHLIGHT_RE.match(region[i])), None)
        if hi is None:
            return None
        col = len(HIGHLIGHT_RE.match(region[hi]).group(1))

        def option_text(line: str) -> str | None:
            m = HIGHLIGHT_RE.match(line)
            if m and len(m.group(1)) == col:
                return m.group(2)
            indent = len(line) - len(line.lstrip(" "))
            return line.strip() if line.strip() and indent == col + 2 else None

        start = hi
        while start > 0 and option_text(region[start - 1]) is not None:
            start -= 1
        end = hi
        while end + 1 < len(region) and option_text(region[end + 1]) is not None:
            end += 1
        if any(line.strip() and not HINT_RE.search(line) for line in region[end + 1:]):
            return None
        options = [option_text(line) or "" for line in region[start:end + 1]]
        if len(options) < 2:
            return None
        numbers = []
        selected = hi - start
        question_lines = region[:start]
        footer_lines = region[end + 1:]
    kept = [line.strip() for line in question_lines if line.strip() and not HINT_RE.search(line)]
    question = "\n".join(kept[-QUESTION_LINES:])
    footer = "\n".join(line.strip() for line in footer_lines if line.strip())
    dialog = Dialog(question=question, options=options, selected=selected, numbers=numbers, footer=footer)
    dialog.kind = classify(dialog)
    return dialog


TRUST_OPTION_RE = re.compile(r"trust this (?:folder|directory|workspace|project)", re.I)


def classify(dialog: Dialog) -> str:
    if any(WORK_QUESTION_OPTION_RE.search(option) for option in dialog.options) or WORK_QUESTION_TEXT_RE.search(
        f"{dialog.question}\n{dialog.footer}"
    ):
        return "question"
    text = dialog.question
    if ACCOUNT_RE.search(text):
        return "question"
    for kind, pattern in KIND_PATTERNS:
        if pattern.search(text):
            return kind
    # Folder-trust wording changes between versions; its answer row is stable.
    # Only this narrow option text is used — a work question's options never decide.
    if any(TRUST_OPTION_RE.search(option) for option in dialog.options):
        return "trust"
    return "question"


def auto_answer_index(dialog: Dialog) -> int | None:
    """Index of the option the standing policy picks, or None when the user must decide."""
    for pattern in ANSWER_POLICY.get(dialog.kind, ()):
        for index, text in enumerate(dialog.options):
            if pattern.search(text) and (dialog.kind == "mode-offer" or not NEGATIVE_RE.search(text)):
                return index
    return None


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text).lower().rstrip("…").rstrip(".")


def match_option(options: list[str], option_text: str) -> int | None:
    """Index of ``option_text`` among ``options``: exact first, else the longest
    option that is a prefix of it or that it prefixes (screens truncate long
    rows).  "Yes" must not match "Yes, and don't ask again …"."""
    want = _norm(option_text)
    norms = [_norm(text) for text in options]
    if want in norms:
        return norms.index(want)
    candidates = [(len(n), i) for i, n in enumerate(norms) if n and (n.startswith(want) or want.startswith(n))]
    if not candidates:
        return None
    best = max(candidates)
    return best[1] if sum(1 for length, _i in candidates if length == best[0]) == 1 else None


class PaneBusy(RuntimeError):
    """Another sender holds this pane, or a person is using it; nothing was sent."""


TmuxQuery = Callable[[list[str]], str]
LOCK_DIR = event_ledger.BUS / "locks"
HUMAN_ACTIVE_SECONDS = float(os.environ.get("AGENT_BUS_HUMAN_ACTIVE_SECONDS", "20"))
CONFIRM_WAIT = 1.5


@contextmanager
def pane_lock(pane_id: str) -> Iterator[None]:
    """Hold the per-pane sender lock; raise PaneBusy when someone else holds it."""
    if not pane_id:
        yield
        return
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", pane_id)
    with open(LOCK_DIR / f"pane-{name}.lock", "a+", encoding="utf-8") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PaneBusy(f"another sender is driving {pane_id}; nothing sent") from None
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def pane_hands_off(pane_id: str, tmux: TmuxQuery, *, human: bool = True, now: float | None = None) -> str:
    """Why keys must not go to ``pane_id`` right now ('' when it is free).

    ``tmux(args)`` returns the stdout of one tmux command on the pane's server.
    A pane in copy mode would swallow the keys.  With ``human``, a pane that a
    person typed into (an attached client showing it was active recently) is
    left alone too."""
    if tmux(["display-message", "-p", "-t", pane_id, "#{pane_in_mode}"]).strip() == "1":
        return "the pane is in tmux copy mode"
    if human:
        now = time.time() if now is None else now
        for row in tmux(["list-clients", "-F", "#{client_activity}\t#{pane_id}"]).splitlines():
            activity, _sep, shown = row.partition("\t")
            if shown.strip() == pane_id and activity.strip().isdigit():
                idle = now - int(activity)
                if idle < HUMAN_ACTIVE_SECONDS:
                    return f"someone typed in this window {max(0, int(idle))}s ago"
    return ""


def _fingerprint(dialog: Dialog) -> tuple[str, str, tuple[str, ...]]:
    return dialog.kind, dialog.headline, tuple(_norm(text) for text in dialog.options)


def _drive_locked(
    option_text: str,
    *,
    capture: Callable[[], str],
    send: Callable[[str], None],
    max_steps: int,
    pause: float,
    sleep: Callable[[float], None],
) -> None:
    if not _norm(option_text):
        raise ValueError("missing option")
    pinned: tuple[str, str, tuple[str, ...]] | None = None
    moves = 0

    def stop(reason: str) -> ValueError:
        sent = f"{moves} navigation key(s) were sent, Enter was not" if moves else "nothing sent"
        return ValueError(f"{reason}; {sent}")

    for _step in range(max_steps):
        dialog = read_dialog(capture())
        if dialog is None:
            raise stop("the dialog is no longer on screen")
        if pinned is None:
            pinned = _fingerprint(dialog)
        elif _fingerprint(dialog) != pinned:
            raise stop("a different dialog is on screen now")
        target = match_option(dialog.options, option_text)
        if target is None:
            raise stop("the option is not in this dialog")
        if dialog.selected == target:
            send("C-m")
            waited = 0.0
            while waited < CONFIRM_WAIT:
                sleep(pause)
                waited += pause
                after = read_dialog(capture())
                if after is None or _fingerprint(after) != pinned:
                    return
            raise RuntimeError("Enter was sent but the same dialog is still open")
        send("Down" if target > dialog.selected else "Up")
        moves += 1
        sleep(pause)
    raise RuntimeError(f"could not move the highlight to the chosen option ({moves} keys sent)")


def drive(
    option_text: str,
    *,
    capture: Callable[[], str],
    send: Callable[[str], None],
    max_steps: int = 12,
    pause: float = 0.15,
    sleep: Callable[[float], None] = time.sleep,
    pane_id: str = "",
    tmux: TmuxQuery | None = None,
) -> None:
    """Select ``option_text`` in the dialog on screen and confirm it, verifying every step.

    With ``pane_id`` the per-pane lock is held while driving (PaneBusy when
    taken); with ``tmux`` as well a pane in copy mode is refused.  This is the
    path for an explicit choice by a person or a leader, so recent typing in
    the window does not block it."""
    with pane_lock(pane_id):
        if pane_id and tmux is not None:
            reason = pane_hands_off(pane_id, tmux, human=False)
            if reason:
                raise PaneBusy(f"{reason}; nothing sent")
        _drive_locked(option_text, capture=capture, send=send, max_steps=max_steps, pause=pause, sleep=sleep)


def auto_approve(
    *,
    capture: Callable[[], str],
    send: Callable[[str], None],
    sleep: Callable[[float], None] = time.sleep,
    pane_id: str = "",
    tmux: TmuxQuery | None = None,
) -> dict[str, str] | None:
    """Answer a permission-type dialog on screen by policy.

    Returns ``{"kind", "question", "prompt", "answer"}`` (``question`` is the
    headline, ``prompt`` the lines above the options, kept for audit) when one
    was answered, ``None`` when there is no dialog or it is a question the
    user must answer.  Raises PaneBusy (nothing sent) when another sender holds
    the pane, it is in copy mode, or a person typed into it just now; ValueError
    / RuntimeError when driving stopped.
    """
    with pane_lock(pane_id):
        if pane_id and tmux is not None:
            reason = pane_hands_off(pane_id, tmux)
            if reason:
                raise PaneBusy(f"{reason}; nothing sent")
        dialog = read_dialog(capture())
        if dialog is None:
            return None
        index = auto_answer_index(dialog)
        if index is None:
            return None
        answer = dialog.options[index]
        _drive_locked(answer, capture=capture, send=send, max_steps=12, pause=0.15, sleep=sleep)
        return {"kind": dialog.kind, "question": dialog.headline, "prompt": dialog.question, "answer": answer}
