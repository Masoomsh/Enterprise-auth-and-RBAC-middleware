from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CacheEntry:
    content: str
    prompt_tokens: int
    completion_tokens: int


class PineconeSemanticCache:
    def __init__(
        self,
        index: Any,
        namespace: str,
        similarity_threshold: float,
        ttl_seconds: int,
        max_response_chars: int,
        vector_field: str | None = None,
    ) -> None:
        self._index = index
        self._namespace = namespace
        self._threshold = similarity_threshold
        self._ttl_seconds = ttl_seconds
        self._max_response_chars = max_response_chars
        self._vector_field = vector_field

    async def lookup(
        self, vector: list[float], scope_id: str
    ) -> CacheEntry | None:
        now = int(time.time())
        filters = {
            "scope_id": {"$eq": scope_id},
            "expires_at": {"$gt": now},
        }
        if self._vector_field:
            response = await asyncio.to_thread(
                self._index.documents.search,
                namespace=self._namespace,
                top_k=1,
                score_by=[{
                    "type": "dense_vector",
                    "field": self._vector_field,
                    "values": vector,
                }],
                include_fields=["content", "prompt_tokens", "completion_tokens"],
                filter=filters,
            )
        else:
            response = await asyncio.to_thread(
                self._index.query,
                vector=vector,
                top_k=1,
                include_metadata=True,
                namespace=self._namespace,
                filter=filters,
            )
        matches = _field(response, "matches", []) or []
        if not matches:
            return None
        match = matches[0]
        score = _field(match, "score", 0.0)
        metadata = _field(match, "metadata", None)
        if metadata is None:
            metadata = match.to_dict() if hasattr(match, "to_dict") else match
        metadata = metadata or {}
        if score < self._threshold or not isinstance(metadata.get("content"), str):
            return None
        return CacheEntry(
            content=metadata["content"],
            prompt_tokens=int(metadata.get("prompt_tokens", 0)),
            completion_tokens=int(metadata.get("completion_tokens", 0)),
        )

    async def store(
        self,
        vector: list[float],
        scope_id: str,
        category: str,
        model: str,
        entry: CacheEntry,
    ) -> None:
        if len(entry.content) > self._max_response_chars:
            return
        metadata = {
            "scope_id": scope_id,
            "category": category,
            "model": model,
            "content": entry.content,
            "prompt_tokens": entry.prompt_tokens,
            "completion_tokens": entry.completion_tokens,
            "expires_at": int(time.time()) + self._ttl_seconds,
        }
        if self._vector_field:
            await asyncio.to_thread(
                self._index.documents.upsert,
                documents=[{
                    "_id": str(uuid.uuid4()),
                    self._vector_field: vector,
                    **metadata,
                }],
                namespace=self._namespace,
            )
            return
        await asyncio.to_thread(
            self._index.upsert,
            vectors=[{
                "id": str(uuid.uuid4()),
                "values": vector,
                "metadata": metadata,
            }],
            namespace=self._namespace,
        )


def _field(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    if hasattr(value, key):
        return getattr(value, key)
    if hasattr(value, "get") and callable(value.get):
        return value.get(key, default)
    return default
