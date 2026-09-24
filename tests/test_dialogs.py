#!/usr/bin/env python3
"""Dialog reading, standing auto-answer policy, and verified driving."""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import dialogs  # noqa: E402

RULE = "─" * 60
INPUT_BOX = "\n".join([RULE, "❯", RULE, "  ⏵⏵ bypass permissions on"])


def permission_prompt(selected: int = 0) -> str:
    rows = ["Yes", "Yes, and don't ask again for git commands in /repo", "No, and tell Claude what to do differently"]
    lines = [f"{'   ❯' if i == selected else '    '} {i + 1}. {text}" for i, text in enumerate(rows)]
    return "\n".join(["● Bash(git push)", RULE, " Bash command", "", "   git push origin main", "",
                      " Do you want to proceed?", *lines, "", " Esc to cancel · Tab to amend"])


class ReadAndPolicyTest(unittest.TestCase):
    def test_auto_mode_offer_keeps_bypass(self) -> None:
        screen = (Path(__file__).with_name("fixtures_claude_auto_mode_dialog.txt")).read_text(encoding="utf-8")
        dialog = dialogs.read_dialog(screen)
        self.assertEqual(dialog.kind, "mode-offer")
        self.assertEqual(dialog.options[dialogs.auto_answer_index(dialog)], "No, keep bypass permissions")

    def test_real_folder_trust_dialog_is_answered_yes(self) -> None:
        # Real Claude Code 2.1.280 wording ("Accessing workspace … one you trust?"),
        # highlight starts on "No, exit".
        screen = Path(__file__).with_name("fixtures_claude_trust_dialog.txt").read_text(encoding="utf-8")
        dialog = dialogs.read_dialog(screen)
        self.assertEqual(dialog.kind, "trust")
        self.assertEqual(dialog.options[dialogs.auto_answer_index(dialog)], "Yes, I trust this folder")

    def test_real_bash_permission_prompt_picks_always_allow(self) -> None:
        # Real Claude Code 2.1.280 prompt (verified end-to-end: the command ran
        # after the automatic answer).
        screen = Path(__file__).with_name("fixtures_claude_bash_permission.txt").read_text(encoding="utf-8")
        dialog = dialogs.read_dialog(screen)
        self.assertEqual((dialog.kind, dialog.selected), ("permission", 0))
        self.assertTrue(dialog.options[dialogs.auto_answer_index(dialog)].startswith("Yes, and always allow"))

    def test_real_plan_approval_proceeds_with_auto_accept(self) -> None:
        # Real Claude Code 2.1.280 plan-mode exit dialog.
        screen = Path(__file__).with_name("fixtures_claude_plan_approval.txt").read_text(encoding="utf-8")
        dialog = dialogs.read_dialog(screen)
        self.assertEqual(dialog.kind, "plan-approval")
        self.assertEqual(dialog.options[dialogs.auto_answer_index(dialog)], "Yes, auto-accept edits")

    def test_permission_prompt_picks_the_most_permissive_yes(self) -> None:
        dialog = dialogs.read_dialog(permission_prompt())
        self.assertEqual(dialog.kind, "permission")
        self.assertEqual(dialog.selected, 0)
        self.assertIn("don't ask again", dialog.options[dialogs.auto_answer_index(dialog)])

    def test_questions_about_the_work_are_never_auto_answered(self) -> None:
        screen = "\n".join([RULE, " Which database should the service use?", "",
                            "   ❯ 1. PostgreSQL", "     2. SQLite", "", " Enter to select · Esc to cancel"])
        dialog = dialogs.read_dialog(screen)
        self.assertEqual(dialog.kind, "question")
        self.assertIsNone(dialogs.auto_answer_index(dialog))

    def test_real_codex_work_question_is_never_auto_answered(self) -> None:
        # Real Codex 0.156.1 request_user_input whose question reads like a
        # permission prompt ("Do you want to proceed …?").
        screen = Path(__file__).with_name("fixtures_codex_request_user_input.txt").read_text(encoding="utf-8")
        dialog = dialogs.read_dialog(screen)
        self.assertEqual(dialog.kind, "question")
        self.assertIsNone(dialogs.auto_answer_index(dialog))

    def test_claude_ask_user_question_rows_mark_a_work_question(self) -> None:
        # Claude Code AskUserQuestion always adds "Type something." (strings
        # verified in the 2.1.280 binary).
        screen = "\n".join([RULE, " ☐ Migration  ✔ Submit  →", "", " Do you want to proceed with the database migration?", "",
                            " ❯ 1. Yes, migrate now", "   2. No, wait until Friday", "   3. Type something.", "",
                            " Enter to select · Tab/Arrow keys to navigate · Esc to cancel"])
        dialog = dialogs.read_dialog(screen)
        self.assertEqual(dialog.kind, "question")
        self.assertIsNone(dialogs.auto_answer_index(dialog))

    def test_real_codex_plan_prompt_ignores_numbered_plan_steps_above(self) -> None:
        # Real Codex 0.156.1 "Implement this plan?"; the plan's own "1. … 2. …"
        # and an earlier "Do you want to proceed" answer sit right above it.
        screen = Path(__file__).with_name("fixtures_codex_plan_implement.txt").read_text(encoding="utf-8")
        dialog = dialogs.read_dialog(screen)
        self.assertEqual((dialog.kind, dialog.numbers, dialog.selected), ("plan-approval", [1, 2, 3], 0))
        self.assertTrue(dialog.options[dialogs.auto_answer_index(dialog)].startswith("Yes, implement this plan"))

    def test_question_is_only_the_lines_right_above_the_options(self) -> None:
        history = [f"● earlier line {n}" for n in range(20)]
        screen = "\n".join(["● Do you want to proceed?", *history, " Pick a colour", "   › 1. Red", "     2. Blue"])
        dialog = dialogs.read_dialog(screen)
        self.assertEqual(dialog.kind, "question")
        self.assertEqual(dialog.question.splitlines()[-1], "Pick a colour")
        self.assertLessEqual(len(dialog.question.splitlines()), dialogs.QUESTION_LINES)

    def test_codex_draft_under_a_numbered_reply_is_not_a_dialog(self) -> None:
        # Codex hides its placeholder once the input box holds a draft, so the
        # input region cannot be recognised; the draft row "› 1. …" must still
        # not be taken for a highlighted option (Enter would submit the draft).
        footer = "  GPT-6-Astra default · ~/work/demo"
        draft_as_option = "\n".join(["• This step requires approval from ops:", "  1. Run migrations", "  2. Deploy", "",
                                     "› 3. Run the full test suite before deploying", "", footer])
        self.assertIsNone(dialogs.read_dialog(draft_as_option))
        draft_below_list = "\n".join(["• Do you want to proceed?", "  1. Yes", "  2. No", "", "› 继续", "", footer])
        self.assertIsNone(dialogs.read_dialog(draft_below_list))
        lone_draft = "\n".join(["• requires approval", "", "› 1. Run the full test suite", "", footer])
        self.assertIsNone(dialogs.read_dialog(lone_draft))

    def test_account_choices_are_left_to_the_user(self) -> None:
        screen = "\n".join([RULE, " Detected a custom API key in your environment", " Do you want to use this API key?",
                            " ❯ 1. Yes", "   2. No (recommended)", " Enter to confirm · Esc to cancel"])
        dialog = dialogs.read_dialog(screen)
        self.assertEqual(dialog.kind, "question")
        self.assertIsNone(dialogs.auto_answer_index(dialog))

    def test_no_dialog_while_the_input_box_is_drawn(self) -> None:
        self.assertIsNone(dialogs.read_dialog("● Do you want to proceed?\n  1. Yes\n  2. No\n" + INPUT_BOX))


