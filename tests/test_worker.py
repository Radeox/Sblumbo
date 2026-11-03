#!/usr/bin/env python3
"""Comprehensive test suite for Sblumbo and its edge cases."""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sblumbo
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
        ollama_think=False,
        url_fetch_timeout=2,
        git_auto_commit=True,
    )


class TestParserEdgeCases(unittest.TestCase):
    """Verify all Markdown and section parser edge cases."""

    def test_empty_and_meaningless_inputs(self) -> None:
        for empty_sample in [
            "",
            "   \n\t  ",
            "* \n- \n+ ",
            "- [ ]\n- [x]\n* [X] ",
            "[ ]\n[x]",
            "---\n***\n___",
            "<!-- comment only -->",
        ]:
            self.assertFalse(
                has_meaningful_content(empty_sample),
                f"Should be empty: {empty_sample!r}",
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
            "* Task one\n"
            "## To-Do\n"
            "* Task two\n"
            "#### 💡Spunti\n"
            "* An idea\n"
            "##### 📦 Appunti\n"
            "* A note\n"
            "###### 🔗 Risorse\n"
            "* https://example.com\n"
        )
        sections = parse_daily_sections(raw)
        self.assertEqual(
            split_markdown_items(sections["todo"]), ["Task one", "Task two"]
        )
        self.assertEqual(split_markdown_items(sections["ideas"]), ["An idea"])
        self.assertEqual(split_markdown_items(sections["notes"]), ["A note"])
        self.assertEqual(
            extract_links_from_section(sections["links"]),
            [("https://example.com", "")],
        )

    def test_frontmatter_vs_horizontal_rule(self) -> None:
        # Case 1: True YAML frontmatter
        with_yaml = "---\ntitle: Daily\ndate: 2026-11-02\n---\n### 📝 Todo\n* Run backup\n"
        self.assertEqual(
            split_markdown_items(parse_daily_sections(with_yaml)["todo"]),
            ["Run backup"],
        )

        # Case 2: '---' used as horizontal rule at start and end (must NOT consume the note)
        with_hr = "---\n### 📝 Todo\n* Do not delete me\n---\n"
        self.assertEqual(
            split_markdown_items(parse_daily_sections(with_hr)["todo"]),
            ["Do not delete me"],
        )

    def test_long_subheading_with_keywords_does_not_switch_section(self) -> None:
        raw = (
            "### 📦 Notes\n"
            "### Architecture of tasks and ideas in the project\n"
            "* Important technical detail\n"
        )
        sections = parse_daily_sections(raw)
        self.assertEqual(sections["todo"], "")
        self.assertEqual(sections["ideas"], "")
        self.assertIn("Important technical detail", sections["notes"])

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
        raw = "### 🔗 Links\n### Herolapse\nJust plain text without url\n"
        sections = parse_daily_sections(raw)
        self.assertEqual(sections["links"], "")

    def test_bullets_without_space_and_checkboxes(self) -> None:
        raw = (
            "*Buy milk\n"
            "-Call plumber\n"
            "- [ ] Pay bill\n"
            "[x] Checked task\n"
            "1) Numbered item\n"
        )
        items = split_markdown_items(raw)
        self.assertEqual(
            items,
            [
                "Buy milk",
                "Call plumber",
                "Pay bill",
                "Checked task",
                "Numbered item",
            ],
        )

    def test_extract_links_edge_cases(self) -> None:
        raw = (
            "* https://developer.chrome.com/docs/ai/webmcp?hl=it\n"
            "* serfsrl.it - Official filters site\n"
            "* Useful site at end of line: https://example.org/docs\n"
            "* Interesting article (https://news.ycombinator.com/item?id=1), must read\n"
            "* [NixOS Guide](https://nixos.org/manual) — official docs\n"
            "* [Bare Domain](example.net/guide)\n"
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
                "example.net/guide",
                "https://nospace.example.com",
                "https://checkbox.example.com",
            ],
        )
        ctx_map = dict(links)
        self.assertEqual(ctx_map["serfsrl.it"], "Official filters site")
        self.assertEqual(ctx_map["https://example.org/docs"], "Useful site at end of line")

    def test_is_pure_url_item(self) -> None:
        self.assertTrue(is_pure_url_item("https://github.com/org/repo"))
        self.assertTrue(is_pure_url_item("<https://example.com>"))
        self.assertFalse(is_pure_url_item(" Study https://example.com for project"))


