import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agy_telegram_bridge import bot
from agy_telegram_bridge.agent import Agent
from agy_telegram_bridge.config import Settings
from agy_telegram_bridge.daemon import DaemonClient


class FakeMessage:
    def __init__(self, chat_id: int = 42, text: str = ""):
        self.chat_id = chat_id
        self.text = text
        self.replies = []
        self.deleted = False
        self.edits = []

    async def reply_text(self, text: str, **kwargs):
        sent = FakeMessage(chat_id=self.chat_id, text=text)
        sent.kwargs = kwargs
        self.replies.append((text, kwargs, sent))
        return sent

    async def edit_text(self, text: str, **kwargs):
        self.text = text
        self.edits.append((text, kwargs))

    async def delete(self):
        self.deleted = True

    async def reply_chat_action(self, action: str):
        return None


def _make_app(agy_home: Path, allowed: str = "42", turn_timeout: int = 5) -> bot.App:
    settings = Settings.from_env({
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "TELEGRAM_ALLOWED_USERS": allowed,
        "AGY_HOME": str(agy_home),
        "AGY_WORKSPACE_DIR": str(agy_home / "ws"),
        "AGY_TURN_TIMEOUT": str(turn_timeout),
    })
    daemon = DaemonClient(settings)
    return bot.App(settings=settings, agent=Agent(settings, daemon), daemon=daemon)


def _write_step(path: Path, step: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(step) + "\n")


