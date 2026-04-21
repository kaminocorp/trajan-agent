"""Partner Dashboard API — per-product config endpoint.

Returns repositories, infrastructure components, and info variable keys
for a specific product. Never returns variable values.

Bypass-then-scope: handler receives a :class:`PartnerAuthContext` and opens
its own ``async_session_maker`` session with RLS context set to
``ctx.effective_user_id`` before any RLS-protected read.
"""

import uuid as uuid_pkg

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.api.deps.org_api_key_auth import PartnerAuthContext
from app.core.database import async_session_maker
from app.core.rls import set_rls_user_context
from app.models.app_info import AppInfo
from app.models.infra_component import InfraComponent
from app.models.product import Product
from app.models.repository import Repository

from .partner import get_partner_key
from .partner_schemas import (
    ConfigInfoVariable,
    ConfigInfraComponent,
    ConfigRepository,
    ProductConfigResponse,
)

router = APIRouter(
    prefix="/partner/org/products",
    tags=["partner"],
)


@router.get("/{product_id}/config", response_model=ProductConfigResponse)
async def get_product_config(
    product_id: uuid_pkg.UUID,
    ctx: PartnerAuthContext = Depends(get_partner_key),
) -> ProductConfigResponse:
    """Per-product configuration: repos, infrastructure, and variable keys.

    Security: info variables return keys and descriptions only — never values.
    The value column is excluded from the SQL SELECT as defence-in-depth.
    """

    org_id = ctx.api_key.organization_id

    async with async_session_maker() as db:
        await set_rls_user_context(db, ctx.effective_user_id)

        # Verify product belongs to this org
        product_stmt = select(Product.id, Product.name).where(  # type: ignore[call-overload]
            Product.id == product_id,
            Product.organization_id == org_id,
        )
        product_result = await db.execute(product_stmt)
        product_row = product_result.one_or_none()
        if not product_row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Product not found",
            )

        product_name = product_row[1]

        # Repositories — all fields needed
        repo_stmt = select(Repository).where(
            Repository.product_id == product_id  # type: ignore[arg-type]
        )
        repo_result = await db.execute(repo_stmt)
        repos = list(repo_result.scalars().all())

        # Infrastructure components — ordered by display_order
        infra_stmt = (
            select(InfraComponent)
            .where(InfraComponent.product_id == product_id)  # type: ignore[arg-type]
            .order_by(
                InfraComponent.display_order,  # type: ignore[arg-type]
                InfraComponent.created_at,  # type: ignore[arg-type]
            )
        )
        infra_result = await db.execute(infra_stmt)
        infra_components = list(infra_result.scalars().all())

        # Info variables — SELECT only key, description, category, tags (never value)
        info_stmt = select(  # type: ignore[call-overload]
            AppInfo.key,
            AppInfo.description,
            AppInfo.category,
            AppInfo.tags,
        ).where(AppInfo.product_id == product_id)
        info_result = await db.execute(info_stmt)
        info_rows = info_result.all()

        return ProductConfigResponse(
            product_id=product_id,
            product_name=product_name,
            repositories=[
                ConfigRepository(
                    id=r.id,
                    name=r.name,
                    full_name=r.full_name,
                    url=r.url,
                    language=r.language,
                    default_branch=r.default_branch,
                    is_private=r.is_private,
                    stars_count=r.stars_count,
                    forks_count=r.forks_count,
                )
                for r in repos
            ],
            infrastructure=[
                ConfigInfraComponent(
                    id=ic.id,
                    name=ic.name,
                    type=ic.component_type,
                    provider=ic.provider,
                    url=ic.url,
                    region=ic.region,
                    description=ic.description,
                )
                for ic in infra_components
            ],
            info_variables=[
                ConfigInfoVariable(
                    key=row[0],
                    description=row[1],
                    category=row[2],
                    tags=row[3] or [],
                )
                for row in info_rows
            ],
        )
