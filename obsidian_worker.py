#!/usr/bin/env python3
import os
import re
import sys
import json
import subprocess
import urllib.request
import urllib.error
from datetime import datetime
from html.parser import HTMLParser

# ================= CONFIGURAZIONE =================
VAULT_PATH = os.path.expanduser("~/Obsidian")

# Struttura esatta dal tuo Vault
TODO_FILE = os.path.join(VAULT_PATH, "Tasks.md")
NOTES_DIR = os.path.join(VAULT_PATH, "Notes")
IDEAS_DIR = os.path.join(VAULT_PATH, "Ideas")
LINKS_DIR = os.path.join(VAULT_PATH, "Links")

# Ollama
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
MODEL = "qwen2.5:3b"
# ===================================================


class MetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.description = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs_dict = {k.lower(): v for k, v in attrs if v}
        if tag.lower() == "title" and not self.title:
            self._in_title = True
        elif tag.lower() == "meta":
            name = attrs_dict.get("name", "").lower()
            prop = attrs_dict.get("property", "").lower()
            if name in ("description", "og:description") or prop in (
                "og:description",
                "description",
            ):
                if not self.description:
                    self.description = attrs_dict.get("content", "")

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and not self.title:
            self.title = data.strip()


def query_ollama(messages, json_schema=None, timeout=300):
    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": 0.2,
            "num_predict": 512,  # Limita l'output per non andare fuori controllo su CPU
        },
    }
    if json_schema:
        payload["format"] = json_schema

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL, data=data, headers={"Content-Type": "application/json"}
    )
    # Timeout portato a 300s (5 minuti)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        return res["message"]["content"]


def clean_slug(title: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|#]', "", title).strip()
    return re.sub(r"\s+", "_", cleaned)


def fetch_url_metadata(raw_url: str):
    url = raw_url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0",
                "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        )
        with urllib.request.urlopen(req, timeout=6) as response:
            chunk = response.read(65536).decode("utf-8", errors="ignore")
            parser = MetaParser()
            parser.feed(chunk)
            return {
                "url": url,
                "title": parser.title or url,
                "description": parser.description
                or "Nessuna descrizione presente nei metadati.",
            }
    except Exception as e:
        return {
            "url": url,
            "title": url,
            "description": f"Impossibile risolvere l'URL ({e})",
        }


def parse_daily_sections(content: str):
    sections = {"todo": "", "ideas": "", "notes": "", "links": ""}
    patterns = {
        "todo": r"###\s*📝\s*Todo\s*\n(.*?)(?=\n###|\Z)",
        "ideas": r"###\s*💡\s*Ideas\s*\n(.*?)(?=\n###|\Z)",
        "notes": r"###\s*📦\s*Notes\s*\n(.*?)(?=\n###|\Z)",
        "links": r"###\s*🔗\s*Links\s*\n(.*?)(?=\n###|\Z)",
    }
    for key, pattern in patterns.items():
        m = re.search(pattern, content, re.DOTALL)
        if m:
            sections[key] = m.group(1).strip()
    return sections


def process_todos(text: str, source_date: str) -> int:
    if not text:
        return 0

    schema = {
        "type": "object",
        "properties": {"tasks": {"type": "array", "items": {"type": "string"}}},
        "required": ["tasks"],
    }

    prompt = (
        "Sei un assistente per la gestione task. Estrai tutte le azioni atomiche dal testo. "
        "Restituisci frasi concise all'infinito o all'imperativo in lingua italiana."
    )
    res = query_ollama(
        [{"role": "system", "content": prompt}, {"role": "user", "content": text}],
        json_schema=schema,
    )

    tasks = json.loads(res).get("tasks", [])
    if not tasks:
        return 0

    with open(TODO_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n<!-- Ingest Daily {source_date} -->\n")
        for t in tasks:
            # Rimuove l'emoji e usa il tag #YYYY-MM-DD per generare la pill cliccabile
            f.write(f"- [ ] {t} #{source_date}\n")
    return len(tasks)


def process_ideas(text: str, source_date: str) -> int:
    if not text:
        return 0

    schema = {
        "type": "object",
        "properties": {
            "ideas": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "body": {"type": "string"},
                    },
                    "required": ["title", "tags", "body"],
                },
            }
        },
        "required": ["ideas"],
    }

    prompt = (
        "Dato il testo contenente una o più idee/intuizioni: "
        "1. Genera un titolo descrittivo sintetico (in italiano) per il file. "
        "2. Identifica da 2 a 4 tag concettuali (array di stringhe, senza '#'). "
        "3. Riformula il corpo dell'idea in Markdown chiaro, includendo wikilink [[Concetto]] pertinenti."
    )
    res = query_ollama(
        [{"role": "system", "content": prompt}, {"role": "user", "content": text}],
        json_schema=schema,
    )

    ideas = json.loads(res).get("ideas", [])
    os.makedirs(IDEAS_DIR, exist_ok=True)

    for item in ideas:
        filename = f"{clean_slug(item['title'])}.md"
        filepath = os.path.join(IDEAS_DIR, filename)
        tags_yaml = "\n".join([f"  - {t}" for t in item.get("tags", [])])

        md_content = f"""---
title: "{item["title"]}"
date: {source_date}
source: "[[{source_date}]]"
tags:
  - type/idea
{tags_yaml}
---

{item.get("body", "").strip()}
"""
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(md_content)

    return len(ideas)


