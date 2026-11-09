import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from agy_telegram_bridge import formatting as fmt
from agy_telegram_bridge import notify, prompts
from agy_telegram_bridge.agent import read_new_steps
from agy_telegram_bridge.config import ConfigError, Settings


class GfmToHtml(unittest.TestCase):
    def test_bold_heading_bullets(self):
        out = fmt.gfm_to_html("### Title\n**bold** text\n- item")
        self.assertEqual(out, "<b>Title</b>\n<b>bold</b> text\n• item")

    def test_escapes_html_outside_code(self):
        self.assertEqual(fmt.gfm_to_html("a < b & c"), "a &lt; b &amp; c")

    def test_code_is_escaped_and_protected_from_markup(self):
        out = fmt.gfm_to_html("`**x** <y>`\n```py\nprint('<a>')\n```")
        self.assertIn("<code>**x** &lt;y&gt;</code>", out)
        self.assertIn("<pre><code>print(&#x27;&lt;a&gt;&#x27;)</code></pre>", out)

    def test_file_links_become_pills_and_http_links_stay_links(self):
        out = fmt.gfm_to_html("[a.py](file:///tmp/a.py) [site](https://example.com/?q=1&r=2)")
        self.assertIn("<code>a.py</code>", out)
        self.assertIn('<a href="https://example.com/?q=1&amp;r=2">site</a>', out)

    def test_javascript_links_are_not_linked(self):
        self.assertNotIn("<a ", fmt.gfm_to_html("[x](javascript:alert(1))"))


class SplitChunks(unittest.TestCase):
    def test_short_text_untouched(self):
        self.assertEqual(fmt.split_chunks("hi"), ["hi"])

    def test_chunks_respect_limit_and_lose_nothing(self):
        text = "\n\n".join(f"paragraph {i} " + "x" * 200 for i in range(60))
        chunks = fmt.split_chunks(text, limit=1000)
        self.assertTrue(all(len(c) <= 1000 + 4 for c in chunks))
        self.assertEqual("".join(c.replace("\n", "") for c in chunks), text.replace("\n", ""))

    def test_code_fence_is_closed_and_reopened(self):
        code = "```\n" + "\n".join(f"line {i}" for i in range(400)) + "\n```"
        chunks = fmt.split_chunks(code, limit=500)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertEqual(c.count("```") % 2, 0, c[:40])


class Buttons(unittest.TestCase):
    def test_json_and_inline_directives(self):
        text, rows = fmt.extract_buttons('Pick\n<!-- BUTTONS: ["A", {"text": "B", "data": "reply b"}] -->\n[BUTTON: C | go]')
        self.assertEqual(text, "Pick")
        self.assertEqual(rows, [[("A", "A"), ("B", "reply b")], [("C", "go")]])

    def test_nothing_inferred_from_prose(self):
        text, rows = fmt.extract_buttons("Please approve once or reply 1 or 2")
        self.assertEqual(rows, [])
        self.assertEqual(text, "Please approve once or reply 1 or 2")

    def test_bad_json_is_left_alone(self):
        text, rows = fmt.extract_buttons("<!-- BUTTONS: [oops -->")
        self.assertEqual(rows, [])


class ToolSteps(unittest.TestCase):
    def test_labels_are_escaped(self):
        label = fmt.format_tool_step({"name": "run_command", "args": {"toolSummary": "<b>x</b>"}})
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", label)

    def test_args_as_json_string(self):
        label = fmt.format_tool_step({"name": "read_file", "args": json.dumps({"Path": "/a/b/c.txt"})})
        self.assertIn("c.txt", label)
        self.assertNotIn("/a/b", label)


