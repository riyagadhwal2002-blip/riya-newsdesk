"""Model providers: Gemini (free tier), Anthropic Claude (paid), and a mock for offline tests."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

import requests

from .log import log


class LLMError(Exception):
    pass


class BudgetExceeded(LLMError):
    pass


def extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise LLMError("model did not return JSON")


class BaseLLM:
    def __init__(self, settings, db):
        self.cfg = settings.section("llm")
        self.settings = settings
        self.db = db
        self.sleep = time.sleep

    def _check_budget(self) -> None:
        key = "llm_calls_" + datetime.now(timezone.utc).strftime("%Y%m%d")
        used = int(self.db.get(key, "0") or 0)
        if used >= int(self.cfg.get("max_calls_per_day", 300)):
            raise BudgetExceeded(f"daily LLM budget used ({used} calls)")
        self.db.set(key, str(used + 1))

    def complete_json(self, system: str, user: str, temperature: float | None = None) -> dict:
        self._check_budget()
        temp = self.cfg.get("temperature", 0.8) if temperature is None else temperature
        delay = 4
        last: Exception | None = None
        for attempt in range(3):
            try:
                return extract_json(self._call(system, user, temp))
            except LLMError as exc:  # bad JSON → retry
                last = exc
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                if status not in (429, 500, 502, 503, 504):
                    raise LLMError(f"HTTP {status}: {exc.response.text[:300] if exc.response is not None else exc}")
                last = exc
            except requests.RequestException as exc:
                last = exc
            log.warning("LLM attempt %d failed: %s", attempt + 1, last)
            if attempt < 2:
                self.sleep(delay)
                delay *= 3
        raise LLMError(f"LLM failed after retries: {last}")

    def _call(self, system: str, user: str, temperature: float) -> str:  # pragma: no cover - interface
        raise NotImplementedError


class GeminiLLM(BaseLLM):
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def models(self) -> list[str]:
        """Primary model first, then backups (free models are often briefly 'overloaded')."""
        listed = self.cfg.get("gemini_models") or []
        first = self.cfg.get("gemini_model", "gemini-flash-latest")
        return list(dict.fromkeys([first, *listed]))

    def _call(self, system: str, user: str, temperature: float) -> str:
        key = self.settings.secret("GEMINI_API_KEY")
        if not key:
            raise LLMError("GEMINI_API_KEY is not set")
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": temperature, "responseMimeType": "application/json"},
        }
        last_err: Exception | None = None
        for model in self.models():
            r = requests.post(self.URL.format(model=model),
                              headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                              json=body, timeout=90)
            if r.status_code in (404, 429, 500, 503):
                log.warning("Gemini model %s unavailable (HTTP %s), trying next", model, r.status_code)
                last_err = requests.HTTPError(f"{r.status_code} for model {model}", response=r)
                continue
            r.raise_for_status()
            data = r.json()
            try:
                parts = data["candidates"][0]["content"]["parts"]
            except (KeyError, IndexError):
                raise LLMError(f"empty Gemini response: {str(data)[:300]}")
            return "".join(p.get("text", "") for p in parts)
        raise last_err or LLMError("no Gemini model configured")


class AnthropicLLM(BaseLLM):
    URL = "https://api.anthropic.com/v1/messages"

    def _call(self, system: str, user: str, temperature: float) -> str:
        key = self.settings.secret("ANTHROPIC_API_KEY")
        if not key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        r = requests.post(self.URL, headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                             "content-type": "application/json"},
                          json={"model": self.cfg.get("anthropic_model", "claude-sonnet-5-5"), "max_tokens": 2000,
                                "temperature": temperature, "system": system,
                                "messages": [{"role": "user", "content": user}]},
                          timeout=90)
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")


class MockLLM(BaseLLM):
    """Returns queued responses (tests) — or raises if nothing is queued."""

    def __init__(self, settings, db, responses: list | None = None):
        super().__init__(settings, db)
        self.responses = list(responses or [])
        self.calls: list[tuple[str, str]] = []

    def _call(self, system: str, user: str, temperature: float) -> str:
        self.calls.append((system, user))
        if not self.responses:
            raise LLMError("mock has no queued response")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt if isinstance(nxt, str) else json.dumps(nxt)


def make_llm(settings, db):
    provider = settings.section("llm").get("provider", "gemini").lower()
    return {"gemini": GeminiLLM, "anthropic": AnthropicLLM, "mock": MockLLM}[provider](settings, db)
