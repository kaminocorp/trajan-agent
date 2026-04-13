"""API dependencies - re-exports from submodules for backwards compatibility."""

from .auth import (
    CurrentUser,
    DbSession,
    get_current_user,
    get_current_user_optional,
    get_db_with_rls,
    get_jwks,
    get_signing_key,
    security,
)
from .feature_gates import (
    FeatureGate,
    SubscriptionContext,
    check_subscription_active,
    get_subscription_context,
    get_subscription_context_for_product,
    require_active_subscription,
    require_agent_enabled,
    require_product_subscription,
)
from .organization import (
    get_current_organization,
    require_org_admin,
    require_org_owner,
    require_system_admin,
)
from .org_api_key_auth import (
    get_org_api_key,
    require_partner_scope,
)
from .product_access import (
    ProductAccessContext,
    check_product_admin_access,
    check_product_editor_access,
    check_product_viewer_access,
    get_product_access,
    require_product_admin,
    require_product_editor,
    require_variables_access,
)

__all__ = [
    # Auth
    "security",
    "get_jwks",
    "get_signing_key",
    "get_current_user",
    "get_current_user_optional",
    "get_db_with_rls",
    "DbSession",
    "CurrentUser",
    # Organization
    "get_current_organization",
    "require_org_admin",
    "require_org_owner",
    "require_system_admin",
    # Feature gates
    "SubscriptionContext",
    "check_subscription_active",
    "get_subscription_context",
    "get_subscription_context_for_product",
    "FeatureGate",
    "require_active_subscription",
    "require_agent_enabled",
    "require_product_subscription",
    # Partner API auth
    "get_org_api_key",
    "require_partner_scope",
    # Product access
    "ProductAccessContext",
    "get_product_access",
    "require_product_editor",
    "require_product_admin",
    "require_variables_access",
    "check_product_viewer_access",
    "check_product_editor_access",
    "check_product_admin_access",
]
