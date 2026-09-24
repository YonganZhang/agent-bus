#!/usr/bin/env python3
"""Unit tests for exact provider/session state discovery."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import cli_bridge  # noqa: E402
import provider_state  # noqa: E402


def runtime(command: str) -> dict[str, object]:
    return {
        "pane": "test:1.0",
        "pane_id": "%17",
        "pane_pid": 400,
        "pane_start_time": "100",
        "foreground_pid": 410,
        "foreground_start_time": "110",
        "command": command,
        "cwd": "/work/repo",
        "title": "worker",
    }


def target(command: str) -> cli_bridge.Target:
    return cli_bridge.Target(
        name="worker",
        pane="test:1.0",
        pane_id="%17",
        pane_pid=400,
        pane_start_time="100",
        foreground_pid=410,
        foreground_start_time="110",
        expected_command=command,
    )


class ProviderStateTest(unittest.TestCase):
    def common(self, command: str):
        return mock.patch.multiple(
            provider_state.cli_bridge,
            load_targets=mock.DEFAULT,
            target_info=mock.DEFAULT,
        )

    def test_claude_agents_recovers_complete_records_from_truncated_array(self) -> None:
        raw = '[{"pid":410,"status":"busy"},{"pid":999,"status":"idle"},{"pid":'
        completed = type("Completed", (), {"returncode": 0, "stdout": raw, "stderr": ""})()
        with mock.patch.object(provider_state.subprocess, "run", return_value=completed):
            records, warning = provider_state.claude_agents_json()
        self.assertEqual([item["pid"] for item in records], [410, 999])
        self.assertIn("recovered 2 complete records", warning)

    def test_claude_agents_scopes_query_to_target_cwd(self) -> None:
        completed = type("Completed", (), {"returncode": 0, "stdout": "[]", "stderr": ""})()
        with mock.patch.object(provider_state.subprocess, "run", return_value=completed) as run:
            provider_state.claude_agents_json("/work/repo")
        self.assertEqual(run.call_args.args[0], ["claude", "agents", "--cwd", "/work/repo", "--json"])

    def test_claude_agents_pid_is_authoritative_and_exact_transcript_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "session-1.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "A" * 5000}]},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with self.common("claude") as patched, mock.patch.object(
                provider_state, "process_tree_pids", return_value=[410, 411]
            ), mock.patch.object(
                provider_state,
                "claude_agents_json",
                return_value=(
                    [
                        {
                            "pid": 410,
                            "cwd": "/work/repo",
                            "sessionId": "session-1",
                            "status": "busy",
                        }
                    ],
                    "",
                ),
            ), mock.patch.object(
                provider_state, "claude_transcript_path", return_value=transcript
            ), mock.patch.object(
                provider_state, "capture_pane", return_value="Claude is working\n"
            ):
                patched["load_targets"].return_value = {"worker": target("claude")}
                patched["target_info"].return_value = runtime("claude")
                payload = provider_state.snapshot_target("worker", max_chars=300)

        self.assertEqual(payload["provider"], "claude")
        self.assertEqual(payload["state"]["value"], "busy")
        self.assertEqual(payload["state"]["confidence"], "authoritative")
        self.assertEqual(payload["session"], {"id": "session-1", "source": "claude_agents", "exact": True})
        self.assertEqual(payload["fidelity"], "exact")
        self.assertLessEqual(len(payload["last_assistant"]), 300)
        self.assertTrue(payload["last_assistant"].endswith("…"))

    def test_claude_same_cwd_with_wrong_pid_never_claims_exact_history(self) -> None:
        with self.common("claude") as patched, mock.patch.object(
            provider_state, "process_tree_pids", return_value=[410]
        ), mock.patch.object(
            provider_state,
            "claude_agents_json",
            return_value=([{"pid": 999, "cwd": "/work/repo", "sessionId": "wrong", "status": "idle"}], ""),
        ), mock.patch.object(
            provider_state, "capture_pane", return_value="Working… press esc to interrupt\n"
        ):
            patched["load_targets"].return_value = {"worker": target("claude")}
            patched["target_info"].return_value = runtime("claude")
            payload = provider_state.snapshot_target("worker")

        self.assertEqual(payload["provider"], "claude")
        self.assertEqual(payload["session"], {"id": "", "source": "", "exact": False})
        self.assertEqual(payload["fidelity"], "inferred")
        self.assertEqual(payload["state"]["source"], "tmux_live_tail")
        self.assertNotIn("wrong", json.dumps(payload))

    def test_codex_rollout_fd_binds_exact_thread_without_cwd_or_mtime_guess(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "rollout-thread-7.jsonl"
            records = [
                {"type": "session_meta", "payload": {"id": "thread-7"}},
                {"type": "event_msg", "payload": {"type": "task_started"}},
                {"type": "event_msg", "payload": {"type": "agent_message", "message": "finished cleanly"}},
                {"type": "event_msg", "payload": {"type": "task_complete"}},
            ]
            rollout.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
            with self.common("codex") as patched, mock.patch.object(
                provider_state, "process_tree_pids", return_value=[410, 412]
            ), mock.patch.object(
                provider_state, "rollout_paths_for_pids", return_value=[rollout]
            ), mock.patch.object(
                provider_state, "codex_exact_run_state", return_value={}
            ), mock.patch.object(
                provider_state, "capture_pane", return_value="› Ask Codex to do anything\n"
            ):
                patched["load_targets"].return_value = {"worker": target("codex")}
                patched["target_info"].return_value = runtime("codex")
                payload = provider_state.snapshot_target("worker")

        self.assertEqual(payload["provider"], "codex")
        self.assertEqual(payload["session"], {"id": "thread-7", "source": "codex_rollout_fd", "exact": True})
        self.assertEqual(payload["state"]["value"], "idle")
        self.assertEqual(payload["state"]["source"], "codex_rollout_fd")
        self.assertEqual(payload["last_assistant"], "finished cleanly")
        self.assertEqual(payload["fidelity"], "exact")

    def test_codex_open_root_and_subagent_rollouts_select_exact_root_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "rollout-root.jsonl"
            child = Path(tmp) / "rollout-child.jsonl"
            root.write_text(
                json.dumps(
                    {"type": "session_meta", "payload": {"id": "root-thread", "source": "cli"}}
                )
                + "\n"
                + json.dumps(
                    {"type": "event_msg", "payload": {"type": "agent_message", "message": "root answer"}}
                )
                + "\n",
                encoding="utf-8",
            )
            child.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "child-thread",
                            "source": {
                                "subagent": {"thread_spawn": {"parent_thread_id": "root-thread"}}
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with self.common("codex") as patched, mock.patch.object(
                provider_state, "process_tree_pids", return_value=[410, 412]
            ), mock.patch.object(
                provider_state, "rollout_paths_for_pids", return_value=[root, child]
            ), mock.patch.object(
                provider_state, "codex_exact_run_state", return_value={}
            ), mock.patch.object(
                provider_state, "capture_pane", return_value="Working… press esc to interrupt\n"
            ):
                patched["load_targets"].return_value = {"worker": target("codex")}
                patched["target_info"].return_value = runtime("codex")
                payload = provider_state.snapshot_target("worker")

        self.assertEqual(
            payload["session"],
            {"id": "root-thread", "source": "codex_rollout_fd", "exact": True},
        )
        self.assertEqual(payload["last_assistant"], "root answer")
        self.assertEqual(payload["fidelity"], "exact")

    def test_claude_open_dialog_is_needs_input_from_provider_status(self) -> None:
        """An unnumbered dialog matches no screen pattern; Claude itself reports it."""
        dialog = " Make auto mode your default permission mode?\n   ❯ Yes, set auto mode\n     No, keep bypass permissions\n"
        with self.common("claude") as patched, mock.patch.object(
            provider_state, "process_tree_pids", return_value=[410]
        ), mock.patch.object(
            provider_state,
            "claude_agents_json",
            return_value=([{"pid": 410, "sessionId": "s", "status": "waiting", "waitingFor": "dialog open"}], ""),
        ), mock.patch.object(
            provider_state, "claude_transcript_path", return_value=None
        ), mock.patch.object(
            provider_state, "capture_pane", return_value=dialog
        ):
            patched["load_targets"].return_value = {"worker": target("claude")}
            patched["target_info"].return_value = runtime("claude")
            payload = provider_state.snapshot_target("worker")

        self.assertEqual(payload["state"]["value"], "needs_input")
        self.assertEqual(payload["state"]["source"], "claude_agents")
        self.assertIn("waitingFor=dialog open", payload["state"]["evidence"][0]["detail"])

    def test_codex_root_turn_done_but_subagent_mid_turn_is_busy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "rollout-root.jsonl"
            child = Path(tmp) / "rollout-child.jsonl"
            root.write_text(
                "\n".join(json.dumps(row) for row in [
                    {"type": "session_meta", "payload": {"id": "root-thread", "source": "cli"}},
                    {"type": "event_msg", "payload": {"type": "task_started"}},
                    {"type": "event_msg", "payload": {"type": "task_complete"}},
                ]) + "\n",
                encoding="utf-8",
            )
            child.write_text(
                "\n".join(json.dumps(row) for row in [
                    {"type": "session_meta", "payload": {"id": "child-thread", "source": {
                        "subagent": {"thread_spawn": {"parent_thread_id": "root-thread"}}}}},
                    {"type": "event_msg", "payload": {"type": "task_started"}},
                ]) + "\n",
                encoding="utf-8",
            )
            with self.common("codex") as patched, mock.patch.object(
                provider_state, "process_tree_pids", return_value=[410, 412]
            ), mock.patch.object(
                provider_state, "rollout_paths_for_pids", return_value=[root, child]
            ), mock.patch.object(
                provider_state, "codex_exact_run_state", return_value={}
            ), mock.patch.object(
                provider_state, "capture_pane", return_value="› Ask Codex to do anything\n"
            ):
                patched["load_targets"].return_value = {"worker": target("codex")}
                patched["target_info"].return_value = runtime("codex")
                payload = provider_state.snapshot_target("worker")

        self.assertEqual(payload["state"]["value"], "busy")
        self.assertEqual(payload["state"]["source"], "codex_subagent_rollout_fd")
        self.assertTrue(any(item["kind"] == "subagents_busy" for item in payload["state"]["evidence"]))

    def test_codex_snapshot_lists_subagent_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "rollout-root.jsonl"
            child = Path(tmp) / "rollout-child.jsonl"
            root.write_text(json.dumps({"type": "session_meta", "payload": {"id": "root-thread", "source": "cli"}}) + "\n", encoding="utf-8")
            now = "2099-01-01T00:00:00Z"
            child.write_text(
                "\n".join(json.dumps(row) for row in [
                    {"type": "session_meta", "payload": {"id": "child-thread", "thread_source": "subagent",
                                                        "parent_thread_id": "root-thread", "agent_path": "/root/review",
                                                        "agent_nickname": "Erdos",
                                                        "source": {"subagent": {"thread_spawn": {"parent_thread_id": "root-thread"}}}}},
                    {"timestamp": now, "type": "event_msg", "payload": {"type": "task_started"}},
                    {"timestamp": now, "type": "response_item", "payload": {"type": "function_call", "name": "exec_command",
                                                                            "arguments": json.dumps({"cmd": "pytest -q"})}},
                ]) + "\n",
                encoding="utf-8",
            )
            with self.common("codex") as patched, mock.patch.object(
                provider_state, "process_tree_pids", return_value=[410]
            ), mock.patch.object(
                provider_state, "rollout_paths_for_pids", return_value=[root, child]
            ), mock.patch.object(
                provider_state, "codex_exact_run_state", return_value={}
            ), mock.patch.object(
                provider_state, "capture_pane", return_value="› Ask Codex to do anything\n"
            ):
                patched["load_targets"].return_value = {"worker": target("codex")}
                patched["target_info"].return_value = runtime("codex")
                payload = provider_state.snapshot_target("worker")

        self.assertEqual(len(payload["subagents"]), 1)
        self.assertEqual(payload["subagents"][0]["description"], "review（Erdos）")
        self.assertEqual(payload["subagents"][0]["activity"], "运行命令：pytest -q")

    def test_codex_single_subagent_rollout_never_claims_exact_root_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            child = Path(tmp) / "rollout-child.jsonl"
            child.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "child-thread",
                            "source": {
                                "subagent": {"thread_spawn": {"parent_thread_id": "root-thread"}}
                            },
                        },
                    }
                )
                + "\n"
                + json.dumps(
                    {"type": "event_msg", "payload": {"type": "agent_message", "message": "child-only answer"}}
                )
                + "\n",
                encoding="utf-8",
            )
            with self.common("codex") as patched, mock.patch.object(
                provider_state, "process_tree_pids", return_value=[410, 412]
            ), mock.patch.object(
                provider_state, "rollout_paths_for_pids", return_value=[child]
            ), mock.patch.object(
                provider_state, "capture_pane", return_value="Working… press esc to interrupt\n"
            ):
                patched["load_targets"].return_value = {"worker": target("codex")}
                patched["target_info"].return_value = runtime("codex")
                payload = provider_state.snapshot_target("worker")

        self.assertEqual(payload["session"], {"id": "", "source": "", "exact": False})
        self.assertEqual(payload["fidelity"], "inferred")
        self.assertNotIn("child-only answer", payload["last_assistant"])

    def test_codex_run_index_same_cwd_is_ignored_without_rollout_fd_identity(self) -> None:
        with self.common("codex") as patched, mock.patch.object(
            provider_state, "process_tree_pids", return_value=[410]
        ), mock.patch.object(
            provider_state, "rollout_paths_for_pids", return_value=[]
        ), mock.patch.object(
            provider_state, "capture_pane", return_value="Working… press esc to interrupt\n"
        ), mock.patch.object(
            provider_state,
            "_read_json",
            return_value={
                "threads": {"wrong-thread": {"repo": "/work/repo", "last_run_id": "run-1"}},
                "runs": {"run-1": {"thread_id": "wrong-thread", "repo": "/work/repo", "status": "running"}},
            },
        ):
            patched["load_targets"].return_value = {"worker": target("codex")}
            patched["target_info"].return_value = runtime("codex")
            payload = provider_state.snapshot_target("worker")

        self.assertEqual(payload["session"], {"id": "", "source": "", "exact": False})
        self.assertEqual(payload["fidelity"], "inferred")
        self.assertEqual(payload["state"]["source"], "tmux_live_tail")
        self.assertNotIn("wrong-thread", json.dumps(payload))

    def test_exact_claude_status_is_not_overridden_by_prompt_words_on_screen(self) -> None:
        """Claude reports its own open dialogs as "waiting"; a busy session whose
        screen merely shows prompt-like words is still busy."""
        with self.common("claude") as patched, mock.patch.object(
            provider_state, "process_tree_pids", return_value=[410]
        ), mock.patch.object(
            provider_state,
            "claude_agents_json",
            return_value=([{"pid": 410, "sessionId": "session-2", "status": "busy"}], ""),
        ), mock.patch.object(
            provider_state, "claude_transcript_path", return_value=None
        ), mock.patch.object(
            provider_state,
            "capture_pane",
            return_value="Do you want to proceed?\n  1. Yes\n  2. No\nEsc to cancel\n",
        ):
            patched["load_targets"].return_value = {"worker": target("claude")}
            patched["target_info"].return_value = runtime("claude")
            payload = provider_state.snapshot_target("worker")

        self.assertEqual(payload["state"]["value"], "busy")
        self.assertEqual(payload["state"]["source"], "claude_agents")

    def test_live_prompt_that_owns_the_screen_is_needs_input_without_exact_status(self) -> None:
        with self.common("codex") as patched, mock.patch.object(
            provider_state, "process_tree_pids", return_value=[410]
        ), mock.patch.object(
            provider_state, "rollout_paths_for_pids", return_value=[]
        ), mock.patch.object(
            provider_state,
            "capture_pane",
            return_value="Would you like to run the following command?\n  1. Yes\n  2. No\nPress enter to confirm or esc to cancel\n",
        ):
            patched["load_targets"].return_value = {"worker": target("codex")}
            patched["target_info"].return_value = runtime("codex")
            payload = provider_state.snapshot_target("worker")

        self.assertEqual(payload["state"]["value"], "needs_input")
        self.assertEqual(payload["state"]["source"], "tmux_live_tail")
        self.assertTrue(any(item["kind"] == "permission_prompt" for item in payload["state"]["evidence"]))

    def test_prompt_words_above_an_input_box_are_conversation(self) -> None:
        rule = "─" * 60
        screen = "● 回答里引用了一句 Do you want to proceed?\n  1. Yes\n  2. No\n" + "\n".join([rule, "❯", rule, "  ⏵⏵ bypass permissions on"])
        value, _confidence, evidence = provider_state.live_tail_state(screen)
        self.assertNotEqual(value, "needs_input")
        self.assertFalse(any(item["kind"] == "permission_prompt" for item in evidence))

    def test_stale_permission_text_does_not_override_authoritative_busy_state(self) -> None:
        stale = "Do you want to proceed?\n1. Yes\n2. No\nEsc to cancel\n"
        current = "\n".join(f"progress line {index}" for index in range(20)) + "\nWorking… press esc to interrupt\n"
        with self.common("claude") as patched, mock.patch.object(
            provider_state, "process_tree_pids", return_value=[410]
        ), mock.patch.object(
            provider_state,
            "claude_agents_json",
            return_value=([{"pid": 410, "sessionId": "session-3", "status": "busy"}], ""),
        ), mock.patch.object(
            provider_state, "claude_transcript_path", return_value=None
        ), mock.patch.object(
            provider_state, "capture_pane", return_value=stale + current
        ):
            patched["load_targets"].return_value = {"worker": target("claude")}
            patched["target_info"].return_value = runtime("claude")
            payload = provider_state.snapshot_target("worker")

        self.assertEqual(payload["state"]["value"], "busy")
        self.assertEqual(payload["state"]["source"], "claude_agents")
        self.assertFalse(any(item["kind"] == "permission_prompt" for item in payload["state"]["evidence"]))

    def test_codex_run_index_requires_a_controller_pid_in_the_current_process_tree(self) -> None:
        index = {
            "threads": {"thread-7": {"last_run_id": "wrong-run"}},
            "runs": {
                "wrong-run": {
                    "thread_id": "thread-7",
                    "status": "running",
                    "daemon_pid": 999,
                    "updated_at": "2026-07-10T20:00:00+08:00",
                }
            },
        }
        with mock.patch.object(provider_state, "_read_json", return_value=index):
            result = provider_state.codex_exact_run_state("thread-7", pids=[410, 412])
        self.assertEqual(result, {})

    def test_codex_run_index_uses_the_latest_run_bound_to_the_current_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wrong_status = root / "wrong.json"
            right_status = root / "right.json"
            wrong_status.write_text(json.dumps({"status": "running", "daemon_pid": 999}), encoding="utf-8")
            right_status.write_text(json.dumps({"status": "completed", "daemon_pid": 410}), encoding="utf-8")
            index = {
                "threads": {"thread-7": {"last_run_id": "wrong-run"}},
                "runs": {
                    "wrong-run": {
                        "thread_id": "thread-7",
                        "status_file": str(wrong_status),
                        "daemon_pid": 999,
                        "updated_at": "2026-07-10T21:00:00+08:00",
                    },
                    "right-run": {
                        "thread_id": "thread-7",
                        "status_file": str(right_status),
                        "daemon_pid": 410,
                        "updated_at": "2026-07-10T20:00:00+08:00",
                    },
                },
            }

            def read(path: Path) -> dict:
                if path == provider_state.CODEX_RUN_INDEX:
                    return index
                return json.loads(path.read_text(encoding="utf-8"))

            with mock.patch.object(provider_state, "_read_json", side_effect=read):
                result = provider_state.codex_exact_run_state("thread-7", pids=[410, 412])

        self.assertEqual(result["run_id"], "right-run")
        self.assertEqual(result["status"], "completed")


if __name__ == "__main__":
    unittest.main()
