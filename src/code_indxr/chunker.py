from __future__ import annotations

import functools
import hashlib
import logging
import threading
from dataclasses import dataclass
from typing import Any

from tree_sitter import Parser
from tree_sitter_language_pack import get_language

from code_indxr.models import CodeChunk, FileArtifacts, SymbolInfo

logger = logging.getLogger("code_indxr.chunker")

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

IMPORT_NODE_TYPES: frozenset[str] = frozenset(
    {
        "import_statement",
        "import_from_statement",
        "import_declaration",
        "import_header",
        "import_or_export",
        "library_import",
        "use_declaration",
        "using_directive",
        "preproc_include",
    }
)

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

_THREAD_LOCAL = threading.local()


@dataclass(frozen=True)
class _Span:
    start_byte: int
    end_byte: int
    start_line: int  # 1-based inclusive
    end_line: int  # 1-based inclusive
    symbol_name: str
    symbol_kind: str


@functools.lru_cache(maxsize=32)
def _get_cached_language(grammar: str) -> Any | None:
    try:
        return get_language(grammar)  # type: ignore[arg-type]
    except Exception as exc:
        logger.warning("Failed to load tree-sitter language '%s': %s", grammar, exc)
        return None


def _get_thread_parser(grammar: str) -> Parser | None:
    """Return a thread-local tree-sitter Parser so worker threads never share C state."""
    parsers: dict[str, Parser] | None = getattr(_THREAD_LOCAL, "parsers", None)
    if parsers is None:
        parsers = {}
        _THREAD_LOCAL.parsers = parsers

    parser = parsers.get(grammar)
    if parser is not None:
        return parser

    lang = _get_cached_language(grammar)
    if lang is None:
        return None
    parser = Parser(lang)
    parsers[grammar] = parser
    return parser


def _make_chunk_id(repo_id: str, rel_path: str, start_line: int, end_line: int, idx: int) -> str:
    raw = f"{repo_id}:{rel_path}:{start_line}:{end_line}:{idx}".encode()
    return hashlib.blake2b(raw, digest_size=12).hexdigest()


def _extract_symbol_name(node: Any, source_bytes: bytes) -> str:
    """Best-effort symbol name extraction across tree-sitter language grammars."""
    if node.type in {"export_statement", "decorated_definition"}:
        for child in node.named_children:
            if child.type not in {"decorator", "comment"}:
                inner = _extract_symbol_name(child, source_bytes)
                if inner != "anonymous":
                    return inner

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

    for child in node.named_children:
        if child.type in IDENTIFIER_TYPES:
            text = source_bytes[child.start_byte : child.end_byte].decode("utf-8", errors="ignore").strip()
            if text:
                return text

    for child in node.named_children:
        if child.type in {
            "function_signature",
            "method_signature",
            "getter_signature",
            "setter_signature",
            "variable_declaration",
        }:
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


def _extract_signature(node: Any, source_bytes: bytes, end_node: Any | None = None) -> str:
    """Collapse the declaration header (up to its body) into a single <=200-char line."""
    body = None if end_node is not None and end_node is not node else _find_body_node(node)
    end_byte = body.start_byte if body is not None and body.start_byte > node.start_byte else node.end_byte
    raw = source_bytes[node.start_byte : end_byte].decode("utf-8", errors="ignore")
    if body is None:
        raw = raw.splitlines()[0] if raw else ""
    collapsed = " ".join(part.strip() for part in raw.splitlines() if part.strip())
    if len(collapsed) > 200:
        return f"{collapsed[:197]}..."
    return collapsed


