"""AI provider tests. Bedrock is mocked - no AWS calls and no spend."""

from __future__ import annotations

import json

import pytest

from app.core.exceptions import AIProviderError
from app.services.ai_provider import get_ai_provider, parse_json_object
from app.services.bedrock_service import BedrockAIProvider
from app.services.embedding_service import (
    EmbeddingService,
    InMemoryEmbeddingCache,
    cosine_similarity,
)


class FakeBedrockClient:
    """Stands in for `boto3.client('bedrock-runtime')`."""

    def __init__(self, text="{\"ok\": true}", embedding=None, fail_times=0, error_code="ThrottlingException"):
        self.text = text
        self.embedding = embedding or [0.1, 0.2, 0.3]
        self.fail_times = fail_times
        self.error_code = error_code
        self.converse_calls: list[dict] = []
        self.invoke_calls: list[dict] = []

    def converse(self, **kwargs):
        self.converse_calls.append(kwargs)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise _ClientError(self.error_code)
        return {
            "output": {"message": {"content": [{"text": self.text}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }

    def invoke_model(self, **kwargs):
        self.invoke_calls.append(kwargs)
        body = json.loads(kwargs["body"])
        if "texts" in body:
            payload = {"embeddings": [self.embedding for _ in body["texts"]]}
        else:
            payload = {"embedding": self.embedding}
        return {"body": json.dumps(payload).encode()}


class _ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


@pytest.fixture
def bedrock(settings):
    client = FakeBedrockClient()
    return BedrockAIProvider(settings=settings, client=client), client


class TestBedrockText:
    def test_converse_returns_the_model_text(self, bedrock):
        provider, _ = bedrock
        assert provider.complete_text("hello") == '{"ok": true}'

    def test_system_prompt_is_forwarded(self, bedrock):
        provider, client = bedrock
        provider.complete_text("hello", system="be brief")
        assert client.converse_calls[0]["system"] == [{"text": "be brief"}]

    def test_configured_model_id_is_used(self, settings, bedrock):
        provider, client = bedrock
        provider.complete_text("hello")
        assert client.converse_calls[0]["modelId"] == settings.bedrock_model_id

    def test_json_completion_is_parsed(self, bedrock):
        provider, _ = bedrock
        assert provider.complete_json("hello") == {"ok": True}

    def test_fenced_json_is_tolerated(self, settings):
        client = FakeBedrockClient(text='```json\n{"a": 1}\n```')
        provider = BedrockAIProvider(settings=settings, client=client)
        assert provider.complete_json("x") == {"a": 1}

    def test_empty_response_raises(self, settings):
        client = FakeBedrockClient(text="")
        provider = BedrockAIProvider(settings=settings, client=client)
        with pytest.raises(AIProviderError):
            provider.complete_text("x")

    def test_non_json_response_raises(self, settings):
        client = FakeBedrockClient(text="I am not JSON at all")
        provider = BedrockAIProvider(settings=settings, client=client)
        with pytest.raises(AIProviderError):
            provider.complete_json("x")

    def test_transient_errors_are_retried(self, settings):
        client = FakeBedrockClient(fail_times=2)
        provider = BedrockAIProvider(settings=settings, client=client, max_retries=3)
        assert provider.complete_text("x")
        assert len(client.converse_calls) == 3

    def test_persistent_errors_raise(self, settings):
        client = FakeBedrockClient(fail_times=10)
        provider = BedrockAIProvider(settings=settings, client=client, max_retries=2)
        with pytest.raises(AIProviderError):
            provider.complete_text("x")

    def test_non_retryable_errors_fail_fast(self, settings):
        client = FakeBedrockClient(fail_times=10, error_code="AccessDeniedException")
        provider = BedrockAIProvider(settings=settings, client=client, max_retries=3)
        with pytest.raises(AIProviderError):
            provider.complete_text("x")
        assert len(client.converse_calls) == 1

    def test_guardrail_config_is_attached_when_configured(self, settings):
        guarded = settings.model_copy(
            update={"bedrock_guardrail_id": "gr-123", "bedrock_guardrail_version": "1"}
        )
        client = FakeBedrockClient()
        BedrockAIProvider(settings=guarded, client=client).complete_text("x")
        assert client.converse_calls[0]["guardrailConfig"]["guardrailIdentifier"] == "gr-123"

    def test_guardrail_intervention_raises(self, settings):
        class Intervening(FakeBedrockClient):
            def converse(self, **kwargs):
                return {
                    "output": {"message": {"content": [{"text": "blocked"}]}},
                    "stopReason": "guardrail_intervened",
                }

        provider = BedrockAIProvider(settings=settings, client=Intervening())
        with pytest.raises(AIProviderError):
            provider.complete_text("x")


class TestBedrockEmbeddings:
    def test_titan_embeddings_are_returned(self, bedrock):
        provider, _ = bedrock
        assert provider.embed(["hello"]) == [[0.1, 0.2, 0.3]]

    def test_titan_is_called_once_per_text(self, bedrock):
        provider, client = bedrock
        provider.embed(["a", "b", "c"])
        assert len(client.invoke_calls) == 3

    def test_cohere_embeddings_use_a_single_batched_call(self, settings):
        cohere = settings.model_copy(
            update={"bedrock_embedding_model_id": "cohere.embed-english-v3"}
        )
        client = FakeBedrockClient()
        provider = BedrockAIProvider(settings=cohere, client=client)
        assert len(provider.embed(["a", "b"])) == 2
        assert len(client.invoke_calls) == 1

    def test_empty_input_needs_no_call(self, bedrock):
        provider, client = bedrock
        assert provider.embed([]) == []
        assert client.invoke_calls == []


class TestProviderSelection:
    def test_mock_provider_is_the_default(self, settings):
        assert get_ai_provider(settings).name == "mock"

    def test_bedrock_can_be_selected(self, settings, monkeypatch):
        monkeypatch.setattr(
            BedrockAIProvider, "_build_client", lambda self: FakeBedrockClient()
        )
        provider = get_ai_provider(settings.model_copy(update={"ai_provider": "bedrock"}))
        assert provider.name == "bedrock"

    def test_json_parser_rejects_arrays(self):
        with pytest.raises(AIProviderError):
            parse_json_object("[1, 2, 3]")

    def test_json_parser_extracts_embedded_objects(self):
        assert parse_json_object('Sure! {"a": 1} hope that helps') == {"a": 1}


class TestLocalProvider:
    def test_intent_extraction_finds_budget_and_category(self, provider):
        result = provider.complete_json(
            'TASK: intent_extraction\nINPUT_JSON: {"request": "wireless headphones under 2000"}'
        )
        assert result["budget_major"] == 2000
        assert result["category"] == "audio"

    def test_quantity_is_extracted(self, provider):
        result = provider.complete_json(
            'TASK: intent_extraction\nINPUT_JSON: {"request": "buy 3 units of cables"}'
        )
        assert result["quantity"] == 3

    def test_embeddings_are_deterministic(self, provider):
        assert provider.embed(["headphones"]) == provider.embed(["headphones"])

    def test_similar_text_scores_higher_than_unrelated(self, provider):
        query, close, far = provider.embed(
            ["wireless bluetooth headphones", "bluetooth headphones over-ear", "mechanical keyboard"]
        )
        assert cosine_similarity(query, close) > cosine_similarity(query, far)


class TestEmbeddingService:
    def test_repeat_embeddings_are_cached(self, provider):
        service = EmbeddingService(provider, InMemoryEmbeddingCache())
        calls: list[int] = []
        original = provider.embed

        def counting(texts):
            calls.append(len(texts))
            return original(texts)

        provider.embed = counting  # type: ignore[method-assign]
        service.embed_documents(["a", "b"])
        service.embed_documents(["a", "b"])
        assert calls == [2]

    def test_ranking_orders_by_similarity(self, provider):
        service = EmbeddingService(provider, InMemoryEmbeddingCache())
        ranked = service.rank(
            "bluetooth headphones",
            {
                "headphones": "wireless bluetooth headphones with noise cancellation",
                "keyboard": "mechanical keyboard with tactile switches",
            },
        )
        assert ranked[0][0] == "headphones"

    def test_mismatched_vector_count_raises(self, provider):
        service = EmbeddingService(provider, InMemoryEmbeddingCache())
        provider.embed = lambda texts: []  # type: ignore[method-assign]
        with pytest.raises(AIProviderError):
            service.embed_documents(["a"])
