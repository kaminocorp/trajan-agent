"""Tree-sitter AST parser for extracting code symbols and relationships.

Supports Python, TypeScript/JavaScript, Go, Java, and Rust. Each language
has its own set of tree-sitter node types that map to our CodeNodeType enum.

Uses py-tree-sitter + tree-sitter-languages for pre-built grammars.
"""

import logging
from pathlib import Path
from types import ModuleType
from typing import Any

from app.services.codebase.types import (
    LANGUAGE_MAP,
    MAX_FILE_SIZE_BYTES,
    ParsedFile,
    ParsedRelation,
    ParsedSymbol,
)

logger = logging.getLogger(__name__)

# Lazy-loaded tree-sitter module references
_tree_sitter: ModuleType | None = None
_ts_languages: ModuleType | None = None


def _ensure_tree_sitter() -> tuple[ModuleType, ModuleType]:
    """Lazy-import tree-sitter to avoid hard dependency at module load."""
    global _tree_sitter, _ts_languages
    if _tree_sitter is None:
        try:
            import tree_sitter as ts
            import tree_sitter_languages as tsl

            _tree_sitter = ts
            _ts_languages = tsl
        except ImportError as e:
            raise ImportError(
                "tree-sitter and tree-sitter-languages are required for codebase indexing. "
                "Install with: pip install tree-sitter tree-sitter-languages"
            ) from e
    assert _ts_languages is not None  # for mypy
    return _tree_sitter, _ts_languages


# -----------------------------------------------------------------------
# Language-specific node type mappings
# -----------------------------------------------------------------------
# Maps tree-sitter AST node types → our symbol kinds

PYTHON_SYMBOL_TYPES: dict[str, str] = {
    "class_definition": "class",
    "function_definition": "function",
    "import_statement": "import",
    "import_from_statement": "import",
    "assignment": "variable",
}

TYPESCRIPT_SYMBOL_TYPES: dict[str, str] = {
    "class_declaration": "class",
    "function_declaration": "function",
    "method_definition": "method",
    "interface_declaration": "interface",
    "type_alias_declaration": "type",
    "enum_declaration": "enum",
    "import_statement": "import",
    "lexical_declaration": "variable",
    "variable_declaration": "variable",
    "export_statement": "variable",  # may contain function/class — handled specially
    "arrow_function": "function",
}

JAVASCRIPT_SYMBOL_TYPES: dict[str, str] = {
    "class_declaration": "class",
    "function_declaration": "function",
    "method_definition": "method",
    "import_statement": "import",
    "lexical_declaration": "variable",
    "variable_declaration": "variable",
    "export_statement": "variable",
    "arrow_function": "function",
}

GO_SYMBOL_TYPES: dict[str, str] = {
    "type_declaration": "type",
    "function_declaration": "function",
    "method_declaration": "method",
    "import_declaration": "import",
    "var_declaration": "variable",
    "const_declaration": "variable",
}

JAVA_SYMBOL_TYPES: dict[str, str] = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "method_declaration": "method",
    "import_declaration": "import",
    "field_declaration": "variable",
}

RUST_SYMBOL_TYPES: dict[str, str] = {
    "struct_item": "class",
    "enum_item": "enum",
    "function_item": "function",
    "impl_item": "class",
    "trait_item": "interface",
    "type_item": "type",
    "use_declaration": "import",
    "const_item": "variable",
    "static_item": "variable",
}

SYMBOL_TYPES_BY_LANGUAGE: dict[str, dict[str, str]] = {
    "python": PYTHON_SYMBOL_TYPES,
    "typescript": TYPESCRIPT_SYMBOL_TYPES,
    "javascript": JAVASCRIPT_SYMBOL_TYPES,
    "go": GO_SYMBOL_TYPES,
    "java": JAVA_SYMBOL_TYPES,
    "rust": RUST_SYMBOL_TYPES,
}

# Node types whose first named child provides the name
NAME_CHILD_TYPES: dict[str, set[str]] = {
    "python": {"class_definition", "function_definition"},
    "typescript": {
        "class_declaration",
        "function_declaration",
        "method_definition",
        "interface_declaration",
        "type_alias_declaration",
        "enum_declaration",
    },
    "javascript": {"class_declaration", "function_declaration", "method_definition"},
    "go": {"function_declaration", "method_declaration"},
    "java": {
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "method_declaration",
    },
    "rust": {
        "struct_item",
        "enum_item",
        "function_item",
        "trait_item",
        "type_item",
    },
}


