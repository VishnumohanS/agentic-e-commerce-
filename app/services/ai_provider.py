"""AI provider abstraction.

    AIProvider (interface)
        |-- BedrockAIProvider   (AWS Bedrock via boto3)
        |-- LocalAIProvider     (deterministic, offline; dev + tests)
              |
        EmbeddingService / BuyerAgent

Nothing outside `app/services/bedrock_service.py` imports boto3 for AI, so the
model backend can be swapped without touching agent logic.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any

from app.core.config import Settings, get_settings
from app.core.exceptions import AIProviderError


class AIProvider(ABC):
    """Contract every model backend must satisfy."""

    name: str = "abstract"
    model_id: str = "unknown"
    embedding_model_id: str = "unknown"

    @abstractmethod
    def complete_text(self, prompt: str, *, system: str = "", max_tokens: int | None = None) -> str:
        """Return a plain-text completion."""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text."""

    def complete_json(
        self,
        prompt: str,
        *,
        system: str = "",
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Return a JSON object completion, tolerating fenced output."""
        instruction = (
            f"{system}\n\nRespond with a single valid JSON object and nothing else. "
            "Do not use markdown code fences."
        ).strip()
        raw = self.complete_text(prompt, system=instruction, max_tokens=max_tokens)
        return parse_json_object(raw)

    def health(self) -> dict[str, Any]:
        return {"provider": self.name, "model_id": self.model_id}


def parse_json_object(raw: str) -> dict[str, Any]:
    """Extract the first JSON object from a model response."""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise AIProviderError(
                "Model did not return parseable JSON",
                details={"snippet": text[:200]},
            )
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise AIProviderError(
                "Model did not return parseable JSON",
                details={"snippet": text[:200]},
            ) from exc
    if not isinstance(parsed, dict):
        raise AIProviderError("Model returned JSON that is not an object")
    return parsed


def get_ai_provider(settings: Settings | None = None) -> AIProvider:
    """Factory honouring `AI_PROVIDER` (`bedrock` or `mock`)."""
    settings = settings or get_settings()
    if settings.ai_provider == "bedrock":
        from app.services.bedrock_service import BedrockAIProvider

        return BedrockAIProvider(settings=settings)
    from app.services.local_provider import LocalAIProvider

    return LocalAIProvider(settings=settings)
