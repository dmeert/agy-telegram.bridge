"""Send a one-way notification to the allowed Telegram users.

Usage::

    agy-telegram-notify "Backup finished"
    echo "disk 91% full" | agy-telegram-notify
    agy-telegram-notify --env-file /path/to/env "text"

Credentials come from the process environment, falling back to the bridge's
environment file (``~/.config/agy-telegram-bridge/env``). Exit status: 0 sent
to everyone, 1 delivery failed for at least one user, 2 configuration error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Mapping, Optional

from .config import ConfigError, Settings

DEFAULT_ENV_FILE = Path.home() / ".config" / "agy-telegram-bridge" / "env"
MAX_LEN = 4096


class NotifyError(RuntimeError):
    pass


def parse_env_file(path: Path) -> Dict[str, str]:
    """Parse ``KEY=VALUE`` lines the way systemd's EnvironmentFile does (no inline comments)."""
    values: Dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith(("#", ";")) or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        values[key] = value.strip().strip("\"'")
    return values


def send(token: str, chat_id: int, text: str, timeout: float = 10.0) -> None:
    body = json.dumps({"chat_id": chat_id, "text": text[:MAX_LEN], "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return
    except urllib.error.HTTPError as e:
        # Never include the URL (it embeds the token) in error messages.
        raise NotifyError(f"Telegram returned HTTP {e.code} for user {chat_id}") from None
    except (urllib.error.URLError, OSError) as e:
        raise NotifyError(f"network error for user {chat_id}: {type(e).__name__}") from None


def load_settings(env: Mapping[str, str], env_file: Path) -> Settings:
    merged = {**parse_env_file(env_file), **{k: v for k, v in env.items() if v}}
    return Settings.from_env(merged)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="agy-telegram-notify", description=__doc__.split("\n\n")[0])
    parser.add_argument("text", nargs="*", help="message text (default: read stdin)")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    args = parser.parse_args(argv)

    text = " ".join(args.text) if args.text else sys.stdin.read()
    text = text.strip()
    if not text:
        print("nothing to send", file=sys.stderr)
        return 2
    try:
        settings = load_settings(os.environ, args.env_file)
    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2

    failed = 0
    for user_id in sorted(settings.allowed_users):
        try:
            send(settings.bot_token, user_id, text)
        except NotifyError as e:
            print(f"error: {e}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
