"""Pure text helpers: GFM → Telegram HTML, chunking, button directives, tool-step labels."""

from __future__ import annotations

import html
import json
import os
import re
from typing import Any, Dict, List, Tuple

TELEGRAM_LIMIT = 4096
CHUNK_LIMIT = 3800

ButtonRows = List[List[Tuple[str, str]]]  # rows of (label, text sent to the agent when tapped)


def gfm_to_html(text: str) -> str:
    """Convert GitHub-flavoured Markdown into the HTML subset Telegram accepts.

    Telegram's legacy Markdown parser chokes on ``file://`` links and stray ``_``,
    so code, links and bold/headings are mapped to HTML explicitly.
    """
    stash: List[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00TG{len(stash) - 1}\x00"

    out = re.sub(
        r"```[^\n`]*\n(.*?)```",
        lambda m: keep(f"<pre><code>{html.escape(m.group(1).strip(chr(10)))}</code></pre>"),
        text,
        flags=re.DOTALL,
    )
    out = re.sub(r"`([^`\n]+)`", lambda m: keep(f"<code>{html.escape(m.group(1))}</code>"), out)

    def link(m: re.Match) -> str:
        label, url = m.group(1).strip().strip("`"), m.group(2).strip()
        if url.startswith(("http://", "https://")):
            return keep(f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>')
        return keep(f"<code>{html.escape(label)}</code>")  # file://, conversation://, ... → plain pill

    out = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link, out)
    out = html.escape(out)
    out = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"<b>\1</b>", out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"(?m)^(\s*)[-*]\s+", r"\1• ", out)
    for i, fragment in enumerate(stash):
        out = out.replace(f"\x00TG{i}\x00", fragment)
    return out


def split_chunks(text: str, limit: int = CHUNK_LIMIT) -> List[str]:
    """Split on paragraph/line boundaries, closing and reopening code fences across chunks."""
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        head, rest = rest[:cut].rstrip(), rest[cut:].lstrip("\n")
        if head.count("```") % 2:  # cut inside a fenced block
            head += "\n```"
            rest = "```\n" + rest
        chunks.append(head)
    if rest:
        chunks.append(rest)
    return chunks


def clip_utf8(text: str, max_bytes: int) -> str:
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


_BUTTON_JSON = re.compile(r"<!--\s*BUTTONS:\s*(.*?)\s*-->", re.DOTALL)
_BUTTON_INLINE = re.compile(r"\[BUTTON:\s*([^|\]]+?)\s*\|\s*([^\]]+?)\s*\]")


def _rows(items: List[Tuple[str, str]], per_row: int = 2) -> ButtonRows:
    return [items[i : i + per_row] for i in range(0, len(items), per_row)]


def extract_buttons(text: str) -> Tuple[str, ButtonRows]:
    """Strip explicit button directives from an agent reply.

    Supported: ``<!-- BUTTONS: ["A", {"text": "B", "data": "reply B"}] -->`` and
    ``[BUTTON: Label | text to send]``. Nothing is inferred from free text.
    """
    items: List[Tuple[str, str]] = []

    m = _BUTTON_JSON.search(text)
    if m:
        try:
            for entry in json.loads(m.group(1)):
                if isinstance(entry, str):
                    items.append((entry, entry))
                elif isinstance(entry, dict) and entry.get("text"):
                    items.append((str(entry["text"]), str(entry.get("data", entry["text"]))))
            text = text.replace(m.group(0), "")
        except (ValueError, TypeError):
            pass

    for label, data in _BUTTON_INLINE.findall(text):
        items.append((label.strip(), data.strip()))
    text = _BUTTON_INLINE.sub("", text)
    return text.strip(), _rows(items)


def format_tool_step(call: Dict[str, Any]) -> str:
    """One-line HTML status for a tool invocation."""
    name = str(call.get("name", "step"))
    args = call.get("args", {})
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    if not isinstance(args, dict):
        args = {}

    def pill(label: str, detail: str = "") -> str:
        detail = html.escape(str(detail))
        return f"⏳ <b>{label}</b> · <code>{detail}</code>" if detail else f"⏳ <b>{label}</b>"

    if name == "run_command":
        return pill("Command", args.get("toolSummary") or str(args.get("CommandLine", ""))[:48])
    if name in ("read_file", "view_file"):
        return pill("Read", os.path.basename(str(args.get("Path") or args.get("AbsolutePath") or "")))
    if name in ("edit_file", "write_file", "replace_file_content", "write_to_file"):
        return pill("Edit", os.path.basename(str(args.get("Path") or args.get("TargetFile") or "")))
    if name == "call_mcp_tool":
        server, tool = args.get("ServerName", "mcp"), args.get("ToolName", "")
        return pill("MCP", f"{server}/{tool}" if tool else server)
    if name == "search_web":
        return pill("Search", str(args.get("query") or "")[:40])
    if name == "invoke_subagent":
        return pill("Subagent", args.get("Role") or args.get("TypeName") or "subagent")
    return pill("Running", name)
