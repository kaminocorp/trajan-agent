from fastapi import APIRouter

from app.api.v1 import (
    admin,
    agent,
    announcements,
    api_keys,
    app_info,
    changelog,
    documents,
    feedback,
    github,
    infra,
    integrations,
    internal,
    mcp,
    organizations,
    preferences,
    products,
    progress,
    public_tickets,
    repositories,
    timeline,
    users,
    webhooks,
    work_items,
)

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(products.router)
api_router.include_router(changelog.router)
api_router.include_router(repositories.router)
api_router.include_router(work_items.router)
api_router.include_router(documents.router)
api_router.include_router(app_info.router)
api_router.include_router(users.router)
api_router.include_router(preferences.router)
api_router.include_router(github.router)
api_router.include_router(organizations.router)
api_router.include_router(admin.router)
api_router.include_router(feedback.router)
api_router.include_router(timeline.router)
api_router.include_router(progress.router)
api_router.include_router(announcements.router)
api_router.include_router(agent.router)
api_router.include_router(api_keys.router)
api_router.include_router(infra.router)
api_router.include_router(public_tickets.router)
api_router.include_router(internal.router)
api_router.include_router(webhooks.router)
api_router.include_router(integrations.router)
api_router.include_router(mcp.router)
