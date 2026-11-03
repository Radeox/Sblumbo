"""Git operations and fail-safe validation for the Obsidian Vault."""

import os
import shutil
import subprocess


class VaultError(RuntimeError):
    """Raised when the Vault or requested note is invalid."""


def validate_vault(vault_path: str) -> None:
    """Verify that the Vault directory exists, is a directory, and is writable."""
    if not vault_path:
        raise VaultError("VAULT_PATH is not configured.")
    if not os.path.exists(vault_path):
        raise VaultError(f"Obsidian Vault does not exist at path: {vault_path}")
    if not os.path.isdir(vault_path):
        raise VaultError(f"Vault path is not a directory: {vault_path}")
    if not os.access(vault_path, os.R_OK | os.W_OK):
        raise VaultError(
            f"Insufficient permissions (read/write) on Vault: {vault_path}"
        )


def resolve_daily_note_path(
    raw_arg: str | None, daily_dir: str, vault_path: str
) -> str:
    """Resolve and validate the Daily Note path.

    Accepts:
    - Direct path to the `.md` file
    - Filename or date alone (e.g. `2026-11-02` or `2026-11-02.md`), searched in `DAILY_DIR`
    - `None` (no argument): automatically finds today's note in `DAILY_DIR`
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
        # Also check inside DAILY_DIR and VAULT_PATH
        basename = os.path.basename(expanded)
        candidates.append(os.path.join(daily_dir, basename))
        if not basename.endswith(".md"):
            candidates.append(os.path.join(daily_dir, f"{basename}.md"))
        candidates.append(os.path.join(vault_path, expanded))

    for candidate in candidates:
        if os.path.exists(candidate):
            if os.path.isdir(candidate):
                raise VaultError(
                    f"Specified path is a directory, not a note: {candidate}"
                )
            if not os.access(candidate, os.R_OK):
                raise VaultError(
                    f"Cannot read note (permission denied): {candidate}"
                )
            # Avoid processing and deleting the Daily folder index file (e.g. Daily/Daily.md)
            if os.path.basename(candidate).lower() == "daily.md":
                raise VaultError(
                    f"'{candidate}' is the Daily folder index file, not a daily note."
                )
            return candidate

    searched = ", ".join(dict.fromkeys(candidates))
    raise VaultError(f"Daily note not found (searched paths: {searched})")


def ensure_vault_gitignore(vault_path: str, entries_to_ignore: tuple[str, ...] = (".stversions", ".stfolder")) -> None:
    """Ensure Syncthing versioning and folder markers are present in the Vault's .gitignore."""
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
    """Stage changes and commit in the Obsidian Vault safely.

    Returns True if a commit was made, False otherwise.
    """
    if shutil.which("git") is None:
        print("[WARN] 'git' command not found in PATH. Skipping commit.")
        return False

    git_dir = os.path.join(vault_path, ".git")
    if not os.path.exists(git_dir):
        print(f"[WARN] Vault '{vault_path}' is not a Git repository. Skipping commit.")
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
            # Use 'git rm --ignore-unmatch' so an untracked deleted daily note does not fail with code 128
            subprocess.run(
                ["git", "rm", "--quiet", "--ignore-unmatch", "--"] + deleted_rel_paths,
                cwd=vault_path,
                check=True,
            )
    else:
        subprocess.run(["git", "add", "-A"], cwd=vault_path, check=True)

    # Check for staged changes before committing
    diff_check = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=vault_path,
        check=False,
    )
    if diff_check.returncode == 0:
        print("[INFO] No changes to commit in Vault.")
        return False

    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=vault_path,
        check=True,
    )
    return True
