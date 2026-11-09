# Integration discovery (read-only)

You are running through a Telegram bridge on the user's own machine. Find out which parts of this user's environment could usefully connect to this Telegram chat, and **propose** integrations. Do not build anything yet.

## Ground rules

- **Read-only.** Do not install, modify, restart, delete, or send anything. No port scans, no LAN sweeps, no credential guessing. Only inspect localhost, local config files, and tools that are already configured for you.
- **Never print secrets** (tokens, passwords, keys, cookies, URLs containing credentials). If you find one, say where it lives (file or variable name), never its value.
- **No private content.** Do not open or summarise mail bodies, documents, photos or messages. Counts and metadata only.
- If a check needs a permission you don't have, skip it and list it under "Couldn't check".
- Keep every step cheap. Prefer listing and reading configuration over running anything heavy.

## What to look at

1. **Your own tooling:** configured MCP servers, skills and plugins (look under the agent's config and app-data directories). Each one is a ready-made integration surface.
2. **Host services:** `systemctl --user` units and timers, system services worth watching, containers (Docker/Podman if present), your own listening local ports.
3. **Home and network:** Home Assistant, router/controller, NAS, MQTT broker, and similar, but only what is visible from existing configuration, configured tools or localhost.
4. **Personal productivity:** calendar, mail, tasks, notes and drive tools already reachable through MCP or a CLI.
5. **Dev and ops:** git repositories with remotes (CI status, releases), cron jobs and timers, backup jobs, existing log files, monitoring tools, and useful CLIs on `PATH` (`gh`, `kubectl`, `terraform`, ...).
6. **Hardware:** sensors, GPIO or cameras, only when an existing service already exposes them.

## What to produce

The reader is on a phone: short lines, no wide tables.

1. **Inventory**: one line per finding: name · what it is · how you found it.
2. **Proposals**: at most 8, ranked by value ÷ effort. For each:
   - **Idea**: one sentence (for example "Push a message when a systemd timer fails").
   - **Type**: *notification* (host → Telegram via `agy-telegram-notify "text"`), *command* (Telegram → host), or *conversational* (just ask the agent).
   - **Effort** S/M/L · **Risk** low/med/high: what could happen if this Telegram account were compromised.
   - **Needs**: tools, credentials, permissions.
3. **Not recommended**: possible but unwise over a chat (anything that unlocks doors, moves money, or deletes data without a confirmation step).
4. **Couldn't check**: gaps and why.

End with exactly one line like the following so the user can pick the next step with a tap, and adapt the labels to your top proposals:

<!-- BUTTONS: ["Build proposal 1", "Build proposal 2", "Build proposal 3", "Dig deeper"] -->

Do not implement anything until the user chooses.