# -----------------------------------------------------------------------
# Parser
# -----------------------------------------------------------------------


def _get_parser(language: str) -> Any:
    """Create a tree-sitter parser for the given language."""
    ts, tsl = _ensure_tree_sitter()
    try:
        lang = tsl.get_language(language)
        parser = ts.Parser()
        parser.set_language(lang)
        return parser
    except Exception:
        logger.warning(f"No tree-sitter grammar available for {language}")
        return None


def _extract_name(node: Any, language: str, source_bytes: bytes) -> str:
    """Extract the name of a symbol from its AST node."""
    name_types = NAME_CHILD_TYPES.get(language, set())

    if node.type in name_types:
        # Look for a 'name' or 'identifier' child
        for child in node.children:
            if child.type in ("identifier", "name", "type_identifier", "property_identifier"):
                return source_bytes[child.start_byte : child.end_byte].decode("utf-8", "replace")

    # Python assignments: first child is the target
    if node.type == "assignment" and language == "python":
        target = node.children[0] if node.children else None
        if target:
            return source_bytes[target.start_byte : target.end_byte].decode("utf-8", "replace")

    # JS/TS variable declarations: look for variable_declarator → name
    if node.type in ("lexical_declaration", "variable_declaration"):
        for child in node.children:
            if child.type == "variable_declarator":
                for sub in child.children:
                    if sub.type in ("identifier", "name"):
                        return source_bytes[sub.start_byte : sub.end_byte].decode(
                            "utf-8", "replace"
                        )

    # Go type declarations
    if node.type == "type_declaration" and language == "go":
        for child in node.children:
            if child.type == "type_spec":
                for sub in child.children:
                    if sub.type == "type_identifier":
                        return source_bytes[sub.start_byte : sub.end_byte].decode(
                            "utf-8", "replace"
                        )

    # Rust impl — extract the type being implemented
    if node.type == "impl_item" and language == "rust":
        for child in node.children:
            if child.type == "type_identifier":
                return source_bytes[child.start_byte : child.end_byte].decode("utf-8", "replace")

    # Import statements — extract the full import text (trimmed)
    if "import" in node.type:
        text = source_bytes[node.start_byte : node.end_byte].decode("utf-8", "replace")
        # Trim to a reasonable length for the name field
        return text[:200].strip()

    # Fallback: use the node text, truncated
    text = source_bytes[node.start_byte : node.end_byte].decode("utf-8", "replace")
    first_line = text.split("\n")[0][:100]
    return first_line.strip()


def _extract_relations(
    node: Any,
    language: str,
    source_bytes: bytes,
    file_path: str,
    current_class: str | None = None,
) -> list[ParsedRelation]:
    """Extract relationships (imports, calls, extends, implements) from AST."""
    relations: list[ParsedRelation] = []

    # --- Imports ---
    if "import" in node.type:
        text = source_bytes[node.start_byte : node.end_byte].decode("utf-8", "replace")
        relations.append(
            ParsedRelation(
                source_name=file_path,
                source_file=file_path,
                target_name=text.strip()[:200],
                relation_type="imports",
            )
        )

    # --- Class inheritance (extends/implements) ---
    if language == "python" and node.type == "class_definition":
        # Look for argument_list (superclasses)
        for child in node.children:
            if child.type == "argument_list":
                for arg in child.children:
                    if arg.type == "identifier":
                        base = source_bytes[arg.start_byte : arg.end_byte].decode(
                            "utf-8", "replace"
                        )
                        class_name = _extract_name(node, language, source_bytes)
                        relations.append(
                            ParsedRelation(
                                source_name=class_name,
                                source_file=file_path,
                                target_name=base,
                                relation_type="extends",
                            )
                        )

    if language in ("typescript", "javascript", "java") and node.type == "class_declaration":
        for child in node.children:
            if child.type == "class_heritage":
                for clause in child.children:
                    if clause.type == "extends_clause":
                        for sub in clause.children:
                            if sub.type in ("identifier", "type_identifier"):
                                base = source_bytes[sub.start_byte : sub.end_byte].decode(
                                    "utf-8", "replace"
                                )
                                class_name = _extract_name(node, language, source_bytes)
                                relations.append(
                                    ParsedRelation(
                                        source_name=class_name,
                                        source_file=file_path,
                                        target_name=base,
                                        relation_type="extends",
                                    )
                                )
                    if clause.type == "implements_clause":
                        for sub in clause.children:
                            if sub.type in ("identifier", "type_identifier"):
                                iface = source_bytes[sub.start_byte : sub.end_byte].decode(
                                    "utf-8", "replace"
                                )
                                class_name = _extract_name(node, language, source_bytes)
                                relations.append(
                                    ParsedRelation(
                                        source_name=class_name,
                                        source_file=file_path,
                                        target_name=iface,
                                        relation_type="implements",
                                    )
                                )

    # --- Function/method calls ---
    if node.type == "call" or node.type == "call_expression":
        callee_text = ""
        callee = node.children[0] if node.children else None
        if callee:
            callee_text = source_bytes[callee.start_byte : callee.end_byte].decode(
                "utf-8", "replace"
            )
        if callee_text:
            caller = current_class or file_path
            relations.append(
                ParsedRelation(
                    source_name=caller,
                    source_file=file_path,
                    target_name=callee_text[:200],
                    relation_type="calls",
                )
            )

    return relations


