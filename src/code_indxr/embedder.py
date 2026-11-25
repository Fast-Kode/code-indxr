from __future__ import annotations

import asyncio
from typing import Any

import httpx


class EmbedderError(RuntimeError):
    """Raised when Ollama embedding requests fail or the model is unavailable."""


class OllamaEmbedder:
    """High-throughput async Ollama /api/embed client with batching and retries."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "nomic-embed-text",
        batch_size: int = 32,
        concurrency: int = 2,
        timeout: float = 120.0,
        max_chars: int = 4000,
        max_retries: int = 3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.batch_size = max(1, batch_size)
        self.concurrency = max(1, concurrency)
        self.max_chars = max(256, max_chars)
        self.max_retries = max(1, max_retries)
        self._uses_nomic_prefix = "nomic-embed" in model.lower()
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=10.0),
        )

    def _prepare_text(self, text: str, prefix: str) -> str:
        clipped = text[: self.max_chars]
        if self._uses_nomic_prefix:
            return f"{prefix}{clipped}"
        return clipped

    async def _embed_batch(self, inputs: list[str]) -> list[list[float]]:
        payload = {"model": self.model, "input": inputs, "truncate": True}
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = await self._client.post("/api/embed", json=payload)
                if response.status_code == 404:
                    raise EmbedderError(
                        f"Ollama model '{self.model}' not found at {self.base_url}. "
                        f"Run: ollama pull {self.model}"
                    )
                response.raise_for_status()
                data = response.json()
                embeddings = data.get("embeddings")
                if not isinstance(embeddings, list) or len(embeddings) != len(inputs):
                    raise EmbedderError(
                        f"Unexpected Ollama /api/embed response: expected {len(inputs)} vectors, got {type(embeddings).__name__}"
                    )
                return [[float(v) for v in vec] for vec in embeddings]
            except EmbedderError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt < self.max_retries:
                    await asyncio.sleep(0.25 * (2 ** (attempt - 1)))

        raise EmbedderError(
            f"Failed to contact Ollama at {self.base_url} for model '{self.model}': {last_error}"
        ) from last_error

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed document chunks in parallel batches while preserving exact input order."""
        if not texts:
            return []

        prepared = [self._prepare_text(t, "search_document: ") for t in texts]
        batches = [prepared[i : i + self.batch_size] for i in range(0, len(prepared), self.batch_size)]
        semaphore = asyncio.Semaphore(self.concurrency)

        async def run_batch(batch: list[str]) -> list[list[float]]:
            async with semaphore:
                return await self._embed_batch(batch)

        results = await asyncio.gather(*(run_batch(batch) for batch in batches))
        return [vec for batch_vecs in results for vec in batch_vecs]

    async def embed_query(self, query: str) -> list[float]:
        """Embed a single search query."""
        prepared = self._prepare_text(query.strip(), "search_query: ")
        vectors = await self._embed_batch([prepared])
        return vectors[0]

    async def health_check(self) -> dict[str, Any]:
        """Verify Ollama server reachability and check if the configured embedding model is pulled."""
        try:
            response = await self._client.get("/api/tags")
            response.raise_for_status()
            models = [m.get("name", "") for m in response.json().get("models", [])]
            target_base = self.model.split(":")[0]
            available = any(name == self.model or name.split(":")[0] == target_base for name in models)
            return {
                "ok": available,
                "ollama_url": self.base_url,
                "model": self.model,
                "model_available": available,
                "installed_models": models,
            }
        except Exception as exc:
            return {
                "ok": False,
                "ollama_url": self.base_url,
                "model": self.model,
                "model_available": False,
                "error": str(exc),
            }

    async def close(self) -> None:
        await self._client.aclose()
