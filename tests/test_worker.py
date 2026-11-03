#!/usr/bin/env python3
"""Suite di test completa per obsidian_worker e i suoi edge case."""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import obsidian_worker
from worker.config import Config, load_dotenv
from worker.git_utils import (
    VaultError,
    ensure_vault_gitignore,
    git_commit,
    resolve_daily_note_path,
    validate_vault,
)
from worker.ollama_client import OllamaError, query_ollama
from worker.parser import (
    extract_links_from_section,
    has_meaningful_content,
    is_pure_url_item,
    parse_daily_sections,
    split_markdown_items,
)
from worker.processors import (
    clean_slug,
    format_tags_yaml,
    get_unique_filepath,
    process_ideas,
    process_links,
    process_notes,
    process_todos,
    sanitize_tag,
    sanitize_yaml_string,
)


def make_test_config(vault_dir: str) -> Config:
    return Config(
        vault_path=vault_dir,
        todo_file=os.path.join(vault_dir, "Tasks.md"),
        notes_dir=os.path.join(vault_dir, "Notes"),
        ideas_dir=os.path.join(vault_dir, "Ideas"),
        links_dir=os.path.join(vault_dir, "Links"),
        daily_dir=os.path.join(vault_dir, "Daily"),
        ollama_url="http://127.0.0.1:11434/api/chat",
        ollama_model="qwen2.5:3b",
        ollama_timeout=10,
        ollama_temperature=0.1,
        ollama_num_predict=256,
        url_fetch_timeout=2,
        git_auto_commit=True,
    )