def _extract_docstring_excerpt(node: Any, source_bytes: bytes, language: str) -> str:
    """Extract a concise first-line docstring or leading doc-comment excerpt (<=160 chars)."""
    if language == "python":
        body = _find_body_node(node)
        if body is not None and body.named_children:
            first = body.named_children[0]
            str_node = None
            if first.type == "string":
                str_node = first
            elif (
                first.type == "expression_statement"
                and first.named_children
                and first.named_children[0].type == "string"
            ):
                str_node = first.named_children[0]
            if str_node is not None:
                raw = source_bytes[str_node.start_byte : str_node.end_byte].decode("utf-8", errors="ignore").strip()
                cleaned = raw.strip("\"' \t\r\n")
                first_line = next((ln.strip() for ln in cleaned.splitlines() if ln.strip()), "")
                return first_line[:160]

    prev = node.prev_named_sibling
    if prev is not None and "comment" in prev.type:
        raw = source_bytes[prev.start_byte : prev.end_byte].decode("utf-8", errors="ignore")
        lines = [ln.strip().lstrip("/*# ").rstrip("*/").strip() for ln in raw.splitlines()]
        first_line = next((ln for ln in lines if ln), "")
        return first_line[:160]
    return ""


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


def _collect_top_level_nodes(root_node: Any, language: str) -> tuple[list[tuple[Any, Any | None]], list[Any]]:
    """Collect declaration nodes and import nodes from the AST root."""
    target_types = DECLARATION_NODE_TYPES.get(language, frozenset())
    collected: list[tuple[Any, Any | None]] = []
    imports: list[Any] = []

    def walk(parent: Any) -> None:
        children = list(parent.named_children)
        i = 0
        while i < len(children):
            child = children[i]
            if child.type in IMPORT_NODE_TYPES:
                imports.append(child)
                i += 1
                continue

            if language == "dart" and child.type in {"function_signature", "method_signature"}:
                body_sibling = (
                    children[i + 1] if (i + 1 < len(children) and children[i + 1].type == "function_body") else None
                )
                collected.append((child, body_sibling))
                i += 2 if body_sibling is not None else 1
                continue

            if child.type in target_types:
                collected.append((child, None))
            elif child.type in {"program", "source_file", "compilation_unit", "module", "namespace_declaration"}:
                walk(child)
            i += 1

    walk(root_node)
    return collected, imports


def _extract_member_spans(
    parent_node: Any,
    parent_name: str,
    source_bytes: bytes,
    language: str,
) -> list[tuple[_Span, Any]]:
    body = _find_body_node(parent_node)
    if body is None:
        return []

    spans: list[tuple[_Span, Any]] = []
    children = list(body.named_children)
    i = 0
    while i < len(children):
        child = children[i]
        if language == "dart" and child.type in {
            "method_signature",
            "function_signature",
            "getter_signature",
            "setter_signature",
        }:
            body_sibling = (
                children[i + 1] if (i + 1 < len(children) and children[i + 1].type == "function_body") else None
            )
            end_node = body_sibling or child
            member_name = _extract_symbol_name(child, source_bytes)
            spans.append(
                (
                    _Span(
                        start_byte=child.start_byte,
                        end_byte=end_node.end_byte,
                        start_line=child.start_point[0] + 1,
                        end_line=end_node.end_point[0] + 1,
                        symbol_name=f"{parent_name}.{member_name}",
                        symbol_kind=child.type,
                    ),
                    child,
                )
            )
            i += 2 if body_sibling is not None else 1
            continue

        if child.type in MEMBER_NODE_TYPES:
            member_name = _extract_symbol_name(child, source_bytes)
            spans.append(
                (
                    _Span(
                        start_byte=child.start_byte,
                        end_byte=child.end_byte,
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                        symbol_name=f"{parent_name}.{member_name}",
                        symbol_kind=child.type,
                    ),
                    child,
                )
            )
        i += 1
    return spans


