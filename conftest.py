"""Test isolation for the whole repository.

* Every tmux command issued by the tests (directly or by the code under test)
  goes to a private tmux server: ``TMUX_TMPDIR`` points at a temporary
  directory and ``$TMUX`` / ``$TMUX_PANE`` are removed, so a developer's real
  tmux server is never touched even when pytest runs inside a tmux pane.
* ``HOME`` and ``HISTFILE`` point into the temporary directory, and
  ``AGENT_BUS_DIR`` / ``AGENT_BUS_SNAPSHOT_DIR`` are always temporary, so no
  test reads or writes the developer's ``~/.codex``, ``~/.claude`` or shell
  history.  Git identity comes from ``GIT_*`` variables instead of
  ``~/.gitconfig``.
* ``claude`` and ``codex`` on ``PATH`` are stubs that exit with an error, so
  no test can start a real AI CLI (tests that need a fake CLI put their own
  stub first on ``PATH``).
* ``SECRETARY_ENSURE_TMUX`` (the boot helper that closes the placeholder
  window after recovery) points at a path that does not exist, so recovery
  code under test can never run it.
* Auto-approve switches from the developer's environment are cleared so tests
  see the documented defaults.

These variables are set at import time (before any test module imports the bus
modules, which read them once at import).
"""
from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
_TMP = Path(tempfile.mkdtemp(prefix="agent-bus-tests-"))

for _key in ("TMUX", "TMUX_PANE", "AGENT_BUS_AUTO_APPROVE", "CARDS_AUTO_APPROVE", "SECRETARY_TMUX_SOCKET",
             "SECRETARY_BUS_TMUX_SOCKET"):
    os.environ.pop(_key, None)
os.environ["TMUX_TMPDIR"] = str(_TMP / "tmux")
(_TMP / "tmux").mkdir()
(_TMP / "home").mkdir()
os.environ["HOME"] = str(_TMP / "home")
os.environ["HISTFILE"] = str(_TMP / "home" / ".bash_history")
os.environ["AGENT_BUS_DIR"] = str(_TMP / "bus")
os.environ["AGENT_BUS_SNAPSHOT_DIR"] = str(_TMP / "snapshots")
for _key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "AGENT_BUS_DEFAULT_CODEX_HOME", "AGENT_BUS_CODEX_HOMES_ROOT",
             "AGENT_BUS_DASHBOARD_STATE_DIR", "AGENT_CLI_TARGETS", "AGENT_EVENT_LEDGER_DIR", "WEBTERM_ENV",
             "TMUX_CARD_USER", "TMUX_CARD_PASS"):
    os.environ.pop(_key, None)
for _key, _value in (("GIT_AUTHOR_NAME", "Agent Bus Tests"), ("GIT_AUTHOR_EMAIL", "tests@example.invalid"),
                     ("GIT_COMMITTER_NAME", "Agent Bus Tests"), ("GIT_COMMITTER_EMAIL", "tests@example.invalid")):
    os.environ[_key] = _value
_STUBS = _TMP / "stub-bin"
_STUBS.mkdir()
for _name in ("claude", "codex"):
    _stub = _STUBS / _name
    _stub.write_text(f"#!/bin/sh\necho 'test stub: the real {_name} CLI is not used in tests' >&2\nexit 97\n")
    _stub.chmod(0o755)
os.environ["PATH"] = f"{_STUBS}{os.pathsep}{os.environ.get('PATH', '')}"
os.environ["SECRETARY_ENSURE_TMUX"] = str(_TMP / "no-ensure-tmux-session")

for _path in (REPO / "scripts", REPO / "dashboard"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


def _cleanup() -> None:
    socket = _TMP / "tmux" / f"tmux-{os.getuid()}" / "default"
    if socket.exists() and shutil.which("tmux"):
        subprocess.run(["tmux", "-S", str(socket), "kill-server"], capture_output=True, check=False, timeout=10)
    shutil.rmtree(_TMP, ignore_errors=True)


atexit.register(_cleanup)
