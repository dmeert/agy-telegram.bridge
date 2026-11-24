"""Pure text helpers: GFM → Telegram HTML, chunking, button directives, tool-step labels."""

from __future__ import annotations

import html
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

TELEGRAM_LIMIT = 4096
CHUNK_LIMIT = 3800

ButtonRows = List[List[Tuple[str, str]]]  # rows of (label, text sent to the agent when tapped)

_FENCE_LINE = re.compile(r"(?m)^[ \t]*```([^\n`]*)$")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(\s*((?:[^()\s]|\([^()\s]*\))+)\s*\)")


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
        lambda m: keep(f"<pre><code>{html.escape(m.group(1).strip(chr(10)), quote=False)}</code></pre>"),
        text,
        flags=re.DOTALL,
    )
    out = re.sub(r"`([^`\n]+)`", lambda m: keep(f"<code>{html.escape(m.group(1), quote=False)}</code>"), out)

    def link(m: re.Match) -> str:
        label, url = m.group(1).strip().strip("`"), m.group(2).strip()
        if url.startswith(("http://", "https://")):
            return keep(f'<a href="{html.escape(url, quote=True)}">{html.escape(label, quote=False)}</a>')
        return keep(f"<code>{html.escape(label, quote=False)}</code>")  # file://, conversation://, ... → plain pill

    out = _LINK_RE.sub(link, out)
    out = html.escape(out, quote=False)
    out = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"<b>\1</b>", out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"(?m)^(\s*)[-*]\s+", r"\1• ", out)
    for i, fragment in enumerate(stash):
        out = out.replace(f"\x00TG{i}\x00", fragment)
    return out


def _unclosed_fence(text: str) -> Optional[str]:
    """Return the language token of an unclosed fenced block in ``text``, or ``None``."""
    open_lang: Optional[str] = None
    for m in _FENCE_LINE.finditer(text):
        info = m.group(1).strip()
        if open_lang is None:
            open_lang = info.split()[0][:24] if info else ""
        elif not info:
            open_lang = None
    return open_lang


def split_chunks(text: str, limit: int = CHUNK_LIMIT) -> List[str]:
    """Split on paragraph/line boundaries, closing and reopening code fences across chunks."""
    html_budget = TELEGRAM_LIMIT if limit == CHUNK_LIMIT else None
    if len(text) <= limit and (html_budget is None or len(gfm_to_html(text)) <= html_budget):
        return [text]
    chunks: List[str] = []
    rest = text
    while len(rest) > limit or (html_budget is not None and len(gfm_to_html(rest)) > html_budget):
        cap = min(len(rest), limit)
        while True:
            cut = rest.rfind("\n\n", 0, cap)
            if cut < cap // 2:
                cut = rest.rfind("\n", 0, cap)
            if cut < cap // 2:
                cut = cap
            head = rest[:cut].rstrip()
            lang = _unclosed_fence(head)
            closed = f"{head}\n```" if lang is not None else head
            if html_budget is None or len(gfm_to_html(closed)) <= html_budget or cap <= 64:
                break
            cap = max(64, int(cap * 0.75))
        rest = rest[cut:].lstrip("\n")
        if lang is not None:
            rest = f"```{lang}\n" + rest
        chunks.append(closed)
    if rest:
        chunks.append(rest)
    return chunks


def clip_utf8(text: str, max_bytes: int) -> str:
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def clip_utf16(text: str, max_units: int = TELEGRAM_LIMIT) -> str:
    """Clip ``text`` to ``max_units`` UTF-16 code units (Telegram's length metric)."""
    encoded = text.encode("utf-16-le")
    if len(encoded) <= max_units * 2:
        return text
    return encoded[: max_units * 2].decode("utf-16-le", errors="ignore")


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
