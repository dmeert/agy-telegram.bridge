"""Locate the Antigravity daemon's language-server endpoint and CSRF token.

Everything is kept in memory only; nothing is written to disk. The CSRF token
comes from ``AGY_CSRF_TOKEN`` if set, otherwise from the ``--csrf_token`` flag
on the command line of the daemon process owned by the same user.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

from .config import Settings

log = logging.getLogger(__name__)

_PROBE_PATH = "/exa.language_server_pb.LanguageServerService/GetServerConfiguration"
# Variables the agent subprocess must never inherit from the bridge's own environment.
_PRIVATE_ENV = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS")


class DaemonClient:
    def __init__(self, settings: Settings):
        self._s = settings
        self._lock = threading.RLock()
        self._port: Optional[int] = settings.ls_port
        self._token: Optional[str] = settings.csrf_token

    # -- process discovery -------------------------------------------------

    def pid(self) -> Optional[int]:
        try:
            res = subprocess.run(
                ["systemctl", "--user", "show", "-p", "MainPID", "--value", self._s.daemon_unit],
                capture_output=True, text=True, timeout=3,
            )
            val = res.stdout.strip()
            if val.isdigit() and int(val) > 0:
                return int(val)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            res = subprocess.run(
                ["pgrep", "-u", str(os.getuid()), "-f", "agy.*remote-control"],
                capture_output=True, text=True, timeout=3,
            )
            pids = [int(p) for p in res.stdout.split() if p.isdigit()]
            return pids[0] if pids else None
        except (OSError, subprocess.SubprocessError):
            return None

    @staticmethod
    def _cmdline(pid: int) -> str:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                return f.read().decode("utf-8", errors="ignore").replace("\0", " ")
        except OSError:
            return ""

    # -- port --------------------------------------------------------------

    @staticmethod
    def _probe(port: int, token: Optional[str] = None) -> Tuple[bool, bool]:
        """Return (is_language_server, token_accepted)."""
        if not 0 < port <= 65535:
            return False, False
        headers = {"Content-Type": "application/json"}
        if token:
            headers["x-codeium-csrf-token"] = token
        req = urllib.request.Request(f"http://127.0.0.1:{port}{_PROBE_PATH}", data=b"{}", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                body = json.loads(resp.read().decode() or "{}")
                return True, bool(token) and "config" in body
        except urllib.error.HTTPError as e:
            return e.code == 401, False  # 401 = language server, token missing/invalid
        except (OSError, ValueError):
            return False, False

    def _candidate_ports(self, pid: Optional[int]) -> List[int]:
        found: List[int] = []
        if self._port:
            found.append(self._port)
        if pid:
            m = re.search(r"--hub-port=(\d+)", self._cmdline(pid))
            if m:
                found.append(int(m.group(1)))
            try:
                res = subprocess.run(["ss", "-tlpnH"], capture_output=True, text=True, timeout=3)
                for line in res.stdout.splitlines():
                    if f"pid={pid}," in line:
                        m = re.search(r":(\d+)\s", line)
                        if m:
                            found.append(int(m.group(1)))
            except (OSError, subprocess.SubprocessError):
                log.debug("ss unavailable; skipping listening-port scan")
        return list(dict.fromkeys(found))

    def port(self, force_refresh: bool = False) -> Optional[int]:
        with self._lock:
            if self._port and not force_refresh and self._probe(self._port)[0]:
                return self._port
            for candidate in self._candidate_ports(self.pid()):
                if self._probe(candidate)[0]:
                    if candidate != self._port:
                        log.info("Language server found on port %s", candidate)
                    self._port = candidate
                    return candidate
            log.warning("Could not locate the language server port (set AGY_LS_PORT to override)")
            return None

    # -- CSRF token --------------------------------------------------------

    def _discover_token(self, port: int) -> Optional[str]:
        pid = self.pid()
        if not pid:
            return None
        m = re.search(r"--csrf_token=(\S+)", self._cmdline(pid))
        if not m:
            return None
        token = m.group(1).strip("'\"")
        return token if self._probe(port, token)[1] else None

    def token(self, port: int, force_refresh: bool = False) -> Optional[str]:
        with self._lock:
            if self._token and not force_refresh and self._probe(port, self._token)[1]:
                return self._token
            fresh = self._discover_token(port)
            if fresh:
                self._token = fresh
                log.info("CSRF token refreshed from daemon command line")
                return fresh
            if self._token and self._probe(port, self._token)[1]:
                return self._token
            log.warning("No valid CSRF token (set AGY_CSRF_TOKEN or start the daemon with --csrf_token=...)")
            return None

    # -- public helpers ----------------------------------------------------

    def status(self) -> Tuple[Optional[int], bool]:
        """(port, authenticated) for display; never raises."""
        port = self.port()
        if not port:
            return None, False
        return port, self.token(port) is not None

    def agent_env(self, force_refresh: bool = False) -> Dict[str, str]:
        """Environment for ``agentapi`` subprocesses (never includes the bridge's own secrets)."""
        env = {k: v for k, v in os.environ.items() if k not in _PRIVATE_ENV}
        env["ANTIGRAVITY_PROJECT_ID"] = self._s.project_id
        env["ANTIGRAVITY_CONVERSATION_ID"] = ""
        env["ANTIGRAVITY_SOURCE_METADATA"] = ""
        port = self.port(force_refresh=force_refresh)
        if port:
            env["ANTIGRAVITY_LS_ADDRESS"] = f"localhost:{port}"
            tok = self.token(port, force_refresh=force_refresh)
            if tok:
                env["ANTIGRAVITY_CSRF_TOKEN"] = tok
        return env
