"""Bundled agent prompts."""

from importlib import resources


def discover_integrations() -> str:
    """Read-only prompt that makes the agent survey the user's environment and propose integrations."""
    return resources.files("agy_telegram_bridge").joinpath("prompts/discover_integrations.md").read_text(encoding="utf-8")
