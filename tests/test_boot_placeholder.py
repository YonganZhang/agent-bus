"""开机/tmux 丢失时 ensure-tmux-session 只建一个空 shell 占位窗口。

- 占位窗口不起 AI,快照认作 shell-only、不可恢复,不进恢复基线;
- 占位窗口在高位号(默认 999),restore 按快照原窗口号重建时不会被挤到"最大号+1";
- `--close-placeholder` 只关带 @boot_placeholder 标记、空闲、且不是唯一窗口的那个。

脚本本体是 contrib/boot/ensure-tmux-session。集成测试只在独立 tmux server
(TMUX_TMPDIR 指向临时目录,并去掉 $TMUX)里跑,不碰真实 secretary_web。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import secretary_recovery as recovery  # noqa: E402

ENSURE = Path(__file__).resolve().parents[1] / "contrib" / "boot" / "ensure-tmux-session"
SESSION = "secretary_web"
CLAUDE_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CLAUDE_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
FRESH_BOOT_CLAUDE = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def snapshot_pane(window: int, session_id: str, cwd: str) -> dict:
    return {
        "window_index": window, "window_name": f"w{window}", "pane_index": 0, "pane_id": f"%{window}",
        "cwd": cwd, "preference_key": recovery.preference_key(SESSION, window, 0, cwd),
        "provider": recovery._provider_record("claude", session_id, "claude-agents-pid"),
    }


class RestoreKeepsWindowNumbersTest(unittest.TestCase):
    """纯数据: 同一份快照,live 里多出来的那个窗口放在哪决定原窗口号会不会漂移。"""

    def plan(self, live_panes: list[dict], cwd: str) -> dict:
        snapshot = {"tmux_server": "old", "panes": [snapshot_pane(0, CLAUDE_A, cwd), snapshot_pane(1, CLAUDE_B, cwd)]}
        return recovery.build_recovery_plan(snapshot, {"tmux_server": "new", "panes": live_panes}, SESSION)

    def test_fresh_claude_at_window_zero_pushes_the_original_window_zero_to_the_end(self) -> None:
        """改前的开机现场: 0 号是一个全新 claude(带新会话号) -> 原窗口号级联漂移(0->1, 1->2)。"""
        with tempfile.TemporaryDirectory() as cwd:
            boot = snapshot_pane(0, FRESH_BOOT_CLAUDE, cwd)
            items = {i["session_id"]: i for i in self.plan([boot], cwd)["items"]}
        self.assertEqual((items[CLAUDE_A]["target_window"], items[CLAUDE_A]["remapped_from"]), (1, 0))
        self.assertEqual((items[CLAUDE_B]["target_window"], items[CLAUDE_B]["remapped_from"]), (2, 1))

    def test_idle_placeholder_at_high_index_leaves_every_original_number_free(self) -> None:
        with tempfile.TemporaryDirectory() as cwd:
            placeholder = {
                "window_index": 999, "window_name": "boot-placeholder", "pane_index": 0, "pane_id": "%0",
                "cwd": cwd, "preference_key": recovery.preference_key(SESSION, 999, 0, cwd),
                "provider": recovery._provider_record("", "", "shell-only"),
            }
            plan = self.plan([placeholder], cwd)
        targets = {i["session_id"]: (i["status"], i["target_window"], "remapped_from" in i) for i in plan["items"]}
        self.assertEqual(targets, {CLAUDE_A: ("restore", 0, False), CLAUDE_B: ("restore", 1, False)})


@unittest.skipUnless(shutil.which("tmux") and ENSURE.is_file(), "tmux and contrib/boot/ensure-tmux-session required")
class EnsureSecretaryTmuxPlaceholderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = dict(os.environ, TMUX_TMPDIR=self.tmp.name)
        # $TMUX 会让 tmux 客户端连到外层(真实)server —— 必须去掉,整组测试只认临时 socket。
        for key in ("TMUX", "TMUX_PANE"):
            self.env.pop(key, None)
        self.socket = Path(self.tmp.name) / f"tmux-{os.getuid()}" / "default"
        self.proc = subprocess.Popen([str(ENSURE)], env=self.env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop)
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.tmux("has-session", "-t", SESSION, check=False).returncode == 0:
                break
            if self.proc.poll() is not None:
                self.fail(f"ensure-tmux-session exited early: {self.proc.communicate()}")
            time.sleep(0.2)
        else:
            self.fail("placeholder session never appeared")

    def stop(self) -> None:
        # 只杀临时 socket 上的 server(显式 -S),真实 server 不受影响。
        subprocess.run(["tmux", "-S", str(self.socket), "kill-server"], env=self.env,
                       capture_output=True, timeout=10, check=False)
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream:
                stream.close()

    def tmux(self, *args, check=True):
        return subprocess.run(["tmux", "-S", str(self.socket), *args], env=self.env, capture_output=True,
                              text=True, timeout=10, check=check)

    def panes(self) -> list[list[str]]:
        fmt = "#{window_index}\t#{window_name}\t#{pane_id}\t#{@boot_placeholder}\t#{pane_current_command}\t#{pane_pid}"
        return [line.split("\t") for line in self.tmux("list-panes", "-s", "-t", SESSION, "-F", fmt).stdout.splitlines()]

    def close_placeholder(self):
        return subprocess.run([str(ENSURE), "--close-placeholder"], env=self.env, capture_output=True,
                              text=True, timeout=30)

    def test_placeholder_is_an_idle_shell_at_the_high_index(self) -> None:
        """改前: 0 号窗口、$HOME 下直接起 claude。"""
        panes = self.panes()
        self.assertEqual(len(panes), 1, panes)
        index, name, _pane_id, flag, _command, pane_pid = panes[0]
        self.assertEqual((index, name, flag), ("999", "boot-placeholder", "1"))
        time.sleep(0.5)
        children = subprocess.run(["pgrep", "-P", pane_pid], capture_output=True, text=True).stdout.split()
        self.assertEqual(children, [], "placeholder shell must not start any program")

    def test_snapshot_sees_the_placeholder_as_a_non_recoverable_shell(self) -> None:
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        env["TMUX_TMPDIR"] = self.tmp.name
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(recovery, "read_cards_prefs", return_value=({}, "")):
            state = recovery.capture_state(SESSION)
        self.assertEqual(state["pane_count"], 1)
        self.assertEqual(state["recoverable_pane_count"], 0)
        provider = state["panes"][0]["provider"]
        self.assertEqual((provider["kind"], provider["session_id"], provider["recoverable"]), ("", "", False))

    def test_close_placeholder_only_when_other_windows_exist_and_it_is_idle(self) -> None:
        placeholder = self.panes()[0][2]
        # 唯一窗口: 关了 session 就没了,必须保留
        result = self.close_placeholder()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("only pane", result.stderr)
        self.assertEqual([p[2] for p in self.panes()], [placeholder])
        # 另一个带标记、但前台在跑程序的占位(有人在用)也要保留
        busy = self.tmux("new-window", "-d", "-P", "-F", "#{pane_id}", "-t", f"{SESSION}:5",
                         "exec sleep 60").stdout.strip()
        self.tmux("set-option", "-p", "-t", busy, "@boot_placeholder", "1")
        restored = self.tmux("new-window", "-d", "-P", "-F", "#{pane_id}", "-t", f"{SESSION}:0",
                             "bash --norc --noprofile").stdout.strip()
        time.sleep(0.5)
        result = self.close_placeholder()
        self.assertEqual(result.returncode, 0, result.stderr)
        remaining = {p[2] for p in self.panes()}
        self.assertNotIn(placeholder, remaining)
        self.assertEqual(remaining, {busy, restored})
        self.assertIn("foreground is 'sleep'", result.stderr)


if __name__ == "__main__":
    unittest.main()
