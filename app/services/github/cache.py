"""
TTL caching for GitHub API responses.

Provides in-memory caching with time-to-live for frequently accessed,
slowly-changing GitHub data like repository trees, languages, and contributors.

Cache durations are tuned based on how frequently the underlying data changes:
- Trees: 5 minutes (change with commits, but same session usually sees same data)
- Languages: 1 hour (rarely changes)
- Contributors: 1 hour (rarely changes)
- Repo details: 10 minutes (stars/forks can change, but not frequently)
"""

import hashlib
import logging
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from cachetools import TTLCache  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

# Type vars for decorator typing
P = ParamSpec("P")
T = TypeVar("T")

# Separate caches with appropriate TTLs and sizes
_tree_cache: TTLCache[str, Any] = TTLCache(maxsize=100, ttl=300)  # 5 min
_languages_cache: TTLCache[str, Any] = TTLCache(maxsize=200, ttl=3600)  # 1 hour
_contributors_cache: TTLCache[str, Any] = TTLCache(maxsize=200, ttl=3600)  # 1 hour
_repo_details_cache: TTLCache[str, Any] = TTLCache(maxsize=200, ttl=600)  # 10 min
_agent_context_cache: TTLCache[str, Any] = TTLCache(maxsize=50, ttl=60)  # 60s


def _make_cache_key(func_name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """
    Generate a cache key from function name and arguments.

    Includes a hash of the auth token from 'self' (first arg) to prevent
    cross-user cache pollution — different tokens must produce different keys.
    """
    # Extract token identity from 'self' (first arg is the GitHubReadOperations instance)
    token_hash = ""
    if args:
        instance = args[0]
        token = getattr(instance, "token", None)
        if token:
            token_hash = hashlib.md5(token.encode()).hexdigest()[:8]

    cache_args = args[1:] if args else ()
    key_data = f"{func_name}:{token_hash}:{cache_args}:{sorted(kwargs.items())}"
    return hashlib.md5(key_data.encode()).hexdigest()


def cached_github_call(
    cache: TTLCache[str, Any],
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """
    Decorator for caching async GitHub API calls.

    Usage:
        @cached_github_call(_tree_cache)
        async def get_repo_tree(self, owner: str, repo: str, branch: str) -> RepoTree:
            ...

    The cache key is generated from the function name and arguments (excluding 'self').
    On cache hit, returns immediately without making an API call.
    On cache miss, executes the function and stores the result.
    """

    def decorator(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            key = _make_cache_key(func.__name__, args, kwargs)

            # Check cache
            if key in cache:
                logger.debug(f"Cache HIT: {func.__name__}")
                cached_result: T = cache[key]
                return cached_result

            # Cache miss - execute and store
            logger.debug(f"Cache MISS: {func.__name__}")
            result = await func(*args, **kwargs)
            cache[key] = result
            return result

        return wrapper

    return decorator


def clear_all_caches() -> None:
    """Clear all GitHub caches. Useful for testing or when data is known to be stale."""
    _tree_cache.clear()
    _languages_cache.clear()
    _contributors_cache.clear()
    _repo_details_cache.clear()
    _agent_context_cache.clear()
    logger.debug("Cleared all GitHub caches")


def get_cache_stats() -> dict[str, dict[str, int]]:
    """Get current cache statistics for monitoring."""
    return {
        "tree": {"size": len(_tree_cache), "maxsize": _tree_cache.maxsize},
        "languages": {"size": len(_languages_cache), "maxsize": _languages_cache.maxsize},
        "contributors": {
            "size": len(_contributors_cache),
            "maxsize": _contributors_cache.maxsize,
        },
        "repo_details": {
            "size": len(_repo_details_cache),
            "maxsize": _repo_details_cache.maxsize,
        },
        "agent_context": {
            "size": len(_agent_context_cache),
            "maxsize": _agent_context_cache.maxsize,
        },
    }


# Export cache instances for decorator use
tree_cache = _tree_cache
languages_cache = _languages_cache
contributors_cache = _contributors_cache
repo_details_cache = _repo_details_cache
agent_context_cache = _agent_context_cache