class TestParserEdgeCases(unittest.TestCase):
    """Verifica tutti gli edge case del parser Markdown e delle sezioni."""

    def test_empty_and_meaningless_inputs(self) -> None:
        for empty_sample in [
            "",
            "   \n\t  ",
            "* \n- \n+ ",
            "- [ ]\n- [x]\n* [X] ",
            "[ ]\n[x]",
            "---\n***\n___",
            "<!-- solo commento -->",
        ]:
            self.assertFalse(
                has_meaningful_content(empty_sample),
                f"Dovrebbe essere vuoto: {empty_sample!r}",
            )
            self.assertEqual(split_markdown_items(empty_sample), [])

    def test_template_with_only_empty_sections(self) -> None:
        raw = "### 📝 Todo\n### 💡Ideas\n\n### 📦 Notes\n### 🔗 Links\n"
        sections = parse_daily_sections(raw)
        self.assertEqual(
            sections, {"todo": "", "ideas": "", "notes": "", "links": ""}
        )

    def test_headings_without_space_and_aliases(self) -> None:
        raw = (
            "###📝Todo\n"
            "* Task uno\n"
            "## To-Do\n"
            "* Task due\n"
            "#### 💡Spunti\n"
            "* Un'idea\n"
            "##### 📦 Appunti\n"
            "* Una nota\n"
            "###### 🔗 Risorse\n"
            "* https://example.com\n"
        )
        sections = parse_daily_sections(raw)
        self.assertEqual(
            split_markdown_items(sections["todo"]), ["Task uno", "Task due"]
        )
        self.assertEqual(split_markdown_items(sections["ideas"]), ["Un'idea"])
        self.assertEqual(split_markdown_items(sections["notes"]), ["Una nota"])
        self.assertEqual(
            extract_links_from_section(sections["links"]),
            [("https://example.com", "")],
        )

    def test_frontmatter_vs_horizontal_rule(self) -> None:
        # Caso 1: Vero YAML frontmatter
        with_yaml = "---\ntitle: Daily\ndate: 2026-11-02\n---\n### 📝 Todo\n* Fare backup\n"
        self.assertEqual(
            split_markdown_items(parse_daily_sections(with_yaml)["todo"]),
            ["Fare backup"],
        )

        # Caso 2: '---' usato come separatore orizzontale all'inizio e alla fine (NON deve mangiare la nota)
        with_hr = "---\n### 📝 Todo\n* Non cancellarmi\n---\n"
        self.assertEqual(
            split_markdown_items(parse_daily_sections(with_hr)["todo"]),
            ["Non cancellarmi"],
        )

    def test_long_subheading_with_keywords_does_not_switch_section(self) -> None:
        raw = (
            "### 📦 Notes\n"
            "### Architettura dei task e delle idee nel progetto\n"
            "* Dettaglio tecnico importante\n"
        )
        sections = parse_daily_sections(raw)
        self.assertEqual(sections["todo"], "")
        self.assertEqual(sections["ideas"], "")
        self.assertIn("Dettaglio tecnico importante", sections["notes"])

    def test_section_with_only_unrecognized_subheading_and_links_is_isolated(self) -> None:
        raw = (
            "### 📝 Todo\n"
            "### 💡Ideas\n"
            "### 📦 Notes\n"
            "### 🔗 Links\n"
            "### Herolapse\n"
            "* https://github.com/raspberrypi/rpi-image-gen\n"
        )
        sections = parse_daily_sections(raw)
        self.assertEqual(sections["todo"], "")
        self.assertEqual(sections["ideas"], "")
        self.assertEqual(sections["notes"], "")
        self.assertEqual(
            extract_links_from_section(sections["links"]),
            [("https://github.com/raspberrypi/rpi-image-gen", "")],
        )

    def test_links_section_with_only_subheading_and_no_urls_is_empty(self) -> None:
        raw = "### 🔗 Links\n### Herolapse\nSolo testo senza url\n"
        sections = parse_daily_sections(raw)
        self.assertEqual(sections["links"], "")

    def test_bullets_without_space_and_checkboxes(self) -> None:
        raw = (
            "*Comprare latte\n"
            "-Chiamare idraulico\n"
            "- [ ] Pagare bolletta\n"
            "[x] Task già spuntato\n"
            "1) Primo punto numerato\n"
        )
        items = split_markdown_items(raw)
        self.assertEqual(
            items,
            [
                "Comprare latte",
                "Chiamare idraulico",
                "Pagare bolletta",
                "Task già spuntato",
                "Primo punto numerato",
            ],
        )

    def test_extract_links_edge_cases(self) -> None:
        raw = (
            "* https://developer.chrome.com/docs/ai/webmcp?hl=it\n"
            "* serfsrl.it - Sito ufficiale filtri aria\n"
            "* Sito utile a fine riga: https://example.org/docs\n"
            "* Articolo interessante (https://news.ycombinator.com/item?id=1), da leggere\n"
            "* [Guida NixOS](https://nixos.org/manual) — documentazione ufficiale\n"
            "* [Dominio Nudo](example.net/guida)\n"
            "*https://nospace.example.com\n"
            "- [ ] https://checkbox.example.com\n"
        )
        links = extract_links_from_section(raw)
        urls = [u for u, _ in links]
        self.assertEqual(
            urls,
            [
                "https://developer.chrome.com/docs/ai/webmcp?hl=it",
                "serfsrl.it",
                "https://example.org/docs",
                "https://news.ycombinator.com/item?id=1",
                "https://nixos.org/manual",
                "example.net/guida",
                "https://nospace.example.com",
                "https://checkbox.example.com",
            ],
        )
        # Verifica conservazione contesto
        ctx_map = dict(links)
        self.assertEqual(ctx_map["serfsrl.it"], "Sito ufficiale filtri aria")
        self.assertEqual(ctx_map["https://example.org/docs"], "Sito utile a fine riga")

    def test_is_pure_url_item(self) -> None:
        self.assertTrue(is_pure_url_item("https://github.com/org/repo"))
        self.assertTrue(is_pure_url_item("<https://example.com>"))
        self.assertFalse(is_pure_url_item(" Studiare https://example.com per il progetto"))


