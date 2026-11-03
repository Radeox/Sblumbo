"""Deterministic parser for Daily Note sections and Markdown lists."""

import re


# Known section aliases (case-insensitive, with or without emojis)
_SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "todo": ("todo", "todos", "task", "tasks", "to do", "da fare"),
    "ideas": ("ideas", "idea", "idee", "spunti"),
    "notes": ("notes", "note", "appunti", "appunto"),
    "links": ("links", "link", "url", "urls", "risorse"),
}

# Regex matching Markdown headings of levels 1-6 (with or without space after #{2,6}, e.g. "###📝 Todo")
_HEADING_RE = re.compile(r"^\s*(#{1,6}\s+|#{2,6})(.+?)\s*$")

# Regex for Markdown horizontal divider lines (---, ***, ___)
_HR_RE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$", re.MULTILINE)

# Regex to strip HTML comments before parsing
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _strip_frontmatter(content: str) -> str:
    """Remove leading YAML frontmatter delimited by '---'."""
    text = content.lstrip("\ufeff")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text

    for idx in range(1, len(lines)):
        stripped = lines[idx].strip()
        if stripped in ("---", "..."):
            return "\n".join(lines[idx + 1 :])
        # If we encounter a Markdown heading before closing '---', the initial line was a divider, not YAML
        if _HEADING_RE.match(lines[idx]):
            return text
    return text


def _classify_heading(heading_text: str) -> str | None:
    """Return canonical section key ('todo', 'ideas', 'notes', 'links') or None."""
    # Strip emojis and punctuation, leaving letters and numbers
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
    """Check whether a section or item contains at least one meaningful alphanumeric character.

    Prevents lines with only empty bullets ('- ', '* ', '- [ ]', '[x]') or dividers ('---')
    from being processed.
    """
    if not text:
        return False
    cleaned = _HTML_COMMENT_RE.sub("", text)
    cleaned = _HR_RE.sub("", cleaned)
    # Remove empty checkboxes and list markers
    cleaned = re.sub(
        r"^\s*(?:(?:[*+-]|\d+[.)])\s*)?(?:\[[ xX]?\]\s*)?",
        "",
        cleaned,
        flags=re.MULTILINE,
    )
    return bool(re.search(r"[^\W_]", cleaned, flags=re.UNICODE))


def parse_daily_sections(content: str) -> dict[str, str]:
    """Split Daily Note into the 4 canonical sections: todo, ideas, notes, links.

    - Properly stops at the next section even if the current one is empty.
    - Preserves unrecognised subheadings (e.g. '### Subtitle') without overflowing links into notes.
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
            # Unknown subheading inside Links: ignore to avoid mistaking it for a link
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
    """Split text into blocks based on bullet points (-, *, +) or numbers (1., 1)).

    Preserves indented and continuation lines.
    Supports bullet without leading space (e.g. '*Todo') and checkboxes ('- [ ] Todo', '[ ] Todo').
    Filters out empty bullets automatically.
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
            # If canonical heading (Todo/Ideas/Notes/Links), ignore it
            if _classify_heading(m_head.group(2)) is not None:
                continue
            # User subheading (e.g. "#### Call Alice"): preserve as separate item
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
                # Free-form text before list items
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
    """Return True if text consists strictly of one or more URLs."""
    tokens = [t.strip("<>()[].,;") for t in text.split() if t.strip("<>()[].,;")]
    if not tokens:
        return False
    return all(_URL_TOKEN_RE.match(t) is not None for t in tokens)


def extract_links_from_section(text: str) -> list[tuple[str, str]]:
    """Extract list of (url, optional_context) tuples from the Links section.

    Ignores lines without valid URLs or recognizable domains.
    Supports:
    - Bare URLs at start or end of line (e.g. 'serfsrl.it', 'Filters site: https://serfsrl.it')
    - Bullets without space ('*https://example.com') and checkboxes ('- [ ] https://example.com')
    - Markdown links '[Title](https://example.com)' or '[Title](example.com)'
    """
    if not has_meaningful_content(text):
        return []

    results: list[tuple[str, str]] = []
    md_link_re = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or _HR_RE.match(line) or _HEADING_RE.match(line):
            continue

        # Remove leading bullet (-, *, +, 1.) and optional checkbox ([ ], [x])
        line = re.sub(
            r"^(?:(?:[*+-]|\d+[.)])\s*(?:\[[ xX]?\]\s*)?|\[[ xX]?\]\s*)", "", line
        ).strip()
        if not line:
            continue

        # Case 1: Markdown format [text](url)
        md_match = md_link_re.search(line)
        if md_match:
            label, candidate_url = md_match.group(1).strip(), md_match.group(2).strip("<>")
            if _URL_TOKEN_RE.match(candidate_url):
                rest = (line[: md_match.start()] + " " + line[md_match.end() :]).strip(" -—:")
                note = f"{label}. {rest}".strip(". ") if rest else label
                results.append((candidate_url, note))
                continue

        # Case 2: Search for URL token anywhere in the line
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
