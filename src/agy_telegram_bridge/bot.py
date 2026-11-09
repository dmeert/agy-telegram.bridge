"""Telegram front-end: auth, commands, and live streaming of agent turns."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import socket
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import formatting as fmt
from .agent import Agent, read_new_steps
from .config import ConfigError, Settings
from .daemon import DaemonClient

log = logging.getLogger("agy_telegram_bridge")

POLL_INTERVAL = 1.5      # transcript poll period (s)
TYPING_INTERVAL = 4.0    # Telegram "typing…" lasts ~5 s
PILL_INTERVAL = 5.0      # min seconds between progress-pill edits (rate limits)
PAYLOAD_CACHE = 512      # remembered button payloads (callback_data is capped at 64 bytes)


@dataclass
class Watch:
    """One live transcript follower per conversation."""

    expected_finals: int = 1


@dataclass
class App:
    settings: Settings
    agent: Agent
    daemon: DaemonClient
    host: str = field(default_factory=lambda: socket.gethostname().split(".")[0])
    fresh_next: bool = False  # /new was issued: next message starts a new conversation
    watches: Dict[str, Watch] = field(default_factory=dict)
    payloads: "OrderedDict[str, str]" = field(default_factory=OrderedDict)
    _seq: int = 0

    def remember(self, text: str) -> str:
        self._seq += 1
        key = f"s:{self._seq}"
        self.payloads[key] = text
        while len(self.payloads) > PAYLOAD_CACHE:
            self.payloads.popitem(last=False)
        return key


def _app(context: ContextTypes.DEFAULT_TYPE) -> App:
    return context.application.bot_data["app"]


def _keyboard(app: App, rows: fmt.ButtonRows) -> Optional[InlineKeyboardMarkup]:
    if not rows:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label[:64], callback_data=app.remember(text)) for label, text in row] for row in rows]
    )


# --------------------------------------------------------------------------- auth


async def _authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Deny by default and stay silent toward strangers; the rejected ID is logged for the owner."""
    user, chat = update.effective_user, update.effective_chat
    if user and user.id in _app(context).settings.allowed_users and chat and chat.type == "private":
        return True
    log.warning("Rejected update from user_id=%s chat_type=%s", user.id if user else None, chat.type if chat else None)
    if update.callback_query:
        await update.callback_query.answer()
    return False


# --------------------------------------------------------------------------- sending


async def send_reply(target: Message, text: str, markup: Optional[InlineKeyboardMarkup] = None) -> None:
    chunks = fmt.split_chunks(text or "(empty response)")
    for i, chunk in enumerate(chunks):
        last = i == len(chunks) - 1
        body = fmt.gfm_to_html(chunk)
        try:
            if len(body) > fmt.TELEGRAM_LIMIT:
                raise ValueError("rendered chunk too long")
            await target.reply_text(
                body, parse_mode="HTML", reply_markup=markup if last else None, disable_web_page_preview=True
            )
        except Exception:
            try:
                await target.reply_text(chunk[: fmt.TELEGRAM_LIMIT], reply_markup=markup if last else None)
            except Exception as e:  # pragma: no cover - network failure
                log.error("Could not deliver reply: %s", e)


async def _card(update: Update, text: str, rows: Optional[List[List[InlineKeyboardButton]]] = None) -> None:
    """Send a status card, editing in place when triggered from a button."""
    markup = InlineKeyboardMarkup(rows) if rows else None
    query = update.callback_query
    if query and query.message:
        try:
            await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
            return
        except Exception as e:
            if "not modified" in str(e).lower():
                return
    target = query.message if query and query.message else update.message
    if target:
        await target.reply_text(text, reply_markup=markup, parse_mode="HTML")


# --------------------------------------------------------------------------- commands


def _esc(value: Any) -> str:
    return html.escape(str(value))


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update, context):
        return
    app = _app(context)
    port, ok = await asyncio.to_thread(app.daemon.status)
    conv = await asyncio.to_thread(app.agent.active_conversation)
    text = (
        f"{'🟢' if ok else '🔴'} <b>Antigravity</b> · <code>{_esc(app.host)}</code>\n"
        f"• Project: <code>{_esc(app.settings.project_name)}</code> (trust: {_esc(app.settings.trust_mode)})\n"
        f"• Session: <code>{_esc(conv[:8] + '…') if conv else 'none yet'}</code>\n\n"
        "<b>Commands</b>\n"
        "• /session — session &amp; daemon status\n"
        "• /new [prompt] — start a fresh conversation\n\n"
        "<i>Send any message to prompt the agent.</i>"
    )
    await _card(update, text, [[InlineKeyboardButton("💬 Session", callback_data="cmd:session")]])


