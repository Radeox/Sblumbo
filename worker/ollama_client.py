"""Client HTTP per la comunicazione con il server locale Ollama."""

import json
import urllib.error
import urllib.request
from typing import Any

from .config import Config


class OllamaError(RuntimeError):
    """Errore sollevato quando Ollama non è raggiungibile o restituisce dati non validi."""


def check_ollama_available(config: Config) -> None:
    """Verifica rapidamente che il demone Ollama sia in ascolto prima di iniziare l'ingest.

    Solleva OllamaError se il servizio non risponde.
    """
    # Ricava la base URL (es. http://127.0.0.1:11434)
    base_url = config.ollama_url.split("/api/")[0]
    tags_url = f"{base_url}/api/tags"
    req = urllib.request.Request(tags_url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status != 200:
                raise OllamaError(f"Ollama ha risposto con status {resp.status}")
            data = json.loads(resp.read().decode("utf-8"))
            models = [m.get("name", "") for m in data.get("models", [])]
            # Controllo soft sul modello (accetta anche match senza tag :latest)
            model_base = config.ollama_model.split(":")[0]
            if models and not any(
                m == config.ollama_model or m.startswith(f"{model_base}:") for m in models
            ):
                raise OllamaError(
                    f"Il modello '{config.ollama_model}' non risulta installato su Ollama. "
                    f"Modelli disponibili: {', '.join(models)}. "
                    f"Esegui: ollama pull {config.ollama_model}"
                )
    except urllib.error.URLError as exc:
        raise OllamaError(
            f"Impossibile connettersi a Ollama su {base_url}: {exc.reason}. "
            "Assicurati che il servizio Ollama sia attivo."
        ) from exc
    except json.JSONDecodeError as exc:
        raise OllamaError(f"Risposta non valida da Ollama ({tags_url}): {exc}") from exc


def query_ollama(
    messages: list[dict[str, str]],
    config: Config,
    json_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Invia una richiesta chat a Ollama e restituisce il JSON decodificato.

    Solleva OllamaError in caso di timeout, errore di rete o JSON malformato.
    """
    payload: dict[str, Any] = {
        "model": config.ollama_model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": config.ollama_temperature,
            "num_predict": config.ollama_num_predict,
        },
    }
    if json_schema:
        payload["format"] = json_schema

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        config.ollama_url,
        data=data,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=config.ollama_timeout) as resp:
            raw_body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="ignore")
        raise OllamaError(
            f"Errore HTTP {exc.code} da Ollama ({config.ollama_url}): {err_body}"
        ) from exc
    except urllib.error.URLError as exc:
        raise OllamaError(
            f"Errore di connessione a Ollama ({config.ollama_url}): {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise OllamaError(
            f"Timeout ({config.ollama_timeout}s) durante la chiamata a Ollama"
        ) from exc

    try:
        res = json.loads(raw_body)
        content = res["message"]["content"]
    except (KeyError, json.JSONDecodeError) as exc:
        raise OllamaError(f"Struttura risposta Ollama imprevista: {exc}") from exc

    if json_schema is not None:
        try:
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise OllamaError(f"Il JSON generato non è un oggetto: {content!r}")
            return parsed
        except json.JSONDecodeError as exc:
            raise OllamaError(
                f"Il modello non ha restituito un JSON valido: {content!r}"
            ) from exc

    return {"content": content}
