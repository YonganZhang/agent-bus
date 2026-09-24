#!/usr/bin/env python3
"""Tests for durable Secretary Bus owner-mode configuration."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEADER = ROOT / "scripts" / "leader.py"


class BusConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.bus = Path(self.tmp.name) / "bus"
        self.env = os.environ.copy()
        self.env["AGENT_BUS_DIR"] = str(self.bus)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def leader(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(LEADER), *args],
            text=True,
            capture_output=True,
            env=self.env,
            check=True,
        )

    def test_trusted_owner_defaults_false_and_persists_when_enabled(self) -> None:
        initial = json.loads(self.leader("config", "--json").stdout)
        self.assertFalse(initial["trusted_owner"])

        enabled = json.loads(
            self.leader("config", "--trusted-owner", "true", "--json").stdout
        )
        self.assertTrue(enabled["trusted_owner"])
        self.assertEqual(json.loads((self.bus / "config.json").read_text())["trusted_owner"], True)

        reread = json.loads(self.leader("config", "--json").stdout)
        self.assertTrue(reread["trusted_owner"])

    def test_auto_approve_permissions_defaults_false_toggles_and_is_type_checked(self) -> None:
        # Auto-approve is opt-in: off until the operator enables it.
        self.assertFalse(json.loads(self.leader("config", "--json").stdout)["auto_approve_permissions"])
        on = json.loads(self.leader("config", "--auto-approve-permissions", "true", "--json").stdout)
        self.assertTrue(on["auto_approve_permissions"])
        off = json.loads(self.leader("config", "--auto-approve-permissions", "false", "--json").stdout)
        self.assertFalse(off["auto_approve_permissions"])
        self.assertFalse(off["trusted_owner"])
        stored = json.loads((self.bus / "config.json").read_text())
        self.assertIs(stored["auto_approve_permissions"], False)
        self.assertFalse(json.loads(self.leader("config", "--json").stdout)["auto_approve_permissions"])

        (self.bus / "config.json").write_text(json.dumps({"version": 1, "auto_approve_permissions": "yes"}))
        bad = subprocess.run(
            ["python3", str(LEADER), "config", "--json"], text=True, capture_output=True, env=self.env
        )
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("auto_approve_permissions must be true or false", bad.stderr)


    def test_env_switch_overrides_the_config_both_ways(self) -> None:
        import sys
        sys.path.insert(0, str(ROOT / "scripts"))
        import cli_bridge
        from unittest import mock

        with mock.patch.dict(os.environ, {"AGENT_BUS_AUTO_APPROVE": "1"}):
            self.assertTrue(cli_bridge.auto_approve_enabled())
        with mock.patch.dict(os.environ, {"AGENT_BUS_AUTO_APPROVE": "0"}):
            self.assertFalse(cli_bridge.auto_approve_enabled())


if __name__ == "__main__":
    unittest.main()