def split_markdown_items(text: str) -> list[str]:
    """Divide il testo in blocchi basati su elenchi puntati (- o *) o numerati,

    mantenendo insieme le righe rientrate dello stesso punto.
    """
    items = []
    current_item = []

    for line in text.splitlines():
        # Match per inizio punto elenco: '-', '*', oppure '1.' all'inizio riga
        if re.match(r"^\s*([*+-]|\d+\.)\s+", line):
            if current_item:
                items.append("\n".join(current_item).strip())
                current_item = []
            # Rimuove il marcatore iniziale (* o -)
            cleaned_line = re.sub(r"^\s*([*+-]|\d+\.)\s+", "", line)
            current_item.append(cleaned_line)
        else:
            if current_item:
                current_item.append(line)
            elif line.strip():
                # Se c'è testo senza marcatore prima del primo elenco
                current_item.append(line)

    if current_item:
        items.append("\n".join(current_item).strip())

    return [item for item in items if item]


def process_notes(text: str, source_date: str) -> int:
    if not text:
        return 0

    # Suddivide deterministicamente per ogni punto (* o -)
    raw_notes = split_markdown_items(text)
    if not raw_notes:
        return 0

    os.makedirs(NOTES_DIR, exist_ok=True)
    count = 0

    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "markdown_body": {"type": "string"},
        },
        "required": ["title", "tags", "markdown_body"],
    }

    prompt = (
        "Sei un assistente per la knowledge base personale in lingua ITALIANA. "
        "Dato l'appunto grezzo: "
        "1. Genera un titolo breve e fedele al testo (max 4-5 parole). "
        "2. Assegna da 2 a 4 tag pertinenti in italiano (senza #). "
        "3. Scrivi un corpo Markdown sintetico attenendoti ESCLUSIVAMENTE ai fatti menzionati. "
        "REGOLA FONDAMENTALE: Non inventare agende, punti elenco o dettagli tecnici non presenti. "
        "Se l'appunto è di una sola frase, la nota deve contenere solo quella frase riformulata con wikilink [[Concetto]]."
    )

    for single_note_text in raw_notes:
        res = query_ollama(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": single_note_text},
            ],
            json_schema=schema,
        )

        data = json.loads(res)
        title = data.get("title", f"Nota_{source_date}_{count + 1}").strip()
        filename = f"{clean_slug(title)}.md"
        filepath = os.path.join(NOTES_DIR, filename)

        tags_yaml = "\n".join([f"  - {t}" for t in data.get("tags", [])])
        md_content = f"""---
title: "{title}"
date: {source_date}
source: "[[{source_date}]]"
tags:
  - type/note
{tags_yaml}
---

{data.get("markdown_body", "").strip()}
"""
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(md_content)
        count += 1

    return count


