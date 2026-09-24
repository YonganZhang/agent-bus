#!/usr/bin/env python3
"""Regenerate the README screenshots (docs/images/*.png) from synthetic content only.

Manual script (not collected by pytest).  Everything it shows is made up here:

* a private tmux server on its own socket under the work directory, never your
  real tmux server;
* a throw-away HOME with synthetic Git projects, a synthetic Claude transcript
  and a synthetic task plan, so the dashboard cannot read your real
  ``~/.claude`` / ``~/.codex``;
* fake ``claude`` / ``codex`` executables on PATH that only print a static
  screen (``claude agents --json`` answers ``[]``);
* the plan integration served by ``tests/dashboard/fixtures/fake_plan_cli.py``;
* random Basic Auth credentials that exist only for this run.

Run:  python3 tests/e2e/readme_screenshots.py [--out docs/images] [--workdir _tmp/readme-shots]
Needs tmux, git, Playwright for Python and its Chromium.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FAKE_PLAN_CLI = REPO / "tests" / "dashboard" / "fixtures" / "fake_plan_cli.py"
SESSION = "demo"
ESC = "\x1b"

CLAUDE_SCREEN = f"""{ESC}[38;5;174m*{ESC}[0m {ESC}[1mWelcome to Claude Code{ESC}[0m
  {ESC}[2mcwd: ~/projects/parser-demo{ESC}[0m

{ESC}[2m>{ESC}[0m Add a --json flag to the report command and cover it with a test.

{ESC}[1;37m⏺{ESC}[0m I'll add the flag, then extend the report tests.

{ESC}[1;32m⏺{ESC}[0m {ESC}[1mUpdate{ESC}[0m(src/report.py)
  ⎿  Updated src/report.py with 18 additions and 2 removals
     {ESC}[32m+    parser.add_argument("--json", action="store_true"){ESC}[0m
     {ESC}[32m+    if args.json:{ESC}[0m
     {ESC}[32m+        print(json.dumps(summary, indent=2)){ESC}[0m
     {ESC}[31m-    print(format_table(summary)){ESC}[0m

{ESC}[1;32m⏺{ESC}[0m {ESC}[1mBash{ESC}[0m(python -m pytest -q tests/test_report.py)
  ⎿  {ESC}[32m........{ESC}[0m
     {ESC}[32m8 passed in 0.41s{ESC}[0m

{ESC}[1;37m⏺{ESC}[0m Done: `report --json` prints one object per section; 8 tests pass.

{ESC}[2m─────────────────────────────────────────────────────{ESC}[0m
>
{ESC}[2m─────────────────────────────────────────────────────{ESC}[0m
  {ESC}[38;5;174m⏵⏵ accept edits on{ESC}[0m {ESC}[2m(shift+tab to cycle){ESC}[0m
"""

CODEX_SCREEN = f"""{ESC}[2m╭──────────────────────────────────────────────╮{ESC}[0m
{ESC}[2m│{ESC}[0m {ESC}[1m>_ OpenAI Codex{ESC}[0m                               {ESC}[2m│{ESC}[0m
{ESC}[2m│{ESC}[0m directory: ~/projects/data-pipeline          {ESC}[2m│{ESC}[0m
{ESC}[2m╰──────────────────────────────────────────────╯{ESC}[0m

{ESC}[1m›{ESC}[0m Why does the nightly lint job fail?

{ESC}[36m•{ESC}[0m The lint step fails on an unused import in {ESC}[36msrc/load.py{ESC}[0m.
  Removing it makes {ESC}[1mruff check{ESC}[0m and the unit tests pass locally.

{ESC}[1m›{ESC}[0m

  {ESC}[2m? for shortcuts                              92% context left{ESC}[0m
"""

SHELL_SCREEN = f"""$ make test
python -m pytest -q
{ESC}[32m........................................{ESC}[0m {ESC}[32m[ 62%]{ESC}[0m
{ESC}[32m........................{ESC}[0m                 {ESC}[32m[100%]{ESC}[0m
{ESC}[32m64 passed in 3.12s{ESC}[0m
$ git log --oneline -3
{ESC}[33m4e1f2a9{ESC}[0m docs: explain the site layout
{ESC}[33m9b07c3d{ESC}[0m feat: add the RSS feed
{ESC}[33m1d2e3f4{ESC}[0m chore: initial import
$ """

PLAN = """# parser-demo · Task Plan

🧭 当前 [P2] task=[P2.2] JSON 输出
> Done Criteria: `report --json` 有测试覆盖，README 写明用法

## Current Coordinate

- Goal: 报告命令支持机器可读输出
- Current task: P2.2
- Next task: P2.3
- Latest learning: 表格和 JSON 共用同一个 summary 结构，避免两份数据
- Blocker: none

## Active Work

- [x] P1.1 读现有的报告代码
- [x] P1.2 把汇总逻辑抽成 summary()
- [x] P2.1 设计 JSON 字段 · gate=字段表评审通过
- [ ] P2.2 实现 --json 并补测试 · state=in_progress · gate=pytest 全绿 · dep=P2.1
  - [x] P2.2.1 参数解析
  - [ ] P2.2.2 边界：空报告
- [ ] P2.3 README 写用法 · dep=P2.2
- [ ] P3.1 发布 0.2.0

## Phase 进展

| Phase | 内容 | 状态 |
|---|---|---|
| P1 | 整理现有代码 | 完成 |
| P2 | JSON 输出 | 进行中 |
| P3 | 发布 | 待办 |

## 决策表

| 时间 | 决策 | 理由 |
|---|---|---|
| 第 2 天 | JSON 用 summary() 的字段名 | 不维护第二套命名 |
"""

TRANSCRIPT = [
    {"type": "user", "message": {"role": "user", "content": "Add a --json flag to the report command and cover it with a test."}},
    {"type": "assistant", "message": {"role": "assistant", "stop_reason": "tool_use", "content": [
        {"type": "text", "text": "I'll add the flag, then extend the report tests."}]}},
    {"type": "assistant", "message": {"role": "assistant", "stop_reason": "end_turn", "content": [
        {"type": "text", "text": (
            "Done. `report --json` prints one object per section.\n\n"
            "| File | Change |\n|---|---|\n| `src/report.py` | new `--json` flag, shared `summary()` |\n"
            "| `tests/test_report.py` | 3 new cases (empty, one section, many sections) |\n\n"
            "- `python -m pytest -q tests/test_report.py`: **8 passed**\n"
            "- Next: document the flag in the README (plan task P2.3).")}]}},
    {"type": "system", "subtype": "turn_duration", "durationMs": 48210},
]


def run(cmd: list[str], **kwargs) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL, **kwargs).stdout


def git(repo: Path, *args: str, env: dict[str, str]) -> None:
    run(["git", "-C", str(repo), *args], env=env)


def make_project(home: Path, name: str, env: dict[str, str], *, remote: bool, pending: dict[str, str],
                 modified: dict[str, str], ahead: int) -> Path:
    repo = home / "projects" / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main", env=env)
    (repo / "README.md").write_text(f"# {name}\n\nSynthetic demo project.\n", encoding="utf-8")
    for rel in modified:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("original\n", encoding="utf-8")
    git(repo, "add", "-A", env=env)
    git(repo, "commit", "-q", "-m", "chore: initial import", env=env)
    if remote:
        bare = home / "remotes" / f"{name}.git"
        bare.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "init", "-q", "--bare", str(bare)], env=env)
        git(repo, "remote", "add", "origin", str(bare), env=env)
        git(repo, "push", "-q", "-u", "origin", "main", env=env)
    for index in range(ahead):
        (repo / f"CHANGELOG-{index}.md").write_text(f"- change {index}\n", encoding="utf-8")
        git(repo, "add", "-A", env=env)
        git(repo, "commit", "-q", "-m", f"feat: step {index + 1}", env=env)
    for rel, text in {**pending, **modified}.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    return repo


def fake_cli(bin_dir: Path, name: str, agents_json: Path) -> None:
    """A ``claude`` / ``codex`` on PATH for the dashboard's own calls:
    ``claude agents --json`` prints the synthetic records in ``agents_json``."""
    script = bin_dir / name
    script.write_text(f"#!/usr/bin/env bash\n[ \"$1\" = \"agents\" ] && {{ cat \"{agents_json}\"; exit 0; }}\nexit 1\n",
                      encoding="utf-8")
    script.chmod(0o755)


def fake_pane_command(name: str, screen: Path, *, alt_screen: bool = False) -> str:
    """Print a static screen, then idle as a process whose argv[0] is ``name``
    (tmux reports that as the pane's current command, so the card shows that provider).
    Claude Code draws in the alternate screen; the dashboard reads its transcript then."""
    enter = "printf \"\\033[?1049h\\033[H\"; " if alt_screen else ""
    return f"bash --norc --noprofile -c '{enter}cat {screen}; exec -a {name} sleep infinity'"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(REPO / "docs" / "images"))
    parser.add_argument("--workdir", default=str(REPO / "_tmp" / "readme-shots"))
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright

    work = Path(args.workdir).resolve()
    if work.exists():
        shutil.rmtree(work)
    home, bin_dir = work / "home", work / "bin"
    for path in (home, bin_dir, work / "bus"):
        path.mkdir(parents=True)
    git_env = {**os.environ, "HOME": str(home), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_AUTHOR_NAME": "Demo", "GIT_AUTHOR_EMAIL": "demo@example.invalid",
               "GIT_COMMITTER_NAME": "Demo", "GIT_COMMITTER_EMAIL": "demo@example.invalid"}

    parser_demo = make_project(home, "parser-demo", git_env, remote=True, ahead=2,
                               pending={"notes/json-fields.md": "fields\n", "tests/test_report_json.py": "# new\n",
                                        "examples/report.json": "{}\n"},
                               modified={"src/report.py": "changed\n", "tests/test_report.py": "changed\n"})
    plan = parser_demo / "_wiki-methodology" / "_top" / "_task_plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(PLAN, encoding="utf-8")
    logs = plan.parent / "_logs"
    logs.mkdir()
    (logs / f"{time.strftime('%Y-%m-%d')}-plan.md").write_text(
        "# plan log\n\n"
        "- 09:12 note:learn P2.2 表格和 JSON 共用 summary()，只维护一份字段\n"
        "- 10:40 note:progress P2.2 参数解析完成，开始补空报告的测试\n",
        encoding="utf-8")
    data_pipeline = make_project(home, "data-pipeline", git_env, remote=False, ahead=0,
                                 pending={}, modified={"src/load.py": "changed\n"})
    notes_site = make_project(home, "notes-site", git_env, remote=True, ahead=0, pending={}, modified={})

    screens = work / "screens"
    screens.mkdir()
    for name, text in (("claude", CLAUDE_SCREEN), ("codex", CODEX_SCREEN)):
        (screens / f"{name}.txt").write_text(text, encoding="utf-8")
        fake_cli(bin_dir, name, work / "agents.json")
    (screens / "shell.txt").write_text(SHELL_SCREEN, encoding="utf-8")

    session_id = "0f0e0d0c-0b0a-4000-8000-000000000001"
    transcript = home / ".claude" / "projects" / ("-" + str(parser_demo).strip("/").replace("/", "-")) / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in TRANSCRIPT), encoding="utf-8")

    sock = work / "tmux.sock"
    tmux_env = {**os.environ, "HOME": str(home), "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    tmux_env.pop("TMUX", None)
    tmux_env.pop("TMUX_PANE", None)

    def tmux(*targs: str) -> str:
        return run(["tmux", "-S", str(sock), *targs], env=tmux_env)

    tmux("-f", os.devnull, "new-session", "-d", "-s", SESSION, "-x", "110", "-y", "34", "-n", "parser-demo",
         "-c", str(parser_demo), fake_pane_command("claude", screens / "claude.txt", alt_screen=True))
    tmux("new-window", "-t", SESSION, "-n", "data-pipeline", "-c", str(data_pipeline),
         fake_pane_command("codex", screens / "codex.txt"))
    tmux("new-window", "-t", SESSION, "-n", "notes-site", "-c", str(notes_site),
         f"bash --norc --noprofile -c 'cat {screens / 'shell.txt'}; exec bash --norc --noprofile'")
    claude_pane = tmux("display-message", "-p", "-t", f"{SESSION}:0", "#{pane_id}").strip()
    tmux("set-option", "-p", "-t", claude_pane, "@ai_session_id", session_id)
    tmux("set-option", "-p", "-t", claude_pane, "@ai_transcript", str(transcript))
    claude_pid = tmux("display-message", "-p", "-t", claude_pane, "#{pane_pid}").strip()
    (work / "agents.json").write_text(json.dumps([{"pid": int(claude_pid), "sessionId": session_id,
                                                    "cwd": str(parser_demo), "status": "idle"}]), encoding="utf-8")
    archive_prompt = work / "archive-prompt.md"
    archive_prompt.write_text("Archive: triage the changes, commit them and update the plan.\n", encoding="utf-8")

    port = free_port()
    user, password = "demo", secrets.token_hex(12)
    server_env = {
        **tmux_env, "AGENT_BUS_DIR": str(work / "bus"), "TMUX_CARD_TMUX_SOCKET": str(sock),
        "TMUX_CARD_SESSION": SESSION, "TMUX_CARD_HOST": "127.0.0.1", "TMUX_CARD_PORT": str(port),
        "TMUX_CARD_USER": user, "TMUX_CARD_PASS": password, "WEBTERM_ENV": str(work / "no-webterm.env"),
        "TMUX_CARD_CAPTURE_LOCK": str(work / "capture.lock"), "TMUX_CARD_PROJECTS_DIR": str(home / "projects"),
        "TMUX_CARD_LOCAL_ARTIFACT_ROOT": str(home), "CARDS_TOP_CLI": str(FAKE_PLAN_CLI),
        "CARDS_ARCHIVE_PROMPT": str(archive_prompt), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    }
    for key in ("CARDS_AUTO_APPROVE", "AGENT_BUS_AUTO_APPROVE", "CODEX_HOME", "CLAUDE_CONFIG_DIR"):
        server_env.pop(key, None)
    log = (work / "server.log").open("w", encoding="utf-8")
    server = subprocess.Popen([sys.executable, str(REPO / "dashboard" / "server.py")], env=server_env,
                              stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}/cards"
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    try:
        auth = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        auth.add_password(None, base, user, password)
        opener = urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(auth))
        deadline = time.monotonic() + 30
        while True:
            try:
                panes = json.loads(opener.open(f"{base}/api/panes", timeout=5).read())["panes"]
                if len(panes) == 3 and all((p.get("git") or {}).get("state") == "ok" for p in panes) \
                        and (panes[0].get("git") or {}).get("task_title"):
                    break
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(f"dashboard not ready; see {work / 'server.log'}")
            time.sleep(0.5)
        (work / "panes.json").write_text(json.dumps(panes, ensure_ascii=False, indent=1), encoding="utf-8")
        pane_q = claude_pane.replace("%", "%25")
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            desktop = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1,
                                          http_credentials={"username": user, "password": password})
            page = desktop.new_page()
            page.add_init_script("localStorage.setItem('tmuxCardViewMode', 'dialog');")
            page.goto(f"{base}/?pane={pane_q}")
            page.wait_for_selector(".card-git .cg-task")
            page.wait_for_selector("text=8 passed")
            page.wait_for_timeout(1200)
            page.screenshot(path=str(out / "cards-desktop.png"))
            page.locator(f'[data-detail-plan="{claude_pane}"] >> visible=true').first.click()
            page.wait_for_selector(".plan-tree")
            page.wait_for_timeout(800)
            page.screenshot(path=str(out / "plan-panel.png"))
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            if page.is_visible("#planPanel"):
                page.click("#planClose")
            page.locator(f'[data-detail-terminal="{claude_pane}"] >> visible=true').first.click()
            page.wait_for_selector("#terminalPanel.visible")
            page.wait_for_timeout(2500)
            page.screenshot(path=str(out / "terminal-view.png"))
            desktop.close()
            mobile = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                                         is_mobile=True, has_touch=True,
                                         http_credentials={"username": user, "password": password})
            phone = mobile.new_page()
            phone.add_init_script("localStorage.setItem('tmuxCardViewMode', 'dialog');")
            phone.goto(f"{base}/?pane={pane_q}")
            phone.wait_for_selector("text=8 passed")
            phone.wait_for_timeout(1000)
            phone.click("#floatMain")  # the round ⌁ button: 计划 / 终端 / ESC
            phone.wait_for_timeout(600)
            phone.screenshot(path=str(out / "mobile-cards.png"))
            mobile.close()
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)
        log.close()
        subprocess.run(["tmux", "-S", str(sock), "kill-server"], capture_output=True, check=False)
    for name in ("cards-desktop.png", "plan-panel.png", "terminal-view.png", "mobile-cards.png"):
        print(out / name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
