#!/usr/bin/env python3
"""Entry-point principale per l'ingestione delle Daily Note di Obsidian."""

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
        description="Elabora una Daily Note di Obsidian smistando Todo, Ideas, Notes e Links tramite Ollama."
    )
    parser.add_argument(
        "daily_note",
        nargs="?",
        default=None,
        help=(
            "Percorso alla Daily Note (es. ~/Obsidian/Daily/2026-11-02.md) "
            "oppure solo la data (es. 2026-11-02). Se omesso, usa la data di oggi."
        ),
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Percorso opzionale a un file .env personalizzato.",
    )
    parser.add_argument(
        "--keep-daily",
        action="store_true",
        help="Non eliminare la Daily Note al termine dell'ingestione.",
    )
    parser.add_argument(
        "--no-commit",
        action="store_true",
        help="Disabilita il commit Git automatico nel Vault al termine dell'operazione.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Mostra le sezioni estratte dalla Daily Note senza interrogare Ollama né modificare file.",
    )
    return parser.parse_args()


def extract_source_date(daily_path: str) -> str:
    """Estrae la data YYYY-MM-DD dal nome del file o usa la data odierna."""
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
        summary_parts.append(f"{todos_count} todo")
    if ideas_count:
        summary_parts.append(f"{ideas_count} {'idea' if ideas_count == 1 else 'idee'}")
    if notes_count:
        summary_parts.append(f"{notes_count} {'nota' if notes_count == 1 else 'note'}")
    if links_count:
        summary_parts.append(f"{links_count} {'link' if links_count == 1 else 'links'}")

    return ", ".join(summary_parts) if summary_parts else "nessun elemento estratto"


def main() -> int:
    args = parse_args()
    config = Config.from_env(args.env_file)

    # 1. Fail-safe: verifica esistenza e permessi del Vault e della Daily Note
    try:
        validate_vault(config.vault_path)
        daily_path = resolve_daily_note_path(
            args.daily_note, config.daily_dir, config.vault_path
        )
    except VaultError as exc:
        print(f"[ERRORE] {exc}", file=sys.stderr)
        return 1

    # 2. Lettura sicura della Daily Note
    try:
        with open(daily_path, "r", encoding="utf-8") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError) as exc:
        print(
            f"[ERRORE] Impossibile leggere la nota '{daily_path}': {exc}",
            file=sys.stderr,
        )
        return 1

    if not has_meaningful_content(content):
        print(f"[INFO] La nota '{daily_path}' è vuota. Nessuna operazione eseguita.")
        return 0

    source_date = extract_source_date(daily_path)
    sections = parse_daily_sections(content)

    # Verifica che almeno una sezione abbia contenuto reale
    if not any(sections.values()):
        print(
            f"[INFO] Nessuna sezione compilata trovata in '{daily_path}'. "
            "La nota non verrà rimossa."
        )
        return 0

    if args.dry_run:
        print(f"=== DRY RUN: {daily_path} (data: {source_date}) ===")
        for sec_name, sec_body in sections.items():
            status = f"{len(sec_body.splitlines())} righe" if sec_body else "VUOTA"
            print(f"\n--- Sezione [{sec_name.upper()}] ({status}) ---")
            if sec_body:
                print(sec_body)
        return 0

    # 3. Fail-safe: verifica preventiva che Ollama e il modello siano disponibili
    try:
        check_ollama_available(config)
    except OllamaError as exc:
        print(f"[ERRORE] {exc}", file=sys.stderr)
        print(
            "[SAFEGUARD] Elaborazione annullata. La Daily Note originale è rimasta intatta.",
            file=sys.stderr,
        )
        return 1

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Elaborazione Daily: {daily_path}")

    # 4. Elaborazione di ogni sezione con tracciamento errori
    errors: list[str] = []
    touched_paths: list[str] = []

    todos_count = 0
    if sections["todo"]:
        try:
            todos_count = process_todos(sections["todo"], source_date, config)
            if todos_count:
                touched_paths.append(config.todo_file)
                print(f"  -> Todo estratti: {todos_count}")
            else:
                errors.append(
                    "Sezione Todo: nessun task estratto da una sezione non vuota."
                )
        except Exception as exc:
            errors.append(f"Sezione Todo: {exc}")

    ideas_files: list[str] = []
    if sections["ideas"]:
        try:
            ideas_files = process_ideas(sections["ideas"], source_date, config)
            if ideas_files:
                touched_paths.extend(ideas_files)
                print(f"  -> Idee create: {len(ideas_files)}")
            else:
                errors.append(
                    "Sezione Ideas: nessuna idea creata da una sezione non vuota."
                )
        except Exception as exc:
            errors.append(f"Sezione Ideas: {exc}")

    notes_files: list[str] = []
    if sections["notes"]:
        try:
            notes_files = process_notes(sections["notes"], source_date, config)
            if notes_files:
                touched_paths.extend(notes_files)
                print(f"  -> Note create: {len(notes_files)}")
            else:
                errors.append(
                    "Sezione Notes: nessuna nota creata da una sezione non vuota."
                )
        except Exception as exc:
            errors.append(f"Sezione Notes: {exc}")

    links_files: list[str] = []
    if sections["links"]:
        try:
            links_files = process_links(sections["links"], source_date, config)
            if links_files:
                touched_paths.extend(links_files)
                print(f"  -> Link archiviati: {len(links_files)}")
            else:
                errors.append(
                    "Sezione Links: nessun link estratto da una sezione non vuota."
                )
        except Exception as exc:
            errors.append(f"Sezione Links: {exc}")

    total_extracted = (
        todos_count + len(ideas_files) + len(notes_files) + len(links_files)
    )
    if total_extracted == 0 and not errors:
        errors.append("Nessun elemento estratto dalla Daily Note.")

    # 5. Fail-safe critico: se una qualsiasi sezione è fallita o non ha prodotto output, NON eliminare la Daily Note
    if errors:
        print(
            "\n[ERRORE] Si sono verificati errori durante l'elaborazione:",
            file=sys.stderr,
        )
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        print(
            f"[SAFEGUARD] La Daily Note '{daily_path}' NON è stata rimossa per evitare perdite di dati.",
            file=sys.stderr,
        )
        return 1

    # 6. Rimozione della Daily Note (solo se tutto è andato a buon fine)
    if not args.keep_daily and os.path.exists(daily_path):
        try:
            os.remove(daily_path)
            touched_paths.append(daily_path)
            print(f"Daily note '{daily_path}' rimossa con successo.")
        except OSError as exc:
            print(
                f"[WARN] Impossibile rimuovere la Daily Note '{daily_path}': {exc}",
                file=sys.stderr,
            )

    # 7. Commit Git automatico nel Vault
    summary_str = format_summary(
        todos_count, len(ideas_files), len(notes_files), len(links_files)
    )
    now_str = datetime.now().strftime("%H:%M:%S")
    commit_msg = f"Ingest {source_date} ore {now_str} — {summary_str}"

    if config.git_auto_commit and not args.no_commit:
        try:
            committed = git_commit(
                config.vault_path,
                commit_msg,
                paths_to_stage=touched_paths,
            )
            if committed:
                print(f"Git commit eseguito: '{commit_msg}'")
        except Exception as exc:
            print(
                f"[WARN] Errore durante il commit Git nel Vault: {exc}",
                file=sys.stderr,
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
