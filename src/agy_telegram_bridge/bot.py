"""Telegram front-end: auth, commands, and live streaming of agent turns."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import socket
import sys
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from typing import Any, Deque, Dict, List, Optional, Set

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
from . import prompts
from .agent import Agent, read_new_steps
from .config import ConfigError, Settings
from .daemon import DaemonClient

log = logging.getLogger("agy_telegram_bridge")

POLL_INTERVAL = 1.5      # transcript poll period (s)
TYPING_INTERVAL = 4.0    # Telegram "typing…" lasts ~5 s
PILL_INTERVAL = 5.0      # min seconds between progress-pill edits (rate limits)
PAYLOAD_CACHE = 512      # remembered button payloads (callback_data is capped at 64 bytes)
INLINE_PAYLOAD_MAX = 62  # "p:" + payload fits within Telegram's 64-byte callback_data limit
IDLE_SETTLE_POLLS = 2    # empty polls after a reply before trusting daemon idle state


@dataclass
class Watch:
    """One live transcript follower per conversation."""

    targets: Deque[Message] = field(default_factory=deque)
    expected_finals: int = 1
    awaiting_input: bool = False
    delivered_any: bool = False
    cancelled: bool = False

    def current_target(self, fallback: Message) -> Message:
        return self.targets[0] if self.targets else fallback

    def pop_target(self, fallback: Message) -> Message:
        return self.targets.popleft() if self.targets else fallback


@dataclass
class App:
    settings: Settings
    agent: Agent
    daemon: DaemonClient
    host: str = field(default_factory=lambda: socket.gethostname().split(".")[0])
    fresh_users: Set[int] = field(default_factory=set)  # user IDs whose next message starts a new thread
    user_convs: Dict[int, str] = field(default_factory=dict)
    watches: Dict[str, Watch] = field(default_factory=dict)
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    payloads: "OrderedDict[str, str]" = field(default_factory=OrderedDict)
    _seq: int = 0

    def remember(self, text: str) -> str:
        if len(text.encode("utf-8")) <= INLINE_PAYLOAD_MAX:
            return f"p:{text}"
        self._seq += 1
        key = f"s:{self._seq}"
        self.payloads[key] = text
        while len(self.payloads) > PAYLOAD_CACHE:
            self.payloads.popitem(last=False)
        return key

    def recall(self, data: str) -> Optional[str]:
        if data.startswith("p:"):
            return data[2:]
        if data.startswith("s:"):
            return self.payloads.get(data)
        return None


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


async def _active_conv(app: App, chat_id: int) -> str:
    cached = app.user_convs.get(chat_id, "")
    if cached:
        if cached in app.watches or app.agent.transcript_paths(cached)[0].exists():
            return cached
        app.user_convs.pop(chat_id, None)
    if len(app.settings.allowed_users) == 1:
        conv = await asyncio.to_thread(app.agent.active_conversation)
        if conv:
            app.user_convs[chat_id] = conv
        return conv
    return ""


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update, context):
        return
    app = _app(context)
    port, ok = await asyncio.to_thread(app.daemon.status)
    conv = await _active_conv(app, update.effective_chat.id)
    text = (
        f"{'🟢' if ok else '🔴'} <b>Antigravity</b> · <code>{_esc(app.host)}</code>\n"
        f"• Project: <code>{_esc(app.settings.project_name)}</code> (trust: {_esc(app.settings.trust_mode)})\n"
        f"• Session: <code>{_esc(conv[:8] + '…') if conv else 'none yet'}</code>\n\n"
        "<b>Commands</b>\n"
        "• /session — session &amp; daemon status\n"
        "• /new [prompt] — start a fresh conversation\n"
        "• /cancel — stop streaming the active turn\n"
        "• /discover — read-only survey of your environment, then integration ideas\n\n"
        "<i>Send any message to prompt the agent.</i>"
    )
    await _card(update, text, [[
        InlineKeyboardButton("💬 Session", callback_data="cmd:session"),
        InlineKeyboardButton("🔎 Discover", callback_data="cmd:discover"),
    ]])


async def session_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update, context):
        return
    app = _app(context)
    port, ok = await asyncio.to_thread(app.daemon.status)
    conv = await _active_conv(app, update.effective_chat.id)
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
        app.fresh_users.add(target.chat_id)
        await target.reply_text("✨ Ready. Your next message starts a new thread.")
        return
    await run_turn(app, target, prompt, force_new=True)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update, context) or not update.message:
        return
    app = _app(context)
    conv = await _active_conv(app, update.message.chat_id)
    watch = app.watches.get(conv) if conv else None
    if not watch:
        await update.message.reply_text("ℹ️ No active turn is currently streaming.")
        return
    watch.cancelled = True
    await update.message.reply_text("⏹ Stopped streaming this turn. Send `/new` to start a fresh thread.")


async def _start_discovery(app: App, target: Message) -> None:
    await target.reply_text(
        "🔎 Starting a read-only survey of your environment. This can take a few minutes; "
        "the agent will propose integrations and wait for your choice before building anything."
    )
    await run_turn(app, target, prompts.discover_integrations(), force_new=True)


async def discover_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update, context) or not update.message:
        return
    await _start_discovery(_app(context), update.message)


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
    elif data == "cmd:discover":
        await _start_discovery(app, query.message)
    elif data == "cmd:new":
        app.fresh_users.add(query.message.chat_id)
        await query.message.reply_text("✨ Ready. Your next message starts a new thread.")
    elif data.startswith(("p:", "s:")):
        text = app.recall(data)
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
    chat_id = target.chat_id

    async with app.turn_lock:
        conv = "" if (force_new or chat_id in app.fresh_users) else await _active_conv(app, chat_id)

        if not conv:
            conv, err = await asyncio.to_thread(app.agent.start_conversation, payload)
            if not conv:
                await target.reply_text(f"❌ Could not start a session: {err}"[:fmt.TELEGRAM_LIMIT])
                return
            app.fresh_users.discard(chat_id)
            app.user_convs[chat_id] = conv
            offset = 0
            await target.reply_text(f"💬 New thread · {conv[:8]}…")
            watch = app.watches[conv] = Watch(targets=deque([target]))
        else:
            watch = app.watches.get(conv)
            offset = 0
            if not watch:
                transcript = app.agent.transcript_paths(conv)[0]
                try:
                    offset = transcript.stat().st_size
                except OSError:
                    app.user_convs.pop(chat_id, None)
                    await target.reply_text("❌ Transcript not found; try /new.")
                    return

            answering = bool(watch and watch.awaiting_input)
            if watch:
                if answering:
                    watch.awaiting_input = False
                    if watch.targets:
                        watch.targets[0] = target
                    else:
                        watch.targets.append(target)
                else:
                    watch.expected_finals += 1
                    watch.targets.append(target)

            ok, err = await asyncio.to_thread(app.agent.send_message, conv, payload)
            if not ok:
                if watch:
                    if answering:
                        watch.awaiting_input = True
                    else:
                        watch.expected_finals -= 1
                        if watch.targets:
                            watch.targets.pop()
                await target.reply_text(f"❌ Dispatch failed: {err}"[:fmt.TELEGRAM_LIMIT])
                return
            if watch:
                if not answering:
                    await target.reply_text("↪ Sent to the running turn.")
                return
            watch = app.watches[conv] = Watch(targets=deque([target]))

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
    idle_polls = 0

    async def drop_status() -> None:
        nonlocal status
        if status:
            try:
                await status.delete()
            except Exception:
                pass
            status = None

    while loop.time() < deadline and not watch.cancelled:
        await asyncio.sleep(POLL_INTERVAL)
        if watch.cancelled:
            break
        now = loop.time()
        reply_target = watch.current_target(target)
        if not watch.awaiting_input and now - last_typing >= TYPING_INTERVAL:
            last_typing = now
            await _typing(reply_target)

        steps, offset = await asyncio.to_thread(read_new_steps, transcript, offset)
        if not steps:
            if watch.delivered_any and not watch.awaiting_input and watch.expected_finals > 0:
                idle_polls += 1
                if idle_polls >= IDLE_SETTLE_POLLS:
                    if await asyncio.to_thread(app.agent.is_conversation_idle, conv):
                        await drop_status()
                        return
            continue
        idle_polls = 0

        for step in steps:
            truncated = step.get("truncated_fields") or []
            calls = step.get("tool_calls") or []
            if calls and "tool_calls" in truncated:
                full = await asyncio.to_thread(app.agent.full_step, conv, step.get("step_index"))
                if full and full.get("tool_calls"):
                    calls = full["tool_calls"]

            if calls:
                ask_calls = [c for c in calls if c.get("name") == "ask_question"]
                if ask_calls and step.get("step_index") not in asked:
                    asked.add(step.get("step_index"))
                    watch.awaiting_input = True
                    await drop_status()
                    for ac in ask_calls:
                        await _forward_questions(app, reply_target, ac)
                    continue

                shown = next((c for c in calls if c.get("name") not in ("update_focus", "update_steps")), calls[0])
                label = fmt.format_tool_step(shown)
                if label != last_label and now - last_pill >= PILL_INTERVAL:
                    last_label, last_pill = label, now
                    try:
                        if status:
                            await status.edit_text(label, parse_mode="HTML")
                        else:
                            status = await reply_target.reply_text(label, parse_mode="HTML")
                    except Exception:
                        pass
                continue

            content = step.get("content")
            if step.get("type") == "PLANNER_RESPONSE" and (content or step.get("status") == "ERROR"):
                if "content" in truncated:
                    content = await asyncio.to_thread(app.agent.full_content, conv, step.get("step_index")) or content
                await drop_status()
                out_target = watch.pop_target(target)
                if not content and step.get("status") == "ERROR":
                    await send_reply(out_target, "⚠️ The agent encountered an error while processing this turn.")
                else:
                    text, rows = fmt.extract_buttons(content or "")
                    await send_reply(out_target, text, _keyboard(app, rows))
                watch.delivered_any = True
                watch.awaiting_input = False
                watch.expected_finals -= 1
                if watch.expected_finals <= 0:
                    return

    await drop_status()
    if watch.cancelled:
        return
    await watch.current_target(target).reply_text(
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
        BotCommand("discover", "Survey my environment and suggest integrations"),
        BotCommand("cancel", "Stop streaming the current turn"),
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
    for name, handler in (
        ("start", start_command),
        ("help", start_command),
        ("session", session_command),
        ("new", new_command),
        ("cancel", cancel_command),
        ("discover", discover_command),
    ):
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