def process_links(text: str, source_date: str) -> int:
    if not text:
        return 0

    lines = text.splitlines()
    raw_urls = []
    for l in lines:
        cleaned = l.strip().lstrip("-* \t")
        if not cleaned:
            continue
        parts = cleaned.split()
        if parts:
            raw_urls.append(parts[0])

    if not raw_urls:
        return 0

    os.makedirs(LINKS_DIR, exist_ok=True)
    count = 0

    for raw_url in raw_urls:
        meta = fetch_url_metadata(raw_url)

        schema = {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "description", "tags"],
        }
        prompt = (
            "Sei un archivista di risorse web. Rispondi SEMPRE E SOLO IN ITALIANO. "
            "Dati URL, titolo grezzo e descrizione estratta: "
            "1. 'title': Titolo pulito, compatto e formale in italiano (rimuovi slogan commerciali superflui). "
            "2. 'description': Spiegazione chiara di 1-2 frasi in LINGUA ITALIANA (traduci se il testo originale è in un'altra lingua) che spieghi di cosa si occupa il sito/prodotto. "
            "3. 'tags': Da 2 a 4 tag tematici in italiano (senza #)."
        )
        user_input = f"URL: {meta['url']}\nTitolo grezzo: {meta['title']}\nDescrizione grezza: {meta['description']}"

        try:
            res = query_ollama(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_input},
                ],
                json_schema=schema,
            )
            parsed = json.loads(res)
        except Exception:
            parsed = {
                "title": meta["title"],
                "description": meta["description"],
                "tags": ["web"],
            }

        title = parsed.get("title", meta["url"]).strip()
        filename = f"{clean_slug(title)}.md"
        filepath = os.path.join(LINKS_DIR, filename)

        tags_yaml = "\n".join([f"  - {t}" for t in parsed.get("tags", [])])
        md_content = f"""---
title: "{title}"
url: "{meta["url"]}"
date: {source_date}
source: "[[{source_date}]]"
tags:
  - type/link
{tags_yaml}
---

# [{meta["url"]}]({meta["url"]})

{parsed.get("description", "").strip()}
"""
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(md_content)
        count += 1

    return count


def git_commit(vault_path: str, message: str):
    subprocess.run(["git", "add", "."], cwd=vault_path, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=vault_path, check=True)


def main():
    if len(sys.argv) < 2:
        print("Uso: python3 obsidian_worker.py <path_to_daily.md>")
        sys.exit(1)

    daily_path = os.path.abspath(sys.argv[1])
    if not os.path.exists(daily_path):
        print(f"Errore: il file {daily_path} non esiste.")
        sys.exit(1)

    # Estrae la data dal nome del file (es: 2026-11-02.md)
    filename_without_ext = os.path.splitext(os.path.basename(daily_path))[0]
    match_date = re.search(r"\d{4}-\d{2}-\d{2}", filename_without_ext)
    source_date = (
        match_date.group(0) if match_date else datetime.now().strftime("%Y-%m-%d")
    )

    with open(daily_path, "r", encoding="utf-8") as f:
        content = f.read()

    sections = parse_daily_sections(content)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Elaborazione Daily: {daily_path}")
    todos_count = process_todos(sections["todo"], source_date)
    ideas_count = process_ideas(sections["ideas"], source_date)
    notes_count = process_notes(sections["notes"], source_date)
    links_count = process_links(sections["links"], source_date)

    # Rimozione della Daily Note dopo l'ingest
    if os.path.exists(daily_path):
        os.remove(daily_path)
        print(f"Daily note {daily_path} rimossa con successo.")

    # Commit message formattato con contatori
    now_str = datetime.now().strftime("%H:%M:%S")
    summary_parts = []
    if todos_count:
        summary_parts.append(f"{todos_count} todo")
    if ideas_count:
        summary_parts.append(f"{ideas_count} {'idea' if ideas_count == 1 else 'idee'}")
    if notes_count:
        summary_parts.append(f"{notes_count} {'nota' if notes_count == 1 else 'note'}")
    if links_count:
        summary_parts.append(f"{links_count} {'link' if links_count == 1 else 'links'}")

    summary_str = (
        ", ".join(summary_parts) if summary_parts else "nessun elemento estratto"
    )
    commit_msg = f"chore(daily): ingest {source_date} ore {now_str} — {summary_str}"

    try:
        git_commit(VAULT_PATH, commit_msg)
        print(f"Git commit eseguito: '{commit_msg}'")
    except Exception as e:
        print(f"Attenzione: errore durante il commit Git: {e}")


if __name__ == "__main__":
    main()