def _walk_tree(
    node: Any,
    language: str,
    source_bytes: bytes,
    file_path: str,
    symbols: list[ParsedSymbol],
    relations: list[ParsedRelation],
    current_class: str | None = None,
    depth: int = 0,
) -> None:
    """Recursively walk the AST, extracting symbols and relations."""
    if depth > 50:
        return  # Safety: don't recurse infinitely on pathological ASTs

    symbol_types = SYMBOL_TYPES_BY_LANGUAGE.get(language, {})

    if node.type in symbol_types:
        kind = symbol_types[node.type]
        name = _extract_name(node, language, source_bytes)

        # Detect methods (functions inside classes)
        if kind == "function" and current_class:
            kind = "method"

        symbols.append(
            ParsedSymbol(
                name=name,
                kind=kind,
                file_path=file_path,
                start_line=node.start_point[0] + 1,  # tree-sitter is 0-indexed
                end_line=node.end_point[0] + 1,
            )
        )

        # Track class context for method detection
        if kind == "class":
            current_class = name

    # Extract relations from this node
    rels = _extract_relations(node, language, source_bytes, file_path, current_class)
    relations.extend(rels)

    # Recurse into children
    for child in node.children:
        _walk_tree(
            child,
            language,
            source_bytes,
            file_path,
            symbols,
            relations,
            current_class=current_class if node.type not in symbol_types else current_class,
            depth=depth + 1,
        )


def parse_file(file_path: str, repo_root: str) -> ParsedFile | None:
    """Parse a single source file and extract symbols + relations.

    Args:
        file_path: Absolute path to the file on disk.
        repo_root: Absolute path to the repo root (for relative paths).

    Returns:
        ParsedFile with extracted symbols and relations, or None if
        the file can't be parsed (unsupported language, too large, etc.)
    """
    path = Path(file_path)
    ext = path.suffix.lower()

    language = LANGUAGE_MAP.get(ext)
    if not language:
        return None

    # Check file size
    try:
        size = path.stat().st_size
        if size > MAX_FILE_SIZE_BYTES:
            logger.debug(f"Skipping large file ({size} bytes): {file_path}")
            return None
        if size == 0:
            return None
    except OSError:
        return None

    # Read file
    try:
        source_bytes = path.read_bytes()
    except OSError:
        return None

    # Get parser
    parser = _get_parser(language)
    if not parser:
        return None

    # Parse
    try:
        tree = parser.parse(source_bytes)
    except Exception as e:
        logger.warning(f"tree-sitter parse error for {file_path}: {e}")
        return None

    # Compute relative path
    rel_path = str(path.relative_to(repo_root))

    # Walk AST
    symbols: list[ParsedSymbol] = []
    relations: list[ParsedRelation] = []
    _walk_tree(tree.root_node, language, source_bytes, rel_path, symbols, relations)

    return ParsedFile(
        file_path=rel_path,
        language=language,
        symbols=symbols,
        relations=relations,
    )