class MatchOptionTest(unittest.TestCase):
    def test_short_answer_does_not_match_a_longer_sibling(self) -> None:
        options = ["Yes", "Yes, and don't ask again for git commands", "No"]
        self.assertEqual(dialogs.match_option(options, "Yes"), 0)
        self.assertEqual(dialogs.match_option(options, "Yes, and don't ask again for git commands"), 1)

    def test_truncated_row_still_matches(self) -> None:
        options = ["Yes", "Yes, and don't ask again for git comma…"]
        self.assertEqual(dialogs.match_option(options, "Yes, and don't ask again for git commands in /repo"), 1)


class DriveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.selected = 0
        self.open = True
        self.sent: list[str] = []

    def capture(self) -> str:
        return permission_prompt(self.selected) if self.open else "● done\n" + INPUT_BOX

    def send(self, key: str) -> None:
        self.sent.append(key)
        if key == "Down":
            self.selected = min(self.selected + 1, 2)
        elif key == "Up":
            self.selected = max(self.selected - 1, 0)
        elif key == "C-m":
            self.open = False

    def test_auto_approve_moves_and_verifies_before_enter(self) -> None:
        result = dialogs.auto_approve(capture=self.capture, send=self.send, sleep=lambda _s: None)
        self.assertEqual(self.sent, ["Down", "C-m"])
        self.assertEqual(result["kind"], "permission")

    def test_nothing_is_sent_when_the_dialog_is_gone(self) -> None:
        self.open = False
        self.assertIsNone(dialogs.auto_approve(capture=self.capture, send=self.send, sleep=lambda _s: None))
        with self.assertRaises(ValueError):
            dialogs.drive("Yes", capture=self.capture, send=self.send, sleep=lambda _s: None)
        self.assertEqual(self.sent, [])

    def test_driving_stops_when_another_dialog_replaces_the_pinned_one(self) -> None:
        screens = iter([permission_prompt(0), "\n".join([RULE, " Do you want to delete build/?", " ❯ 1. Yes", "   2. No",
                                                         " Esc to cancel"])])
        with self.assertRaisesRegex(ValueError, r"different dialog.*1 navigation key"):
            dialogs.drive("No, and tell Claude what to do differently", capture=lambda: next(screens),
                          send=self.send, sleep=lambda _s: None)
        self.assertEqual(self.sent, ["Down"])

    def test_enter_that_does_not_close_the_dialog_is_a_failure(self) -> None:
        # e.g. tmux copy mode swallowed the key: never record it as answered.
        with self.assertRaisesRegex(RuntimeError, "still open"):
            dialogs.drive("Yes", capture=lambda: permission_prompt(0), send=self.sent.append, sleep=lambda _s: None)
        self.assertEqual(self.sent, ["C-m"])


class PaneGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = dialogs.LOCK_DIR
        dialogs.LOCK_DIR = Path(self.tmp.name)

    def tearDown(self) -> None:
        dialogs.LOCK_DIR = self.saved
        self.tmp.cleanup()

    def test_second_sender_on_the_same_pane_is_turned_away(self) -> None:
        sent: list[str] = []
        with dialogs.pane_lock("%7"):
            with self.assertRaises(dialogs.PaneBusy):
                dialogs.auto_approve(capture=permission_prompt, send=sent.append, sleep=lambda _s: None, pane_id="%7")
            with dialogs.pane_lock("%8"):
                pass  # other panes are independent
        self.assertEqual(sent, [])

    def test_copy_mode_and_recent_typing_keep_automatic_senders_away(self) -> None:
        def tmux(mode: str, activity: int):
            return lambda args: mode if args[0] == "display-message" else f"{activity}\t%7\n{activity}\t%9\n"

        self.assertIn("copy mode", dialogs.pane_hands_off("%7", tmux("1", 0), now=1000))
        self.assertIn("typed", dialogs.pane_hands_off("%7", tmux("0", 995), now=1000))
        self.assertEqual(dialogs.pane_hands_off("%7", tmux("0", 995), human=False, now=1000), "")
        self.assertEqual(dialogs.pane_hands_off("%7", tmux("0", 900), now=1000), "")
        sent: list[str] = []
        with self.assertRaises(dialogs.PaneBusy):
            dialogs.auto_approve(capture=permission_prompt, send=sent.append, sleep=lambda _s: None, pane_id="%7",
                                 tmux=tmux("0", int(time.time())))
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
