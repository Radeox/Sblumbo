"""Parser deterministico per le sezioni della Daily Note e gli elenchi Markdown."""

import re


# Intestazioni di sezione note (case-insensitive, con o senza emoji)
_SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "todo": ("todo", "todos", "task", "tasks", "to do", "da fare"),
    "ideas": ("ideas", "idea", "idee", "spunti"),
    "notes": ("notes", "note", "appunti", "appunto"),
    "links": ("links", "link", "url", "urls", "risorse"),
}

# Regex che individua qualsiasi heading Markdown di livello 1-6 (anche senza spazio dopo #{2,6}, es. "###📝 Todo")
_HEADING_RE = re.compile(r"^\s*(#{1,6}\s+|#{2,6})(.+?)\s*$")

# Regex per righe di separazione orizzontale Markdown (---, ***, ___)
_HR_RE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$", re.MULTILINE)

# Regex per rimuovere commenti HTML prima del parsing
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _strip_frontmatter(content: str) -> str:
    """Rimuove eventuale YAML frontmatter iniziale delimitato da '---'."""
    text = content.lstrip("\ufeff")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text

    for idx in range(1, len(lines)):
        stripped = lines[idx].strip()
        if stripped in ("---", "..."):
            return "\n".join(lines[idx + 1 :])
        # Se incontriamo un heading Markdown prima della chiusura, il '---' iniziale era un separatore, non YAML
        if _HEADING_RE.match(lines[idx]):
            return text
    return text


def _classify_heading(heading_text: str) -> str | None:
    """Restituisce la chiave di sezione ('todo', 'ideas', 'notes', 'links') se l'heading
    corrisponde a una delle sezioni della Daily Note, altrimenti None.
    """
    # Rimuove emoji e punteggiatura mantenendo solo lettere/numeri
    cleaned = re.sub(r"[^\w\s]", " ", heading_text, flags=re.UNICODE).strip().lower()
    cleaned = re.sub(r"\s+", " ", cleaned)
    tokens = cleaned.split()
    if not tokens or len(tokens) > 3:
        return None

    for section_key, aliases in _SECTION_ALIASES.items():
        if cleaned in aliases:
            return section_key

    if len(tokens) <= 2:
        for section_key, aliases in _SECTION_ALIASES.items():
            if tokens[-1] in aliases or tokens[0] in aliases:
                return section_key
    return None


def has_meaningful_content(text: str) -> bool:
    """Verifica se una sezione o un elemento contiene almeno un carattere alfanumerico utile.

    Evita che righe con soli bullet vuoti ('- ', '* ', '- [ ]', '[x]') o separatori ('---')
    vengano passate all'LLM causando allucinazioni (Bug #1).
    """
    if not text:
        return False
    cleaned = _HTML_COMMENT_RE.sub("", text)
    cleaned = _HR_RE.sub("", cleaned)
    # Rimuove checkbox vuote e marcatori di lista prima di controllare
    cleaned = re.sub(
        r"^\s*(?:(?:[*+-]|\d+[.)])\s*)?(?:\[[ xX]?\]\s*)?",
        "",
        cleaned,
        flags=re.MULTILINE,
    )
    return bool(re.search(r"[^\W_]", cleaned, flags=re.UNICODE))


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
        if _HR_RE.match(line):
            continue

        m = _HEADING_RE.match(line)
        if m:
            matched_section = _classify_heading(m.group(2))
            if matched_section is not None:
                current_section = matched_section
                continue
            # Sottotitolo non riconosciuto dentro Links: lo ignoriamo per non scambiarlo per link
            if current_section == "links":
                continue

        if current_section is not None:
            sections[current_section].append(line)

    result: dict[str, str] = {}
    for key, lines in sections.items():
        joined = "\n".join(lines).strip()
        if key == "links":
            result[key] = joined if extract_links_from_section(joined) else ""
        else:
            result[key] = joined if split_markdown_items(joined) else ""

    return result


