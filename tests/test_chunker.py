from __future__ import annotations

from code_indxr.chunker import chunk_file


def test_chunk_typescript_react_extracts_symbols() -> None:
    source = """import React from "react";

export interface UserProps {
  id: string;
  name: string;
}

export const formatUser = (name: string): string => {
  return name.trim().toUpperCase();
};

export function UserBadge({ id, name }: UserProps) {
  return <span data-id={id}>{formatUser(name)}</span>;
}
"""
    chunks = chunk_file("repo1", "src/UserBadge.tsx", source, "tsx")
    assert len(chunks) >= 2
    symbols = {c.symbol_name for c in chunks}
    assert "UserBadge" in symbols
    assert any("UserProps" in s or "formatUser" in s for s in symbols)
    assert all(c.start_line >= 1 and c.end_line >= c.start_line for c in chunks)
    assert "src/UserBadge.tsx" in chunks[0].embed_text()


def test_chunk_python_extracts_classes_and_functions() -> None:
    source = """import os

class OrderProcessor:
    def validate(self, order_id: str) -> bool:
        return bool(order_id)

    def submit(self, order_id: str) -> dict:
        return {"id": order_id, "status": "ok"}

def compute_tax(amount: float, rate: float = 0.1) -> float:
    return round(amount * rate, 2)
"""
    chunks = chunk_file("repo1", "app/orders.py", source, "python")
    symbols = {c.symbol_name for c in chunks}
    assert "OrderProcessor" in symbols or "OrderProcessor.validate" in symbols
    assert "compute_tax" in symbols


def test_chunk_kotlin_java_and_dart() -> None:
    kt_src = """package demo

class BillingClient {
    fun charge(amountCents: Int): Boolean {
        return amountCents > 0
    }
}

fun formatCurrency(cents: Int): String = "$${cents / 100}"
"""
    kt_chunks = chunk_file("repo1", "BillingClient.kt", kt_src, "kotlin")
    assert len(kt_chunks) >= 2
    assert any("BillingClient" in c.symbol_name for c in kt_chunks)
    assert any("formatCurrency" in c.symbol_name for c in kt_chunks)

    java_src = """package demo;

public class PaymentGateway {
    public boolean authorize(String cardToken) {
        return cardToken != null && !cardToken.isEmpty();
    }
}
"""
    java_chunks = chunk_file("repo1", "PaymentGateway.java", java_src, "java")
    assert len(java_chunks) >= 1
    assert any("PaymentGateway" in c.symbol_name for c in java_chunks)

    dart_src = """class CartController {
  final List<String> items = [];

  void addItem(String sku) {
    items.add(sku);
  }
}

double calculateSubtotal(List<double> prices) {
  return prices.fold(0.0, (sum, value) => sum + value);
}
"""
    dart_chunks = chunk_file("repo1", "lib/cart.dart", dart_src, "dart")
    assert len(dart_chunks) >= 2
    assert any("CartController" in c.symbol_name for c in dart_chunks)
    assert any("calculateSubtotal" in c.symbol_name for c in dart_chunks)


def test_chunk_oversized_class_splits_into_methods_and_respects_limit() -> None:
    methods = "\n\n".join(
        f"    def method_{i}(self, x: int) -> int:\n        # filler {'a' * 120}\n        return x + {i}"
        for i in range(15)
    )
    source = f"class HugeService:\n{methods}\n"

    chunks = chunk_file("repo1", "huge.py", source, "python", max_chunk_chars=600)
    assert len(chunks) >= 4
    assert all(len(c.content) <= 750 for c in chunks)


def test_chunk_fallback_for_markdown_and_empty_file() -> None:
    assert chunk_file("repo1", "empty.md", "   \n\n", "markdown") == []

    md_source = "\n".join(f"Line {i}: architecture notes and deployment steps." for i in range(120))
    chunks = chunk_file("repo1", "docs/arch.md", md_source, "markdown", max_chunk_chars=500, overlap_lines=3)
    assert len(chunks) >= 4
    assert chunks[0].start_line == 1
    assert chunks[-1].end_line == 120
