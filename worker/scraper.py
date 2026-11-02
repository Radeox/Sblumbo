"""Scraper leggero per estrarre titolo e descrizione dagli URL."""

import html
import urllib.error
import urllib.request
from html.parser import HTMLParser


class MetaParser(HTMLParser):
    """Parser HTML per estrarre <title> e <meta name/property="description">."""

    def __init__(self) -> None:
        super().__init__()
        self.title: str = ""
        self.description: str = ""
        self._in_title: bool = False
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {k.lower(): (v or "") for k, v in attrs if k}
        tag_lower = tag.lower()

        if tag_lower == "title" and not self.title:
            self._in_title = True
            self._title_parts = []
        elif tag_lower == "meta":
            name = attrs_dict.get("name", "").lower()
            prop = attrs_dict.get("property", "").lower()
            if name in ("description", "og:description", "twitter:description") or prop in (
                "og:description",
                "description",
                "twitter:description",
            ):
                if not self.description:
                    content = attrs_dict.get("content", "").strip()
                    if content:
                        self.description = html.unescape(content)
            elif prop == "og:title" and not self.title:
                content = attrs_dict.get("content", "").strip()
                if content:
                    self.title = html.unescape(content)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title" and self._in_title:
            self._in_title = False
            joined = "".join(self._title_parts).strip()
            if joined:
                self.title = html.unescape(joined)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)


def fetch_url_metadata(raw_url: str, timeout: int = 6) -> dict[str, str]:
    """Scarica i primi 64KB della pagina web per estrarre titolo e descrizione.

    Non solleva eccezioni in caso di sito irraggiungibile, ma restituisce un fallback.
    """
    url = raw_url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) "
                    "Gecko/20100101 Firefox/130.0"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            chunk = response.read(65536).decode("utf-8", errors="ignore")
            parser = MetaParser()
            parser.feed(chunk)
            return {
                "url": url,
                "title": parser.title or url,
                "description": (
                    parser.description or "Nessuna descrizione presente nei metadati."
                ),
            }
    except Exception as exc:
        return {
            "url": url,
            "title": url,
            "description": f"Impossibile risolvere l'URL ({exc})",
        }
