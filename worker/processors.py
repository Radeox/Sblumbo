"""Processori per le singole sezioni (Todo, Ideas, Notes, Links) e salvataggio su file."""

import os
import re

from .config import Config
from .ollama_client import query_ollama
from .parser import (
    extract_links_from_section,
    has_meaningful_content,
    is_pure_url_item,
    split_markdown_items,
)
from .prompts import (
    IDEA_ITEM_SCHEMA,
    IDEA_SYSTEM_PROMPT,
    LINK_SCHEMA,
    LINK_SYSTEM_PROMPT,
    NOTE_ITEM_SCHEMA,
    NOTE_SYSTEM_PROMPT,
    TODO_SCHEMA,
    TODO_SYSTEM_PROMPT,
)
from .scraper import fetch_url_metadata

# Frasi del system prompt che piccoli modelli (es. qwen2.5:3b) potrebbero ripetere
_HALLUCINATED_PHRASES = (
    "estrai tutte le azioni atomiche",
    "restituisci frasi concise all'infinito",
    "identifica e estrai tutte le azioni",
    "sei un assistente per la gestione",
    "sei un estrattore rigoroso",
)


def clean_slug(title: str, fallback: str = "Nota") -> str:
    """Converte un titolo in un nome file sicuro per qualsiasi filesystem."""
    cleaned = re.sub(r'[\\/*?:"<>|#\r\n\t]+', "", title).strip(" ._")
    slug = re.sub(r"\s+", "_", cleaned)
    if not slug:
        return fallback
    return slug[:120]


def get_unique_filepath(directory: str, base_slug: str) -> str:
    """Restituisce un percorso file `.md` non esistente, aggiungendo `_2`, `_3` in caso di collisione."""
    os.makedirs(directory, exist_ok=True)
    candidate = os.path.join(directory, f"{base_slug}.md")
    if not os.path.exists(candidate):
        return candidate

    counter = 2
    while True:
        candidate = os.path.join(directory, f"{base_slug}_{counter}.md")
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def sanitize_yaml_string(value: str) -> str:
    """Esegue l'escape dei caratteri speciali per stringhe YAML racchiuse tra doppi apici."""
    cleaned = value.replace("\r", " ").replace("\n", " ").strip()
    cleaned = cleaned.replace("\\", "\\\\").replace('"', '\\"')
    return cleaned


def sanitize_tag(tag: str) -> str:
    """Normalizza un tag per il frontmatter YAML di Obsidian (niente '#' o spazi)."""
    cleaned = tag.strip().lstrip("#").strip()
    # In Obsidian i tag YAML con spazi non sono validi: convertiamo gli spazi in trattini
    cleaned = re.sub(r"\s+", "-", cleaned)
    cleaned = re.sub(r'[\\/*?:"<>|,]', "", cleaned)
    return cleaned


def format_tags_yaml(base_tag: str, raw_tags: list[str]) -> str:
    """Formatta la lista di tag YAML evitando duplicati e rimuovendo tag vuoti."""
    seen: set[str] = {base_tag.lower()}
    lines = [f"  - {base_tag}"]

    for raw in raw_tags:
        if not isinstance(raw, str):
            continue
        t = sanitize_tag(raw)
        if t and t.lower() not in seen:
            seen.add(t.lower())
            lines.append(f"  - {t}")

    return "\n".join(lines)


def _clean_placeholder_wikilinks(body: str) -> str:
    """Rimuove segnaposto letterali come [[Concetto]] o [[Idea]] se il modello li ha inseriti."""
    cleaned = re.sub(r"\s*\[\[(?:Concetto|Idea)\]\]", "", body, flags=re.IGNORECASE)
    return cleaned.strip()


def _is_hallucinated_task(task: str) -> bool:
    """Rileva se un task generato è in realtà una ripetizione del prompt di sistema."""
    low = task.strip().lower()
    if not low or len(low) < 2:
        return True
    return any(phrase in low for phrase in _HALLUCINATED_PHRASES)


def process_todos(text: str, source_date: str, config: Config) -> int:
    """Estrae e salva i Todo nel file Tasks.md del Vault."""
    items = split_markdown_items(text)
    if not items:
        return 0

    clean_input = "\n".join(f"- {item}" for item in items)

    parsed = query_ollama(
        [
            {"role": "system", "content": TODO_SYSTEM_PROMPT},
            {"role": "user", "content": f"Testo da analizzare:\n{clean_input}"},
        ],
        config=config,
        json_schema=TODO_SCHEMA,
    )

    raw_tasks = parsed.get("tasks", [])
    if not isinstance(raw_tasks, list):
        return 0

    valid_tasks: list[str] = []
    for t in raw_tasks:
        if not isinstance(t, str):
            continue
        # Rimuove eventuale "- [ ] " se il modello l'ha incluso nel testo del task
        cleaned_t = re.sub(r"^\s*([-*+]|\d+\.)?\s*(\[[ xX]?\]\s*)?", "", t).strip()
        if cleaned_t and not _is_hallucinated_task(cleaned_t):
            valid_tasks.append(cleaned_t)

    if not valid_tasks:
        return 0

    todo_dir = os.path.dirname(config.todo_file)
    if todo_dir:
        os.makedirs(todo_dir, exist_ok=True)

    with open(config.todo_file, "a", encoding="utf-8") as f:
        f.write(f"\n<!-- Ingest Daily {source_date} -->\n")
        for t in valid_tasks:
            f.write(f"- [ ] {t} #{source_date}\n")

    return len(valid_tasks)


