import asyncio
import os
import uuid

import pytest
from pinecone import Pinecone

from semantic_router.api import _configure_system_trust, _dense_vector_field
from semantic_router.cache import CacheEntry, PineconeSemanticCache
from semantic_router.config import Settings
from semantic_router.providers import OllamaClient


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("TEST_PINECONE_LIVE", "").lower() not in {"1", "true", "yes"},
    reason="Set TEST_PINECONE_LIVE=1 to run the live Pinecone/Ollama integration test",
)
async def test_live_pinecone_semantic_cache_round_trip():
    settings = Settings.from_env()
    settings.validate()
    if not settings.pinecone_api_key:
        raise RuntimeError("PINECONE_API_KEY is required for the live Pinecone test")

    _configure_system_trust()
    pinecone = Pinecone(api_key=settings.pinecone_api_key)
    listed = pinecone.list_indexes()
    indexes = listed.get("indexes", []) if isinstance(listed, dict) else listed.indexes
    available_names = [
        item.get("name") if isinstance(item, dict) else item.name
        for item in indexes
    ]
    if settings.pinecone_index not in available_names:
        pytest.fail(
            f"Pinecone index '{settings.pinecone_index}' is not visible to the configured "
            f"API key. Visible index names: {available_names or 'none'}. Check that "
            "PINECONE_API_KEY belongs to the project containing this index and that "
            "PINECONE_INDEX exactly matches its name."
        )

    index = pinecone.Index(settings.pinecone_index)
    description = pinecone.describe_index(settings.pinecone_index)
    vector_field = _dense_vector_field(description)
    if not vector_field:
        pytest.fail(
            "The live Pinecone test expects the configured index to use a schema-based "
            "dense-vector field."
        )
    ollama = OllamaClient(settings.ollama_base_url)

    namespace = f"pytest-{uuid.uuid4().hex}"
    scope_id = "semantic-cache-live-round-trip"
    cache = PineconeSemanticCache(
        index=index,
        namespace=namespace,
        similarity_threshold=max(settings.similarity_threshold, 0.99),
        ttl_seconds=300,
        max_response_chars=settings.cache_max_response_chars,
        vector_field=vector_field,
    )

    try:
        vector = await ollama.embed(
            settings.embedding_model,
            "Live integration test for semantic cache vector similarity.",
        )
        stats = index.describe_index_stats()
        dimension = stats.get("dimension") if isinstance(stats, dict) else stats.dimension
        assert dimension == len(vector), (
            f"Pinecone index dimension {dimension} does not match embedding dimension {len(vector)}"
        )

        expected = CacheEntry(
            content="Temporary Pinecone integration-test response",
            prompt_tokens=7,
            completion_tokens=5,
        )
        await cache.store(vector, scope_id, "simple", settings.simple_model, expected)

        actual = None
        for _ in range(30):
            actual = await cache.lookup(vector, scope_id)
            if actual is not None:
                break
            await asyncio.sleep(1)

        assert actual == expected, "Pinecone did not return the stored semantic cache entry"
    finally:
        try:
            index.documents.delete(delete_all=True, namespace=namespace)
        finally:
            await ollama.close()