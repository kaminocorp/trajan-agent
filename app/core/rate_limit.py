"""Rate limiting utilities for sensitive endpoints.

Provides in-memory rate limiting for endpoints that reveal or export secrets.
Uses a sliding window approach with automatic cleanup of expired entries.
"""

import time
import uuid as uuid_pkg
from collections import defaultdict
from dataclasses import dataclass
from typing import TypeAlias

from fastapi import HTTPException, status


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting."""

    requests: int  # Maximum requests allowed
    window_seconds: int  # Time window in seconds


# Default configurations for different endpoint types
REVEAL_LIMIT = RateLimitConfig(requests=30, window_seconds=60)  # 30 reveals per minute
EXPORT_LIMIT = RateLimitConfig(requests=10, window_seconds=60)  # 10 exports per minute

# Public API rate limits (keyed by API key ID)
PUBLIC_WRITE_LIMIT = RateLimitConfig(requests=120, window_seconds=60)
PUBLIC_INTERPRET_LIMIT = RateLimitConfig(requests=30, window_seconds=60)
PUBLIC_READ_LIMIT = RateLimitConfig(requests=120, window_seconds=60)

# Partner API rate limits (keyed by org API key ID)
PARTNER_READ_LIMIT = RateLimitConfig(requests=60, window_seconds=60)


# Type alias for clarity
UserId: TypeAlias = uuid_pkg.UUID
Timestamp: TypeAlias = float


class RateLimiter:
    """In-memory rate limiter with sliding window.

    Tracks request timestamps per user and enforces configurable limits.
    Automatically cleans up expired entries to prevent memory bloat.

    Note: This is an in-memory implementation suitable for single-instance
    deployments. For multi-instance deployments, consider Redis-based limiting.
    """

    def __init__(self) -> None:
        # Map of user_id -> endpoint_key -> list of timestamps
        self._requests: dict[UserId, dict[str, list[Timestamp]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self._last_cleanup = time.time()
        self._cleanup_interval = 300  # Clean up every 5 minutes

    def _cleanup_expired(self, window_seconds: int) -> None:
        """Remove expired entries to prevent memory growth."""
        now = time.time()

        # Only run cleanup periodically
        if now - self._last_cleanup < self._cleanup_interval:
            return

        cutoff = now - window_seconds
        users_to_remove: list[UserId] = []

        for user_id, endpoints in self._requests.items():
            endpoints_to_remove: list[str] = []
            for endpoint, timestamps in endpoints.items():
                # Filter to only recent timestamps
                endpoints[endpoint] = [ts for ts in timestamps if ts > cutoff]
                if not endpoints[endpoint]:
                    endpoints_to_remove.append(endpoint)

            for endpoint in endpoints_to_remove:
                del endpoints[endpoint]

            if not endpoints:
                users_to_remove.append(user_id)

        for user_id in users_to_remove:
            del self._requests[user_id]

        self._last_cleanup = now

    def check_rate_limit(
        self,
        user_id: UserId,
        endpoint_key: str,
        config: RateLimitConfig,
    ) -> None:
        """Check if request is within rate limits.

        Args:
            user_id: The user making the request
            endpoint_key: Unique identifier for the endpoint (e.g., "reveal", "export")
            config: Rate limit configuration to apply

        Raises:
            HTTPException: 429 Too Many Requests if limit exceeded
        """
        now = time.time()
        cutoff = now - config.window_seconds

        # Clean up periodically
        self._cleanup_expired(config.window_seconds)

        # Get user's request history for this endpoint
        timestamps = self._requests[user_id][endpoint_key]

        # Filter to only requests within the window
        recent_requests = [ts for ts in timestamps if ts > cutoff]

        if len(recent_requests) >= config.requests:
            retry_after = int(min(recent_requests) + config.window_seconds - now) + 1
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded. Try again in {retry_after} seconds.",
                headers={"Retry-After": str(retry_after)},
            )

        # Record this request
        recent_requests.append(now)
        self._requests[user_id][endpoint_key] = recent_requests

    def get_remaining(
        self,
        user_id: UserId,
        endpoint_key: str,
        config: RateLimitConfig,
    ) -> int:
        """Get remaining requests in current window."""
        now = time.time()
        cutoff = now - config.window_seconds

        timestamps = self._requests.get(user_id, {}).get(endpoint_key, [])
        recent_count = sum(1 for ts in timestamps if ts > cutoff)

        return max(0, config.requests - recent_count)


# Singleton instance for application-wide use
rate_limiter = RateLimiter()
