"""agent_window.sh: 起 claude/codex 必须显式 --resume-id 或 --fresh,没有隐式的"接最近一次"。

全部跑在独立 tmux server(TMUX_TMPDIR 指向临时目录、`-f /dev/null` 不读用户配置)和假 HOME 里;
claude/codex 是 PATH 上的假命令,不会拉起真 AI,也不碰真实 secretary_web。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
AGENT_WINDOW = SCRIPTS / "agent_window.sh"

CLAUDE_CLI = "11111111-1111-4111-8111-111111111111"
CLAUDE_SDK = "22222222-2222-4222-8222-222222222222"
CLAUDE_LIVE = "33333333-3333-4333-8333-333333333333"
CODEX_SHARED = "01a00000-0000-7000-8000-00000000000a"
CODEX_ISOLATED = "01a00000-0000-7000-8000-00000000000b"
CODEX_SUBAGENT = "01a00000-0000-7000-8000-00000000000c"
CODEX_OTHER_CWD = "01a00000-0000-7000-8000-00000000000d"

FAKE_AI = """#!/usr/bin/env bash
# 假 claude/codex: `claude agents --json` 回放预置记录,其它调用只记下参数就退出。
if [ "$(basename "$0")" = claude ] && [ "${1:-}" = agents ]; then
  cat "$FAKE_CLAUDE_AGENTS"; exit 0