def process_ideas(text: str, source_date: str, config: Config) -> list[str]:
    """Elabora ogni idea individualmente e restituisce i percorsi dei file creati."""
    raw_ideas = [
        item for item in split_markdown_items(text) if not is_pure_url_item(item)
    ]
    if not raw_ideas:
        return []

    os.makedirs(config.ideas_dir, exist_ok=True)
    created_files: list[str] = []

    for idx, single_idea_text in enumerate(raw_ideas, start=1):
        data = query_ollama(
            [
                {"role": "system", "content": IDEA_SYSTEM_PROMPT},
                {"role": "user", "content": f"Idea dell'utente:\n{single_idea_text}"},
            ],
            config=config,
            json_schema=IDEA_ITEM_SCHEMA,
        )

        raw_title = str(data.get("title", "")).strip() or f"Idea_{source_date}_{idx}"
        slug = clean_slug(raw_title, fallback=f"Idea_{source_date}_{idx}")
        filepath = get_unique_filepath(config.ideas_dir, slug)

        tags_list = data.get("tags", [])
        if not isinstance(tags_list, list):
            tags_list = []
        tags_yaml = format_tags_yaml("type/idea", tags_list)

        body = _clean_placeholder_wikilinks(str(data.get("body", "")))
        if not has_meaningful_content(body):
            body = single_idea_text

        safe_title = sanitize_yaml_string(raw_title)
        md_content = (
            f"---\n"
            f'title: "{safe_title}"\n'
            f"date: {source_date}\n"
            f'source: "[[{source_date}]]"\n'
            f"tags:\n"
            f"{tags_yaml}\n"
            f"---\n\n"
            f"{body}\n"
        )
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(md_content)
        created_files.append(filepath)

    return created_files


def process_notes(text: str, source_date: str, config: Config) -> list[str]:
    """Elabora ogni appunto individualmente e restituisce i percorsi dei file creati."""
    raw_notes = [
        item for item in split_markdown_items(text) if not is_pure_url_item(item)
    ]
    if not raw_notes:
        return []

    os.makedirs(config.notes_dir, exist_ok=True)
    created_files: list[str] = []

    for idx, single_note_text in enumerate(raw_notes, start=1):
        data = query_ollama(
            [
                {"role": "system", "content": NOTE_SYSTEM_PROMPT},
                {"role": "user", "content": f"Appunto dell'utente:\n{single_note_text}"},
            ],
            config=config,
            json_schema=NOTE_ITEM_SCHEMA,
        )

        raw_title = str(data.get("title", "")).strip() or f"Nota_{source_date}_{idx}"
        slug = clean_slug(raw_title, fallback=f"Nota_{source_date}_{idx}")
        filepath = get_unique_filepath(config.notes_dir, slug)

        tags_list = data.get("tags", [])
        if not isinstance(tags_list, list):
            tags_list = []
        tags_yaml = format_tags_yaml("type/note", tags_list)

        body = _clean_placeholder_wikilinks(str(data.get("markdown_body", "")))
        if not has_meaningful_content(body):
            body = single_note_text

        safe_title = sanitize_yaml_string(raw_title)
        md_content = (
            f"---\n"
            f'title: "{safe_title}"\n'
            f"date: {source_date}\n"
            f'source: "[[{source_date}]]"\n'
            f"tags:\n"
            f"{tags_yaml}\n"
            f"---\n\n"
            f"{body}\n"
        )
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(md_content)
        created_files.append(filepath)

    return created_files


def process_links(text: str, source_date: str, config: Config) -> list[str]:
    """Estrae i link, scarica i metadati web, li riassume con Ollama e salva le note."""
    extracted = extract_links_from_section(text)
    if not extracted:
        return []

    os.makedirs(config.links_dir, exist_ok=True)
    created_files: list[str] = []

    for idx, (raw_url, user_context) in enumerate(extracted, start=1):
        meta = fetch_url_metadata(raw_url, timeout=config.url_fetch_timeout)

        user_input = (
            f"URL: {meta['url']}\n"
            f"Titolo grezzo: {meta['title']}\n"
            f"Descrizione grezza: {meta['description']}"
        )
        if user_context:
            user_input += f"\nNota dell'utente: {user_context}"

        try:
            parsed = query_ollama(
                [
                    {"role": "system", "content": LINK_SYSTEM_PROMPT},
                    {"role": "user", "content": user_input},
                ],
                config=config,
                json_schema=LINK_SCHEMA,
            )
        except Exception as exc:
            print(f"[WARN] Fallback metadati per {meta['url']} (errore LLM: {exc})")
            parsed = {
                "title": meta["title"],
                "description": user_context or meta["description"],
                "tags": ["web"],
            }

        raw_title = str(parsed.get("title", "")).strip() or meta["title"] or meta["url"]
        slug = clean_slug(raw_title, fallback=f"Link_{source_date}_{idx}")
        filepath = get_unique_filepath(config.links_dir, slug)

        tags_list = parsed.get("tags", [])
        if not isinstance(tags_list, list):
            tags_list = ["web"]
        tags_yaml = format_tags_yaml("type/link", tags_list)

        description = str(parsed.get("description", "")).strip()
        if not description:
            description = user_context or meta["description"]

        safe_title = sanitize_yaml_string(raw_title)
        safe_url = sanitize_yaml_string(meta["url"])

        md_content = (
            f"---\n"
            f'title: "{safe_title}"\n'
            f'url: "{safe_url}"\n'
            f"date: {source_date}\n"
            f'source: "[[{source_date}]]"\n'
            f"tags:\n"
            f"{tags_yaml}\n"
            f"---\n\n"
            f"# [{raw_title}]({meta['url']})\n\n"
            f"{description}\n"
        )
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(md_content)
        created_files.append(filepath)

    return created_files
