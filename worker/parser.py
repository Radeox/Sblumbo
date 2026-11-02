"""Parser deterministico per le sezioni della Daily Note e gli elenchi Markdown."""

import re


# Intestazioni di sezione note (case-insensitive, con o senza emoji)
_SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "todo": ("todo", "todos", "task", "tasks"),
    "ideas": ("ideas", "idea", "idee"),
    "notes": ("notes", "note", "appunti"),
    "links": ("links", "link"),
}

# Regex che individua qualsiasi heading Markdown di livello 1-4: es. "### 📝 Todo"
_HEADING_RE = re.compile(r"^\s*(#{1,4})\s+(.+?)\s*$")

# Regex per rimuovere commenti HTML prima del parsing
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _strip_frontmatter(content: str) -> str:
    """Rimuove eventuale YAML frontmatter iniziale delimitato da '---'."""
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return content

    for idx in range(1, len(lines)):
        if lines[idx].strip() in ("---", "..."):
            return "\n".join(lines[idx + 1 :])
    return content


def _classify_heading(heading_text: str) -> str | None:
    """Restituisce la chiave di sezione ('todo', 'ideas', 'notes', 'links') se l'heading
    corrisponde a una delle sezioni della Daily Note, altrimenti None.
    """
    # Rimuove emoji e punteggiatura mantenendo solo lettere/numeri
    cleaned = re.sub(r"[^\w\s]", " ", heading_text, flags=re.UNICODE).strip().lower()
    tokens = cleaned.split()
    if not tokens:
        return None

    for section_key, aliases in _SECTION_ALIASES.items():
        if cleaned in aliases or tokens[-1] in aliases or tokens[0] in aliases:
            return section_key
    return None


def has_meaningful_content(text: str) -> bool:
    """Verifica se una sezione o un elemento contiene almeno un carattere alfanumerico.

    Evita che righe con soli bullet vuoti ('- ', '* ', '- [ ]') vengano passate all'LLM
    causando allucinazioni (Bug #1).
    """
    if not text:
        return False
    # Rimuove checkbox vuote e marcatori di lista prima di controllare
    cleaned = re.sub(r"^\s*([*+-]|\d+\.)\s*(\[[ xX]?\]\s*)?", "", text, flags=re.MULTILINE)
    return bool(re.search(r"[\w]", cleaned, flags=re.UNICODE))


def parse_daily_sections(content: str) -> dict[str, str]:
    """Suddivide la Daily Note nelle 4 sezioni canoniche: todo, ideas, notes, links.

    A differenza di una regex dot-all fragile:
    - Si ferma correttamente alla sezione successiva anche se la corrente è vuota.
    - Non viene ingannata da sotto-titoli generici (es. '### Herolapse') dentro una sezione,
      oppure li ignora/preserva come contesto senza far tracimare i link nelle note (Bug #2).
    """
    sections: dict[str, list[str]] = {
        "todo": [],
        "ideas": [],
        "notes": [],
        "links": [],
    }

    body = _strip_frontmatter(content)
    body = _HTML_COMMENT_RE.sub("", body)

    current_section: str | None = None

    for line in body.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            matched_section = _classify_heading(m.group(2))
            if matched_section is not None:
                current_section = matched_section
                continue
            # Se troviamo un sottotitolo non riconosciuto (es. "### Herolapse" dentro Links o Notes),
            # non cambiamo sezione: nella sezione Links lo ignoriamo, in Notes/Ideas lo manteniamo come testo.
            if current_section == "links":
                continue

        if current_section is not None:
            sections[current_section].append(line)

    result: dict[str, str] = {}
    for key, lines in sections.items():
        joined = "\n".join(lines).strip()
        result[key] = joined if has_meaningful_content(joined) else ""

    return result


def split_markdown_items(text: str) -> list[str]:
    """Divide il testo in blocchi basati su elenchi puntati (-, *, +) o numerati (1.),
    mantenendo insieme le righe rientrate o consecutive dello stesso punto.

    Filtra automaticamente eventuali punti elenco vuoti.
    """
    if not has_meaningful_content(text):
        return []

    items: list[str] = []
    current_item: list[str] = []

    bullet_re = re.compile(r"^\s*([*+-]|\d+\.)\s+(.*)$")

    for line in text.splitlines():
        # Ignora intestazioni markdown orfane eventualmente rimaste
        if _HEADING_RE.match(line):
            continue

        m = bullet_re.match(line)
        if m:
            if current_item:
                joined = "\n".join(current_item).strip()
                if has_meaningful_content(joined):
                    items.append(joined)
                current_item = []
            cleaned_line = m.group(2).strip()
            if cleaned_line:
                current_item.append(cleaned_line)
        else:
            if current_item:
                current_item.append(line)
            elif line.strip():
                # Testo libero prima di qualsiasi elenco
                current_item.append(line.strip())

    if current_item:
        joined = "\n".join(current_item).strip()
        if has_meaningful_content(joined):
            items.append(joined)

    return items


_URL_TOKEN_RE = re.compile(
    r"^(https?://\S+|(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?:/\S*)?)$"
)


def is_pure_url_item(text: str) -> bool:
    """Restituisce True se il testo è costituito esclusivamente da uno o più URL
    (evita che link finiti per errore in Note/Idee vengano inventati come note).
    """
    tokens = [t.strip("<>()[]") for t in text.split() if t.strip()]
    if not tokens:
        return False
    return all(_URL_TOKEN_RE.match(t) is not None for t in tokens)


def extract_links_from_section(text: str) -> list[tuple[str, str]]:
    """Estrae una lista di tuple (url, contesto_opzionale) dalla sezione Links.

    Ignora righe che non contengono URL validi o domini riconoscibili (es. sottotitoli).
    Supporta sia URL nudi (es. 'serfsrl.it' o 'https://example.com - nota')
    sia link Markdown '[Titolo](https://example.com)'.
    """
    if not has_meaningful_content(text):
        return []

    results: list[tuple[str, str]] = []
    md_link_re = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or _HEADING_RE.match(line):
            continue

        # Rimuove bullet iniziale (-, *, +, 1.)
        line = re.sub(r"^([*+-]|\d+\.)\s+", "", line).strip()
        if not line:
            continue

        # Caso 1: Link in formato Markdown [testo](url)
        md_match = md_link_re.search(line)
        if md_match:
            label, url = md_match.group(1).strip(), md_match.group(2).strip()
            rest = (line[: md_match.start()] + " " + line[md_match.end() :]).strip(" -—:")
            note = f"{label}. {rest}".strip(". ") if rest else label
            results.append((url, note))
            continue

        # Caso 2: Primo token è un URL o dominio
        parts = line.split(maxsplit=1)
        first_token = parts[0].strip("<>")
        if _URL_TOKEN_RE.match(first_token):
            context = parts[1].strip(" -—:") if len(parts) > 1 else ""
            results.append((first_token, context))

    return results
