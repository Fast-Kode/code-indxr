from __future__ import annotations

import functools
import hashlib
from dataclasses import dataclass
from typing import Any

from tree_sitter_language_pack import get_parser

from code_indxr.models import CodeChunk

LANGUAGE_TO_TS_GRAMMAR: dict[str, str] = {
    "python": "python",
    "typescript": "typescript",
    "tsx": "tsx",
    "javascript": "javascript",
    "kotlin": "kotlin",
    "java": "java",
    "dart": "dart",
    "go": "go",
    "rust": "rust",
    "csharp": "csharp",
    "cpp": "cpp",
    "c": "c",
    "ruby": "ruby",
    "php": "php",
    "swift": "swift",
}

DECLARATION_NODE_TYPES: dict[str, frozenset[str]] = {
    "python": frozenset({"function_definition", "class_definition", "decorated_definition"}),
    "typescript": frozenset(
        {
            "function_declaration",
            "class_declaration",
            "abstract_class_declaration",
            "interface_declaration",
            "type_alias_declaration",
            "enum_declaration",
            "export_statement",
            "lexical_declaration",
            "variable_declaration",
        }
    ),
    "tsx": frozenset(
        {
            "function_declaration",
            "class_declaration",
            "abstract_class_declaration",
            "interface_declaration",
            "type_alias_declaration",
            "enum_declaration",
            "export_statement",
            "lexical_declaration",
            "variable_declaration",
        }
    ),
    "javascript": frozenset(
        {
            "function_declaration",
            "class_declaration",
            "export_statement",
            "lexical_declaration",
            "variable_declaration",
        }
    ),
    "kotlin": frozenset(
        {
            "class_declaration",
            "object_declaration",
            "function_declaration",
            "property_declaration",
            "companion_object",
        }
    ),
    "java": frozenset(
        {
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
            "record_declaration",
            "method_declaration",
            "constructor_declaration",
        }
    ),
    "dart": frozenset(
        {
            "class_definition",
            "mixin_declaration",
            "extension_declaration",
            "enum_declaration",
            "function_signature",
            "method_signature",
            "type_alias",
        }
    ),
    "go": frozenset({"function_declaration", "method_declaration", "type_declaration"}),
    "rust": frozenset({"function_item", "struct_item", "enum_item", "trait_item", "impl_item"}),
    "csharp": frozenset({"class_declaration", "interface_declaration", "struct_declaration", "method_declaration"}),
    "cpp": frozenset({"function_definition", "class_specifier", "struct_specifier"}),
    "c": frozenset({"function_definition", "struct_specifier"}),
    "ruby": frozenset({"class", "module", "method", "singleton_method"}),
    "php": frozenset({"class_declaration", "interface_declaration", "trait_declaration", "function_definition"}),
    "swift": frozenset({"class_declaration", "protocol_declaration", "function_declaration"}),
}

MEMBER_NODE_TYPES: frozenset[str] = frozenset(
    {
        "function_definition",
        "decorated_definition",
        "method_definition",
        "function_declaration",
        "method_declaration",
        "constructor_declaration",
        "property_declaration",
        "public_field_definition",
        "field_declaration",
        "companion_object",
        "class_declaration",
        "class_definition",
        "method_signature",
        "function_signature",
        "getter_signature",
        "setter_signature",
        "declaration",
    }
)

IDENTIFIER_TYPES: frozenset[str] = frozenset(
    {
        "identifier",
        "type_identifier",
        "simple_identifier",
        "property_identifier",
        "field_identifier",
        "constant",
    }
)


@dataclass(frozen=True)
class _Span:
    start_byte: int
    end_byte: int
    start_line: int  # 1-based inclusive
    end_line: int  # 1-based inclusive
    symbol_name: str
    symbol_kind: str


@functools.lru_cache(maxsize=32)
def _get_cached_parser(grammar: str) -> Any | None:
    try:
        return get_parser(grammar)  # type: ignore[arg-type]
    except Exception:
        return None


def _make_chunk_id(repo_id: str, rel_path: str, start_line: int, end_line: int, idx: int) -> str:
    raw = f"{repo_id}:{rel_path}:{start_line}:{end_line}:{idx}".encode("utf-8")
    return hashlib.blake2b(raw, digest_size=12).hexdigest()


