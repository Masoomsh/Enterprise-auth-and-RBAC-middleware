import time

import pytest

from semantic_router.cache import CacheEntry, PineconeSemanticCache


class FakeIndex:
    def __init__(self, matches=None):
        self.matches = matches or []
        self.query_args = None
        self.upsert_args = None

    def query(self, **kwargs):
        self.query_args = kwargs
        return {"matches": self.matches}

    def upsert(self, **kwargs):
        self.upsert_args = kwargs


class FakeDocuments:
    def __init__(self):
        self.matches = []
        self.search_args = None
        self.upsert_args = None

    def search(self, **kwargs):
        self.search_args = kwargs
        return {"matches": self.matches}

    def upsert(self, **kwargs):
        self.upsert_args = kwargs


class FakeDocumentIndex:
    def __init__(self):
        self.documents = FakeDocuments()


@pytest.mark.asyncio
async def test_cache_uses_vector_similarity_threshold_scope_and_ttl():
    index = FakeIndex([{
        "score": 0.94,
        "metadata": {
            "content": "A semantically equivalent answer",
            "prompt_tokens": 12,
            "completion_tokens": 4,
        },
    }])
    cache = PineconeSemanticCache(index, "test-namespace", 0.9, 60, 40000)
    started_at = int(time.time())

    entry = await cache.lookup([0.1, 0.2], "scope-a")
    finished_at = int(time.time())

    assert entry == CacheEntry("A semantically equivalent answer", 12, 4)
    assert index.query_args["vector"] == [0.1, 0.2]
    assert index.query_args["namespace"] == "test-namespace"
    assert index.query_args["filter"]["scope_id"] == {"$eq": "scope-a"}
    assert started_at <= index.query_args["filter"]["expires_at"]["$gt"] <= finished_at


@pytest.mark.asyncio
async def test_cache_rejects_below_threshold_and_sets_expiration_on_write():
    index = FakeIndex([{"score": 0.4, "metadata": {"content": "not similar enough"}}])
    cache = PineconeSemanticCache(index, "test-namespace", 0.9, 60, 40000)
    assert await cache.lookup([0.1], "scope-a") is None

    await cache.store(
        [0.1], "scope-a", "simple", "small",
        CacheEntry("answer", 3, 5),
    )
    metadata = index.upsert_args["vectors"][0]["metadata"]
    assert metadata["expires_at"] >= int(time.time()) + 59
    assert metadata == {
        "scope_id": "scope-a",
        "category": "simple",
        "model": "small",
        "content": "answer",
        "prompt_tokens": 3,
        "completion_tokens": 5,
        "expires_at": metadata["expires_at"],
    }


@pytest.mark.asyncio
async def test_schema_index_uses_document_search_and_upsert():
    index = FakeDocumentIndex()
    index.documents.matches = [{
        "_id": "entry-1",
        "score": 0.99,
        "content": "document cache hit",
        "prompt_tokens": 2,
        "completion_tokens": 3,
    }]
    cache = PineconeSemanticCache(index, "schema-ns", 0.9, 60, 40000, "embedding")

    entry = await cache.lookup([0.1, 0.2], "scope-a")
    await cache.store(
        [0.1, 0.2], "scope-a", "simple", "small",
        CacheEntry("document cache hit", 2, 3),
    )

    assert entry == CacheEntry("document cache hit", 2, 3)
    assert index.documents.search_args["score_by"] == [{
        "type": "dense_vector",
        "field": "embedding",
        "values": [0.1, 0.2],
    }]
    assert index.documents.search_args["filter"]["scope_id"] == {"$eq": "scope-a"}
    document = index.documents.upsert_args["documents"][0]
    assert document["embedding"] == [0.1, 0.2]
    assert document == {
        "_id": document["_id"],
        "embedding": [0.1, 0.2],
        "scope_id": "scope-a",
        "category": "simple",
        "model": "small",
        "content": "document cache hit",
        "prompt_tokens": 2,
        "completion_tokens": 3,
        "expires_at": document["expires_at"],
    }
    assert document["expires_at"] >= int(time.time()) + 59
