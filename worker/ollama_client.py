"""HTTP client for communicating with the local Ollama server."""

import json
import re
import urllib.error
import urllib.request
from typing import Any

from .config import Config


class OllamaError(RuntimeError):
    """Raised when Ollama is unreachable or returns invalid data."""


def check_ollama_available(config: Config) -> None:
    """Verify that the Ollama daemon is running and the model is installed.

    Raises OllamaError if the service does not respond or the model is missing.
    """
    base_url = config.ollama_url.split("/api/")[0]
    tags_url = f"{base_url}/api/tags"
    req = urllib.request.Request(tags_url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status != 200:
                raise OllamaError(f"Ollama responded with status {resp.status}")
            data = json.loads(resp.read().decode("utf-8"))
            models = [m.get("name", "") for m in data.get("models", [])]
            # Soft match on model name (allows tags or without :latest)
            model_base = config.ollama_model.split(":")[0]
            if models and not any(
                m == config.ollama_model or m.startswith(f"{model_base}:") for m in models
            ):
                raise OllamaError(
                    f"Model '{config.ollama_model}' is not installed in Ollama. "
                    f"Available models: {', '.join(models)}. "
                    f"Run: ollama pull {config.ollama_model}"
                )
    except urllib.error.URLError as exc:
        raise OllamaError(
            f"Cannot connect to Ollama at {base_url}: {exc.reason}. "
            "Please ensure the Ollama service is running."
        ) from exc
    except json.JSONDecodeError as exc:
        raise OllamaError(f"Invalid response from Ollama ({tags_url}): {exc}") from exc


def query_ollama(
    messages: list[dict[str, str]],
    config: Config,
    json_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send a chat request to Ollama and return decoded JSON.

    Raises OllamaError on timeout, network error, or malformed JSON.
    """
    payload: dict[str, Any] = {
        "model": config.ollama_model,
        "messages": messages,
        "stream": False,
        "think": config.ollama_think,
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
            f"HTTP error {exc.code} from Ollama ({config.ollama_url}): {err_body}"
        ) from exc
    except urllib.error.URLError as exc:
        raise OllamaError(
            f"Connection error to Ollama ({config.ollama_url}): {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise OllamaError(
            f"Timeout ({config.ollama_timeout}s) while calling Ollama"
        ) from exc

    try:
        res = json.loads(raw_body)
        content = res["message"]["content"]
    except (KeyError, json.JSONDecodeError) as exc:
        raise OllamaError(f"Unexpected Ollama response structure: {exc}") from exc

    if json_schema is not None:
        # Strip <think>...</think> reasoning traces if present in content
        cleaned_content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        if cleaned_content.startswith("```"):
            lines = cleaned_content.splitlines()
            if len(lines) >= 2 and lines[-1].strip() == "```":
                cleaned_content = "\n".join(lines[1:-1]).strip()
        try:
            parsed = json.loads(cleaned_content)
        except json.JSONDecodeError:
            # Fallback: extract substring between first '{' and last '}'
            start = cleaned_content.find("{")
            end = cleaned_content.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    parsed = json.loads(cleaned_content[start : end + 1])
                except json.JSONDecodeError as exc:
                    raise OllamaError(
                        f"Model did not return valid JSON: {content!r}"
                    ) from exc
            else:
                raise OllamaError(
                    f"Model did not return valid JSON: {content!r}"
                )

        if not isinstance(parsed, dict):
            raise OllamaError(f"Generated JSON is not an object: {content!r}")
        return parsed

    return {"content": content}
