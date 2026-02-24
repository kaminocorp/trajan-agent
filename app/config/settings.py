import logging

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Database - transaction pooled connection for app operations (port 6543)
    database_url: str = "postgresql+asyncpg://postgres:password@localhost:6543/postgres"
    # Database - direct connection for migrations (port 5432)
    database_url_direct: str = "postgresql+asyncpg://postgres:password@localhost:5432/postgres"

    # Supabase
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_jwks_url: str = ""  # JWKS endpoint for ES256 JWT verification
    supabase_service_role_key: str = ""  # Service role key for admin operations (user invites)

    # Application
    debug: bool = False
    cors_origins: list[str] = ["http://localhost:3000"]
    frontend_url: str = "http://localhost:3000"  # For constructing shareable URLs

    # Public API — custom domain for external-facing endpoints
    # Empty string = host-filtering middleware disabled (safe for local dev)
    public_api_host: str = ""

    # AI / Anthropic
    anthropic_api_key: str = ""

    # Security - Encryption key for sensitive data at rest (GitHub tokens, etc.)
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    token_encryption_key: str = ""

    # Stripe - Payment processing
    # Use sk_test_*/pk_test_* for development, sk_live_*/pk_live_* for production
    # Empty string = Stripe disabled (feature gating still works, just no payments)
    stripe_secret_key: str = ""
    stripe_publishable_key: str = ""
    stripe_webhook_secret: str = ""  # From `stripe listen` (dev) or Stripe Dashboard (prod)

    # Stripe Price IDs - Create these in Stripe Dashboard (test mode for dev, live for prod)
    # Tier base prices (monthly subscriptions)
    stripe_price_indie_base: str = ""
    stripe_price_pro_base: str = ""
    stripe_price_scale_base: str = ""
    # Single overage price for all tiers ($10/repo)
    stripe_price_repo_overage: str = ""

    # Stripe Meter ID for repo overage (from Stripe Dashboard)
    stripe_meter_id: str = ""

    # Stripe Coupon ID for referral program (100% off for 1 month)
    # If empty, will be created automatically on first use
    stripe_referral_coupon_id: str = ""

    # Internal API security — shared secret for cron-triggered endpoints
    # Generate with: python -c "import secrets; print(secrets.token_urlsafe(32))"
    cron_secret: str = ""

    # Postmark - Transactional email delivery
    # Empty string = Postmark disabled (no transactional emails sent)
    postmark_api_key: str = ""
    postmark_from_email: str = "hello@trajancloud.com"

    # Scheduler settings
    # Enable/disable the internal APScheduler (set False for local dev to avoid noise)
    scheduler_enabled: bool = True
    # Hour (UTC) to run auto-progress job (default: 6 AM UTC)
    auto_progress_hour: int = 6
    # Plan prompt email: how often to nudge orgs without a plan (days)
    plan_prompt_frequency_days: int = 3
    # Plan prompt email: hour (UTC) to check and send (default: 9 AM UTC)
    plan_prompt_email_hour: int = 9
    # Weekly digest: hour (UTC) to send (default: 17 = 5 PM UTC)
    weekly_digest_hour: int = 17
    # Weekly digest: day of week (default: fri)
    weekly_digest_day: str = "fri"

    @property
    def stripe_enabled(self) -> bool:
        """Check if Stripe is configured (has secret key)."""
        return bool(self.stripe_secret_key)

    @property
    def postmark_enabled(self) -> bool:
        """Check if Postmark is configured (has API key)."""
        return bool(self.postmark_api_key)

    @model_validator(mode="after")
    def validate_required_vars(self) -> "Settings":
        """Validate critical environment variables are set in production."""
        if self.debug:
            return self

        required = {
            "supabase_url": self.supabase_url,
            "supabase_jwks_url": self.supabase_jwks_url,
            "supabase_service_role_key": self.supabase_service_role_key,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(
                f"Missing required environment variables for production: {', '.join(missing)}. "
                f"Set debug=True to bypass this check in development."
            )

        if not self.token_encryption_key:
            logger.warning(
                "token_encryption_key is not set — sensitive tokens will be stored in plaintext"
            )

        return self


settings = Settings()
