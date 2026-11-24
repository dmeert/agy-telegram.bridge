"""Environment-based configuration (12-factor; works with systemd ``EnvironmentFile=``)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

TRUST_MODES = ("restricted", "full")


class ConfigError(ValueError):
    """Raised when the environment is missing or has invalid settings."""


def _xdg(env: Mapping[str, str], var: str, default: str) -> Path:
    raw = (env.get(var) or "").strip()
    return Path(raw).expanduser() if raw else Path.home() / default


def _parse_user_ids(raw: str) -> frozenset[int]:
    ids = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.lstrip("-").isdigit():
            raise ConfigError(f"TELEGRAM_ALLOWED_USERS: {part!r} is not a numeric Telegram user ID")
        ids.add(int(part))
    return frozenset(ids)


def _parse_int(env: Mapping[str, str], var: str, default: Optional[int]) -> Optional[int]:
    raw = (env.get(var) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{var} must be an integer, got {raw!r}") from None


@dataclass(frozen=True)
class Settings:
    bot_token: str
    allowed_users: frozenset[int]
    agy_home: Path
    daemon_unit: str
    project_id: str
    project_name: str
    trust_mode: str
    workspace_dir: Path
    ls_port: Optional[int]
    csrf_token: Optional[str]
    turn_timeout: int
    log_file: Optional[Path]
    config_dir: Path = field(default_factory=lambda: Path.home() / ".config" / "agy-telegram-bridge")

    @property
    def agentapi_path(self) -> Path:
        return self.agy_home / "bin" / "agentapi"

    @property
    def conversations_db(self) -> Path:
        return self.agy_home / "conversation_summaries.db"

    @property
    def project_file(self) -> Path:
        return Path.home() / ".gemini" / "config" / "projects" / f"{self.project_id}.json"

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env

        token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if not token:
            token_file = (env.get("TELEGRAM_BOT_TOKEN_FILE") or "").strip()
            if token_file:
                try:
                    token = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
                except OSError as e:
                    raise ConfigError(f"TELEGRAM_BOT_TOKEN_FILE could not be read: {e}") from None
        if not token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is not set (get one from @BotFather)")

        allowed = _parse_user_ids(env.get("TELEGRAM_ALLOWED_USERS", ""))
        if not allowed:
            raise ConfigError(
                "TELEGRAM_ALLOWED_USERS is empty. The bot is deny-by-default: "
                "list the numeric Telegram user IDs allowed to control the agent."
            )

        trust = (env.get("AGY_TRUST_MODE") or "restricted").strip().lower()
        if trust not in TRUST_MODES:
            raise ConfigError(f"AGY_TRUST_MODE must be one of {TRUST_MODES}, got {trust!r}")

        project_id = (env.get("AGY_PROJECT_ID") or "telegram").strip()
        if not project_id or "/" in project_id or "\\" in project_id or project_id in (".", ".."):
            raise ConfigError(f"AGY_PROJECT_ID must be a simple identifier, got {project_id!r}")

        turn_timeout = _parse_int(env, "AGY_TURN_TIMEOUT", 3000)
        if turn_timeout is None or turn_timeout <= 0:
            raise ConfigError(f"AGY_TURN_TIMEOUT must be a positive integer, got {turn_timeout!r}")

        data_home = _xdg(env, "XDG_DATA_HOME", ".local/share")
        config_home = _xdg(env, "XDG_CONFIG_HOME", ".config")
        log_file = (env.get("AGY_TELEGRAM_LOG_FILE") or "").strip()

        return cls(
            bot_token=token,
            allowed_users=allowed,
            agy_home=Path(env.get("AGY_HOME") or Path.home() / ".gemini" / "antigravity-cli").expanduser(),
            daemon_unit=(env.get("AGY_DAEMON_UNIT") or "antigravity-cli-daemon.service").strip(),
            project_id=project_id,
            project_name=(env.get("AGY_PROJECT_NAME") or "Telegram").strip(),
            trust_mode=trust,
            workspace_dir=Path(
                env.get("AGY_WORKSPACE_DIR") or data_home / "agy-telegram-bridge" / "workspace"
            ).expanduser(),
            ls_port=_parse_int(env, "AGY_LS_PORT", None),
            csrf_token=(env.get("AGY_CSRF_TOKEN") or "").strip() or None,
            turn_timeout=turn_timeout,
            log_file=Path(log_file).expanduser() if log_file else None,
            config_dir=config_home / "agy-telegram-bridge",
        )
