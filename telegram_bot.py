#!/usr/bin/env python3
"""SblumBot: Morning briefing worker for Telegram task notifications."""

import argparse
import sys

from worker import (
    Config,
    VaultError,
    validate_vault,
)
from worker.telegram import (
    extract_pending_todos,
    format_telegram_message,
    generate_morning_briefing,
    send_telegram_message,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SblumBot: Send a morning briefing and categorized task digest to Telegram."
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Optional path to a custom .env file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the formatted Telegram message to stdout without sending it.",
    )
    parser.add_argument(
        "--quiet-if-empty",
        action="store_true",
        help="Do not send any message if there are zero pending tasks.",
    )
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="Skip Ollama recap generation and output the raw task list directly.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = Config.from_env(args.env_file)

    # 1. Validate Vault path
    try:
        validate_vault(config.vault_path)
    except VaultError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    # 2. Extract pending tasks from Tasks.md
    todos = extract_pending_todos(config.todo_file)
    total_count = len(todos)

    if total_count == 0 and args.quiet_if_empty:
        print("[INFO] No pending tasks found. Quiet mode enabled; skipping message.")
        return 0

    # 3. Generate briefing via Ollama or fallback
    if total_count == 0:
        briefing = None
    elif args.no_ai:
        briefing = {
            "summary": "",
            "categories": [{"title": "Pending Tasks", "emoji": "📋", "tasks": todos}],
        }
    else:
        briefing = generate_morning_briefing(todos, config)

    # 4. Format message
    message = format_telegram_message(briefing, total_count=total_count)

    # 5. Dry-run mode
    if args.dry_run:
        print("=== SblumBot Telegram Message (Dry Run) ===")
        print(message)
        return 0

    # 6. Verify Telegram credentials
    if not config.telegram_bot_token or not config.telegram_chat_id:
        print(
            "[ERROR] TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in your environment or .env file.",
            file=sys.stderr,
        )
        return 1

    # 7. Send message via Telegram API
    success = send_telegram_message(
        token=config.telegram_bot_token,
        chat_id=config.telegram_chat_id,
        text=message,
    )

    if success:
        print(f"[INFO] SblumBot successfully sent morning briefing ({total_count} tasks).")
        return 0
    else:
        print("[ERROR] SblumBot failed to send message via Telegram.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
