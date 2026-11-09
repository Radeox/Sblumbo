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
from .parser import capitalize_first, has_meaningful_content
from .prompts import MORNING_RECAP_PROMPT, MORNING_RECAP_SCHEMA


_UNCHECKED_TASK_RE = re.compile(
    r"^\s*(?:[-*+]|\d+[.)])\s+\[ \]\s*(.*)$"
)


def is_placeholder_text(text: str) -> bool:
    """Check if a string looks like dummy schema types or prompt instruction placeholders."""
    if not text:
        return True
    s = text.strip().lower()
    if s in {"string", "str", "none", "null", "undefined", "todo", "task", "tasks"}:
        return True
    clean_s = re.sub(r"[^\w\s]", " ", s)
    if re.search(r"\buna\s+frase\s+sintetica\b", clean_s):
        return True
    if re.search(r"\bmassimo\s+15\s*20\s+parole\b", clean_s) or re.search(r"\bmassimo\s+15\s*2\s*0\s+parole\b", clean_s):
        return True
    return False


def clean_task_display(task: str) -> str:
    """Strip date tags (#YYYY-MM-DD, 📅 YYYY-MM-DD) and clean/capitalize task text for display."""
    cleaned = re.sub(r"\s*(?:📅\s*#?|#)\d{4}-\d{2}-\d{2}\b", "", task)
    cleaned = re.sub(r"\s+#\d{4}-\d{1,2}\b", "", cleaned).strip()
    return capitalize_first(cleaned)


def clean_summary(summary: str) -> str:
    """Clean robotic boilerplate prefixes from the summary and ensure capitalization."""
    if not summary:
        return ""
    cleaned = summary.strip()
    if is_placeholder_text(cleaned):
        return ""

    pattern = (
        r"^(?:Ecco\s+(?:una\s+|un\s+|il\s+)?(?:panoramica|riepilogo|sintesi|quadro)[\w\s]*:\s*"
        r"|Riepilogo(?:\s+della\s+giornata)?:\s*)"
    )
    cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE).strip()
    if is_placeholder_text(cleaned):
        return ""

    return capitalize_first(cleaned) if cleaned else ""


def _normalize_task_text(text: str) -> str:
    """Lowercase and strip punctuation for soft matching tasks."""
    return re.sub(r"[^\w]+", "", text.lower())


def extract_pending_todos(tasks_path: str) -> list[str]:
    """Read Tasks.md and extract only unchecked pending tasks (- [ ]).

    Filters out completed tasks (- [x]), empty checkboxes, headers, comments, and frontmatter.
    Strips date tags so downstream consumers receive clean task descriptions.
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
            cleaned_task = clean_task_display(task_text)
            if has_meaningful_content(cleaned_task) and not is_placeholder_text(cleaned_task):
                pending.append(cleaned_task)

    return pending


def generate_morning_briefing(todos: list[str], config: Config) -> dict[str, Any]:
    """Call Ollama to group pending todos into categories.

    Falls back to a default un-categorized structure if Ollama fails or produces invalid data.
    Ensures that 100% of pending todos are preserved and no placeholder dummy data is shown.
    """
    if not todos:
        return {"categories": []}

    todos_text = "\n".join(f"- {t}" for t in todos)
    user_prompt = (
        f"Ecco l'elenco dei {len(todos)} task da categorizzare:\n"
        f"{todos_text}\n\n"
        "Genera il JSON raggruppando tutti i task nelle rispettive 'categories'."
    )

    try:
        data = query_ollama(
            [
                {"role": "system", "content": MORNING_RECAP_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            config=config,
            json_schema=MORNING_RECAP_SCHEMA,
        )
        raw_cats = data.get("categories", [])
        categories: list[dict[str, Any]] = []

        # Map normalized versions of input todos to original string
        normalized_todos = {_normalize_task_text(t): t for t in todos}
        matched_todos: set[str] = set()

        if isinstance(raw_cats, list):
            for cat in raw_cats:
                if not isinstance(cat, dict):
                    continue

                raw_title = str(cat.get("title", "")).strip()
                raw_emoji = str(cat.get("emoji", "")).strip()

                title = "Attività" if is_placeholder_text(raw_title) else (raw_title or "Attività")
                emoji = (
                    "📋"
                    if (is_placeholder_text(raw_emoji) or (raw_emoji.isalpha() and len(raw_emoji) > 2))
                    else (raw_emoji or "📋")
                )

                tasks: list[str] = []
                for raw_t in cat.get("tasks", []):
                    t_str = str(raw_t).strip()
                    if not t_str or is_placeholder_text(t_str):
                        continue
                    clean_t = clean_task_display(t_str)
                    if not clean_t or is_placeholder_text(clean_t):
                        continue

                    norm_t = _normalize_task_text(clean_t)
                    matched_orig = normalized_todos.get(norm_t)
                    if not matched_orig:
                        for norm_orig, orig_str in normalized_todos.items():
                            if norm_orig in norm_t or norm_t in norm_orig:
                                matched_orig = orig_str
                                break

                    if matched_orig:
                        tasks.append(matched_orig)
                        matched_todos.add(matched_orig)
                    else:
                        tasks.append(clean_t)

                if tasks:
                    categories.append({"title": title, "emoji": emoji, "tasks": tasks})

        # If no valid categories were formed (e.g. LLM returned pure placeholders):
        if not categories:
            categories = [{"title": "Tutti i Task", "emoji": "📋", "tasks": todos}]
        elif matched_todos:
            # If the LLM matched some tasks but dropped others, ensure no todos are lost
            missing = [t for t in todos if t not in matched_todos]
            if missing:
                categories.append({"title": "Altri Task", "emoji": "📋", "tasks": missing})

        return {
            "categories": categories,
        }
    except Exception as exc:
        print(f"[WARN] Ollama morning recap fallback ({exc})")
        return {
            "categories": [{"title": "Tutti i Task", "emoji": "📋", "tasks": todos}],
        }


def format_telegram_message(briefing: dict[str, Any] | None, total_count: int) -> str:
    """Format the morning briefing into a Telegram HTML message."""
    header = "📓 <b>SblumBot — Morning Briefing</b>\n"

    if total_count == 0 or not briefing or not briefing.get("categories"):
        return (
            f"{header}\n"
            "🎉 <i>No pending tasks! All caught up for today.</i>"
        )

    lines: list[str] = [header]

    for cat in briefing.get("categories", []):
        raw_emoji = str(cat.get("emoji", "")).strip()
        raw_title = str(cat.get("title", "Tasks")).strip()

        if is_placeholder_text(raw_title):
            raw_title = "Attività"
        if is_placeholder_text(raw_emoji) or (raw_emoji.isalpha() and len(raw_emoji) > 2):
            raw_emoji = "📋"

        # Strip any leading emoji from title to avoid duplicate symbols (e.g. 💼 💼 Work)
        cleaned_title = re.sub(
            r"^[\U00010000-\U0010ffff\u2600-\u27bf\u2300-\u23ff\s]+",
            "",
            raw_title,
        ).strip()
        if not cleaned_title:
            cleaned_title = raw_title

        safe_title = html.escape(cleaned_title)
        emoji = raw_emoji or "📓"
        tasks = cat.get("tasks", [])
        if not tasks:
            continue

        valid_tasks = []
        for t in tasks:
            clean_t = clean_task_display(str(t).strip())
            if clean_t and not is_placeholder_text(clean_t):
                valid_tasks.append(clean_t)

        if not valid_tasks:
            continue

        lines.append(f"{emoji} <b>{safe_title}</b>")
        for t in valid_tasks:
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
