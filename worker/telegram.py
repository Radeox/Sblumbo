"""Telegram Bot module for SblumBot (morning briefing and task digest)."""

import html
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

from .config import Config
from .ollama_client import query_ollama
from .parser import has_meaningful_content
from .prompts import MORNING_RECAP_PROMPT, MORNING_RECAP_SCHEMA


_UNCHECKED_TASK_RE = re.compile(
    r"^\s*(?:[-*+]|\d+[.)])\s+\[ \]\s*(.*)$"
)


def extract_pending_todos(tasks_path: str) -> list[str]:
    """Read Tasks.md and extract only unchecked pending tasks (- [ ]).

    Filters out completed tasks (- [x]), empty checkboxes, headers, comments, and frontmatter.
    """
    if not os.path.isfile(tasks_path):
        return []

    try:
        with open(tasks_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return []

    pending: list[str] = []
    in_frontmatter = False
    lines = content.splitlines()

    for idx, raw_line in enumerate(lines):
        line = raw_line.strip()
        if idx == 0 and line == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if line in ("---", "..."):
                in_frontmatter = False
            continue

        if not line or line.startswith("<!--") or line.startswith("#"):
            continue

        m = _UNCHECKED_TASK_RE.match(raw_line)
        if m:
            task_text = m.group(1).strip()
            if has_meaningful_content(task_text):
                pending.append(task_text)

    return pending


def generate_morning_briefing(todos: list[str], config: Config) -> dict[str, Any]:
    """Call Ollama to create a general recap and group pending todos into categories.

    Falls back to a default un-categorized structure if Ollama fails.
    """
    if not todos:
        return {"summary": "", "categories": []}

    todos_text = "\n".join(f"- {t}" for t in todos)
    user_prompt = f"Elenco dei task da analizzare:\n{todos_text}"

    try:
        data = query_ollama(
            [
                {"role": "system", "content": MORNING_RECAP_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            config=config,
            json_schema=MORNING_RECAP_SCHEMA,
        )
        summary = str(data.get("summary", "")).strip()
        raw_cats = data.get("categories", [])
        categories: list[dict[str, Any]] = []

        if isinstance(raw_cats, list):
            for cat in raw_cats:
                if isinstance(cat, dict):
                    title = str(cat.get("title", "")).strip() or "General"
                    emoji = str(cat.get("emoji", "")).strip() or "📋"
                    tasks = [str(t).strip() for t in cat.get("tasks", []) if t]
                    if tasks:
                        categories.append({"title": title, "emoji": emoji, "tasks": tasks})

        if not categories:
            categories = [{"title": "Pending Tasks", "emoji": "📋", "tasks": todos}]

        return {
            "summary": summary or "Ecco il riepilogo delle attività in sospeso.",
            "categories": categories,
        }
    except Exception as exc:
        print(f"[WARN] Ollama morning recap fallback ({exc})")
        return {
            "summary": f"Hai {len(todos)} task in sospeso da gestire oggi.",
            "categories": [{"title": "Tutti i Task", "emoji": "📋", "tasks": todos}],
        }


def format_telegram_message(briefing: dict[str, Any] | None, total_count: int) -> str:
    """Format the morning briefing into a Telegram HTML message."""
    header = "🤖 <b>SblumBot — Morning Briefing</b>\n"

    if total_count == 0 or not briefing or not briefing.get("categories"):
        return (
            f"{header}\n"
            "🎉 <i>No pending tasks! All caught up for today.</i>"
        )

    date_str = datetime.now().strftime("%A, %d %b %Y")
    lines: list[str] = [
        header,
        f"📅 <i>{date_str}</i>",
        f"📊 <b>Total Pending:</b> {total_count}\n",
    ]

    summary = briefing.get("summary", "").strip()
    if summary:
        safe_summary = html.escape(summary)
        lines.append(f"💡 <b>Today's Focus:</b>\n{safe_summary}\n")

    for cat in briefing.get("categories", []):
        emoji = cat.get("emoji", "📋")
        title = html.escape(cat.get("title", "Tasks"))
        tasks = cat.get("tasks", [])
        if not tasks:
            continue

        lines.append(f"{emoji} <b>{title}</b>")
        for t in tasks:
            lines.append(f"• {html.escape(t)}")
        lines.append("")

    return "\n".join(lines).strip()


def split_telegram_message(text: str, max_length: int = 4096) -> list[str]:
    """Split a message into chunks within Telegram's max message limit (4096 chars)."""
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    current_chunk: list[str] = []
    current_len = 0

    for line in text.splitlines(keepends=True):
        line_len = len(line)
        if current_len + line_len > max_length:
            if current_chunk:
                chunks.append("".join(current_chunk).strip())
                current_chunk = []
                current_len = 0

            # If a single line itself is longer than max_length, force-slice it
            if line_len > max_length:
                for i in range(0, line_len, max_length):
                    chunks.append(line[i : i + max_length].strip())
            else:
                current_chunk.append(line)
                current_len += line_len
        else:
            current_chunk.append(line)
            current_len += line_len

    if current_chunk:
        chunk_str = "".join(current_chunk).strip()
        if chunk_str:
            chunks.append(chunk_str)

    return chunks


def send_telegram_message(token: str, chat_id: str, text: str, timeout: int = 10) -> bool:
    """Send text message(s) via Telegram Bot API using HTTPS POST.

    Splits automatically if message exceeds 4096 characters.
    """
    if not token or not chat_id:
        print("[ERROR] Telegram BOT token or Chat ID is missing.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks = split_telegram_message(text)
    all_success = True

    for chunk in chunks:
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    all_success = False
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="ignore")
            print(f"[ERROR] Telegram API HTTP {exc.code}: {err_body}")
            all_success = False
        except Exception as exc:
            print(f"[ERROR] Failed to send Telegram message: {exc}")
            all_success = False

    return all_success