class TestBugReproductionAndProcessors(unittest.TestCase):
    """Reproduce the single-line Todo + untracked Git commit edge cases."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = self.tmp.name
        os.makedirs(os.path.join(self.vault, "Daily"), exist_ok=True)
        self.config = make_test_config(self.vault)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @patch("worker.processors.query_ollama")
    def test_single_line_todo_when_llm_returns_empty_list(self, mock_ollama) -> None:
        """Bug #1: When a note has 1 Todo line and Ollama returns {'tasks': []},
        the deterministic fallback must still save the task to Tasks.md.
        """
        mock_ollama.return_value = {"tasks": []}

        count = process_todos("* Send invoice to accountant", "2026-11-02", self.config)
        self.assertEqual(count, 1)
        self.assertTrue(os.path.isfile(self.config.todo_file))

        with open(self.config.todo_file, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("- [ ] Send invoice to accountant #2026-11-02", content)

    @patch("worker.processors.query_ollama")
    def test_single_line_todo_when_llm_hallucinates_system_prompt(self, mock_ollama) -> None:
        """When model repeats system prompt phrases, it is filtered and triggers fallback."""
        mock_ollama.return_value = {
            "tasks": [
                "Estrai tutte le azioni atomiche dal testo.",
                "Restituisci frasi concise all'infinito in italiano.",
            ]
        }

        count = process_todos("Dentist at 16:00", "2026-11-02", self.config)
        self.assertEqual(count, 1)
        with open(self.config.todo_file, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("- [ ] Dentist at 16:00 #2026-11-02", content)

    @patch("worker.processors.query_ollama")
    def test_todo_when_llm_returns_dict_items_or_drops_items(self, mock_ollama) -> None:
        """When user writes 2 items but model only returns 1, nothing is lost."""
        mock_ollama.return_value = {"tasks": [{"task": "First task only"}]}

        count = process_todos("* First task\n* Second task", "2026-11-02", self.config)
        self.assertEqual(count, 2)
        with open(self.config.todo_file, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("- [ ] First task #2026-11-02", content)
        self.assertIn("- [ ] Second task #2026-11-02", content)

    def test_git_commit_with_untracked_deleted_daily_note(self) -> None:
        """Bug #2: When the Daily Note was not tracked by Git and gets removed by os.remove(),
        git_commit must NOT fail with exit status 128.
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

        daily_path = os.path.join(self.vault, "Daily", "2026-11-02.md")
        with open(daily_path, "w", encoding="utf-8") as f:
            f.write("### 📝 Todo\n* Single task\n")
        with open(self.config.todo_file, "w", encoding="utf-8") as f:
            f.write("- [ ] Single task #2026-11-02\n")

        os.remove(daily_path)

        committed = git_commit(
            self.vault,
            "Ingest 2026-11-02",
            paths_to_stage=[self.config.todo_file, daily_path],
        )
        self.assertTrue(committed)

        committed_empty = git_commit(
            self.vault,
            "Empty ingest",
            paths_to_stage=[daily_path],
        )
        self.assertFalse(committed_empty)

    def test_git_commit_with_tracked_deleted_daily_note(self) -> None:
        """When the Daily Note was already tracked by Git and removed, deletion is staged and committed."""
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

    @patch("sblumbo.check_ollama_available")
    @patch("sblumbo.process_todos", return_value=0)
    def test_safeguard_preserves_daily_note_if_processor_returns_zero(
        self, _mock_todos, _mock_check
    ) -> None:
        """Critical safeguard: if a non-empty section returns 0 extracted items,
        the Daily Note must NOT be deleted.
        """
        daily_path = os.path.join(self.vault, "Daily", "2026-11-02.md")
        with open(daily_path, "w", encoding="utf-8") as f:
            f.write("### 📝 Todo\n* Important line\n")

        env_file = os.path.join(self.vault, ".env.test")
        with open(env_file, "w", encoding="utf-8") as f:
            f.write(f"VAULT_PATH={self.vault}\nGIT_AUTO_COMMIT=false\n")

        with patch.object(
            sys,
            "argv",
            ["sblumbo.py", "2026-11-02", "--env-file", env_file],
        ):
            with patch.dict(os.environ, {"VAULT_PATH": self.vault, "GIT_AUTO_COMMIT": "false"}):
                exit_code = sblumbo.main()

        self.assertEqual(exit_code, 1)
        self.assertTrue(
            os.path.exists(daily_path),
            "Daily Note should not be deleted when 0 items were extracted!",
        )

    @patch("worker.processors.query_ollama")
    @patch("worker.processors.fetch_url_metadata")
    def test_ideas_notes_links_creation_and_collisions(
        self, mock_meta, mock_ollama
    ) -> None:
        """Verify file creation, anti-collision, YAML sanitization, and fallbacks."""
        mock_ollama.side_effect = [
            {"title": 'Idea: "Special" / Test?', "tags": ["#ai", "home automation"], "body": "Clean [[Concetto]] body"},
            {"title": 'Idea: "Special" / Test?', "tags": ["AI"], "body": ""},
            {"title": "Tech Note", "tags": ["linux"], "markdown_body": "Note details"},
             OllamaError("LLM offline for link"),
        ]
        mock_meta.return_value = {
            "url": "https://example.com",
            "title": "Example Title",
            "description": "Example Desc",
        }

        ideas = process_ideas("* First idea\n* Second idea", "2026-11-02", self.config)
        self.assertEqual(len(ideas), 2)
        self.assertNotEqual(ideas[0], ideas[1])
        self.assertTrue(ideas[1].endswith("_2.md"))

        with open(ideas[0], "r", encoding="utf-8") as f:
            idea1_text = f.read()
        self.assertIn('title: "Idea: \\"Special\\" / Test?"', idea1_text)
        self.assertIn("  - home-automation", idea1_text)
        self.assertNotIn("[[Concetto]]", idea1_text)

        # Second idea had empty body from LLM -> uses user text fallback
        with open(ideas[1], "r", encoding="utf-8") as f:
            idea2_text = f.read()
        self.assertIn("Second idea", idea2_text)

        notes = process_notes("* Important note", "2026-11-02", self.config)
        self.assertEqual(len(notes), 1)

        # Link works even if query_ollama raises OllamaError (uses metadata fallback)
        links = process_links("* https://example.com - My context", "2026-11-02", self.config)
        self.assertEqual(len(links), 1)
        with open(links[0], "r", encoding="utf-8") as f:
            link_text = f.read()
        self.assertIn("My context", link_text)


