"""AWS Bedrock model provider.

Uses the Bedrock Runtime `converse` API for text (works uniformly across
Anthropic Claude and Amazon Nova) and `invoke_model` for embeddings (Titan v2
or Cohere Embed). Credentials come from the standard boto3 chain - in ECS that
is the task role, locally it is `aws configure` / a named profile. No API keys
live in the codebase.

Optional Bedrock Guardrails are attached to every text call: the buyer agent is
authorized to spend money, so an AWS-managed policy layer sits in front of its
reasoning in addition to the deterministic AP2 checks.
"""

from __future__ import annotations

import json
import time
from typing import Any

from app.core.config import Settings, get_settings
from app.core.exceptions import AIProviderError
from app.core.logging import get_logger
from app.services.ai_provider import AIProvider

logger = get_logger(__name__)

_RETRYABLE = {
    "ThrottlingException",
    "ServiceUnavailableException",
    "ModelTimeoutException",
    "InternalServerException",
    "ModelNotReadyException",
}


class BedrockAIProvider(AIProvider):
    """Text generation and embeddings on Amazon Bedrock."""

    name = "bedrock"

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        client: Any | None = None,
        max_retries: int = 3,
    ) -> None:
        self._settings = settings or get_settings()
        self.model_id = self._settings.bedrock_model_id
        self.embedding_model_id = self._settings.bedrock_embedding_model_id
        self._max_retries = max_retries
        self._client = client or self._build_client()

    def _build_client(self) -> Any:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover
            raise AIProviderError("boto3 is required for the Bedrock provider") from exc

        session_kwargs: dict[str, Any] = {"region_name": self._settings.aws_region}
        if self._settings.aws_profile:
            session_kwargs["profile_name"] = self._settings.aws_profile
        session = boto3.session.Session(**session_kwargs)
        return session.client(
            "bedrock-runtime",
            config=Config(
                retries={"max_attempts": 2, "mode": "standard"},
                read_timeout=60,
                connect_timeout=10,
            ),
        )

    # --- text ------------------------------------------------------------

    def complete_text(self, prompt: str, *, system: str = "", max_tokens: int | None = None) -> str:
        request: dict[str, Any] = {
            "modelId": self.model_id,
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {
                "maxTokens": max_tokens or self._settings.bedrock_max_tokens,
                "temperature": self._settings.bedrock_temperature,
            },
        }
        if system:
            request["system"] = [{"text": system}]
        if self._settings.bedrock_guardrail_id:
            request["guardrailConfig"] = {
                "guardrailIdentifier": self._settings.bedrock_guardrail_id,
                "guardrailVersion": self._settings.bedrock_guardrail_version or "DRAFT",
            }

        started = time.perf_counter()
        response = self._call(lambda: self._client.converse(**request), operation="converse")
        duration_ms = int((time.perf_counter() - started) * 1000)

        stop_reason = response.get("stopReason")
        usage = response.get("usage", {})
        logger.info(
            "Bedrock converse completed",
            extra={
                "model_id": self.model_id,
                "duration_ms": duration_ms,
                "stop_reason": stop_reason,
                "input_tokens": usage.get("inputTokens"),
                "output_tokens": usage.get("outputTokens"),
            },
        )
        if stop_reason == "guardrail_intervened":
            raise AIProviderError(
                "Bedrock guardrail blocked the model response",
                details={"stop_reason": stop_reason},
            )
        return self._extract_text(response)

    @staticmethod
    def _extract_text(response: dict[str, Any]) -> str:
        content = (response.get("output") or {}).get("message", {}).get("content", [])
        chunks = [block.get("text", "") for block in content if isinstance(block, dict)]
        text = "".join(chunks).strip()
        if not text:
            raise AIProviderError("Bedrock returned an empty response")
        return text

    # --- embeddings ------------------------------------------------------

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self.embedding_model_id
        if model.startswith("cohere."):
            return self._embed_cohere(texts, model)
        return [self._embed_titan(text, model) for text in texts]

    def _embed_titan(self, text: str, model: str) -> list[float]:
        body = {"inputText": text[:8000]}
        if "titan-embed-text-v2" in model:
            body["dimensions"] = 1024
            body["normalize"] = True
        payload = self._invoke_model(model, body)
        vector = payload.get("embedding")
        if not isinstance(vector, list):
            raise AIProviderError("Bedrock embedding response missing 'embedding'")
        return [float(v) for v in vector]

    def _embed_cohere(self, texts: list[str], model: str) -> list[list[float]]:
        body = {
            "texts": [t[:2000] for t in texts],
            "input_type": "search_document",
            "truncate": "END",
        }
        payload = self._invoke_model(model, body)
        vectors = payload.get("embeddings")
        if not isinstance(vectors, list):
            raise AIProviderError("Bedrock embedding response missing 'embeddings'")
        return [[float(v) for v in vector] for vector in vectors]

    def _invoke_model(self, model_id: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self._call(
            lambda: self._client.invoke_model(
                modelId=model_id,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(body),
            ),
            operation="invoke_model",
        )
        raw = response["body"]
        data = raw.read() if hasattr(raw, "read") else raw
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        return json.loads(data)

    # --- shared ----------------------------------------------------------

    def _call(self, fn: Any, *, operation: str) -> dict[str, Any]:
        """Invoke Bedrock with bounded retries on transient errors."""
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return fn()
            except Exception as exc:  # botocore ClientError and friends
                code = _error_code(exc)
                last_error = exc
                if code in _RETRYABLE and attempt < self._max_retries:
                    delay = 0.4 * (2 ** (attempt - 1))
                    logger.warning(
                        "Bedrock call retrying",
                        extra={
                            "operation": operation,
                            "attempt": attempt,
                            "error_type": code or type(exc).__name__,
                        },
                    )
                    time.sleep(delay)
                    continue
                break
        logger.error(
            "Bedrock call failed",
            extra={
                "operation": operation,
                "model_id": self.model_id,
                "error_type": _error_code(last_error) or type(last_error).__name__,
            },
        )
        raise AIProviderError(
            f"Bedrock {operation} failed: {_error_code(last_error) or type(last_error).__name__}",
            details={"operation": operation, "model_id": self.model_id},
        ) from last_error

    def health(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "model_id": self.model_id,
            "embedding_model_id": self.embedding_model_id,
            "region": self._settings.aws_region,
            "guardrail": bool(self._settings.bedrock_guardrail_id),
        }


def _error_code(exc: Exception | None) -> str | None:
    if exc is None:
        return None
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        if code:
            return str(code)
    return type(exc).__name__
