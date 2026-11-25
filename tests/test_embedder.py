from __future__ import annotations

import json

import httpx
import pytest
import respx

from code_indxr.embedder import EmbedderError, OllamaEmbedder


@respx.mock
async def test_embed_documents_batches_and_applies_nomic_prefix() -> None:
    captured_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured_payloads.append(payload)
        embeddings = [[float(i + 1), 0.5] for i in range(len(payload["input"]))]
        return httpx.Response(200, json={"model": payload["model"], "embeddings": embeddings})

    respx.post("http://127.0.0.1:11434/api/embed").mock(side_effect=handler)

    embedder = OllamaEmbedder(
        base_url="http://127.0.0.1:11434",
        model="nomic-embed-text",
        batch_size=2,
        concurrency=2,
    )
    try:
        vectors = await embedder.embed_documents(["doc one", "doc two", "doc three"])
    finally:
        await embedder.close()

    assert len(vectors) == 3
    assert len(captured_payloads) == 2
    all_inputs = [item for p in captured_payloads for item in p["input"]]
    assert all(text.startswith("search_document: ") for text in all_inputs)


@respx.mock
async def test_embed_query_applies_nomic_query_prefix() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["input"] == ["search_query: how does auth work?"]
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2, 0.3]]})

    respx.post("http://127.0.0.1:11434/api/embed").mock(side_effect=handler)

    embedder = OllamaEmbedder(base_url="http://127.0.0.1:11434", model="nomic-embed-text")
    try:
        vec = await embedder.embed_query("how does auth work?")
    finally:
        await embedder.close()

    assert vec == [0.1, 0.2, 0.3]


@respx.mock
async def test_embed_handles_non_nomic_model_without_prefix() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["input"] == ["plain query"]
        return httpx.Response(200, json={"embeddings": [[1.0, 0.0]]})

    respx.post("http://127.0.0.1:11434/api/embed").mock(side_effect=handler)

    embedder = OllamaEmbedder(base_url="http://127.0.0.1:11434", model="mxbai-embed-large")
    try:
        vec = await embedder.embed_query("plain query")
    finally:
        await embedder.close()

    assert vec == [1.0, 0.0]


@respx.mock
async def test_health_check_and_connection_error() -> None:
    respx.get("http://127.0.0.1:11434/api/tags").mock(
        return_value=httpx.Response(
            200,
            json={"models": [{"name": "nomic-embed-text:latest"}]},
        )
    )
    embedder = OllamaEmbedder(base_url="http://127.0.0.1:11434", model="nomic-embed-text")
    try:
        status = await embedder.health_check()
    finally:
        await embedder.close()

    assert status["ok"] is True
    assert status["model_available"] is True

    respx.post("http://127.0.0.1:11434/api/embed").mock(side_effect=httpx.ConnectError("refused"))
    embedder2 = OllamaEmbedder(base_url="http://127.0.0.1:11434", model="nomic-embed-text", max_retries=1)
    try:
        with pytest.raises(EmbedderError):
            await embedder2.embed_query("test")
    finally:
        await embedder2.close()