async def session_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update, context):
        return
    app = _app(context)
    port, ok = await asyncio.to_thread(app.daemon.status)
    conv = await asyncio.to_thread(app.agent.active_conversation)
    title = await asyncio.to_thread(app.agent.conversation_title, conv) if conv else ""
    lines = [
        f"💬 <b>Session</b> · <code>{_esc(app.host)}</code>\n",
        f"• Daemon: {'🟢 online' if ok else '🔴 unreachable'}" + (f" (<code>localhost:{port}</code>)" if port else ""),
        f"• Project: <code>{_esc(app.settings.project_name)}</code>",
    ]
    if title:
        lines.append(f"• Title: <code>{_esc(title)}</code>")
    lines.append(f"• ID: <code>{_esc(conv)}</code>" if conv else "• ID: <i>none (created on next message)</i>")
    rows = [[
        InlineKeyboardButton("＋ New thread", callback_data="cmd:new"),
        InlineKeyboardButton("🏠 Home", callback_data="cmd:start"),
    ]]
    await _card(update, "\n".join(lines), rows)


async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update, context):
        return
    app = _app(context)
    target = update.callback_query.message if update.callback_query else update.message
    prompt = " ".join(context.args or [])
    if not prompt:
        app.fresh_next = True
        await target.reply_text("✨ Ready. Your next message starts a new thread.")
        return
    await run_turn(app, target, prompt, force_new=True)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update, context) or not update.message or not update.message.text:
        return
    await run_turn(_app(context), update.message, update.message.text)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await _authorized(update, context):
        return
    await query.answer()
    app, data = _app(context), query.data or ""
    if data == "cmd:session":
        await session_command(update, context)
    elif data == "cmd:start":
        await start_command(update, context)
    elif data == "cmd:new":
        app.fresh_next = True
        await query.message.reply_text("✨ Ready. Your next message starts a new thread.")
    elif data.startswith("s:"):
        text = app.payloads.get(data)
        if text is None:
            await query.message.reply_text("⌛ That button has expired.")
            return
        await query.message.reply_text(f"› {text}"[:fmt.TELEGRAM_LIMIT])
        await run_turn(app, query.message, text)


# --------------------------------------------------------------------------- turns


async def run_turn(app: App, target: Message, text: str, force_new: bool = False) -> None:
    """Send ``text`` to the agent and stream the reply back into the chat."""
    await _typing(target)
    payload = f"[via Telegram] {text}"
    conv = "" if (force_new or app.fresh_next) else await asyncio.to_thread(app.agent.active_conversation)

    if not conv:
        conv, err = await asyncio.to_thread(app.agent.start_conversation, payload)
        if not conv:
            await target.reply_text(f"❌ Could not start a session: {err}"[:fmt.TELEGRAM_LIMIT])
            return
        app.fresh_next = False
        offset = 0
        await target.reply_text(f"💬 New thread · {conv[:8]}…")
    else:
        transcript = app.agent.transcript_paths(conv)[0]
        try:
            offset = transcript.stat().st_size
        except OSError:
            await target.reply_text("❌ Transcript not found; try /new.")
            return
        watch = app.watches.get(conv)
        if watch:  # a follower is already streaming this conversation: just deliver the message
            watch.expected_finals += 1
        ok, err = await asyncio.to_thread(app.agent.send_message, conv, payload)
        if not ok:
            if watch:
                watch.expected_finals -= 1
            await target.reply_text(f"❌ Dispatch failed: {err}"[:fmt.TELEGRAM_LIMIT])
            return
        if watch:
            await target.reply_text("↪ Sent to the running turn.")
            return

    watch = app.watches[conv] = Watch()
    try:
        await _follow(app, target, conv, offset, watch)
    finally:
        app.watches.pop(conv, None)