def split_markdown_items(text: str) -> list[str]:
    """Divide il testo in blocchi basati su elenchi puntati (-, *, +) o numerati (1., 1)),
    mantenendo insieme le righe rientrate o consecutive dello stesso punto.

    Supporta anche bullet senza spazio iniziale (es. '*Todo') e checkbox ('- [ ] Todo', '[ ] Todo').
    Filtra automaticamente eventuali punti elenco vuoti.
    """
    if not has_meaningful_content(text):
        return []

    items: list[str] = []
    current_item: list[str] = []

    bullet_re = re.compile(
        r"^\s*(?:(?:[*+-]|\d+[.)])\s+(?:\[[ xX]?\]\s*)?|\[[ xX]?\]\s+|[*-](?=\w))(.*)$"
    )
    empty_bullet_re = re.compile(
        r"^\s*(?:(?:[*+-]|\d+[.)])\s*(?:\[[ xX]?\]\s*)?|\[[ xX]?\]\s*)$"
    )

    for line in text.splitlines():
        if _HR_RE.match(line) or empty_bullet_re.match(line):
            if current_item:
                joined = "\n".join(current_item).strip()
                if has_meaningful_content(joined):
                    items.append(joined)
                current_item = []
            continue

        m_head = _HEADING_RE.match(line)
        if m_head:
            # Se è un'intestazione canonica (Todo/Ideas/Notes/Links), la ignoriamo
            if _classify_heading(m_head.group(2)) is not None:
                continue
            # Se è un sottotitolo scritto dall'utente (es. "#### Chiamare Marco"),
            # ne preserviamo il testo come elemento separato
            if current_item:
                joined = "\n".join(current_item).strip()
                if has_meaningful_content(joined):
                    items.append(joined)
                current_item = []
            heading_body = m_head.group(2).strip()
            if has_meaningful_content(heading_body):
                current_item.append(heading_body)
            continue

        m = bullet_re.match(line)
        if m:
            if current_item:
                joined = "\n".join(current_item).strip()
                if has_meaningful_content(joined):
                    items.append(joined)
                current_item = []
            cleaned_line = m.group(1).strip()
            if has_meaningful_content(cleaned_line):
                current_item.append(cleaned_line)
        else:
            if current_item:
                current_item.append(line)
            elif line.strip() and has_meaningful_content(line):
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
    tokens = [t.strip("<>()[].,;") for t in text.split() if t.strip("<>()[].,;")]
    if not tokens:
        return False
    return all(_URL_TOKEN_RE.match(t) is not None for t in tokens)


def extract_links_from_section(text: str) -> list[tuple[str, str]]:
    """Estrae una lista di tuple (url, contesto_opzionale) dalla sezione Links.

    Ignora righe che non contengono URL validi o domini riconoscibili (es. sottotitoli).
    Supporta:
    - URL nudi all'inizio o a fine riga (es. 'serfsrl.it', 'Sito filtri: https://serfsrl.it')
    - Bullet senza spazio ('*https://example.com') e checkbox ('- [ ] https://example.com')
    - Link Markdown '[Titolo](https://example.com)' o '[Titolo](example.com)'
    """
    if not has_meaningful_content(text):
        return []

    results: list[tuple[str, str]] = []
    md_link_re = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or _HR_RE.match(line) or _HEADING_RE.match(line):
            continue

        # Rimuove bullet iniziale (-, *, +, 1.) e checkbox opzionale ([ ], [x])
        line = re.sub(
            r"^(?:(?:[*+-]|\d+[.)])\s*(?:\[[ xX]?\]\s*)?|\[[ xX]?\]\s*)", "", line
        ).strip()
        if not line:
            continue

        # Caso 1: Link in formato Markdown [testo](url)
        md_match = md_link_re.search(line)
        if md_match:
            label, candidate_url = md_match.group(1).strip(), md_match.group(2).strip("<>")
            if _URL_TOKEN_RE.match(candidate_url):
                rest = (line[: md_match.start()] + " " + line[md_match.end() :]).strip(" -—:")
                note = f"{label}. {rest}".strip(". ") if rest else label
                results.append((candidate_url, note))
                continue

        # Caso 2: Cerca un token URL ovunque nella riga (all'inizio, in mezzo o alla fine)
        tokens = line.split()
        found_idx: int | None = None
        found_url: str = ""
        for idx, tok in enumerate(tokens):
            cleaned_tok = tok.strip("<>()[]\"'.,;:")
            if _URL_TOKEN_RE.match(cleaned_tok):
                found_idx = idx
                found_url = cleaned_tok
                break

        if found_idx is not None:
            other_tokens = tokens[:found_idx] + tokens[found_idx + 1 :]
            context = " ".join(other_tokens).strip(" -—:()")
            results.append((found_url, context))

    return results
