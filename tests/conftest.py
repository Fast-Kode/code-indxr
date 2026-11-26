from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from code_indxr.config import Settings
from code_indxr.embedder import EmbedderError


class FakeEmbedder:
    """Deterministic local embedder for fast unit and integration tests."""

    def __init__(self, dim: int = 16, fail_on_call: bool = False) -> None:
        self.dim = dim
        self.fail_on_call = fail_on_call
        self.document_calls: list[list[str]] = []
        self.query_calls: list[str] = []

    def _vectorize(self, text: str) -> list[float]:
        tokens = [t.strip(".,:;()[]{}<>\"'`") for t in text.lower().split()]
        vec = [0.0] * self.dim
        for token in tokens:
            if not token:
                continue
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest()
            idx = int.from_bytes(digest, "little") % self.dim
            vec[idx] += 1.0
        norm = sum(v * v for v in vec) ** 0.5
        if norm == 0.0:
            vec[0] = 1.0
            return vec
        return [v / norm for v in vec]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self.fail_on_call:
            raise EmbedderError("Simulated embedder failure")
        self.document_calls.append(list(texts))
        return [self._vectorize(t) for t in texts]

    async def embed_query(self, query: str) -> list[float]:
        if self.fail_on_call:
            raise EmbedderError("Simulated embedder failure")
        self.query_calls.append(query)
        return self._vectorize(query)

    async def health_check(self) -> dict[str, Any]:
        return {
            "ok": not self.fail_on_call,
            "ollama_url": "http://127.0.0.1:11434",
            "model": "nomic-embed-text",
            "model_available": not self.fail_on_call,
        }

    async def close(self) -> None:
        return None


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder(dim=32)


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "code-indxr-data",
        ollama_url="http://127.0.0.1:11434",
        embed_model="nomic-embed-text",
        embed_batch_size=8,
        embed_concurrency=2,
    )


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """Create a multi-language mini repository (TS/React, Python, Kotlin, Java, Dart)."""
    repo = tmp_path / "sample-multi-repo"
    (repo / "src" / "components").mkdir(parents=True)
    (repo / "backend").mkdir(parents=True)
    (repo / "android" / "app").mkdir(parents=True)
    (repo / "lib").mkdir(parents=True)
    (repo / "node_modules" / "pkg").mkdir(parents=True)

    (repo / ".gitignore").write_text("ignored_dir/\n*.log\n", encoding="utf-8")
    (repo / "ignored_dir").mkdir()
    (repo / "ignored_dir" / "secret.py").write_text("SECRET = 123\n", encoding="utf-8")
    (repo / "debug.log").write_text("noisy log output\n", encoding="utf-8")
    (repo / "node_modules" / "pkg" / "index.js").write_text("export const x = 1;\n", encoding="utf-8")
    (repo / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")

    (repo / "src" / "components" / "AuthCard.tsx").write_text(
        """import React from "react";

export interface AuthCardProps {
  username: string;
  onLogout: () => void;
}

export function AuthCard({ username, onLogout }: AuthCardProps) {
  return (
    <div className="auth-card">
      <h2>Welcome {username}</h2>
      <button onClick={onLogout}>Sign out</button>
    </div>
  );
}
""",
        encoding="utf-8",
    )

    (repo / "backend" / "token_service.py").write_text(
        '''"""JWT refresh token management."""

class TokenService:
    """Handles access and refresh tokens."""

    def refresh_access_token(self, refresh_token: str) -> str:
        if not refresh_token:
            raise ValueError("Missing refresh token")
        return f"access:{refresh_token}"


def verify_signature(payload: bytes, signature: str) -> bool:
    return bool(payload and signature)
''',
        encoding="utf-8",
    )

    (repo / "android" / "app" / "SessionManager.kt").write_text(
        """package com.example.app

class SessionManager(private val storageKey: String) {
    fun clearSession(): Boolean {
        return storageKey.isNotEmpty()
    }
}

fun buildAuthHeader(token: String): String = "Bearer $token"
""",
        encoding="utf-8",
    )

    (repo / "android" / "app" / "CryptoHelper.java").write_text(
        """package com.example.app;

public class CryptoHelper {
    public static String hashPassword(String rawPassword) {
        return Integer.toHexString(rawPassword.hashCode());
    }
}
""",
        encoding="utf-8",
    )

    (repo / "lib" / "profile_widget.dart").write_text(
        """import 'package:flutter/widgets.dart';

class ProfileWidget extends StatelessWidget {
  final String displayName;

  const ProfileWidget({super.key, required this.displayName});

  @override
  Widget build(BuildContext context) {
    return Text(displayName);
  }
}

String formatDisplayName(String first, String last) {
  return '$first $last'.trim();
}
""",
        encoding="utf-8",
    )

    return repo