fi
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$FAKE_LAUNCH_LOG"
"""


def write_rollout(home: Path, session_id: str, cwd: str, *, thread_source: str = "user", age: int = 0) -> None:
    day = home / "sessions" / "2026" / "09" / "23"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-09-23T10-00-00-{session_id}.jsonl"
    meta = {"type": "session_meta", "payload": {"id": session_id, "cwd": cwd, "thread_source": thread_source}}
    path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))


@unittest.skipUnless(shutil.which("tmux"), "tmux required")
class AgentWindowExplicitSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.projects = root / "projects"
        self.proj = self.projects / "demo-proj-abc123"
        self.proj.mkdir(parents=True)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for name in ("claude", "codex"):
            fake = bin_dir / name
            fake.write_text(FAKE_AI, encoding="utf-8")
            fake.chmod(0o755)
        self.launch_log = root / "launched.log"
        agents = root / "agents.json"
        agents.write_text(json.dumps([
            {"pid": 4242, "cwd": str(self.proj), "kind": "interactive", "sessionId": CLAUDE_LIVE, "status": "idle"},
            {"pid": 4343, "cwd": "/elsewhere", "kind": "interactive",
             "sessionId": "44444444-4444-4444-8444-444444444444", "status": "idle"},
        ]), encoding="utf-8")

        # Claude transcripts: 交互式一条(旧) + 后台 claude -p 一条(更新,--continue 会接到它)
        enc = self.home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(self.proj))
        enc.mkdir(parents=True)
        for sid, entry, age in ((CLAUDE_CLI, "cli", 600), (CLAUDE_SDK, "sdk-cli", 10)):
            path = enc / f"{sid}.jsonl"
            path.write_text(json.dumps({"type": "user", "entrypoint": entry, "sessionId": sid}) + "\n",
                            encoding="utf-8")
            stamp = time.time() - age
            os.utime(path, (stamp, stamp))

        # Codex rollouts: 共享 home 一条、隔离 home 一条,另有子智能体和别的 cwd(都不该列出)
        write_rollout(self.home / ".codex", CODEX_SHARED, str(self.proj), age=300)
        write_rollout(self.home / ".codex-homes" / "demo-aaaaaa", CODEX_ISOLATED, str(self.proj), age=100)
        write_rollout(self.home / ".codex", CODEX_SUBAGENT, str(self.proj), thread_source="subagent", age=5)
        write_rollout(self.home / ".codex", CODEX_OTHER_CWD, "/elsewhere", age=1)

        self.env = dict(
            os.environ,
            HOME=str(self.home),
            PATH=f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            TMUX_TMPDIR=self.tmp.name,
            AGENT_BUS_CARDS_CHECKPOINT="0",
            AGENT_BUS_PROJECTS_DIR=str(self.projects),
            # No launch wrapper: the start command is the bare AI command.
            AGENT_BUS_SESSION_SHELL=str(root / "no-wrapper"),
            FAKE_CLAUDE_AGENTS=str(agents),
            FAKE_LAUNCH_LOG=str(self.launch_log),
        )
        # $TMUX 会让 tmux 客户端连到外层(真实)server,必须去掉。
        for key in ("TMUX", "TMUX_PANE", "CODEX_HOME", "AGENT_BUS_DEFAULT_CODEX_HOME",
                    "AGENT_BUS_CLAUDE_PERMISSION_MODE"):
            self.env.pop(key, None)
        self.tmux("-f", "/dev/null", "new-session", "-d", "-s", "t", "-n", "w0", "-c", str(root),
                  "bash --norc --noprofile")
        self.addCleanup(lambda: self.tmux("kill-server", check=False))

    def tmux(self, *args, check=True):
        return subprocess.run(["tmux", *args], env=self.env, capture_output=True, text=True,
                              timeout=10, check=check)

    def run_aw(self, *args):
        return subprocess.run(["bash", str(AGENT_WINDOW), *args, "--session", "t"],
                              env=self.env, capture_output=True, text=True, timeout=60)

    def windows(self) -> list[str]:
        out = self.tmux("list-windows", "-t", "t", "-F", "#{window_index}\t#{pane_start_command}").stdout
        return out.splitlines()

    def start_command_of_new_window(self, before: list[str]) -> str:
        added = [line for line in self.windows() if line not in before]
        self.assertEqual(len(added), 1, added)
        return added[0].split("\t", 1)[1]

    # ---- 没给 --resume-id / --fresh: 拒绝、不开窗口、列出候选 ----

    def test_new_claude_without_choice_refuses_and_lists_candidates(self) -> None:
        """改前: 直接开窗口跑 `claude --continue`,接到最新的那条 —— 这里是后台 claude -p 的会话。"""
        before = self.windows()
        result = self.run_aw("new", "claude", "--cwd", str(self.proj))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.windows(), before)
        self.assertFalse(self.launch_log.exists())
        err = result.stderr
        self.assertIn(CLAUDE_LIVE, err)            # 同 cwd 的活会话
        self.assertIn(CLAUDE_CLI, err)             # 最近的交互式 transcript
        self.assertNotIn(CLAUDE_SDK, err)          # 后台 claude -p 不列,只计数
        self.assertIn("sdk-cli", err)
        self.assertNotIn("44444444-4444-4444-8444-444444444444", err)  # 别的 cwd
        self.assertIn("--resume-id <会话ID>", err)
        self.assertIn("--fresh", err)
        self.assertNotIn("--continue", self.tmux("list-windows", "-a", "-F", "#{pane_start_command}").stdout)

    def test_new_codex_without_choice_refuses_before_creating_an_isolated_home(self) -> None:
        """改前: 新隔离 CODEX_HOME 里跑 `codex`(名义恢复、实为新会话);--shared-home 时跑 `resume --last`。"""
        homes_before = sorted(p.name for p in (self.home / ".codex-homes").iterdir())
        for extra in ((), ("--shared-home",)):
            with self.subTest(extra=extra):
                before = self.windows()
                result = self.run_aw("new", "codex", *extra, "--cwd", str(self.proj))
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(self.windows(), before)
                err = result.stderr
                self.assertIn(CODEX_SHARED, err)
                self.assertIn(CODEX_ISOLATED, err)
                self.assertNotIn(CODEX_SUBAGENT, err)
                self.assertNotIn(CODEX_OTHER_CWD, err)
                self.assertIn("--resume-id <会话ID>", err)
                if extra:
                    self.assertIn("--shared-home --resume-id", err)
        self.assertEqual(sorted(p.name for p in (self.home / ".codex-homes").iterdir()), homes_before)
        self.assertFalse(self.launch_log.exists())

    def test_open_without_choice_refuses(self) -> None:
        """改前: `open <kw>` 默认 `claude --continue`,`open --codex <kw>` 默认 `resume --last`/新隔离 home。"""
        for extra in ((), ("--codex",)):
            with self.subTest(extra=extra):
                before = self.windows()
                result = self.run_aw("open", *extra, "demo-proj")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(self.windows(), before)
                self.assertIn("--resume-id <会话ID>", result.stderr)
                self.assertIn("open --session t " + ("--codex " if extra else "") + "demo-proj --fresh",
                              result.stderr)

    def test_legacy_continue_flags_and_conflicting_choices_are_rejected(self) -> None:
        before = self.windows()
        for args in (("new", "claude", "--continue", "--cwd", str(self.proj)),
                     ("open", "--resume", "demo-proj"),
                     ("new", "claude", "--fresh", "--resume-id", CLAUDE_CLI, "--cwd", str(self.proj))):
            with self.subTest(args=args):
                result = self.run_aw(*args)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.windows(), before)

    # ---- 显式选择: 起的命令正是所选 ----

    def test_explicit_choices_launch_exactly_the_requested_session(self) -> None:
        cases = [
            (("new", "claude", "--fresh"), r"claude --session-id [0-9a-f-]{36};"),
            (("new", "claude", "--resume-id", CLAUDE_CLI), rf"claude --resume {CLAUDE_CLI};"),
            (("new", "codex", "--shared-home", "--fresh"), r"'codex; exec bash'"),
            (("new", "codex", "--shared-home", "--resume-id", CODEX_SHARED), rf"codex resume {CODEX_SHARED};"),
        ]
        for args, pattern in cases:
            with self.subTest(args=args):
                before = self.windows()
                result = self.run_aw(*args, "--cwd", str(self.proj))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                command = self.start_command_of_new_window(before)
                self.assertRegex(command, pattern)
                self.assertNotIn("--continue", command)
                self.assertNotIn("--permission-mode", command)
                self.assertNotIn("--last", command)

    def test_claude_permission_mode_is_opt_in(self) -> None:
        self.env["AGENT_BUS_CLAUDE_PERMISSION_MODE"] = "bypassPermissions"
        before = self.windows()
        result = self.run_aw("new", "claude", "--fresh", "--cwd", str(self.proj))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        command = self.start_command_of_new_window(before)
        self.assertRegex(command, r"claude --session-id [0-9a-f-]{36} --permission-mode bypassPermissions;")

    def test_codex_home_with_space_and_quote_still_launches(self) -> None:
        """改前: `bash -lc 'env CODEX_HOME=/…/it's home codex'` —— 单引号提前闭合、空格把路径
        拆成两个词,codex 根本起不来;@ai_codex_home 也只记下空格前那一截。
        这种路径启动时不记 @ai_codex_home(不 eval 还原),由 stamp_live_panes 事后从进程补。"""
        home = Path(self.tmp.name) / "homes" / "it's a home"
        home.mkdir(parents=True)
        before = self.windows()
        result = self.run_aw("new", "codex", "--codex-home", str(home), "--fresh", "--cwd", str(self.proj))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        added = [line for line in self.windows() if line not in before]
        self.assertEqual(len(added), 1, added)
        index = added[0].split("\t", 1)[0]
        deadline = time.time() + 10
        while time.time() < deadline and not self.launch_log.exists():
            time.sleep(0.1)
        self.assertTrue(self.launch_log.exists(), "codex never started: the launch command is broken")
        self.assertIn("codex", self.launch_log.read_text(encoding="utf-8"))
        stamped = self.tmux("show-options", "-p", "-v", "-t", f"t:{index}", "@ai_codex_home", check=False).stdout.strip()
        self.assertEqual(stamped, "")  # never a truncated half path

    def test_bash_windows_need_no_session_choice(self) -> None:
        before = self.windows()
        result = self.run_aw("new", "bash", "--cwd", str(self.proj))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.windows()), len(before) + 1)


if __name__ == "__main__":
    unittest.main()