def _extract_symbol_name(node: Any, source_bytes: bytes) -> str:
    """Best-effort symbol name extraction across tree-sitter language grammars."""
    # Unwrap export_statement or decorated_definition to inner declaration
    if node.type in {"export_statement", "decorated_definition"}:
        for child in node.named_children:
            if child.type not in {"decorator", "comment"}:
                inner = _extract_symbol_name(child, source_bytes)
                if inner != "anonymous":
                    return inner

    # Variable / lexical declarations (e.g., `export const UserCard = () => ...`)
    if node.type in {"lexical_declaration", "variable_declaration"}:
        for child in node.named_children:
            if child.type == "variable_declarator":
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    return source_bytes[name_node.start_byte : name_node.end_byte].decode("utf-8", errors="ignore")

    name_field = node.child_by_field_name("name")
    if name_field is not None:
        text = source_bytes[name_field.start_byte : name_field.end_byte].decode("utf-8", errors="ignore").strip()
        if text:
            return text

    # Direct identifier children (Kotlin simple_identifier, Dart identifier, etc.)
    for child in node.named_children:
        if child.type in IDENTIFIER_TYPES:
            text = source_bytes[child.start_byte : child.end_byte].decode("utf-8", errors="ignore").strip()
            if text:
                return text

    # Nested signature (e.g., Dart declaration -> function_signature -> identifier)
    for child in node.named_children:
        if child.type in {"function_signature", "method_signature", "getter_signature", "setter_signature", "variable_declaration"}:
            nested = _extract_symbol_name(child, source_bytes)
            if nested != "anonymous":
                return nested

    return "anonymous"


def _find_body_node(node: Any) -> Any | None:
    body = node.child_by_field_name("body")
    if body is not None:
        return body
    for child in node.named_children:
        if child.type in {"class_body", "block", "declaration_list", "interface_body", "enum_body", "suite"}:
            return child
        if child.type in {"export_statement", "decorated_definition"}:
            nested = _find_body_node(child)
            if nested is not None:
                return nested
    return None


def _split_lines_window(
    lines: list[str],
    start_line_1based: int,
    max_chunk_chars: int,
    overlap_lines: int,
    symbol_name: str,
    symbol_kind: str,
) -> list[tuple[int, int, str, str, str]]:
    """Split a block of lines into overlapping char-bounded windows."""
    if not lines:
        return []

    windows: list[tuple[int, int, str, str, str]] = []
    idx = 0
    total = len(lines)
    part = 1

    while idx < total:
        cur_chars = 0
        end_idx = idx
        while end_idx < total:
            line_len = len(lines[end_idx]) + 1
            if end_idx > idx and cur_chars + line_len > max_chunk_chars:
                break
            cur_chars += line_len
            end_idx += 1

        chunk_text = "\n".join(lines[idx:end_idx]).strip("\n")
        if chunk_text.strip():
            part_name = symbol_name if (idx == 0 and end_idx == total) else f"{symbol_name}#part{part}"
            windows.append(
                (
                    start_line_1based + idx,
                    start_line_1based + end_idx - 1,
                    part_name,
                    symbol_kind,
                    chunk_text,
                )
            )
            part += 1

        if end_idx >= total:
            break
        step_back = min(overlap_lines, max(0, end_idx - idx - 1))
        idx = end_idx - step_back

    return windows


def _collect_top_level_nodes(root_node: Any, language: str) -> list[tuple[Any, Any | None]]:
    """Collect declaration nodes from the AST root, pairing Dart signatures with bodies."""
    target_types = DECLARATION_NODE_TYPES.get(language, frozenset())
    collected: list[tuple[Any, Any | None]] = []

    def walk(parent: Any) -> None:
        children = list(parent.named_children)
        i = 0
        while i < len(children):
            child = children[i]
            # Dart top-level functions appear as adjacent (function_signature, function_body) siblings
            if language == "dart" and child.type in {"function_signature", "method_signature"}:
                body_sibling = children[i + 1] if (i + 1 < len(children) and children[i + 1].type == "function_body") else None
                collected.append((child, body_sibling))
                i += 2 if body_sibling is not None else 1
                continue

            if child.type in target_types:
                collected.append((child, None))
            elif child.type in {"program", "source_file", "compilation_unit", "module", "namespace_declaration"}:
                walk(child)
            i += 1

    walk(root_node)
    return collected


def _extract_member_spans(
    parent_node: Any,
    parent_name: str,
    source_bytes: bytes,
    language: str,
) -> list[_Span]:
    body = _find_body_node(parent_node)
    if body is None:
        return []

    spans: list[_Span] = []
    children = list(body.named_children)
    i = 0
    while i < len(children):
        child = children[i]
        if language == "dart" and child.type in {"method_signature", "function_signature", "getter_signature", "setter_signature"}:
            body_sibling = children[i + 1] if (i + 1 < len(children) and children[i + 1].type == "function_body") else None
            end_node = body_sibling or child
            member_name = _extract_symbol_name(child, source_bytes)
            spans.append(
                _Span(
                    start_byte=child.start_byte,
                    end_byte=end_node.end_byte,
                    start_line=child.start_point[0] + 1,
                    end_line=end_node.end_point[0] + 1,
                    symbol_name=f"{parent_name}.{member_name}",
                    symbol_kind=child.type,
                )
            )
            i += 2 if body_sibling is not None else 1
            continue

        if child.type in MEMBER_NODE_TYPES:
            member_name = _extract_symbol_name(child, source_bytes)
            spans.append(
                _Span(
                    start_byte=child.start_byte,
                    end_byte=child.end_byte,
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                    symbol_name=f"{parent_name}.{member_name}",
                    symbol_kind=child.type,
                )
            )
        i += 1
    return spans


