# agy-telegram.bridge

A lightweight bridge connecting AI agents with Telegram for real-time messaging, task execution, and automated notifications.

It relays Telegram messages to a headless [Antigravity](https://antigravity.google) CLI daemon (`agy remote-control serve`) via `agentapi`, streams live tool-step progress back into the chat, and renders the agent's GitHub-flavoured Markdown as native Telegram HTML.

## Features

- **Isolated project** – bot conversations live in their own Antigravity project and never mix with CLI/web sessions.
- **Live progress** – a single status line (`Command`, `Read`, `MCP`, …) is edited in place while the agent works.
- **Interactive prompts** – `ask_question` calls are forwarded as inline buttons; agents can also emit explicit buttons (see below).
- **Faithful rendering** – GFM → Telegram HTML (bold, headings, code fences, links), long replies split on paragraph boundaries without breaking code fences.
- **Integration discovery** – `/discover` has the agent survey your environment read-only and propose what to connect.
- **Notifications** – `agy-telegram-notify` pushes one-way messages from scripts, timers and cron.
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

## Installation (step by step)

Plan on 10 minutes. You need a Linux machine that already runs the Antigravity CLI daemon, plus the Telegram app on your phone.

### 0. Check the prerequisites

```bash
python3 --version                                  # 3.11 or newer
systemctl --user --version | head -1               # systemd user session available
ls ~/.gemini/antigravity-cli/bin/agentapi          # the agentapi wrapper exists
pgrep -af 'remote-control'                         # the Antigravity daemon is running
```

The last check should print the `agy … remote-control serve` process. If your daemon isn't running yet, set it up first (see the Antigravity CLI docs); the bridge only talks to it. Note the name of the systemd user unit that runs it (`systemctl --user list-units | grep -i -e agy -e antigravity`). The default the bridge expects is `antigravity-cli-daemon.service`; if yours differs, set `AGY_DAEMON_UNIT` in step 4.

### 1. Create your bot with BotFather

1. In Telegram, open [@BotFather](https://t.me/BotFather). Check the blue verification tick; there are many look-alikes.
2. Send `/newbot`.
3. Enter a display name (anything, e.g. `My Agent`).
4. Enter a username. It must be unique and end in `bot`, e.g. `my_agent_example_bot`.
5. BotFather replies with an **HTTP API token** that looks like `123456789:AA…`. Keep it private: it is the bot's password. Don't paste it into chats, issues or screenshots.
6. Recommended hardening, still in BotFather:
   - `/setjoingroups` → choose your bot → **Disable** (nobody can add it to a group).
   - `/setprivacy` → choose your bot → **Enable** (default; irrelevant in private chats, but keeps it safe if you ever lift the group restriction).
7. If the token ever leaks: BotFather → `/revoke` → choose your bot, then put the new token in the env file (step 4) and restart the service.

### 2. Find your numeric Telegram user ID

The bridge only obeys numeric user IDs you list explicitly. **Usernames don't work**; they can be changed or claimed by someone else. Pick one:

- **Ask a bot:** message [@userinfobot](https://t.me/userinfobot); it replies with your `Id`.
- **Without a third-party bot:** finish steps 3 to 5 with `TELEGRAM_ALLOWED_USERS=1` as a placeholder, send any message to your bot, then read your ID from the log line `Rejected update from user_id=<ID> …` (`journalctl --user -u agy-telegram-bridge -n 20`, or `systemctl --user status agy-telegram-bridge`). Put that ID into the env file and restart.

### 3. Install the bridge

```bash
git clone https://github.com/dmeert/agy-telegram.bridge.git
cd agy-telegram.bridge

python3 -m venv ~/.local/share/agy-telegram-bridge/venv
~/.local/share/agy-telegram-bridge/venv/bin/pip install .
```

### 4. Configure it (token stays out of your shell history)

```bash
umask 077
install -d -m 700 ~/.config/agy-telegram-bridge

read -rsp "Bot token from BotFather: " T; echo
read -rp  "Your numeric Telegram user ID: " U
printf 'TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_ALLOWED_USERS=%s\n' "$T" "$U" > ~/.config/agy-telegram-bridge/env
unset T U

ls -l ~/.config/agy-telegram-bridge/env      # must show -rw-------
```

Optional settings (trust mode, workspace folder, daemon unit name, …) are documented in [`examples/env.example`](examples/env.example). Append them to the same file, **one `KEY=VALUE` per line and no trailing comments**.

> [!IMPORTANT]
> Decide on the trust mode now. The default `restricted` only grants the agent a workspace folder and leaves approvals to Antigravity's own policy. `AGY_TRUST_MODE=full` pre-approves commands, file access and MCP tools and disables the sandbox: **anyone who controls your Telegram account or bot token then controls your machine.** Start with `restricted`.

### 5. Run it as a service

```bash
install -D -m 644 examples/agy-telegram-bridge.service ~/.config/systemd/user/agy-telegram-bridge.service
systemctl --user daemon-reload
systemctl --user enable --now agy-telegram-bridge.service

# keep user services running when you're logged out (headless servers, Raspberry Pi, …)
loginctl enable-linger "$USER"

systemctl --user status agy-telegram-bridge.service
```

Healthy output includes `Starting bridge (project=Telegram, trust=restricted, allowed users=1)`. If you use a different daemon unit name, edit `After=`/`Wants=` in the installed unit.

*Try it without systemd first (optional):*

```bash
set -a; . ~/.config/agy-telegram-bridge/env; set +a
~/.local/share/agy-telegram-bridge/venv/bin/agy-telegram-bridge     # Ctrl-C to stop
```

### 6. Say hello

1. Open your bot in Telegram (the link BotFather gave you, `t.me/<username>`), press **Start**.
2. You should get a card with 🟢 and the project name. 🔴 means the bridge can't reach the daemon: see Troubleshooting.
3. Send a normal message such as `What can you do on this machine?`. You'll see a live status line (⏳ Read · …, ⏳ Command · …) and then the answer.
4. Tap **🔎 Discover** (or send `/discover`) to let the agent survey your environment and suggest integrations (next section).

### Troubleshooting

| Symptom | Cause and fix |
| :-- | :-- |
| `configuration error: TELEGRAM_BOT_TOKEN is not set` / `TELEGRAM_ALLOWED_USERS is empty` | Env file missing, empty, or not readable by the service. Check the path and permissions from step 4. |
| Bot never answers | Your ID isn't in `TELEGRAM_ALLOWED_USERS` (the bridge ignores strangers silently). Look for `Rejected update from user_id=…` in the log. Group chats are also ignored on purpose. |
| `Conflict: terminated by other getUpdates request` | Another process is polling with the **same token** (an old bot, a second copy, a dev machine). Stop it or create a separate bot. |
| 🔴 *daemon unreachable* | The daemon isn't running, or its unit name differs (`AGY_DAEMON_UNIT`). If it isn't started by systemd, set `AGY_LS_PORT` and `AGY_CSRF_TOKEN` yourself. |
| `Dispatch failed … agentapi … not found` | `agentapi` points at a binary that was replaced by an Antigravity update. Restart the daemon so it rewrites the wrapper, then retry. |
| Replies arrive but actions never run | In `restricted` mode the agent may be waiting for a permission prompt that a headless daemon can't show in Telegram. Grant what you need in the Antigravity UI, or consciously switch to `AGY_TRUST_MODE=full`. |
| Transient `Bad Gateway` / network warnings in the log | Telegram or your connection blipped; the bridge retries by itself. |
| No logs from `journalctl --user` | The user journal isn't persistent on your distro. Use `systemctl --user status`, or set `AGY_TELEGRAM_LOG_FILE`. |

Update with `git pull && ~/.local/share/agy-telegram-bridge/venv/bin/pip install . && systemctl --user restart agy-telegram-bridge`. Uninstall with `systemctl --user disable --now agy-telegram-bridge`, delete the unit, `~/.config/agy-telegram-bridge` and `~/.local/share/agy-telegram-bridge`, then `/deletebot` in BotFather if you no longer need the bot.

## Discover integrations in your own environment

Not sure what is worth connecting to Telegram? Let your agent find out, **read-only**:

- Send **`/discover`** (or tap **🔎 Discover** on the start card). The bridge starts a fresh thread with the bundled prompt [`discover_integrations.md`](src/agy_telegram_bridge/prompts/discover_integrations.md).
- Or paste that prompt into any Antigravity session on the machine.

The agent inventories what it can see (its MCP servers, skills and plugins, systemd units and timers, containers, local services, git repos, CLIs, and tools such as Home Assistant, a router or NAS, calendar and mail connectors) without scanning your network, printing secrets or opening private content. It then returns up to eight proposals ranked by value ÷ effort, each tagged **notification**, **command** or **conversational** with effort and risk, plus a "not recommended" list, and ends with tap-to-choose buttons. It builds nothing until you pick one.

Read the proposals critically: anything reachable from Telegram is only as safe as your Telegram account. Prefer notifications over commands, and put a confirmation step in front of anything that changes the physical world.

### Pushing notifications from scripts and timers

The bridge ships a tiny one-way sender used by many proposals:

```bash
~/.local/share/agy-telegram-bridge/venv/bin/agy-telegram-notify "Backup finished ✅"
echo "disk 91% full on $(hostname)" | ~/.local/share/agy-telegram-bridge/venv/bin/agy-telegram-notify
```

It reads the token and allowed users from the process environment or `~/.config/agy-telegram-bridge/env` (override with `--env-file`) and messages every allowed user. Exit codes: `0` sent, `1` delivery failed, `2` configuration error. Typical use is `OnFailure=` in a systemd unit or a line at the end of a cron job.

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
| `/discover` | Read-only survey of your environment, then integration proposals |
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
