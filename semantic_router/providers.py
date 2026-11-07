from __future__ import annotations

from typing import Any

import httpx


class OllamaClient:
    def __init__(self, base_url: str, timeout: float = 120.0) -> None:
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def embed(self, model: str, text: str) -> list[float]:
        response = await self._client.post("/api/embed", json={"model": model, "input": text})
        response.raise_for_status()
        payload = response.json()
        embeddings = payload.get("embeddings")
        if not embeddings or not embeddings[0]:
            raise ValueError("Ollama returned no embedding")
        return embeddings[0]

    async def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        options: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._client.post(
            "/api/chat",
            json={"model": model, "messages": messages, "stream": False, "options": options},
        )
        response.raise_for_status()
        payload = response.json()
        message = payload.get("message", {})
        if not isinstance(message.get("content"), str):
            raise ValueError("Ollama returned an invalid chat response")
        return payload

    async def close(self) -> None:
        await self._client.aclose()
