"""Changelog generation service.

Provides AI-powered changelog generation from commit history:
- ChangelogGenerator: Fetches commits, batches them, groups via Claude, persists results
"""

from app.services.changelog.generator import ChangelogGenerator
from app.services.changelog.types import (
    BatchResult,
    ChangelogGroupedEntry,
    GenerationProgress,
    GenerationResult,
)

__all__ = [
    "ChangelogGenerator",
    "BatchResult",
    "ChangelogGroupedEntry",
    "GenerationProgress",
    "GenerationResult",
]
