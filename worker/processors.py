"""Processors for individual sections (Todo, Ideas, Notes, Links) and file storage."""

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

# System prompt phrases that small models (e.g. qwen2.5:3b) might mistakenly parrot
_HALLUCINATED_PHRASES = (
    "estrai tutte le azioni atomiche",
    "restituisci frasi concise all'infinito",
    "identifica e estrai tutte le azioni",
    "sei un assistente per la gestione",
    "sei un assistente che riformula",
    "sei un estrattore rigoroso",
    "ogni punto fornito dall'utente deve produrre",
    "non ripetere mai queste istruzioni",
)


def clean_slug(title: str, fallback: str = "Note") -> str:
    """Convert a title into a filesystem-safe filename slug."""
    cleaned = re.sub(r'[\\/*?:"<>|#\r\n\t]+', "", title).strip(" ._")
    slug = re.sub(r"\s+", "_", cleaned)
    if not slug:
        return fallback
    return slug[:120]


def get_unique_filepath(directory: str, base_slug: str) -> str:
    """Return a non-existent `.md` filepath, appending `_2`, `_3` on collisions."""
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
    """Escape special characters for double-quoted YAML strings."""
    cleaned = value.replace("\r", " ").replace("\n", " ").strip()
    cleaned = cleaned.replace("\\", "\\\\").replace('"', '\\"')
    return cleaned


def sanitize_tag(tag: str) -> str:
    """Normalize a tag for Obsidian YAML frontmatter (no '#' or whitespace)."""
    cleaned = tag.strip().lstrip("#").strip()
    cleaned = re.sub(r"\s+", "-", cleaned)
    cleaned = re.sub(r'[\\/*?:"<>|,]', "", cleaned)
    return cleaned


def format_tags_yaml(base_tag: str, raw_tags: list[str]) -> str:
    """Format YAML tag list avoiding duplicates and removing empty items."""
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
    """Remove literal placeholders like [[Concetto]] or [[Idea]] if inserted by model."""
    cleaned = re.sub(r"\s*\[\[(?:Concetto|Idea)\]\]", "", body, flags=re.IGNORECASE)
    return cleaned.strip()


def _clean_task_line(raw: str) -> str:
    """Clean a single task line by stripping bullets, checkboxes, or redundant date tags."""
    cleaned = re.sub(
        r"^\s*(?:(?:[-*+]|\d+[.)])\s*)?(?:\[[ xX]?\]\s*)?", "", raw
    ).strip()
    # Remove any trailing date tag already added
    cleaned = re.sub(r"\s+#\d{4}-\d{2}-\d{2}\s*$", "", cleaned).strip()
    return cleaned


def _is_hallucinated_task(task: str) -> bool:
    """Detect if a generated task is empty or an echo of the system prompt."""
    low = task.strip().lower()
    if not has_meaningful_content(low):
        return True
    return any(phrase in low for phrase in _HALLUCINATED_PHRASES)


def process_todos(text: str, source_date: str, config: Config) -> int:
    """Extract and append Todos to Tasks.md in the Vault.

    Includes a deterministic fallback to original user points if the LLM
    returns an empty list or drops items.
    """
    raw_items = split_markdown_items(text)
    if not raw_items:
        return 0

    # Ensure every non-empty line in Todo section is preserved
    todo_lines: list[str] = []
    for item in raw_items:
        for line in item.splitlines():
            cleaned_line = _clean_task_line(line)
            if has_meaningful_content(cleaned_line):
                todo_lines.append(cleaned_line)

    if not todo_lines:
        return 0

    clean_input = "\n".join(f"- {line}" for line in todo_lines)

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
        raw_tasks = []

    valid_tasks: list[str] = []
    for entry in raw_tasks:
        if isinstance(entry, dict):
            # Tolerate models returning {"task": "..."} objects
            candidate = str(
                entry.get("task")
                or entry.get("title")
                or entry.get("action")
                or entry.get("description")
                or ""
            )
        elif isinstance(entry, str):
            candidate = entry
        else:
            continue

        for subline in candidate.splitlines():
            cleaned_t = _clean_task_line(subline)
            if cleaned_t and not _is_hallucinated_task(cleaned_t):
                valid_tasks.append(cleaned_t)

    # Deterministic fallback: if LLM dropped items or returned [], use clean original lines
    if len(valid_tasks) < len(todo_lines):
        valid_tasks = todo_lines

    todo_dir = os.path.dirname(config.todo_file)
    if todo_dir:
        os.makedirs(todo_dir, exist_ok=True)

    with open(config.todo_file, "a", encoding="utf-8") as f:
        f.write(f"\n<!-- Ingest Daily {source_date} -->\n")
        for t in valid_tasks:
            f.write(f"- [ ] {t} #{source_date}\n")

    return len(valid_tasks)


def process_ideas(text: str, source_date: str, config: Config) -> list[str]:
    """Process each idea item individually and return created file paths."""
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
    """Process each note item individually and return created file paths."""
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
    """Extract links, scrape metadata, summarize with Ollama, and save notes."""
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
            print(f"[WARN] Metadata fallback for {meta['url']} (LLM error: {exc})")
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