class TurnStreamingAndConcurrency(unittest.IsolatedAsyncioTestCase):
    async def test_ask_question_in_parallel_and_truncated_completes_without_hanging(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(bot, "POLL_INTERVAL", 0.01):
            app = _make_app(Path(d), turn_timeout=1)
            conv = "conv-ask-1"
            t_compact, t_full = app.agent.transcript_paths(conv)

            with (
                mock.patch.object(app.agent, "start_conversation", return_value=(conv, "")),
                mock.patch.object(app.agent, "send_message", return_value=(True, "")) as send_mock,
            ):
                msg1 = FakeMessage(chat_id=42)
                turn_task = asyncio.create_task(bot.run_turn(app, msg1, "Deploy app"))
                await asyncio.sleep(0.03)

                # Parallel tool call: update_focus is calls[0], ask_question is calls[1] and truncated.
                _write_step(t_full, {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "tool_calls": [
                        {"name": "update_focus", "args": {"focus": "Asking target"}},
                        {
                            "name": "ask_question",
                            "args": {"questions": [{"question": "Environment?", "options": ["staging", "prod"]}]},
                        },
                    ],
                })
                _write_step(t_compact, {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "truncated_fields": ["tool_calls"],
                    "tool_calls": [
                        {"name": "update_focus", "args": {"focus": "Asking target"}},
                        {"name": "ask_question", "args": "{}"},
                    ],
                })
                await asyncio.sleep(0.04)

                # Prompt buttons were sent, without a trailing "Running · ask_question" pill.
                texts = [r[0] for r in msg1.replies]
                self.assertTrue(any("Environment?" in t for t in texts), texts)
                self.assertFalse(any("ask_question" in t for t in texts), texts)
                self.assertTrue(app.watches[conv].awaiting_input)

                # Answering the prompt must not inflate expected_finals to 2.
                msg2 = FakeMessage(chat_id=42)
                await bot.run_turn(app, msg2, "staging")
                send_mock.assert_called_once_with(conv, "[via Telegram] staging")
                self.assertEqual(app.watches[conv].expected_finals, 1)

                # Single final response completes the turn well before the 1s timeout.
                _write_step(t_compact, {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "content": "Deployed to staging.",
                })
                await asyncio.wait_for(turn_task, timeout=0.5)

                all_replies = [r[0] for r in msg1.replies] + [r[0] for r in msg2.replies]
                self.assertTrue(any("Deployed to staging." in t for t in all_replies), all_replies)
                self.assertFalse(any("No final answer" in t for t in all_replies), all_replies)

    async def test_concurrent_dispatches_serialize_and_settle_when_idle(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(bot, "POLL_INTERVAL", 0.01):
            app = _make_app(Path(d), turn_timeout=1)
            conv = "conv-race-1"
            t_compact, _ = app.agent.transcript_paths(conv)
            idle_flag = False

            def slow_start(payload: str):
                return conv, ""

            with (
                mock.patch.object(app.agent, "start_conversation", side_effect=slow_start) as start_mock,
                mock.patch.object(app.agent, "send_message", return_value=(True, "")) as send_mock,
                mock.patch.object(app.agent, "is_conversation_idle", side_effect=lambda _: idle_flag),
            ):
                m1, m2 = FakeMessage(chat_id=42), FakeMessage(chat_id=42)
                t1 = asyncio.create_task(bot.run_turn(app, m1, "first"))
                t2 = asyncio.create_task(bot.run_turn(app, m2, "second"))
                await asyncio.sleep(0.03)

                # Only one conversation started; second message joined the active watch.
                self.assertEqual(start_mock.call_count, 1)
                self.assertEqual(send_mock.call_count, 1)
                await asyncio.wait_for(t2, timeout=0.2)

                # Agent merges both inputs into one final response and goes idle.
                _write_step(t_compact, {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "Combined answer"})
                await asyncio.sleep(0.03)
                idle_flag = True
                await asyncio.wait_for(t1, timeout=0.5)

                combined_count = sum("Combined answer" in r[0] for r in m1.replies + m2.replies)
                self.assertEqual(combined_count, 1)

    async def test_multi_user_isolation_and_per_user_fresh_flag(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(bot, "POLL_INTERVAL", 0.01):
            app = _make_app(Path(d), allowed="10,20", turn_timeout=1)
            created = []

            def fake_start(payload: str):
                cid = f"conv-{len(created) + 1}"
                created.append((cid, payload))
                t_compact, _ = app.agent.transcript_paths(cid)
                _write_step(t_compact, {"step_index": 1, "type": "PLANNER_RESPONSE", "content": f"ok {cid}"})
                return cid, ""

            with (
                mock.patch.object(app.agent, "start_conversation", side_effect=fake_start),
                mock.patch.object(app.agent, "send_message", return_value=(True, "")) as send_mock,
            ):
                u10, u20 = FakeMessage(chat_id=10), FakeMessage(chat_id=20)
                await bot.run_turn(app, u10, "hi from 10")
                await bot.run_turn(app, u20, "hi from 20")
                self.assertEqual(app.user_convs[10], "conv-1")
                self.assertEqual(app.user_convs[20], "conv-2")

                # User 10 requests /new; User 20's next message still uses conv-2.
                app.fresh_users.add(10)
                t20_compact, _ = app.agent.transcript_paths("conv-2")

                async def feed_u20():
                    await asyncio.sleep(0.02)
                    _write_step(t20_compact, {"step_index": 2, "type": "PLANNER_RESPONSE", "content": "still conv-2"})

                await asyncio.gather(bot.run_turn(app, u20, "more from 20"), feed_u20())
                send_mock.assert_called_once_with("conv-2", "[via Telegram] more from 20")
                self.assertIn(10, app.fresh_users)

    def test_short_button_payloads_survive_cache_reset(self):
        with tempfile.TemporaryDirectory() as d:
            app = _make_app(Path(d))
            short_key = app.remember("Build proposal 1")
            long_text = "x" * 80
            long_key = app.remember(long_text)

            self.assertTrue(short_key.startswith("p:"))
            self.assertLessEqual(len(short_key.encode("utf-8")), 64)
            self.assertTrue(long_key.startswith("s:"))
            self.assertEqual(app.recall(long_key), long_text)

            # Simulate process restart wiping in-memory cache: short buttons still work.
            app.payloads.clear()
            self.assertEqual(app.recall(short_key), "Build proposal 1")
            self.assertIsNone(app.recall(long_key))

    async def test_cancel_and_error_step_handling(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(bot, "POLL_INTERVAL", 0.01):
            app = _make_app(Path(d), turn_timeout=1)
            conv = "conv-cancel"
            app.agent.transcript_paths(conv)[0].parent.mkdir(parents=True, exist_ok=True)
            app.agent.transcript_paths(conv)[0].write_text("", encoding="utf-8")

            with mock.patch.object(app.agent, "start_conversation", return_value=(conv, "")):
                msg = FakeMessage(chat_id=42)
                task = asyncio.create_task(bot.run_turn(app, msg, "long task"))
                await asyncio.sleep(0.03)
                app.watches[conv].cancelled = True
                await asyncio.wait_for(task, timeout=0.3)
                self.assertFalse(any("No final answer" in r[0] for r in msg.replies))

            # Planner error step with empty content finishes immediately with a warning.
            conv_err = "conv-err"
            t_err, _ = app.agent.transcript_paths(conv_err)
            with mock.patch.object(app.agent, "start_conversation", return_value=(conv_err, "")):
                msg_err = FakeMessage(chat_id=42)
                task_err = asyncio.create_task(bot.run_turn(app, msg_err, "trigger error", force_new=True))
                await asyncio.sleep(0.02)
                _write_step(t_err, {"step_index": 1, "type": "PLANNER_RESPONSE", "status": "ERROR", "content": ""})
                await asyncio.wait_for(task_err, timeout=0.3)
                self.assertTrue(any("encountered an error" in r[0] for r in msg_err.replies))


if __name__ == "__main__":
    unittest.main()
