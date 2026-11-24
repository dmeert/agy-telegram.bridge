import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agy_telegram_bridge.agent import Agent
from agy_telegram_bridge.config import ConfigError, Settings
from agy_telegram_bridge.daemon import DaemonClient


class ProjectSyncAndHardening(unittest.TestCase):
    def _make_agent(self, home: Path, trust_mode: str = "restricted", **extra: str) -> Agent:
        env = {
            "HOME": str(home),
            "TELEGRAM_BOT_TOKEN": "123:secret",
            "TELEGRAM_ALLOWED_USERS": "42",
            "AGY_TRUST_MODE": trust_mode,
            **extra,
        }
        with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
            settings = Settings.from_env(env)
        return Agent(settings, DaemonClient(settings))

    def test_restricted_creates_private_file_and_denies_bridge_config_dir(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                agent = self._make_agent(home, "restricted")
                agent.ensure_project()
                project_file = agent._s.project_file
                self.assertTrue(project_file.exists())
                mode = stat.S_IMODE(project_file.stat().st_mode)
                self.assertEqual(mode, 0o600)

                data = json.loads(project_file.read_text(encoding="utf-8"))
                grants = data["permissionGrants"]["permissionGrants"]
                cfg_dir = str(home / ".config" / "agy-telegram-bridge")
                self.assertIn(f"read_file({cfg_dir})", grants["deny"])
                self.assertIn(f"write_file({cfg_dir})", grants["deny"])
                self.assertNotIn("allow", grants)
                self.assertNotIn("settings", data)

    def test_preserves_ui_grants_and_metadata_in_restricted_mode(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                agent = self._make_agent(home, "restricted")
                agent.ensure_project()
                project_file = agent._s.project_file

                # Simulate Antigravity UI adding granular grants, metadata, and settings.
                ui_edited = json.loads(project_file.read_text(encoding="utf-8"))
                ui_edited["updatedAt"] = "2026-11-24T09:00:00Z"
                ui_edited["isWorkspaceOnly"] = True
                ui_edited["permissionGrants"]["v2Migrated"] = True
                ui_edited["permissionGrants"]["permissionGrants"]["allow"] = ["command(git)", "mcp(home-assistant/*)"]
                ui_edited["permissionGrants"]["permissionGrants"]["ask"] = ["command(rm)"]
                ui_edited["settings"] = {"sandboxMode": True}
                project_file.write_text(json.dumps(ui_edited, indent=2), encoding="utf-8")

                agent.ensure_project()
                after = json.loads(project_file.read_text(encoding="utf-8"))
                self.assertEqual(after["updatedAt"], "2026-11-24T09:00:00Z")
                self.assertTrue(after["isWorkspaceOnly"])
                self.assertTrue(after["permissionGrants"]["v2Migrated"])
                self.assertEqual(
                    after["permissionGrants"]["permissionGrants"]["allow"],
                    ["command(git)", "mcp(home-assistant/*)"],
                )
                self.assertEqual(after["permissionGrants"]["permissionGrants"]["ask"], ["command(rm)"])
                self.assertEqual(after["settings"], {"sandboxMode": True})

    def test_downgrading_full_to_restricted_removes_wildcards_but_keeps_user_grants(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                full_agent = self._make_agent(home, "full")
                full_agent.ensure_project()
                project_file = full_agent._s.project_file

                # User also adds a custom rule while in full mode.
                data = json.loads(project_file.read_text(encoding="utf-8"))
                data["permissionGrants"]["permissionGrants"]["allow"].append("command(git)")
                project_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

                # Restarting in restricted mode must strip full-mode wildcards and settings.
                restricted_agent = self._make_agent(home, "restricted")
                restricted_agent.ensure_project()

                after = json.loads(project_file.read_text(encoding="utf-8"))
                grants = after["permissionGrants"]["permissionGrants"]
                self.assertEqual(grants.get("allow"), ["command(git)"])
                self.assertNotIn("unsandboxed(*)", grants.get("allow", []))
                self.assertNotIn("settings", after)
                cfg_dir = str(home / ".config" / "agy-telegram-bridge")
                self.assertIn(f"read_file({cfg_dir})", grants["deny"])

    def test_send_message_syncs_project_when_resuming_existing_conversation(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                full_agent = self._make_agent(home, "full")
                full_agent.ensure_project()

                restricted_agent = self._make_agent(home, "restricted")
                fake_proc = mock.Mock(returncode=0, stdout="{}", stderr="")
                with mock.patch.object(restricted_agent, "_run", return_value=fake_proc):
                    ok, err = restricted_agent.send_message("conv-123", "[via Telegram] hello")
                self.assertTrue(ok)
                self.assertEqual(err, "")

                after = json.loads(restricted_agent._s.project_file.read_text(encoding="utf-8"))
                self.assertNotIn("settings", after)
                self.assertNotIn("allow", after["permissionGrants"]["permissionGrants"])

    def test_token_file_and_project_id_validation(self):
        with tempfile.TemporaryDirectory() as d:
            tf = Path(d) / "bot_token"
            tf.write_text("999:from-file\n", encoding="utf-8")
            s = Settings.from_env({"TELEGRAM_BOT_TOKEN_FILE": str(tf), "TELEGRAM_ALLOWED_USERS": "1"})
            self.assertEqual(s.bot_token, "999:from-file")
            client = DaemonClient(s)
            with (
                mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN_FILE": str(tf)}, clear=False),
                mock.patch.object(client, "port", return_value=None),
            ):
                self.assertNotIn("TELEGRAM_BOT_TOKEN_FILE", client.agent_env())

            with self.assertRaises(ConfigError):
                Settings.from_env({
                    "TELEGRAM_BOT_TOKEN": "t",
                    "TELEGRAM_ALLOWED_USERS": "1",
                    "AGY_PROJECT_ID": "../escape",
                })
            with self.assertRaises(ConfigError):
                Settings.from_env({
                    "TELEGRAM_BOT_TOKEN": "t",
                    "TELEGRAM_ALLOWED_USERS": "1",
                    "AGY_TURN_TIMEOUT": "0",
                })


if __name__ == "__main__":
    unittest.main()
