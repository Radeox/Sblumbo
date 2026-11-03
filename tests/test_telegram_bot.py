#!/usr/bin/env python3
"""Comprehensive test suite for SblumBot (Telegram morning task briefing)."""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from worker.config import Config
from worker.telegram import (
    extract_pending_todos,
    format_telegram_message,
    generate_morning_briefing,
    send_telegram_message,
    split_telegram_message,
)
import telegram_bot


def make_test_config(vault_dir: str, token: str = "test-token", chat_id: str = "12345678") -> Config:
    return Config(
        vault_path=vault_dir,
        todo_file=os.path.join(vault_dir, "Tasks.md"),
        notes_dir=os.path.join(vault_dir, "Notes"),
        ideas_dir=os.path.join(vault_dir, "Ideas"),
        links_dir=os.path.join(vault_dir, "Links"),
        daily_dir=os.path.join(vault_dir, "Daily"),
        ollama_url="http://127.0.0.1:11434/api/chat",
        ollama_model="qwen3:4b",
        ollama_timeout=10,
        ollama_temperature=0.1,
        ollama_num_predict=512,
        ollama_think=False,
        url_fetch_timeout=2,
        git_auto_commit=True,
        telegram_bot_token=token,
        telegram_chat_id=chat_id,
    )


class TestExtractPendingTodos(unittest.TestCase):
    """Test parsing and extraction of unchecked pending tasks from Tasks.md."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tasks_path = os.path.join(self.tmp.name, "Tasks.md")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_extract_only_unchecked_tasks(self) -> None:
        content = (
            "---\nsticker: emoji//1f5d2-fe0f\n---\n\n"
            "# Tasks\n"
            "<!-- Ingest Daily 2026-11-02 -->\n"
            "- [x] Completed task 1 #2026-11-02\n"
            "- [X] Completed task 2 [x]\n"
            "- [ ] Pending task 1 #2026-11-02\n"
            "* [ ] Pending task 2 with bullet\n"
            "- [ ]\n"  # Empty checkbox
            "Just random text\n"
            "- [ ] Pending task 3 #2026-11-03\n"
        )
        with open(self.tasks_path, "w", encoding="utf-8") as f:
            f.write(content)

        todos = extract_pending_todos(self.tasks_path)
        self.assertEqual(len(todos), 3)
        self.assertIn("Pending task 1", todos)
        self.assertIn("Pending task 2 with bullet", todos)
        self.assertIn("Pending task 3", todos)
        # Ensure date tags are stripped
        self.assertFalse(any("#2026" in t for t in todos))

    def test_empty_or_nonexistent_file(self) -> None:
        # File doesn't exist
        self.assertEqual(extract_pending_todos(os.path.join(self.tmp.name, "missing.md")), [])

        # Empty file
        with open(self.tasks_path, "w", encoding="utf-8") as f:
            f.write("")
        self.assertEqual(extract_pending_todos(self.tasks_path), [])

        # Only completed tasks
        with open(self.tasks_path, "w", encoding="utf-8") as f:
            f.write("- [x] Done 1\n- [x] Done 2\n")
        self.assertEqual(extract_pending_todos(self.tasks_path), [])


class TestMorningBriefingFormatting(unittest.TestCase):
    """Test AI briefing generation and Telegram HTML message layout."""

    @patch("worker.telegram.query_ollama")
    def test_generate_morning_briefing_success(self, mock_ollama) -> None:
        mock_ollama.return_value = {
            "summary": "Today is focused on server maintenance and invoice processing.",
            "categories": [
                {
                    "title": "Work & Admin",
                    "emoji": "💼",
                    "tasks": ["Send invoice to accountant", "Review quarterly report"],
                },
                {
                    "title": "Home & Personal",
                    "emoji": "🏠",
                    "tasks": ["Buy milk"],
                },
            ],
        }
        cfg = make_test_config(self.tmp_dir if hasattr(self, "tmp_dir") else "/tmp")
        briefing = generate_morning_briefing(["Task 1", "Task 2"], cfg)
        self.assertIn("summary", briefing)
        self.assertEqual(len(briefing["categories"]), 2)

    @patch("worker.telegram.query_ollama")
    def test_generate_morning_briefing_fallback_on_error(self, mock_ollama) -> None:
        mock_ollama.side_effect = RuntimeError("Ollama offline")
        cfg = make_test_config("/tmp")
        todos = ["Buy milk", "Fix server"]
        briefing = generate_morning_briefing(todos, cfg)
        # Should gracefully return a fallback structure with original tasks
        self.assertIn("summary", briefing)
        self.assertEqual(len(briefing["categories"]), 1)
        self.assertEqual(briefing["categories"][0]["tasks"], todos)

    def test_format_telegram_message_with_tasks(self) -> None:
        briefing = {
            "summary": "Focus on high priority client work today.",
            "categories": [
                {
                    "title": "⚡ Work Projects",
                    "emoji": "💼",
                    "tasks": ["Fix production bug & deploy <hotfix> #2026-11-03"],
                },
                {
                    "title": "Errands",
                    "emoji": "🛒",
                    "tasks": ["Buy coffee"],
                },
            ],
        }
        msg = format_telegram_message(briefing, total_count=2)
        # Verify notebook header and bot identity
        self.assertIn("📓 <b>SblumBot — Morning Briefing</b>", msg)
        # Verify date line and total count are excluded as requested
        self.assertNotIn("Total Pending", msg)
        self.assertNotIn("Tuesday", msg)
        # Verify HTML escaping for special characters (&, <, >)
        self.assertIn("&amp;", msg)
        self.assertIn("&lt;hotfix&gt;", msg)
        # Verify date tag is stripped from task
        self.assertNotIn("#2026-11-03", msg)
        # Verify leading emoji is not duplicated
        self.assertIn("💼 <b>Work Projects</b>", msg)
        self.assertIn("🛒 <b>Errands</b>", msg)
        self.assertIn("Focus on high priority client work today.", msg)

    def test_format_telegram_message_empty_todos(self) -> None:
        msg = format_telegram_message(briefing=None, total_count=0)
        self.assertIn("SblumBot", msg)
        self.assertIn("No pending tasks", msg)


class TestTelegramMessageSplitting(unittest.TestCase):
    """Test chunking long messages to respect Telegram 4096 character limit."""

    def test_message_under_limit_remains_single(self) -> None:
        short_text = "Hello from SblumBot! Here is your daily recap."
        chunks = split_telegram_message(short_text, max_length=4096)
        self.assertEqual(chunks, [short_text])

    def test_message_over_limit_splits_cleanly(self) -> None:
        # Create a text with multiple sections that exceeds 100 characters
        lines = [f"• Task item number {i} with some descriptive text" for i in range(20)]
        full_text = "\n".join(lines)
        chunks = split_telegram_message(full_text, max_length=120)
        self.assertTrue(len(chunks) > 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 120)
        # Rejoined content should preserve all items
        for line in lines:
            self.assertTrue(any(line in c for c in chunks))


class TestSendTelegramMessage(unittest.TestCase):
    """Test HTTP sending to Telegram Bot API."""

    @patch("urllib.request.urlopen")
    def test_send_telegram_message_success(self, mock_urlopen) -> None:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"ok": true, "result": {"message_id": 42}}'
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        ok = send_telegram_message(
            token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            chat_id="987654321",
            text="Hello <b>world</b>",
        )
        self.assertTrue(ok)
        # Verify request details
        req = mock_urlopen.call_args[0][0]
        self.assertIn("123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11", req.full_url)
        payload = json.loads(req.data.decode("utf-8"))
        self.assertEqual(payload["chat_id"], "987654321")
        self.assertEqual(payload["parse_mode"], "HTML")

    def test_send_telegram_message_missing_credentials(self) -> None:
        self.assertFalse(send_telegram_message(token="", chat_id="123", text="Hi"))
        self.assertFalse(send_telegram_message(token="abc", chat_id="", text="Hi"))

    @patch("urllib.request.urlopen")
    def test_send_telegram_message_api_error(self, mock_urlopen) -> None:
        import urllib.error
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="http://api.telegram.org",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=MagicMock(read=lambda: b'{"ok": false, "description": "Unauthorized"}'),
        )
        ok = send_telegram_message(token="bad-token", chat_id="123", text="Hi")
        self.assertFalse(ok)


class TestTelegramBotCLI(unittest.TestCase):
    """Test CLI behavior of telegram_bot.py."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = self.tmp.name
        self.tasks_path = os.path.join(self.vault, "Tasks.md")
        with open(self.tasks_path, "w", encoding="utf-8") as f:
            f.write("- [ ] Order new cables #2026-11-03\n- [ ] Clean desk\n")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @patch("telegram_bot.send_telegram_message")
    @patch("telegram_bot.generate_morning_briefing")
    def test_dry_run_does_not_send(self, mock_briefing, mock_send) -> None:
        mock_briefing.return_value = {
            "summary": "Quick desk setup tasks.",
            "categories": [{"title": "General", "emoji": "📝", "tasks": ["Order new cables", "Clean desk"]}],
        }
        env_file = os.path.join(self.vault, ".env")
        with open(env_file, "w", encoding="utf-8") as f:
            f.write(f"VAULT_PATH={self.vault}\nTELEGRAM_BOT_TOKEN=dummy\nTELEGRAM_CHAT_ID=123\n")

        with patch.object(sys, "argv", ["telegram_bot.py", "--env-file", env_file, "--dry-run"]):
            exit_code = telegram_bot.main()

        self.assertEqual(exit_code, 0)
        mock_send.assert_not_called()

    @patch("telegram_bot.send_telegram_message", return_value=True)
    @patch("telegram_bot.generate_morning_briefing")
    def test_successful_run_sends_message(self, mock_briefing, mock_send) -> None:
        mock_briefing.return_value = {
            "summary": "Ready for the day.",
            "categories": [{"title": "Tasks", "emoji": "✅", "tasks": ["Clean desk"]}],
        }
        env_file = os.path.join(self.vault, ".env")
        with open(env_file, "w", encoding="utf-8") as f:
            f.write(f"VAULT_PATH={self.vault}\nTELEGRAM_BOT_TOKEN=dummy\nTELEGRAM_CHAT_ID=123\n")

        with patch.object(sys, "argv", ["telegram_bot.py", "--env-file", env_file]):
            exit_code = telegram_bot.main()

        self.assertEqual(exit_code, 0)
        mock_send.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
