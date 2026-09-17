"""Deterministic offline AI provider.

Selected with `AI_PROVIDER=mock`. It lets the whole platform run - and the full
test suite pass - with no AWS account, no credentials and no spend, while
exposing exactly the same `AIProvider` interface as Bedrock.

It is *not* a language model: it is a rule-based stand-in that understands the
three structured tasks the buyer agent issues, plus a hashed bag-of-n-grams
embedding for catalog search. Behaviour is fully reproducible, which is what
makes it useful in tests.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from app.core.config import Settings, get_settings
from app.services.ai_provider import AIProvider, parse_json_object

EMBEDDING_DIM = 256

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

_CATEGORY_HINTS = {
    "audio": ("headphone", "headphones", "earbud", "earbuds", "speaker", "audio", "headset"),
    "wearables": ("watch", "smartwatch", "band", "tracker", "wearable"),
    "accessories": ("case", "cable", "charger", "stand", "adapter", "mount", "pouch"),
    "computing": ("laptop", "keyboard", "mouse", "monitor", "hub", "dock", "ssd"),
    "home": ("lamp", "kettle", "purifier", "vacuum", "home"),
    "fitness": ("dumbbell", "yoga", "mat", "fitness", "gym", "resistance"),
}

_STOPWORDS = {
    "a", "an", "the", "for", "with", "and", "or", "of", "to", "in", "on", "under",
    "below", "less", "than", "buy", "get", "me", "please", "want", "need", "some",
    "my", "i", "is", "it", "that", "this", "at", "best", "good", "nice",
}


def tokenize(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
    return [t for t in tokens if t not in _STOPWORDS and len(t) > 1]


class LocalAIProvider(AIProvider):
    """Rule-based provider with deterministic embeddings."""

    name = "mock"

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self.model_id = "local-deterministic-v1"
        self.embedding_model_id = "local-hash-embedding-256"

    # --- text ------------------------------------------------------------

    def complete_text(self, prompt: str, *, system: str = "", max_tokens: int | None = None) -> str:
        task, payload = _parse_task(prompt)
        handler = {
            "intent_extraction": self._intent_extraction,
            "product_ranking": self._product_ranking,
            "upsell_advisory": self._upsell_advisory,
        }.get(task, self._fallback)
        return json.dumps(handler(payload), ensure_ascii=False)

    def complete_json(
        self, prompt: str, *, system: str = "", max_tokens: int | None = None
    ) -> dict[str, Any]:
        return parse_json_object(self.complete_text(prompt, system=system, max_tokens=max_tokens))

    # --- task handlers ---------------------------------------------------

    def _intent_extraction(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = str(payload.get("request", ""))
        lowered = request.lower()
        return {
            "query": " ".join(tokenize(request)) or request.strip(),
            "category": _guess_category(lowered),
            "budget_major": _extract_budget(lowered),
            "quantity": _extract_quantity(lowered),
            "must_have": _extract_keywords(lowered),
            "summary": f"Find {_guess_category(lowered) or 'a product'} matching: {request.strip()[:120]}",
        }

    def _product_ranking(self, payload: dict[str, Any]) -> dict[str, Any]:
        query_tokens = set(tokenize(str(payload.get("query", ""))))
        candidates = payload.get("candidates") or []
        scored: list[tuple[float, int, str]] = []
        for candidate in candidates:
            text = " ".join(
                str(candidate.get(field, ""))
                for field in ("name", "category", "brand", "description", "tags")
            )
            overlap = len(query_tokens & set(tokenize(text)))
            price = int(candidate.get("price", 0) or 0)
            scored.append((-overlap, price, str(candidate.get("product_id", ""))))
        scored.sort()
        ranked = [product_id for _, _, product_id in scored if product_id]
        return {
            "ranked_product_ids": ranked,
            "rationale": "Ranked by keyword overlap with the request, then by lowest price.",
        }

    def _upsell_advisory(self, payload: dict[str, Any]) -> dict[str, Any]:
        offer = payload.get("offer") or {}
        base = payload.get("base_product") or {}
        related = bool(
            set(tokenize(str(offer.get("category", "")) + " " + str(offer.get("name", ""))))
            & set(tokenize(str(base.get("category", "")) + " " + str(base.get("name", ""))))
        ) or str(base.get("product_id")) in (offer.get("upsell_for") or [])
        return {
            "desirable": bool(related),
            "rationale": (
                "Add-on is complementary to the selected product."
                if related
                else "Add-on is unrelated to the selected product."
            ),
        }

    def _fallback(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"result": "unsupported_task", "echo_keys": sorted(payload.keys())}

    # --- embeddings ------------------------------------------------------

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    @staticmethod
    def _embed_one(text: str) -> list[float]:
        vector = [0.0] * EMBEDDING_DIM
        tokens = tokenize(text)
        grams: list[str] = list(tokens)
        for token in tokens:
            padded = f" {token} "
            grams.extend(padded[i : i + 3] for i in range(len(padded) - 2))
        for gram in grams:
            digest = hashlib.sha256(gram.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % EMBEDDING_DIM
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            weight = 2.0 if len(gram) > 3 else 1.0
            vector[index] += sign * weight
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0:
            return vector
        return [v / norm for v in vector]

    def health(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "model_id": self.model_id,
            "embedding_model_id": self.embedding_model_id,
            "note": "Deterministic offline provider - set AI_PROVIDER=bedrock to use AWS.",
        }


# --- prompt helpers -------------------------------------------------------


def _parse_task(prompt: str) -> tuple[str, dict[str, Any]]:
    task_match = re.search(r"^TASK:\s*(\w+)", prompt or "", flags=re.MULTILINE)
    task = task_match.group(1) if task_match else ""
    payload: dict[str, Any] = {}
    input_match = re.search(r"^INPUT_JSON:\s*(\{.*)", prompt or "", flags=re.MULTILINE | re.DOTALL)
    if input_match:
        try:
            payload = json.loads(input_match.group(1))
        except json.JSONDecodeError:
            payload = {}
    return task, payload


def _extract_budget(text: str) -> float | None:
    patterns = [
        r"(?:under|below|less than|max|maximum|budget(?: of)?|within|upto|up to)\s*(?:rs\.?|inr|\u20b9)?\s*([\d,]+(?:\.\d+)?)\s*(k)?",
        r"(?:rs\.?|inr|\u20b9)\s*([\d,]+(?:\.\d+)?)\s*(k)?",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = float(match.group(1).replace(",", ""))
            if match.group(2) == "k":
                value *= 1000
            return value
    return None


def _extract_quantity(text: str) -> int:
    match = re.search(r"\b(\d+)\s*(?:x|units?|pieces?|pcs)\b", text)
    if match:
        return max(1, min(int(match.group(1)), 10))
    for word, value in _NUMBER_WORDS.items():
        if re.search(rf"\b{word}\b\s+\w", text):
            return value
    return 1


def _guess_category(text: str) -> str:
    for category, hints in _CATEGORY_HINTS.items():
        if any(hint in text for hint in hints):
            return category
    return ""


def _extract_keywords(text: str) -> list[str]:
    tokens = tokenize(text)
    seen: list[str] = []
    for token in tokens:
        if token.isdigit():
            continue
        if token not in seen:
            seen.append(token)
    return seen[:6]