def extract_file_artifacts(
    repo_id: str,
    rel_path: str,
    content: str,
    language: str,
    max_chunk_chars: int = 2400,
    overlap_lines: int = 6,
    min_chunk_chars: int = 40,
) -> FileArtifacts:
    """Single-pass AST extraction returning semantic chunks, symbol outlines, and imports."""
    if not content or not content.strip():
        return FileArtifacts(chunks=[], symbols=[], imports=[], used_fallback=False)

    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    all_lines = normalized.split("\n")
    grammar = LANGUAGE_TO_TS_GRAMMAR.get(language)
    parser = _get_thread_parser(grammar) if grammar else None

    raw_items: list[tuple[int, int, str, str, str]] = []
    symbols: list[SymbolInfo] = []
    imports: list[str] = []
    used_fallback = False

    if parser is not None:
        source_bytes = normalized.encode("utf-8")
        try:
            tree = parser.parse(source_bytes)
            top_nodes, import_nodes = _collect_top_level_nodes(tree.root_node, language)
        except Exception as exc:
            logger.warning("Tree-sitter parse failed for %s (%s): %s", rel_path, language, exc)
            top_nodes, import_nodes = [], []
            used_fallback = True

        for imp_node in import_nodes[:40]:
            imp_text = source_bytes[imp_node.start_byte : imp_node.end_byte].decode("utf-8", errors="ignore").strip()
            if imp_text:
                imports.append(" ".join(imp_text.splitlines())[:160])

        covered_lines: set[int] = set()
        for primary, sibling in top_nodes:
            end_node = sibling or primary
            start_line = primary.start_point[0] + 1
            end_line = end_node.end_point[0] + 1
            symbol_name = _extract_symbol_name(primary, source_bytes)
            symbol_kind = primary.type
            node_text = (
                source_bytes[primary.start_byte : end_node.end_byte].decode("utf-8", errors="ignore").strip("\n")
            )

            if not node_text.strip():
                continue

            symbols.append(
                SymbolInfo(
                    rel_path=rel_path,
                    symbol_name=symbol_name,
                    symbol_kind=symbol_kind,
                    signature=_extract_signature(primary, source_bytes, end_node=end_node),
                    docstring_excerpt=_extract_docstring_excerpt(primary, source_bytes, language),
                    start_line=start_line,
                    end_line=end_line,
                )
            )

            # Always record member symbols for outlines even if the container fits in one chunk
            member_entries = _extract_member_spans(primary, symbol_name, source_bytes, language)
            for span, member_node in member_entries:
                symbols.append(
                    SymbolInfo(
                        rel_path=rel_path,
                        symbol_name=span.symbol_name,
                        symbol_kind=span.symbol_kind,
                        signature=_extract_signature(member_node, source_bytes),
                        docstring_excerpt=_extract_docstring_excerpt(member_node, source_bytes, language),
                        start_line=span.start_line,
                        end_line=span.end_line,
                    )
                )

            if len(node_text) <= max_chunk_chars:
                raw_items.append((start_line, end_line, symbol_name, symbol_kind, node_text))
                covered_lines.update(range(start_line, end_line + 1))
                continue

            if member_entries:
                for span, _member_node in member_entries:
                    span_text = (
                        source_bytes[span.start_byte : span.end_byte].decode("utf-8", errors="ignore").strip("\n")
                    )
                    if not span_text.strip():
                        continue
                    if len(span_text) <= max_chunk_chars:
                        raw_items.append(
                            (span.start_line, span.end_line, span.symbol_name, span.symbol_kind, span_text)
                        )
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
        if grammar is not None and not used_fallback:
            used_fallback = False
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
    return FileArtifacts(chunks=chunks, symbols=symbols, imports=imports, used_fallback=used_fallback)


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
    return extract_file_artifacts(
        repo_id=repo_id,
        rel_path=rel_path,
        content=content,
        language=language,
        max_chunk_chars=max_chunk_chars,
        overlap_lines=overlap_lines,
        min_chunk_chars=min_chunk_chars,
    ).chunks


def _is_imports_only(block: str) -> bool:
    """Avoid creating standalone chunks consisting solely of import/package statements."""
    prefixes = ("import ", "from ", "package ", "export {", "using ", "#include", "//", "#")
    non_empty = [line.strip() for line in block.splitlines() if line.strip()]
    if not non_empty:
        return True
    return all(line.startswith(prefixes) for line in non_empty)
