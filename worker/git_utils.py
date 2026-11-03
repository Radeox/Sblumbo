"""Operazioni Git e validazione fail-safe del Vault Obsidian."""

import os
import shutil
import subprocess


class VaultError(RuntimeError):
    """Errore sollevato quando il Vault o la nota richiesta non sono validi."""


def validate_vault(vault_path: str) -> None:
    """Verifica che la directory del Vault esista, sia una directory e sia scrivibile."""
    if not vault_path:
        raise VaultError("VAULT_PATH non è configurato.")
    if not os.path.exists(vault_path):
        raise VaultError(f"Il Vault Obsidian non esiste al percorso: {vault_path}")
    if not os.path.isdir(vault_path):
        raise VaultError(f"Il percorso del Vault non è una directory: {vault_path}")
    if not os.access(vault_path, os.R_OK | os.W_OK):
        raise VaultError(
            f"Permessi insufficienti (lettura/scrittura) sul Vault: {vault_path}"
        )


def resolve_daily_note_path(
    raw_arg: str | None, daily_dir: str, vault_path: str
) -> str:
    """Risolve e valida il percorso della Daily Note.

    Accetta:
    - Un percorso diretto al file `.md`
    - Solo il nome del file o la data (es. `2026-11-02` o `2026-11-02.md`), cercandolo in `DAILY_DIR`
    - `None` (nessun argomento): cerca automaticamente la nota di oggi in `DAILY_DIR`
    """
    from datetime import datetime

    candidates: list[str] = []

    if not raw_arg:
        today_name = f"{datetime.now().strftime('%Y-%m-%d')}.md"
        candidates.append(os.path.join(daily_dir, today_name))
    else:
        expanded = os.path.expanduser(os.path.expandvars(raw_arg.strip()))
        candidates.append(os.path.abspath(expanded))
        if not expanded.endswith(".md"):
            candidates.append(os.path.abspath(f"{expanded}.md"))
        # Prova anche dentro DAILY_DIR e VAULT_PATH
        basename = os.path.basename(expanded)
        candidates.append(os.path.join(daily_dir, basename))
        if not basename.endswith(".md"):
            candidates.append(os.path.join(daily_dir, f"{basename}.md"))
        candidates.append(os.path.join(vault_path, expanded))

    for candidate in candidates:
        if os.path.exists(candidate):
            if os.path.isdir(candidate):
                raise VaultError(
                    f"Il percorso specificato è una directory, non una nota: {candidate}"
                )
            if not os.access(candidate, os.R_OK):
                raise VaultError(
                    f"Impossibile leggere la nota (permessi negati): {candidate}"
                )
            # Evita di processare ed eliminare per errore il file indice della cartella Daily (es. Daily/Daily.md)
            if os.path.basename(candidate).lower() == "daily.md":
                raise VaultError(
                    f"'{candidate}' è il file indice della cartella Daily, non una Daily Note giornaliera."
                )
            return candidate

    searched = ", ".join(dict.fromkeys(candidates))
    raise VaultError(f"Nota giornaliera non trovata (percorsi cercati: {searched})")


def ensure_vault_gitignore(vault_path: str, entries_to_ignore: tuple[str, ...] = (".stversions", ".stfolder")) -> None:
    """Assicura che le cartelle di versioning di Syncthing siano presenti nel .gitignore del Vault."""
    gitignore_path = os.path.join(vault_path, ".gitignore")
    existing_lines: set[str] = set()

    if os.path.isfile(gitignore_path):
        try:
            with open(gitignore_path, "r", encoding="utf-8") as f:
                existing_lines = {line.strip().rstrip("/") for line in f if line.strip()}
        except OSError:
            return

    missing = [e for e in entries_to_ignore if e.rstrip("/") not in existing_lines]
    if not missing:
        return

    try:
        with open(gitignore_path, "a", encoding="utf-8") as f:
            if existing_lines:
                f.write("\n")
            for entry in missing:
                f.write(f"{entry}\n")
    except OSError:
        pass


def git_commit(
    vault_path: str,
    message: str,
    paths_to_stage: list[str] | None = None,
) -> bool:
    """Esegue lo stage e il commit nel Vault Obsidian in modo sicuro.

    Restituisce True se un commit è stato effettivamente creato, False altrimenti.
    """
    if shutil.which("git") is None:
        print("[WARN] Comando 'git' non trovato nel PATH. Commit saltato.")
        return False

    git_dir = os.path.join(vault_path, ".git")
    if not os.path.exists(git_dir):
        print(f"[WARN] Il Vault '{vault_path}' non è un repository Git. Commit saltato.")
        return False

    ensure_vault_gitignore(vault_path)

    if paths_to_stage is not None:
        abs_vault = os.path.abspath(vault_path)
        existing_rel_paths: list[str] = []
        deleted_rel_paths: list[str] = []

        for p in dict.fromkeys(paths_to_stage):
            abs_p = os.path.abspath(p)
            try:
                if os.path.commonpath([abs_p, abs_vault]) != abs_vault or abs_p == abs_vault:
                    continue
            except ValueError:
                continue

            rel_p = os.path.relpath(abs_p, abs_vault)
            if os.path.exists(abs_p):
                existing_rel_paths.append(rel_p)
            else:
                deleted_rel_paths.append(rel_p)

        if existing_rel_paths:
            subprocess.run(
                ["git", "add", "--"] + existing_rel_paths,
                cwd=vault_path,
                check=True,
            )
        if deleted_rel_paths:
            # Usa 'git rm --ignore-unmatch' così se la Daily Note eliminata non era
            # ancora tracciata da Git non fallisce con exit status 128.
            subprocess.run(
                ["git", "rm", "--quiet", "--ignore-unmatch", "--"] + deleted_rel_paths,
                cwd=vault_path,
                check=True,
            )
    else:
        subprocess.run(["git", "add", "-A"], cwd=vault_path, check=True)

    # Verifica se ci sono modifiche nell'area di staging prima di chiamare git commit
    diff_check = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=vault_path,
        check=False,
    )
    if diff_check.returncode == 0:
        print("[INFO] Nessuna modifica da committare nel Vault.")
        return False

    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=vault_path,
        check=True,
    )
    return True
