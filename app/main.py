import logging
import sys
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import RequestResponseEndpoint
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.api.router import api_router
from app.config import settings
from app.core.database import init_db


def setup_logging() -> None:
    """Configure application logging."""
    # Format: timestamp - level - logger name - message
    log_format = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
    date_format = "%H:%M:%S"

    # Configure root logger
    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        datefmt=date_format,
        stream=sys.stdout,
        force=True,
    )

    # Reduce noise from third-party libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)

    # Quieten uvicorn access logs (we'll log requests ourselves)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Application lifespan: startup and shutdown events."""
    from app.services.github import close_github_client
    from app.services.scheduler import scheduler

    # Startup
    setup_logging()
    logger.info("Trajan API starting up")
    if settings.debug:
        await init_db()
    scheduler.start()
    yield
    # Shutdown
    scheduler.stop()
    await close_github_client()  # Clean up HTTP connection pool
    logger.info("Trajan API shutting down")


app = FastAPI(
    title="Trajan API",
    description="Lightweight developer workspace API",
    version="0.1.0",
    lifespan=lifespan,
)

# Proxy headers middleware - trust X-Forwarded-Proto from reverse proxy (Fly.io)
# This ensures redirects use HTTPS when behind TLS-terminating proxy
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=["*"])

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def public_domain_middleware(
    request: Request, call_next: RequestResponseEndpoint
) -> Response:
    """Host-based filtering, CORS, security headers & welcome endpoint for the public API domain.

    When public_api_host is set and the request's Host header matches it:
    - Only /, /health, and /api/v1/public/* are reachable; all others → 404
    - OPTIONS preflight gets permissive CORS headers (API-key auth, any origin)
    - Responses include security headers (HSTS, X-Content-Type-Options, etc.)
    - GET / returns a welcome JSON with endpoint discovery
    """
    if not settings.public_api_host:
        return await call_next(request)

    host = request.headers.get("host", "").split(":")[0]
    if host != settings.public_api_host:
        return await call_next(request)

    path = request.url.path
    is_allowed = (
        path == "/"
        or path == "/health"
        or path.startswith("/api/v1/public/")
        or path.startswith("/api/v1/partner/")
        or path.startswith("/api/v1/webhooks/")
    )

    if not is_allowed:
        return JSONResponse(status_code=404, content={"detail": "Not found"})

    # Phase 4 — CORS preflight for public domain (any origin, API-key auth)
    if request.method == "OPTIONS":
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Authorization, Content-Type",
                "Access-Control-Max-Age": "86400",
            },
        )

    # Phase 3 — Root welcome endpoint
    response: Response
    if path == "/" and request.method == "GET":
        response = JSONResponse(
            content={
                "name": "Trajan Public API",
                "version": "1.0",
                "docs": "https://www.trajancloud.com/docs",
                "endpoints": {
                    "ticket_api": {
                        "create_ticket": "POST /api/v1/public/tickets/",
                        "interpret_ticket": "POST /api/v1/public/tickets/interpret",
                        "list_tickets": "GET /api/v1/public/tickets/",
                        "get_ticket": "GET /api/v1/public/tickets/{ticket_id}",
                    },
                    "embed_api": {
                        "pulse": "GET /api/v1/partner/org/pulse",
                        "products": "GET /api/v1/partner/org/products",
                        "product_config": "GET /api/v1/partner/org/products/{product_id}/config",
                        "shipped": "GET /api/v1/partner/org/shipped",
                        "contributors": "GET /api/v1/partner/org/contributors",
                        "changelog": "GET /api/v1/partner/org/changelog",
                        "narrative": "GET /api/v1/partner/org/narrative",
                    },
                },
            }
        )
    else:
        response = await call_next(request)

    # Phase 4 — CORS response header for non-preflight requests
    response.headers["Access-Control-Allow-Origin"] = "*"

    # Phase 6 — Security headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["X-Request-Id"] = str(uuid.uuid4())

    return response


@app.middleware("http")
async def request_cache_middleware(request: Request, call_next):
    """Clear request-scoped cache at the start of each request."""
    from app.core.request_cache import clear_request_cache

    clear_request_cache()
    return await call_next(request)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log HTTP requests, skipping OPTIONS preflight."""
    # Skip OPTIONS (CORS preflight) and health checks
    if request.method == "OPTIONS" or request.url.path == "/health":
        return await call_next(request)

    # Log the request
    response = await call_next(request)

    # Only log non-2xx or important endpoints
    path = request.url.path
    if response.status_code >= 400 or any(
        keyword in path for keyword in ["analyze", "generate", "sync", "import"]
    ):
        logger.info(f"{request.method} {path} → {response.status_code}")

    return response


# Include API routes
app.include_router(api_router)


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}
