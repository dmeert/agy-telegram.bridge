# agy-telegram.bridge

A lightweight bridge connecting AI agents with Telegram for real-time messaging, task execution, and automated notifications.

It relays Telegram messages to a headless [Antigravity](https://antigravity.google) CLI daemon (`agy remote-control serve`) via `agentapi`, streams live tool-step progress back into the chat, and renders the agent's GitHub-flavoured Markdown as native Telegram HTML.

## Features

- **Isolated project** – bot conversations live in their own Antigravity project and never mix with CLI/web sessions.
- **Live progress** – a single status line (`Command`, `Read`, `MCP`, …) is edited in place while the agent works.
- **Interactive prompts** – `ask_question` calls are forwarded as inline buttons; agents can also emit explicit buttons (see below).
- **Faithful rendering** – GFM → Telegram HTML (bold, headings, code fences, links), long replies split on paragraph boundaries without breaking code fences.
- **Non-blocking** – concurrent update handling, blocking work in threads, transcripts streamed by byte offset (never re-read).
- **Stateless on disk** – no tokens or state files written; the daemon's port and CSRF token are discovered and kept in memory.

## Security model

| | |
| :-- | :-- |
| **Authorization** | Deny by default. Only numeric user IDs in `TELEGRAM_ALLOWED_USERS`, in **private chats**, are served. Everyone else is ignored silently (their ID is logged so you can see who tried). |
| **Trust mode** | `restricted` (default): the project only gets a workspace folder; Antigravity's own permission policy applies. `full`: pre-approves file access, commands, URLs and MCP tools and disables the sandbox. **`full` means anyone who controls your Telegram account or bot token controls your machine.** |
| **Secrets** | `TELEGRAM_BOT_TOKEN` is read from the environment, never logged (httpx request logging is suppressed because Telegram URLs embed the token) and stripped from the environment of `agentapi` child processes. |
| **CSRF token** | Taken from `AGY_CSRF_TOKEN`, or read from the `--csrf_token=` flag on the command line of the same user's daemon process. No process-memory scanning. |
| **Conversation index** | Antigravity's SQLite index is opened read-only. |

Treat `TELEGRAM_ALLOWED_USERS` as an admin list and enable 2FA on those Telegram accounts.

## Requirements

- Linux with systemd user units, Python ≥ 3.11
- A running Antigravity CLI daemon and the `agentapi` wrapper (`~/.gemini/antigravity-cli/bin/agentapi`)
- A bot token from [@BotFather](https://t.me/BotFather) and your numeric Telegram user ID (e.g. from [@userinfobot](https://t.me/userinfobot))

## Install

```bash
python3 -m venv ~/.local/share/agy-telegram-bridge/venv
~/.local/share/agy-telegram-bridge/venv/bin/pip install .

install -d -m 700 ~/.config/agy-telegram-bridge
install -m 600 examples/env.example ~/.config/agy-telegram-bridge/env   # then edit it

install -D -m 644 examples/agy-telegram-bridge.service ~/.config/systemd/user/agy-telegram-bridge.service
systemctl --user daemon-reload
systemctl --user enable --now agy-telegram-bridge.service
journalctl --user -u agy-telegram-bridge.service -f     # if the user journal is persisted
```

If your daemon unit isn't called `antigravity-cli-daemon.service`, set `AGY_DAEMON_UNIT` and adjust `After=`/`Wants=` in the unit.

## Configuration

All settings are environment variables (see [`examples/env.example`](examples/env.example)).

| Variable | Default | Meaning |
| :-- | :-- | :-- |
| `TELEGRAM_BOT_TOKEN` | – (required) | Bot token from BotFather |
| `TELEGRAM_ALLOWED_USERS` | – (required) | Comma-separated numeric Telegram user IDs |
| `AGY_TRUST_MODE` | `restricted` | `restricted` or `full` (see above) |
| `AGY_WORKSPACE_DIR` | `~/.local/share/agy-telegram-bridge/workspace` | Folder the agent project may work in |
| `AGY_HOME` | `~/.gemini/antigravity-cli` | Antigravity app-data directory |
| `AGY_DAEMON_UNIT` | `antigravity-cli-daemon.service` | systemd user unit of the daemon |
| `AGY_PROJECT_ID` / `AGY_PROJECT_NAME` | `telegram` / `Telegram` | Dedicated project identity |
| `AGY_TURN_TIMEOUT` | `3000` | Seconds to wait for a final answer |
| `AGY_LS_PORT`, `AGY_CSRF_TOKEN` | auto | Override endpoint discovery |
| `AGY_TELEGRAM_LOG_FILE` | unset | Also log to a rotating file (5 MB × 3); default is stdout/journal only |

## Usage

| Command | Purpose |
| :-- | :-- |
| `/start`, `/help` | Status card |
| `/session` | Daemon and session status |
| `/new [prompt]` | Start a fresh conversation (optionally with its first prompt) |
| any text | Prompt the agent |

Sending a message while a turn is running delivers it to that turn; the existing stream keeps reporting.

### Buttons from the agent

An agent reply may include explicit buttons, which are stripped from the visible text:

```text
<!-- BUTTONS: ["Yes", {"text": "Not now", "data": "Ask me again tomorrow"}] -->
[BUTTON: Deploy | deploy to staging]
```

Tapping a button sends its `data` (or label) back to the agent as your next message. Nothing is inferred from free text.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

## License

[Apache-2.0](LICENSE) — permissive, with an explicit patent grant.