def chunk_file(
    repo_id: str,
    rel_path: str,
    content: str,
    language: str,
    max_chunk_chars: int = 2400,
    overlap_lines: int = 6,
    min_chunk_chars: int = 40,
) -> list[CodeChunk]:
    """Split a source file into AST-aware semantic chunks with line-window fallback."""
    if not content or not content.strip():
        return []

    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    all_lines = normalized.split("\n")
    grammar = LANGUAGE_TO_TS_GRAMMAR.get(language)
    parser = _get_cached_parser(grammar) if grammar else None

    raw_items: list[tuple[int, int, str, str, str]] = []

    if parser is not None:
        source_bytes = normalized.encode("utf-8")
        try:
            tree = parser.parse(source_bytes)
            top_nodes = _collect_top_level_nodes(tree.root_node, language)
        except Exception:
            top_nodes = []

        covered_lines: set[int] = set()
        for primary, sibling in top_nodes:
            end_node = sibling or primary
            start_line = primary.start_point[0] + 1
            end_line = end_node.end_point[0] + 1
            symbol_name = _extract_symbol_name(primary, source_bytes)
            symbol_kind = primary.type
            node_text = source_bytes[primary.start_byte : end_node.end_byte].decode("utf-8", errors="ignore").strip("\n")

            if not node_text.strip():
                continue

            if len(node_text) <= max_chunk_chars:
                raw_items.append((start_line, end_line, symbol_name, symbol_kind, node_text))
                covered_lines.update(range(start_line, end_line + 1))
                continue

            # Oversized container -> descend into class/object members first
            member_spans = _extract_member_spans(primary, symbol_name, source_bytes, language)
            if member_spans:
                for span in member_spans:
                    span_text = source_bytes[span.start_byte : span.end_byte].decode("utf-8", errors="ignore").strip("\n")
                    if not span_text.strip():
                        continue
                    if len(span_text) <= max_chunk_chars:
                        raw_items.append((span.start_line, span.end_line, span.symbol_name, span.symbol_kind, span_text))
                    else:
                        raw_items.extend(
                            _split_lines_window(
                                span_text.split("\n"),
                                span.start_line,
                                max_chunk_chars,
                                overlap_lines,
                                span.symbol_name,
                                span.symbol_kind,
                            )
                        )
                    covered_lines.update(range(span.start_line, span.end_line + 1))
            else:
                raw_items.extend(
                    _split_lines_window(
                        node_text.split("\n"),
                        start_line,
                        max_chunk_chars,
                        overlap_lines,
                        symbol_name,
                        symbol_kind,
                    )
                )
                covered_lines.update(range(start_line, end_line + 1))

        # Capture meaningful top-level code not covered by declarations (e.g. scripts/routes/config blocks)
        if raw_items:
            gap_start: int | None = None
            gap_lines: list[str] = []
            for line_no, line_text in enumerate(all_lines, start=1):
                if line_no not in covered_lines:
                    if gap_start is None:
                        gap_start = line_no
                    gap_lines.append(line_text)
                elif gap_start is not None:
                    gap_block = "\n".join(gap_lines).strip()
                    if len(gap_block) >= min_chunk_chars and not _is_imports_only(gap_block):
                        raw_items.extend(
                            _split_lines_window(
                                gap_lines,
                                gap_start,
                                max_chunk_chars,
                                overlap_lines,
                                "module_scope",
                                "module",
                            )
                        )
                    gap_start = None
                    gap_lines = []
            if gap_start is not None:
                gap_block = "\n".join(gap_lines).strip()
                if len(gap_block) >= min_chunk_chars and not _is_imports_only(gap_block):
                    raw_items.extend(
                        _split_lines_window(
                            gap_lines,
                            gap_start,
                            max_chunk_chars,
                            overlap_lines,
                            "module_scope",
                            "module",
                        )
                    )

    if not raw_items:
        raw_items = _split_lines_window(
            all_lines,
            1,
            max_chunk_chars,
            overlap_lines,
            "file",
            "module",
        )

    raw_items.sort(key=lambda item: (item[0], item[1]))

    chunks: list[CodeChunk] = []
    for idx, (start_line, end_line, symbol_name, symbol_kind, text) in enumerate(raw_items):
        chunks.append(
            CodeChunk(
                chunk_id=_make_chunk_id(repo_id, rel_path, start_line, end_line, idx),
                repo_id=repo_id,
                rel_path=rel_path,
                language=language,
                symbol_name=symbol_name,
                symbol_kind=symbol_kind,
                start_line=start_line,
                end_line=end_line,
                content=text,
            )
        )
    return chunks


def _is_imports_only(block: str) -> bool:
    """Avoid creating standalone chunks consisting solely of import/package statements."""
    prefixes = ("import ", "from ", "package ", "export {", "using ", "#include", "//", "#")
    non_empty = [line.strip() for line in block.splitlines() if line.strip()]
    if not non_empty:
        return True
    return all(line.startswith(prefixes) for line in non_empty)
