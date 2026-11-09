# Security policy

This bridge can drive an agent that runs commands on your machine, so security reports are taken seriously.

**Please do not open a public issue for vulnerabilities.** Use GitHub's
[private vulnerability reporting](../../security/advisories/new) on this repository instead.

In scope: authorization bypasses, leaking of the bot token or CSRF token, and anything that lets a
non-allow-listed Telegram user reach the agent. Only the latest release on `main` is supported.

See the "Security model" section of the [README](README.md) for the intended guarantees.
