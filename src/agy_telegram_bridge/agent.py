"""Talking to the Antigravity daemon: ``agentapi`` calls, conversation lookup, transcript streaming."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import Settings
from .daemon import DaemonClient

log = logging.getLogger(__name__)


def extract_error(proc: subprocess.CompletedProcess) -> str:
    out = (proc.stdout or "").strip()
    if out:
        try:
            data = json.loads(out)
            if isinstance(data, dict) and "error" in data:
                return str(data["error"])
        except ValueError:
            pass
    return (proc.stderr or "").strip() or out or "Unknown error"


def read_new_steps(path: Path, offset: int) -> Tuple[List[Dict[str, Any]], int]:
    """Read complete JSONL lines appended after ``offset``; return (steps, new_offset).

    A partially flushed trailing line is left for the next call, and the file
    is never re-read from the start.
    """
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
    except OSError:
        return [], offset
    end = chunk.rfind(b"\n")
    if end < 0:
        return [], offset
    steps: List[Dict[str, Any]] = []
    for line in chunk[: end + 1].decode("utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            steps.append(obj)
    return steps, offset + end + 1


class Agent:
    def __init__(self, settings: Settings, daemon: DaemonClient):
        self._s = settings
        self._daemon = daemon

    # -- project -------------------------------------------------------------

    def ensure_project(self) -> None:
        """Create/refresh the dedicated Antigravity project that isolates Telegram sessions.

        ``restricted`` (default) grants nothing beyond the workspace folder, so the
        agent falls back to Antigravity's own permission policy. ``full`` pre-approves
        file access, commands, URLs and MCP tools and disables the sandbox; only use
        it on a host you are happy for the agent (and anyone with your bot token or
        Telegram account) to control.
        """
        self._s.workspace_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        desired: Dict[str, Any] = {
            "id": self._s.project_id,
            "name": self._s.project_name,
            "projectResources": {"resources": [{"folderUri": self._s.workspace_dir.as_uri()}]},
        }
        if self._s.trust_mode == "full":
            desired["permissionGrants"] = {
                "permissionGrants": {
                    "allow": ["read_file(*)", "write_file(*)", "read_url(*)", "command(*)", "unsandboxed(*)", "mcp(*)"]
                }
            }
            desired["settings"] = {
                "fileAccessPolicy": "AGENT_SETTING_POLICY_ALLOW",
                "sandboxMode": False,
                "autoExecutionPolicy": "CASCADE_COMMANDS_AUTO_EXECUTION_EAGER",
                "artifactReviewMode": "ARTIFACT_REVIEW_MODE_TURBO",
            }
        path = self._s.project_file
        try:
            if path.exists() and json.loads(path.read_text()) == desired:
                return
        except (OSError, ValueError):
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(desired, indent=2))
        log.info("Wrote project definition %s (trust mode: %s)", path, self._s.trust_mode)

    # -- conversations (read-only view of Antigravity's index) --------------

    def _query(self, sql: str, params: tuple = ()) -> Optional[tuple]:
        db = self._s.conversations_db
        if not db.exists():
            return None
        try:
            con = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=2)
            try:
                return con.execute(sql, params).fetchone()
            finally:
                con.close()
        except sqlite3.Error as e:
            log.error("conversation index query failed: %s", e)
            return None

    def transcript_paths(self, conv_id: str) -> Tuple[Path, Path]:
        base = self._s.agy_home / "brain" / conv_id / ".system_generated" / "logs"
        return base / "transcript.jsonl", base / "transcript_full.jsonl"

    def active_conversation(self) -> str:
        """Newest live top-level conversation of the bridge's project, or ''."""
        row = self._query(
            "SELECT conversation_id FROM conversation_summaries "
            "WHERE project_id = ? AND parent_conversation_id = '' AND killed = 0 "
            "ORDER BY last_modified_time DESC LIMIT 1",
            (self._s.project_id,),
        )
        if row and row[0] and self.transcript_paths(row[0])[0].exists():
            return row[0]
        return ""

    def conversation_title(self, conv_id: str) -> str:
        row = self._query("SELECT title, step_count FROM conversation_summaries WHERE conversation_id = ?", (conv_id,))
        return f"{row[0] or 'Untitled'} ({row[1] or 0} steps)" if row else ""

    def full_content(self, conv_id: str, step_index: Any) -> str:
        """Untruncated content of one step, streamed from transcript_full.jsonl."""
        path = self.transcript_paths(conv_id)[1]
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(obj, dict) and obj.get("step_index") == step_index:
                        return obj.get("content") or ""
        except OSError:
            pass
        return ""

    # -- agentapi ------------------------------------------------------------

    def _run(self, args: List[str], timeout: int) -> subprocess.CompletedProcess:
        cmd = [str(self._s.agentapi_path), *args]
        proc = subprocess.run(cmd, env=self._daemon.agent_env(), capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            log.warning("agentapi %s failed (%s); refreshing daemon endpoint and retrying", args[0], extract_error(proc))
            proc = subprocess.run(
                cmd, env=self._daemon.agent_env(force_refresh=True), capture_output=True, text=True, timeout=timeout
            )
        return proc

    def send_message(self, conv_id: str, text: str) -> Tuple[bool, str]:
        try:
            proc = self._run(["send-message", f"--title={self._s.project_name}", conv_id, text], timeout=20)
        except (OSError, subprocess.SubprocessError) as e:
            return False, str(e)
        return (True, "") if proc.returncode == 0 else (False, extract_error(proc))

    def start_conversation(self, text: str) -> Tuple[Optional[str], str]:
        try:
            self.ensure_project()
            proc = self._run(["new-conversation", text], timeout=30)
        except (OSError, subprocess.SubprocessError) as e:
            return None, str(e)
        if proc.returncode != 0:
            return None, extract_error(proc)
        try:
            resp = json.loads(proc.stdout).get("response", {})
            conv_id = resp.get("conversationId") or resp.get("newConversation", {}).get("conversationId")
        except (ValueError, AttributeError):
            conv_id = None
        return (conv_id, "") if conv_id else (None, f"Unexpected response: {proc.stdout[:300]}")
