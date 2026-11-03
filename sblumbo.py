#!/usr/bin/env python3
"""Main entry point for Sblumbo (Obsidian Daily Note ingestion)."""

import argparse
import os
import re
import sys
from datetime import datetime

from worker import (
    Config,
    OllamaError,
    VaultError,
    check_ollama_available,
    git_commit,
    has_meaningful_content,
    parse_daily_sections,
    process_ideas,
    process_links,
    process_notes,
    process_todos,
    resolve_daily_note_path,
    validate_vault,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sblumbo: Ingest an Obsidian Daily Note by triaging Todo, Ideas, Notes, and Links via Ollama."
    )
    parser.add_argument(
        "daily_note",
        nargs="?",
        default=None,
        help=(
            "Path to the Daily Note (e.g., ~/Obsidian/Daily/2026-11-02.md) "
            "or just the date (e.g., 2026-11-02). Defaults to today's date."
        ),
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Optional path to a custom .env file.",
    )
    parser.add_argument(
        "--keep-daily",
        action="store_true",
        help="Do not delete the Daily Note after ingestion.",
    )
    parser.add_argument(
        "--no-commit",
        action="store_true",
        help="Disable automatic Git commit in the Vault upon completion.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print parsed sections without querying Ollama or modifying files.",
    )
    return parser.parse_args()


def extract_source_date(daily_path: str) -> str:
    """Extract YYYY-MM-DD from the filename or fallback to today's date."""
    filename_without_ext = os.path.splitext(os.path.basename(daily_path))[0]
    match_date = re.search(r"\d{4}-\d{2}-\d{2}", filename_without_ext)
    return match_date.group(0) if match_date else datetime.now().strftime("%Y-%m-%d")


def format_summary(
    todos_count: int,
    ideas_count: int,
    notes_count: int,
    links_count: int,
) -> str:
    summary_parts: list[str] = []
    if todos_count:
        summary_parts.append(f"{todos_count} todo{'s' if todos_count != 1 else ''}")
    if ideas_count:
        summary_parts.append(f"{ideas_count} idea{'s' if ideas_count != 1 else ''}")
    if notes_count:
        summary_parts.append(f"{notes_count} note{'s' if notes_count != 1 else ''}")
    if links_count:
        summary_parts.append(f"{links_count} link{'s' if links_count != 1 else ''}")

    return ", ".join(summary_parts) if summary_parts else "no items extracted"


def main() -> int:
    args = parse_args()
    config = Config.from_env(args.env_file)

    # 1. Fail-safe: validate Vault and resolve Daily Note path
    try:
        validate_vault(config.vault_path)
        daily_path = resolve_daily_note_path(
            args.daily_note, config.daily_dir, config.vault_path
        )
    except VaultError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    # 2. Safely read Daily Note content
    try:
        with open(daily_path, "r", encoding="utf-8") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError) as exc:
        print(
            f"[ERROR] Unable to read note '{daily_path}': {exc}",
            file=sys.stderr,
        )
        return 1

    if not has_meaningful_content(content):
        print(f"[INFO] Note '{daily_path}' is empty. Nothing to process.")
        return 0

    source_date = extract_source_date(daily_path)
    sections = parse_daily_sections(content)

    # Verify that at least one section has non-empty content
    if not any(sections.values()):
        print(
            f"[INFO] No non-empty sections found in '{daily_path}'. "
            "Note will not be deleted."
        )
        return 0

    if args.dry_run:
        print(f"=== DRY RUN: {daily_path} (date: {source_date}) ===")
        for sec_name, sec_body in sections.items():
            status = f"{len(sec_body.splitlines())} lines" if sec_body else "EMPTY"
            print(f"\n--- Section [{sec_name.upper()}] ({status}) ---")
            if sec_body:
                print(sec_body)
        return 0

    # 3. Fail-safe: verify Ollama daemon and model availability
    try:
        check_ollama_available(config)
    except OllamaError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        print(
            "[SAFEGUARD] Aborted processing. The original Daily Note remains untouched.",
            file=sys.stderr,
        )
        return 1

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Processing Daily Note: {daily_path}")

    # 4. Process each section with error tracking
    errors: list[str] = []
    touched_paths: list[str] = []

    todos_count = 0
    if sections["todo"]:
        try:
            todos_count = process_todos(sections["todo"], source_date, config)
            if todos_count:
                touched_paths.append(config.todo_file)
                print(f"  -> Extracted todos: {todos_count}")
            else:
                errors.append(
                    "Todo section: no tasks extracted from non-empty section."
                )
        except Exception as exc:
            errors.append(f"Todo section: {exc}")

    ideas_files: list[str] = []
    if sections["ideas"]:
        try:
            ideas_files = process_ideas(sections["ideas"], source_date, config)
            if ideas_files:
                touched_paths.extend(ideas_files)
                print(f"  -> Ideas created: {len(ideas_files)}")
            else:
                errors.append(
                    "Ideas section: no ideas created from non-empty section."
                )
        except Exception as exc:
            errors.append(f"Ideas section: {exc}")

    notes_files: list[str] = []
    if sections["notes"]:
        try:
            notes_files = process_notes(sections["notes"], source_date, config)
            if notes_files:
                touched_paths.extend(notes_files)
                print(f"  -> Notes created: {len(notes_files)}")
            else:
                errors.append(
                    "Notes section: no notes created from non-empty section."
                )
        except Exception as exc:
            errors.append(f"Notes section: {exc}")

    links_files: list[str] = []
    if sections["links"]:
        try:
            links_files = process_links(sections["links"], source_date, config)
            if links_files:
                touched_paths.extend(links_files)
                print(f"  -> Links archived: {len(links_files)}")
            else:
                errors.append(
                    "Links section: no links extracted from non-empty section."
                )
        except Exception as exc:
            errors.append(f"Links section: {exc}")

    total_extracted = (
        todos_count + len(ideas_files) + len(notes_files) + len(links_files)
    )
    if total_extracted == 0 and not errors:
        errors.append("No items extracted from the Daily Note.")

    # 5. Critical fail-safe: if any section failed or produced 0 items, DO NOT delete Daily Note
    if errors:
        print(
            "\n[ERROR] Errors occurred during processing:",
            file=sys.stderr,
        )
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        print(
            f"[SAFEGUARD] Daily note '{daily_path}' was NOT removed to prevent data loss.",
            file=sys.stderr,
        )
        return 1

    # 6. Delete Daily Note (only if processing completed without errors)
    if not args.keep_daily and os.path.exists(daily_path):
        try:
            os.remove(daily_path)
            touched_paths.append(daily_path)
            print(f"Daily note '{daily_path}' removed successfully.")
        except OSError as exc:
            print(
                f"[WARN] Unable to remove Daily Note '{daily_path}': {exc}",
                file=sys.stderr,
            )

    # 7. Automatic Git commit in Vault
    summary_str = format_summary(
        todos_count, len(ideas_files), len(notes_files), len(links_files)
    )
    now_str = datetime.now().strftime("%H:%M:%S")
    commit_msg = f"Ingest {source_date} at {now_str} — {summary_str}"

    if config.git_auto_commit and not args.no_commit:
        try:
            committed = git_commit(
                config.vault_path,
                commit_msg,
                paths_to_stage=touched_paths,
            )
            if committed:
                print(f"Git commit executed: '{commit_msg}'")
        except Exception as exc:
            print(
                f"[WARN] Error during Vault Git commit: {exc}",
                file=sys.stderr,
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
