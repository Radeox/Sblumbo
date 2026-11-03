"""Sblumbo package for Obsidian Daily Note ingestion via Ollama."""

from .config import Config
from .git_utils import VaultError, git_commit, resolve_daily_note_path, validate_vault
from .ollama_client import OllamaError, check_ollama_available, query_ollama
from .parser import (
    extract_links_from_section,
    has_meaningful_content,
    parse_daily_sections,
    split_markdown_items,
)
from .processors import process_ideas, process_links, process_notes, process_todos
from .telegram import (
    extract_pending_todos,
    format_telegram_message,
    generate_morning_briefing,
    send_telegram_message,
    split_telegram_message,
)

__all__ = [
    "Config",
    "OllamaError",
    "VaultError",
    "check_ollama_available",
    "extract_links_from_section",
    "extract_pending_todos",
    "format_telegram_message",
    "generate_morning_briefing",
    "git_commit",
    "has_meaningful_content",
    "parse_daily_sections",
    "process_ideas",
    "process_links",
    "process_notes",
    "process_todos",
    "query_ollama",
    "resolve_daily_note_path",
    "send_telegram_message",
    "split_markdown_items",
    "split_telegram_message",
    "validate_vault",
]