async def _typing(target: Message) -> None:
    try:
        await target.reply_chat_action("typing")
    except Exception:
        pass


async def _follow(app: App, target: Message, conv: str, offset: int, watch: Watch) -> None:
    transcript = app.agent.transcript_paths(conv)[0]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + app.settings.turn_timeout
    last_typing = last_pill = 0.0
    last_label = ""
    status: Optional[Message] = None
    asked: set = set()

    async def drop_status() -> None:
        nonlocal status
        if status:
            try:
                await status.delete()
            except Exception:
                pass
            status = None

    while loop.time() < deadline:
        await asyncio.sleep(POLL_INTERVAL)
        now = loop.time()
        if now - last_typing >= TYPING_INTERVAL:
            last_typing = now
            await _typing(target)

        steps, offset = await asyncio.to_thread(read_new_steps, transcript, offset)
        for step in steps:
            calls = step.get("tool_calls") or []
            if calls:
                first = calls[0]
                if first.get("name") == "ask_question" and step.get("step_index") not in asked:
                    asked.add(step.get("step_index"))
                    await drop_status()
                    await _forward_questions(app, target, first)
                label = fmt.format_tool_step(first)
                if label != last_label and now - last_pill >= PILL_INTERVAL:
                    last_label, last_pill = label, now
                    try:
                        if status:
                            await status.edit_text(label, parse_mode="HTML")
                        else:
                            status = await target.reply_text(label, parse_mode="HTML")
                    except Exception:
                        pass
                continue

            content = step.get("content")
            if step.get("type") == "PLANNER_RESPONSE" and content:
                if "content" in (step.get("truncated_fields") or []):
                    content = await asyncio.to_thread(app.agent.full_content, conv, step.get("step_index")) or content
                await drop_status()
                text, rows = fmt.extract_buttons(content)
                await send_reply(target, text, _keyboard(app, rows))
                watch.expected_finals -= 1
                if watch.expected_finals <= 0:
                    return

    await drop_status()
    await target.reply_text(
        f"⏱ No final answer after {app.settings.turn_timeout // 60} min. The agent may still be working; "
        "send a message to check in."
    )


async def _forward_questions(app: App, target: Message, call: Dict[str, Any]) -> None:
    args = call.get("args", {})
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    for q in (args.get("questions") or [])[:5]:
        options = [str(o) for o in (q.get("options") or [])]
        rows = [[(opt, opt)] for opt in options]
        await target.reply_text(
            f"❓ <b>{_esc(q.get('question', 'Selection required'))}</b>",
            parse_mode="HTML",
            reply_markup=_keyboard(app, rows),
        )


# --------------------------------------------------------------------------- lifecycle


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, (NetworkError, TimedOut)):
        log.warning("Transient Telegram network error: %s", context.error)
        return
    log.error("Unhandled exception in handler", exc_info=context.error)


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands([
        BotCommand("session", "Session & daemon status"),
        BotCommand("new", "Start a fresh conversation"),
        BotCommand("help", "Show commands"),
    ])


def _setup_logging(settings: Settings) -> None:
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if settings.log_file:
        settings.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(settings.log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers, force=True
    )
    # httpx logs full request URLs at INFO, and Telegram's URL embeds the bot token.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_application(settings: Settings) -> Application:
    daemon = DaemonClient(settings)
    app_state = App(settings=settings, agent=Agent(settings, daemon), daemon=daemon)
    # concurrent_updates: a long agent turn must not block button taps (answers, /new, ...)
    application = (
        ApplicationBuilder().token(settings.bot_token).post_init(post_init).concurrent_updates(True).build()
    )
    application.bot_data["app"] = app_state
    application.add_error_handler(error_handler)
    for name, handler in (("start", start_command), ("help", start_command), ("session", session_command), ("new", new_command)):
        application.add_handler(CommandHandler(name, handler))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    return application


def main() -> int:
    try:
        settings = Settings.from_env()
    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2
    _setup_logging(settings)
    application = build_application(settings)
    log.info("Starting bridge (project=%s, trust=%s, allowed users=%d)",
             settings.project_name, settings.trust_mode, len(settings.allowed_users))
    application.run_polling(drop_pending_updates=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