class TestUtilsAndClientEdgeCases(unittest.TestCase):
    """Tests for sanitization, path resolution, .env loading, and Ollama JSON parsing."""

    def test_slug_and_yaml_sanitization(self) -> None:
        self.assertEqual(clean_slug('???///:::"*"', fallback="Fallback"), "Fallback")
        self.assertEqual(sanitize_tag(" #Tag With Spaces/Symbols? "), "Tag-With-SpacesSymbols")
        self.assertEqual(sanitize_yaml_string('Title with "quotes"\nand newline'), 'Title with \\"quotes\\" and newline')
        tags_yaml = format_tags_yaml("type/note", ["Linux", "#linux", "  ", " Nix OS "])
        self.assertEqual(tags_yaml, "  - type/note\n  - Linux\n  - Nix-OS")

    def test_resolve_daily_note_protects_index_and_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daily_dir = os.path.join(tmp, "Daily")
            os.makedirs(daily_dir)
            index_file = os.path.join(daily_dir, "Daily.md")
            with open(index_file, "w", encoding="utf-8") as f:
                f.write("# Index")

            with self.assertRaises(VaultError):
                resolve_daily_note_path(index_file, daily_dir, tmp)

            with self.assertRaises(VaultError):
                resolve_daily_note_path(daily_dir, daily_dir, tmp)

    @patch("urllib.request.urlopen")
    def test_query_ollama_strips_markdown_fences(self, mock_urlopen) -> None:
        class FakeResp:
            status = 200
            def read(self) -> bytes:
                return b'{"message": {"content": "```json\\n{\\"tasks\\": [\\"Run test\\"]}\\n```"}}'
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        mock_urlopen.return_value = FakeResp()
        cfg = make_test_config("/tmp")
        res = query_ollama([{"role": "user", "content": "hi"}], cfg, json_schema={"type": "object"})
        self.assertEqual(res, {"tasks": ["Run test"]})

    @patch("urllib.request.urlopen")
    def test_query_ollama_strips_thinking_tags_and_sends_think_param(self, mock_urlopen) -> None:
        class FakeResp:
            status = 200
            def read(self) -> bytes:
                # Simulates a thinking model returning reasoning inside content
                return b'{"message": {"content": "<think>\\nAnalyzing task list...\\n</think>\\n```json\\n{\\"tasks\\": [\\"Task 1\\"]}\\n```"}}'
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        mock_urlopen.return_value = FakeResp()
        cfg = make_test_config("/tmp")
        res = query_ollama([{"role": "user", "content": "hi"}], cfg, json_schema={"type": "object"})
        self.assertEqual(res, {"tasks": ["Task 1"]})

        # Verify think: False was sent in the request payload
        req_arg = mock_urlopen.call_args[0][0]
        import json as json_mod
        sent_payload = json_mod.loads(req_arg.data.decode("utf-8"))
        self.assertIn("think", sent_payload)
        self.assertFalse(sent_payload["think"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
