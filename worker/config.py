"""Caricamento della configurazione da file .env e variabili d'ambiente."""

import os
from dataclasses import dataclass


def load_dotenv(dotenv_path: str | None = None) -> None:
    """Carica le variabili da un file .env in os.environ senza dipendenze esterne.

    Non sovrascrive variabili già presenti nell'ambiente di sistema.
    """
    if dotenv_path is None:
        # Cerca .env nella directory radice del progetto (accanto al modulo/script)
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        dotenv_path = os.path.join(base_dir, ".env")

    if not os.path.isfile(dotenv_path):
        return

    try:
        with open(dotenv_path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export ") :].strip()
                if "=" not in line:
                    continue

                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()

                # Rimuove commenti inline se il valore non è quotato
                if val and val[0] in ('"', "'") and val[-1] == val[0] and len(val) >= 2:
                    val = val[1:-1]
                elif " #" in val:
                    val = val.split(" #", 1)[0].strip()

                if key and key not in os.environ:
                    os.environ[key] = val
    except OSError as exc:
        print(f"[WARN] Impossibile leggere il file .env ({dotenv_path}): {exc}")


@dataclass(frozen=True)
class Config:
    """Configurazione centralizzata del worker Obsidian."""

    vault_path: str
    todo_file: str
    notes_dir: str
    ideas_dir: str
    links_dir: str
    daily_dir: str
    ollama_url: str
    ollama_model: str
    ollama_timeout: int
    ollama_temperature: float
    ollama_num_predict: int
    url_fetch_timeout: int
    git_auto_commit: bool

    @classmethod
    def from_env(cls, dotenv_path: str | None = None) -> "Config":
        """Costruisce l'oggetto Config leggendo .env e variabili d'ambiente."""
        load_dotenv(dotenv_path)

        raw_vault = os.environ.get("VAULT_PATH", "~/Obsidian")
        vault_path = os.path.abspath(os.path.expanduser(os.path.expandvars(raw_vault)))

        def resolve_in_vault(env_key: str, default_rel: str) -> str:
            val = os.environ.get(env_key, default_rel).strip()
            expanded = os.path.expanduser(os.path.expandvars(val))
            if os.path.isabs(expanded):
                return expanded
            return os.path.join(vault_path, expanded)

        todo_file = resolve_in_vault("TODO_FILE", "Tasks.md")
        notes_dir = resolve_in_vault("NOTES_DIR", "Notes")
        ideas_dir = resolve_in_vault("IDEAS_DIR", "Ideas")
        links_dir = resolve_in_vault("LINKS_DIR", "Links")
        daily_dir = resolve_in_vault("DAILY_DIR", "Daily")

        ollama_url = os.environ.get(
            "OLLAMA_URL", "http://127.0.0.1:11434/api/chat"
        ).strip()
        ollama_model = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b").strip()

        try:
            ollama_timeout = int(os.environ.get("OLLAMA_TIMEOUT", "300"))
        except ValueError:
            ollama_timeout = 300

        try:
            ollama_temperature = float(os.environ.get("OLLAMA_TEMPERATURE", "0.1"))
        except ValueError:
            ollama_temperature = 0.1

        try:
            ollama_num_predict = int(os.environ.get("OLLAMA_NUM_PREDICT", "512"))
        except ValueError:
            ollama_num_predict = 512

        try:
            url_fetch_timeout = int(os.environ.get("URL_FETCH_TIMEOUT", "6"))
        except ValueError:
            url_fetch_timeout = 6

        git_auto_commit_raw = os.environ.get("GIT_AUTO_COMMIT", "true").strip().lower()
        git_auto_commit = git_auto_commit_raw in ("1", "true", "yes", "on")

        return cls(
            vault_path=vault_path,
            todo_file=todo_file,
            notes_dir=notes_dir,
            ideas_dir=ideas_dir,
            links_dir=links_dir,
            daily_dir=daily_dir,
            ollama_url=ollama_url,
            ollama_model=ollama_model,
            ollama_timeout=ollama_timeout,
            ollama_temperature=ollama_temperature,
            ollama_num_predict=ollama_num_predict,
            url_fetch_timeout=url_fetch_timeout,
            git_auto_commit=git_auto_commit,
        )