class TranscriptStreaming(unittest.TestCase):
    def test_incremental_reads_and_partial_lines(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            p.write_text('{"step_index": 1}\n{"step_in')
            steps, off = read_new_steps(p, 0)
            self.assertEqual(steps, [{"step_index": 1}])
            self.assertEqual(off, len('{"step_index": 1}\n'))
            with open(p, "a") as f:
                f.write('dex": 2}\nnot json\n[1]\n')
            steps, off2 = read_new_steps(p, off)
            self.assertEqual(steps, [{"step_index": 2}])  # garbage and non-objects skipped
            self.assertEqual(read_new_steps(p, off2), ([], off2))

    def test_missing_file(self):
        self.assertEqual(read_new_steps(Path("/nonexistent/x.jsonl"), 7), ([], 7))


class Config(unittest.TestCase):
    base = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ALLOWED_USERS": "1, 2;3"}

    def test_parse(self):
        s = Settings.from_env(self.base)
        self.assertEqual(s.allowed_users, frozenset({1, 2, 3}))
        self.assertEqual(s.trust_mode, "restricted")

    def test_deny_by_default_requires_users(self):
        with self.assertRaises(ConfigError):
            Settings.from_env({"TELEGRAM_BOT_TOKEN": "t"})

    def test_requires_token(self):
        with self.assertRaises(ConfigError):
            Settings.from_env({"TELEGRAM_ALLOWED_USERS": "1"})

    def test_rejects_non_numeric_user_and_bad_trust_mode(self):
        with self.assertRaises(ConfigError):
            Settings.from_env({**self.base, "TELEGRAM_ALLOWED_USERS": "alice"})
        with self.assertRaises(ConfigError):
            Settings.from_env({**self.base, "AGY_TRUST_MODE": "yolo"})


class DiscoveryPrompt(unittest.TestCase):
    def test_prompt_is_bundled_read_only_and_ends_with_parsable_buttons(self):
        text = prompts.discover_integrations()
        self.assertIn("Read-only", text)
        self.assertIn("Never print secrets", text)
        _, rows = fmt.extract_buttons(text)
        self.assertGreaterEqual(sum(len(r) for r in rows), 3)

    def test_prompt_contains_no_personal_markers(self):
        text = prompts.discover_integrations().lower()
        for marker in ("/home/", "@gmail", "192.168."):
            self.assertNotIn(marker, text)


class EnvExample(unittest.TestCase):
    path = Path(__file__).resolve().parent.parent / "examples" / "env.example"

    def test_no_inline_comments_and_fillable(self):
        for line in self.path.read_text().splitlines():
            if line and not line.startswith("#"):
                self.assertNotIn(" #", line, f"inline comment would leak into the value: {line}")
        values = notify.parse_env_file(self.path)
        values["TELEGRAM_BOT_TOKEN"] = "123:abc"
        values["TELEGRAM_ALLOWED_USERS"] = "42"
        self.assertEqual(Settings.from_env(values).allowed_users, frozenset({42}))


class Notify(unittest.TestCase):
    def test_env_file_parsing(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "env"
            p.write_text('# c\nexport A="1"\nB=two words\n\nbad line\nC=\n')
            self.assertEqual(notify.parse_env_file(p), {"A": "1", "B": "two words", "C": ""})
        self.assertEqual(notify.parse_env_file(Path("/nonexistent/env")), {})

    def test_sends_to_every_allowed_user_and_truncates(self):
        seen = []

        def fake_urlopen(req, timeout):
            seen.append(json.loads(req.data))
            return io.BytesIO(b"{}")

        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / "env"
            env.write_text("TELEGRAM_BOT_TOKEN=1:abc\nTELEGRAM_ALLOWED_USERS=7,5\n")
            with mock.patch("urllib.request.urlopen", fake_urlopen), mock.patch.dict("os.environ", {}, clear=True):
                self.assertEqual(notify.main(["--env-file", str(env), "x" * 5000]), 0)
        self.assertEqual([m["chat_id"] for m in seen], [5, 7])
        self.assertTrue(all(len(m["text"]) == 4096 for m in seen))

    def test_http_error_never_leaks_token(self):
        err = urllib.error.HTTPError("https://api.telegram.org/bot1:SECRET/sendMessage", 401, "no", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(notify.NotifyError) as cm:
                notify.send("1:SECRET", 1, "hi")
        self.assertNotIn("SECRET", str(cm.exception))

    def test_missing_config_is_exit_2(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(notify.main(["--env-file", "/nonexistent", "hi"]), 2)


if __name__ == "__main__":
    unittest.main()