class TestBugReproductionAndProcessors(unittest.TestCase):
    """Riproduce esattamente il bug del 2026-11-02 (1 riga in Todo + commit Git fallito)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = self.tmp.name
        os.makedirs(os.path.join(self.vault, "Daily"), exist_ok=True)
        self.config = make_test_config(self.vault)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @patch("worker.processors.query_ollama")
    def test_single_line_todo_when_llm_returns_empty_list(self, mock_ollama) -> None:
        """Bug #1: Se la nota ha 1 sola riga in Todo e Ollama restituisce {'tasks': []},
        il fallback deterministico deve comunque salvare il task in Tasks.md.
        """
        mock_ollama.return_value = {"tasks": []}

        count = process_todos("* Mandare fattura al commercialista", "2026-11-02", self.config)
        self.assertEqual(count, 1)
        self.assertTrue(os.path.isfile(self.config.todo_file))

        with open(self.config.todo_file, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("- [ ] Mandare fattura al commercialista #2026-11-02", content)

    @patch("worker.processors.query_ollama")
    def test_single_line_todo_when_llm_hallucinates_system_prompt(self, mock_ollama) -> None:
        """Se il modello ripete il system prompt, viene filtrato e scatta il fallback all'input utente."""
        mock_ollama.return_value = {
            "tasks": [
                "Estrai tutte le azioni atomiche dal testo.",
                "Restituisci frasi concise all'infinito in italiano.",
            ]
        }

        count = process_todos("Dentista ore 16", "2026-11-02", self.config)
        self.assertEqual(count, 1)
        with open(self.config.todo_file, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("- [ ] Dentista ore 16 #2026-11-02", content)

    @patch("worker.processors.query_ollama")
    def test_todo_when_llm_returns_dict_items_or_drops_items(self, mock_ollama) -> None:
        """Se l'utente scrive 2 punti elenco ma il modello ne restituisce solo 1, non si perde nulla."""
        mock_ollama.return_value = {"tasks": [{"task": "Solo il primo task"}]}

        count = process_todos("* Primo task\n* Secondo task", "2026-11-02", self.config)
        self.assertEqual(count, 2)
        with open(self.config.todo_file, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("- [ ] Primo task #2026-11-02", content)
        self.assertIn("- [ ] Secondo task #2026-11-02", content)

    def test_git_commit_with_untracked_deleted_daily_note(self) -> None:
        """Bug #2: Quando la Daily Note non era ancora tracciata da Git e viene eliminata
        da os.remove(), git_commit NON deve fallire con exit status 128.
        """
        subprocess.run(["git", "init"], cwd=self.vault, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=self.vault,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=self.vault,
            check=True,
        )

        # Crea Tasks.md e una Daily Note NON tracciata che poi viene rimossa (come fa il worker)
        daily_path = os.path.join(self.vault, "Daily", "2026-11-02.md")
        with open(daily_path, "w", encoding="utf-8") as f:
            f.write("### 📝 Todo\n* Unico task\n")
        with open(self.config.todo_file, "w", encoding="utf-8") as f:
            f.write("- [ ] Unico task #2026-11-02\n")

        # Rimuove la Daily Note prima del commit (esattamente come fa obsidian_worker.py)
        os.remove(daily_path)

        committed = git_commit(
            self.vault,
            "Ingest 2026-11-02",
            paths_to_stage=[self.config.todo_file, daily_path],
        )
        self.assertTrue(committed)

        # Se l'unico file in paths_to_stage era la Daily Note non tracciata già eliminata,
        # git_commit deve restituire False senza sollevare CalledProcessError(128).
        committed_empty = git_commit(
            self.vault,
            "Ingest vuoto",
            paths_to_stage=[daily_path],
        )
        self.assertFalse(committed_empty)

    def test_git_commit_with_tracked_deleted_daily_note(self) -> None:
        """Se la Daily Note ERA già tracciata da Git e viene eliminata, la sua rimozione viene committata."""
        subprocess.run(["git", "init"], cwd=self.vault, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.vault, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.vault, check=True)

        daily_path = os.path.join(self.vault, "Daily", "2026-11-02.md")
        with open(daily_path, "w", encoding="utf-8") as f:
            f.write("### 📝 Todo\n* Task\n")
        subprocess.run(["git", "add", "Daily/2026-11-02.md"], cwd=self.vault, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=self.vault, check=True, capture_output=True)

        os.remove(daily_path)
        committed = git_commit(
            self.vault,
            "Remove tracked daily",
            paths_to_stage=[daily_path],
        )
        self.assertTrue(committed)

    @patch("obsidian_worker.check_ollama_available")
    @patch("obsidian_worker.process_todos", return_value=0)
    def test_safeguard_preserves_daily_note_if_processor_returns_zero(
        self, _mock_todos, _mock_check
    ) -> None:
        """Safeguard critico: se una sezione non vuota restituisce 0 elementi estratti,
        la Daily Note NON deve essere cancellata.
        """
        daily_path = os.path.join(self.vault, "Daily", "2026-11-02.md")
        with open(daily_path, "w", encoding="utf-8") as f:
            f.write("### 📝 Todo\n* Linea importante\n")

        env_file = os.path.join(self.vault, ".env.test")
        with open(env_file, "w", encoding="utf-8") as f:
            f.write(f"VAULT_PATH={self.vault}\nGIT_AUTO_COMMIT=false\n")

        with patch.object(
            sys,
            "argv",
            ["obsidian_worker.py", "2026-11-02", "--env-file", env_file],
        ):
            with patch.dict(os.environ, {"VAULT_PATH": self.vault, "GIT_AUTO_COMMIT": "false"}):
                exit_code = obsidian_worker.main()

        self.assertEqual(exit_code, 1)
        self.assertTrue(
            os.path.exists(daily_path),
            "La Daily Note non doveva essere eliminata quando vengono estratti 0 elementi!",
        )

    @patch("worker.processors.query_ollama")
    @patch("worker.processors.fetch_url_metadata")
    def test_ideas_notes_links_creation_and_collisions(
        self, mock_meta, mock_ollama
    ) -> None:
        """Verifica creazione file, anti-collisione, sanitizzazione YAML e fallback."""
        mock_ollama.side_effect = [
            {"title": 'Idea: "Speciale" / Test?', "tags": ["#ai", "casa domotica"], "body": "Corpo [[Concetto]] pulito"},
            {"title": 'Idea: "Speciale" / Test?', "tags": ["AI"], "body": ""},
            {"title": "Nota Tecnica", "tags": ["linux"], "markdown_body": "Dettagli nota"},
             OllamaError("LLM offline per il link"),
        ]
        mock_meta.return_value = {
            "url": "https://example.com",
            "title": "Example Title",
            "description": "Example Desc",
        }

        ideas = process_ideas("* Prima idea\n* Seconda idea", "2026-11-02", self.config)
        self.assertEqual(len(ideas), 2)
        self.assertNotEqual(ideas[0], ideas[1])
        self.assertTrue(ideas[1].endswith("_2.md"))

        with open(ideas[0], "r", encoding="utf-8") as f:
            idea1_text = f.read()
        self.assertIn('title: "Idea: \\"Speciale\\" / Test?"', idea1_text)
        self.assertIn("  - casa-domotica", idea1_text)
        self.assertNotIn("[[Concetto]]", idea1_text)

        # La seconda idea aveva body vuoto dall'LLM -> deve usare il fallback al testo utente
        with open(ideas[1], "r", encoding="utf-8") as f:
            idea2_text = f.read()
        self.assertIn("Seconda idea", idea2_text)

        notes = process_notes("* Appunto importante", "2026-11-02", self.config)
        self.assertEqual(len(notes), 1)

        # Il link deve funzionare anche se query_ollama solleva OllamaError (usa fallback metadati)
        links = process_links("* https://example.com - Mio contesto", "2026-11-02", self.config)
        self.assertEqual(len(links), 1)
        with open(links[0], "r", encoding="utf-8") as f:
            link_text = f.read()
        self.assertIn("Mio contesto", link_text)


class TestUtilsAndClientEdgeCases(unittest.TestCase):
    """Test per sanitizzazione, risoluzione percorsi, .env e JSON di Ollama."""

    def test_slug_and_yaml_sanitization(self) -> None:
        self.assertEqual(clean_slug('???///:::"*"', fallback="Fallback"), "Fallback")
        self.assertEqual(sanitize_tag(" #Tag Con Spazi/Simboli? "), "Tag-Con-SpaziSimboli")
        self.assertEqual(sanitize_yaml_string('Titolo con "apici"\ne a capo'), 'Titolo con \\"apici\\" e a capo')
        tags_yaml = format_tags_yaml("type/note", ["Linux", "#linux", "  ", " Nix OS "])
        self.assertEqual(tags_yaml, "  - type/note\n  - Linux\n  - Nix-OS")

    def test_resolve_daily_note_protects_index_and_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daily_dir = os.path.join(tmp, "Daily")
            os.makedirs(daily_dir)
            index_file = os.path.join(daily_dir, "Daily.md")
            with open(index_file, "w", encoding="utf-8") as f:
                f.write("# Indice")

            with self.assertRaises(VaultError):
                resolve_daily_note_path(index_file, daily_dir, tmp)

            with self.assertRaises(VaultError):
                resolve_daily_note_path(daily_dir, daily_dir, tmp)

    @patch("urllib.request.urlopen")
    def test_query_ollama_strips_markdown_fences(self, mock_urlopen) -> None:
        class FakeResp:
            status = 200
            def read(self) -> bytes:
                return b'{"message": {"content": "```json\\n{\\"tasks\\": [\\"Fare test\\"]}\\n```"}}'
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        mock_urlopen.return_value = FakeResp()
        cfg = make_test_config("/tmp")
        res = query_ollama([{"role": "user", "content": "hi"}], cfg, json_schema={"type": "object"})
        self.assertEqual(res, {"tasks": ["Fare test"]})


if __name__ == "__main__":
    unittest.main(verbosity=2)
