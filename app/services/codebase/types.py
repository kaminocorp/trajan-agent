"""Internal types for the codebase indexing pipeline."""

from dataclasses import dataclass, field


@dataclass
class ParsedSymbol:
    """A symbol extracted from a source file's AST."""

    name: str
    kind: str  # "class", "function", "method", "interface", "type", "enum", "variable", "import"
    file_path: str
    start_line: int
    end_line: int
    metadata: dict[str, object] | None = None


@dataclass
class ParsedRelation:
    """A relationship extracted from AST analysis."""

    source_name: str
    source_file: str
    target_name: str
    relation_type: str  # "imports", "calls", "extends", "implements"
    metadata: dict[str, object] | None = None


@dataclass
class ParsedFile:
    """Result of parsing a single source file."""

    file_path: str
    language: str
    symbols: list[ParsedSymbol] = field(default_factory=list)
    relations: list[ParsedRelation] = field(default_factory=list)


@dataclass
class IndexingResult:
    """Summary of an indexing run."""

    repo_id: str
    files_parsed: int
    nodes_created: int
    edges_created: int
    languages: dict[str, int]  # language → file count
    errors: list[str] = field(default_factory=list)
    skipped_files: int = 0


# File extensions → tree-sitter language name
LANGUAGE_MAP: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".java": "java",
    ".rs": "rust",
}

# Directories and patterns to skip during indexing
SKIP_DIRS: set[str] = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".env",
    "dist",
    "build",
    ".next",
    ".nuxt",
    "target",
    "vendor",
    ".tox",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "coverage",
    ".coverage",
    "egg-info",
    ".eggs",
}

SKIP_FILE_PATTERNS: set[str] = {
    ".min.js",
    ".min.css",
    ".d.ts",
    ".map",
    ".lock",
    ".generated.",
    "__generated__",
}

# Max file size to parse (500KB — skip large generated/vendored files)
MAX_FILE_SIZE_BYTES: int = 500_000
