"""Digest email job (daily and weekly).

Sends progress summaries to users who opted in. Fires every hour;
filters to users whose local time matches their configured digest_hour.
Weekly digests run on the configured digest day only; daily digests run
every day. Reads pre-cached ProgressSummary and DashboardShippedSummary
data — zero AI cost at send time.

Since v0.16.15 this job iterates OrgDigestPreference rows (one per user
per org) instead of global UserPreferences, and enforces product-level
access. Each org produces a separate email.
"""

import logging
import time
import uuid as uuid_pkg
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import escape as html_escape
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.domain.dashboard_shipped_operations import dashboard_shipped_ops
from app.domain.org_digest_preference_operations import org_digest_preference_ops
from app.domain.organization_operations import organization_ops
from app.domain.product_access_operations import product_access_ops
from app.domain.progress_summary_operations import progress_summary_ops
from app.models.org_digest_preference import OrgDigestPreference
from app.models.organization import Organization
from app.models.product import Product
from app.models.user import User
from app.services.email.postmark import postmark_service

logger = logging.getLogger(__name__)

# Period and subject configuration per frequency
FREQUENCY_CONFIG: dict[str, dict[str, str]] = {
    "weekly": {
        "period": "7d",
        "subject_single": "Weekly: {product}",
        "subject_all": "Weekly Progress — {org}",
        "heading": "Weekly Progress",
        "subheading": "Your projects this week",
        "unsubscribe_reason": "weekly digests",
    },
    "daily": {
        "period": "1d",
        "subject_single": "Daily Progress: {product}",
        "subject_all": "Daily Progress — {org}",
        "heading": "Daily Progress",
        "subheading": "Your projects today",
        "unsubscribe_reason": "daily digests",
    },
}

# Backwards compat alias
PERIOD = "7d"


@dataclass
class WeeklyDigestReport:
    """Result summary for logging and the manual trigger endpoint."""

    users_checked: int = 0
    users_emailed: int = 0
    emails_sent: int = 0
    errors: int = 0
    duration_seconds: float = 0.0
    skipped_reasons: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class DigestTarget:
    """Immutable work unit produced by :func:`enumerate_digest_eligible_users`.

    Carries only primitives so it crosses the bootstrap/scoped session
    boundary safely — ORM objects don't travel between sessions.
    The caller of :func:`send_digest_for_user` must set RLS context
    to ``user_id`` on the scoped session before invoking.
    """

    user_id: uuid_pkg.UUID
    organization_id: uuid_pkg.UUID
    user_email: str
    org_name: str
    org_role: str
    digest_product_ids: tuple[str, ...] | None  # None ⇒ all org products


@dataclass
class DigestEnumeration:
    """Bootstrap output from :func:`enumerate_digest_eligible_users`."""

    total_checked: int = 0
    eligible: list[DigestTarget] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Email template
# ---------------------------------------------------------------------------

CATEGORY_LABELS = {
    "feature": "New",
    "fix": "Fix",
    "improvement": "Improved",
    "refactor": "Refactor",
    "docs": "Docs",
    "infra": "Infra",
    "security": "Security",
}


def _build_progress_review_html(contributor_summaries: list[dict]) -> str:
    """Render the per-contributor progress review section for the HTML email.

    Each contributor gets a name heading, AI summary paragraph, and commit ref badges.
    Capped at 5 contributors with overflow indicator.
    """
    if not contributor_summaries:
        return ""

    blocks: list[str] = []
    display = contributor_summaries[:5]

    for contrib in display:
        name = html_escape(contrib.get("name", "Unknown"))
        summary = html_escape(contrib.get("summary_text", ""))
        commit_count = contrib.get("commit_count", 0)
        refs = contrib.get("commit_refs", [])

        # Commit ref badges (show up to 3)
        ref_badges = ""
        for ref in refs[:3]:
            sha = html_escape(ref.get("sha", "")[:7])
            if sha:
                ref_badges += (
                    f'<span style="display:inline-block;background:#f0fdf4;color:#166534;'
                    f"font-size:10px;font-family:monospace;padding:1px 5px;"
                    f'border-radius:3px;margin-right:4px;">{sha}</span>'
                )

        commit_label = f"{commit_count} commit{'s' if commit_count != 1 else ''}"
        blocks.append(
            f'<div style="margin-bottom:10px;">'
            f'<p style="font-size:13px;font-weight:600;color:#1e293b;margin:0 0 2px;">'
            f'{name} <span style="font-weight:400;color:#94a3b8;font-size:11px;">'
            f"({commit_label})</span></p>"
            f'<p style="font-size:13px;color:#475569;margin:0 0 4px;line-height:1.4;">'
            f"{summary}</p>"
            f"{ref_badges}"
            f"</div>"
        )

    overflow = ""
    if len(contributor_summaries) > 5:
        extra = len(contributor_summaries) - 5
        overflow = (
            f'<p style="font-size:12px;color:#94a3b8;margin:4px 0 0;">'
            f"… and {extra} more contributor{'s' if extra != 1 else ''}</p>"
        )

    return (
        f'<div style="margin:12px 0;padding:10px 0;border-top:1px solid #e2e8f0;">'
        f'<p style="font-size:12px;font-weight:600;color:#94a3b8;'
        f'text-transform:uppercase;letter-spacing:0.05em;margin:0 0 8px;">'
        f"Progress Review</p>"
        f"{''.join(blocks)}"
        f"{overflow}"
        f"</div>"
    )


def _build_product_html(
    product_name: str,
    narrative: str,
    items: list[dict],
    contributor_summaries: list[dict] | None = None,
) -> str:
    """Render one product block for the HTML email."""
    items_html = ""
    for item in items[:8]:  # Cap at 8 items to keep email compact
        cat = item.get("category", "")
        label = CATEGORY_LABELS.get(cat, cat.capitalize()) if cat else ""
        badge = (
            f'<span style="display:inline-block;background:#f1f5f9;color:#475569;'
            f'font-size:11px;padding:1px 6px;border-radius:3px;margin-right:6px;">'
            f"{label}</span>"
            if label
            else ""
        )
        items_html += f'<li style="margin:4px 0;font-size:14px;color:#334155;">{badge}{html_escape(item.get("description", ""))}</li>\n'

    overflow = ""
    if len(items) > 8:
        overflow = f'<p style="font-size:12px;color:#94a3b8;margin:4px 0 0;">… and {len(items) - 8} more</p>'

    # Progress review section (between narrative and shipped items)
    progress_review = _build_progress_review_html(contributor_summaries or [])

    return f"""
    <div style="margin-bottom:24px;">
      <h2 style="font-size:16px;font-weight:600;color:#0f172a;margin:0 0 6px;">{html_escape(product_name)}</h2>
      <p style="font-size:14px;color:#475569;margin:0 0 10px;line-height:1.5;">{html_escape(narrative)}</p>
      {progress_review}
      {f'<ul style="list-style:none;padding:0;margin:0 0 4px;">{items_html}</ul>{overflow}' if items_html else ""}
    </div>"""


def _build_email_html(
    product_sections: list[str],
    frontend_url: str,
    frequency: str = "weekly",
) -> str:
    """Build the full HTML email body from pre-rendered product sections."""
    config = FREQUENCY_CONFIG.get(frequency, FREQUENCY_CONFIG["weekly"])
    heading = config["heading"]
    subheading = config["subheading"]
    unsub_reason = config["unsubscribe_reason"]
    sections_html = "\n".join(product_sections)

    html_body = f"""\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <div style="max-width:480px;margin:0 auto;padding:24px 16px;">
    <div style="margin-bottom:20px;">
      <h1 style="font-size:18px;font-weight:700;color:#0f172a;margin:0;">{heading}</h1>
      <p style="font-size:13px;color:#94a3b8;margin:4px 0 0;">{subheading}</p>
    </div>
    <div style="background:#ffffff;border:1px solid #e2e8f0;border-radius:8px;padding:20px;">
{sections_html}
    </div>
    <div style="margin-top:16px;text-align:center;">
      <a href="{frontend_url}" style="font-size:13px;color:#c2410c;text-decoration:none;">Open Trajan</a>
      <p style="font-size:11px;color:#cbd5e1;margin:8px 0 0;">
        You're receiving this because you enabled {unsub_reason}.
        <a href="{frontend_url}/settings/notifications" style="color:#94a3b8;">Unsubscribe</a>
      </p>
    </div>
  </div>
</body>
</html>"""

    return html_body


def _build_plain_text(
    product_data: list[tuple[str, str, list[dict], list[dict] | None]],
    frequency: str = "weekly",
) -> str:
    """Build a plain-text fallback from the same data."""
    config = FREQUENCY_CONFIG.get(frequency, FREQUENCY_CONFIG["weekly"])
    lines = [f"{config['heading']} — {config['subheading']}", "=" * 44, ""]

    for name, narrative, items, contrib_summaries in product_data:
        lines.append(f"## {name}")
        lines.append(narrative)

        # Contributor progress review
        if contrib_summaries:
            lines.append("")
            lines.append("Progress Review:")
            for contrib in contrib_summaries[:5]:
                cname = contrib.get("name", "Unknown")
                csummary = contrib.get("summary_text", "")
                ccount = contrib.get("commit_count", 0)
                lines.append(f"  {cname} ({ccount} commits)")
                lines.append(f"    {csummary}")
            if len(contrib_summaries) > 5:
                extra = len(contrib_summaries) - 5
                lines.append(f"  … and {extra} more contributors")

        # Shipped items
        if items:
            lines.append("")
            for item in items[:8]:
                cat = item.get("category", "")
                prefix = f"[{cat}] " if cat else ""
                lines.append(f"  - {prefix}{item.get('description', '')}")
            if len(items) > 8:
                lines.append(f"  … and {len(items) - 8} more")

        lines.append("")

    lines.append(f"Open Trajan: {settings.frontend_url}")
    lines.append(f"Unsubscribe: {settings.frontend_url}/settings/notifications")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core job logic
# ---------------------------------------------------------------------------


async def _get_org_products(
    db: AsyncSession,
    organization_id: uuid_pkg.UUID,
) -> list[Product]:
    """Get all products belonging to a single organization."""
    stmt = select(Product).where(
        Product.organization_id == organization_id  # type: ignore[arg-type]
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _filter_accessible_products(
    db: AsyncSession,
    products: list[Product],
    user_id: uuid_pkg.UUID,
    org_role: str,
) -> list[Product]:
    """Filter products to only those the user has access != 'none'."""
    if not products:
        return []
    product_ids = [p.id for p in products]
    access_map = await product_access_ops.get_effective_access_bulk(
        db, product_ids, user_id, org_role
    )
    return [p for p in products if access_map.get(p.id, "viewer") != "none"]


async def _send_digest_for_org(
    db: AsyncSession,
    pref: OrgDigestPreference,
    user_email: str,
    org_name: str,
    org_role: str,
    report: WeeklyDigestReport,
    frequency: str = "weekly",
) -> bool:
    """Build and send the digest email for a single user + org pair.

    Returns True if at least one email was sent.
    """
    config = FREQUENCY_CONFIG.get(frequency, FREQUENCY_CONFIG["weekly"])
    period = config["period"]

    # Fetch org products and enforce product-level access
    all_products = await _get_org_products(db, pref.organization_id)
    accessible = await _filter_accessible_products(
        db, all_products, pref.user_id, org_role
    )

    # Apply user's product filter on top of access-checked list
    if pref.digest_product_ids:
        selected = {str(pid) for pid in pref.digest_product_ids}
        products = [p for p in accessible if str(p.id) in selected]
    else:
        products = accessible

    if not products:
        report.skipped_reasons["no_products"] = (
            report.skipped_reasons.get("no_products", 0) + 1
        )
        return False

    # Gather cached progress data per product
    product_data: list[tuple[str, str, list[dict], list[dict] | None]] = []

    for product in products:
        summary = await progress_summary_ops.get_by_product_period(db, product.id, period)
        shipped = await dashboard_shipped_ops.get_by_product_period(db, product.id, period)

        narrative = summary.summary_text if summary else ""
        items = shipped.items if shipped and shipped.has_significant_changes else []
        contrib_summaries = summary.contributor_summaries if summary else None

        if not narrative and not items:
            continue

        product_data.append((
            product.name or "Untitled Project",
            narrative,
            items,
            contrib_summaries,
        ))

    if not product_data:
        report.skipped_reasons["no_activity"] = (
            report.skipped_reasons.get("no_activity", 0) + 1
        )
        return False

    # Build one consolidated email per org
    sections = [
        _build_product_html(name, narrative, items, contribs)
        for name, narrative, items, contribs in product_data
    ]
    html_body = _build_email_html(sections, settings.frontend_url, frequency)
    text_body = _build_plain_text(product_data, frequency)

    subject = config["subject_all"].format(org=org_name)

    sent = await postmark_service.send(
        to=user_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
    )
    if sent:
        report.emails_sent += 1
        return True
    else:
        report.errors += 1
        return False


async def enumerate_digest_eligible_users(
    cron_db: AsyncSession,
    frequency: str,
) -> DigestEnumeration:
    """Bootstrap half: find every ``OrgDigestPreference`` whose owner's
    local time currently matches their configured digest hour.

    Runs on ``cron_session_maker`` (BYPASSRLS) — cron has no tenant
    user identity at entry, and the digest surface legitimately needs
    to enumerate across every opted-in user+org pair.

    Weekly digests additionally require today's weekday to match
    ``settings.weekly_digest_day`` in the user's own timezone. Daily
    digests fire every day at the configured hour.

    The returned :class:`DigestTarget` tuples carry every primitive
    the scoped half needs — user email, org name, and the org role
    used for product-access checks — so the per-user session never
    has to cross-reference ``users`` / ``organizations`` /
    ``organization_members`` under RLS just to set up the send.
    """
    enumeration = DigestEnumeration()

    all_prefs = await org_digest_preference_ops.get_all_active_for_frequency(
        cron_db, frequency
    )
    enumeration.total_checked = len(all_prefs)
    logger.info(
        f"[{frequency}-digest] Bootstrap: {len(all_prefs)} org-preferences "
        f"with {frequency} digest enabled"
    )

    if not all_prefs:
        return enumeration

    # Timezone + hour filter (per-user)
    now_utc = datetime.now(UTC)
    target_day = settings.weekly_digest_day  # e.g. "fri"

    prefs_in_window: list[OrgDigestPreference] = []
    for pref in all_prefs:
        try:
            tz = ZoneInfo(pref.digest_timezone or "UTC")
        except (KeyError, ValueError):
            tz = ZoneInfo("UTC")
        local_now = now_utc.astimezone(tz)
        hour_match = local_now.hour == (
            pref.digest_hour if pref.digest_hour is not None else 17
        )

        if frequency == "weekly":
            day_abbrev = local_now.strftime("%a").lower()
            if day_abbrev == target_day and hour_match:
                prefs_in_window.append(pref)
        elif frequency == "daily" and hour_match:
            prefs_in_window.append(pref)

    logger.info(
        f"[{frequency}-digest] {len(prefs_in_window)} org-preferences "
        f"eligible for current hour"
    )

    if not prefs_in_window:
        return enumeration

    # Batch-load users + orgs on the bootstrap session (single query each)
    user_ids = {pref.user_id for pref in prefs_in_window}
    org_ids = {pref.organization_id for pref in prefs_in_window}

    user_result = await cron_db.execute(
        select(User).where(User.id.in_(user_ids))  # type: ignore[attr-defined]
    )
    users_by_id: dict[uuid_pkg.UUID, User] = {
        u.id: u for u in user_result.scalars().all()
    }

    org_result = await cron_db.execute(
        select(Organization).where(
            Organization.id.in_(org_ids)  # type: ignore[attr-defined]
        )
    )
    orgs_by_id: dict[uuid_pkg.UUID, Organization] = {
        o.id: o for o in org_result.scalars().all()
    }

    # Build DigestTargets with role resolution per (user_id, org_id).
    # Role resolution goes through ``organization_ops.get_member_role``
    # which caches per-request — cheap enough without a dedicated batch.
    for pref in prefs_in_window:
        user = users_by_id.get(pref.user_id)
        if not user or not user.email:
            continue
        org = orgs_by_id.get(pref.organization_id)
        if not org:
            continue

        role = await organization_ops.get_member_role(
            cron_db, pref.organization_id, pref.user_id
        )
        if role is None:
            continue

        role_str = role.value if hasattr(role, "value") else str(role)

        product_filter: tuple[str, ...] | None = None
        if pref.digest_product_ids:
            product_filter = tuple(str(pid) for pid in pref.digest_product_ids)

        enumeration.eligible.append(
            DigestTarget(
                user_id=pref.user_id,
                organization_id=pref.organization_id,
                user_email=user.email,
                org_name=org.name,
                org_role=role_str,
                digest_product_ids=product_filter,
            )
        )

    return enumeration


async def send_digest_for_user(
    db: AsyncSession,
    target: DigestTarget,
    frequency: str,
    report: WeeklyDigestReport,
) -> bool:
    """Scoped half: build and send the digest for one user+org pair.

    **Contract:** caller has already set RLS context to
    ``target.user_id`` on ``db``. All reads below
    (``products``, ``product_access``, ``progress_summaries``,
    ``dashboard_shipped_summaries``) go through the user's own RLS
    view — which is exactly what privacy requires.

    Returns True iff an email was actually dispatched.
    """
    config = FREQUENCY_CONFIG.get(frequency, FREQUENCY_CONFIG["weekly"])
    period = config["period"]

    all_products = await _get_org_products(db, target.organization_id)
    accessible = await _filter_accessible_products(
        db, all_products, target.user_id, target.org_role
    )

    if target.digest_product_ids is not None:
        selected = set(target.digest_product_ids)
        products = [p for p in accessible if str(p.id) in selected]
    else:
        products = accessible

    if not products:
        report.skipped_reasons["no_products"] = (
            report.skipped_reasons.get("no_products", 0) + 1
        )
        return False

    product_data: list[tuple[str, str, list[dict], list[dict] | None]] = []
    for product in products:
        summary = await progress_summary_ops.get_by_product_period(
            db, product.id, period
        )
        shipped = await dashboard_shipped_ops.get_by_product_period(
            db, product.id, period
        )

        narrative = summary.summary_text if summary else ""
        items = shipped.items if shipped and shipped.has_significant_changes else []
        contrib_summaries = summary.contributor_summaries if summary else None

        if not narrative and not items:
            continue

        product_data.append((
            product.name or "Untitled Project",
            narrative,
            items,
            contrib_summaries,
        ))

    if not product_data:
        report.skipped_reasons["no_activity"] = (
            report.skipped_reasons.get("no_activity", 0) + 1
        )
        return False

    sections = [
        _build_product_html(name, narrative, items, contribs)
        for name, narrative, items, contribs in product_data
    ]
    html_body = _build_email_html(sections, settings.frontend_url, frequency)
    text_body = _build_plain_text(product_data, frequency)
    subject = config["subject_all"].format(org=target.org_name)

    sent = await postmark_service.send(
        to=target.user_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
    )
    if sent:
        report.emails_sent += 1
        return True
    report.errors += 1
    return False


async def send_digests(
    db: AsyncSession,
    frequency: str = "weekly",
) -> WeeklyDigestReport:
    """Single-session wrapper — **test/dev use only**.

    Production cron goes through :func:`scheduler._run_digest`, which
    opens ``cron_session_maker`` for enumeration and a fresh
    RLS-scoped session per digest recipient. This wrapper does both
    halves on one session: fine under ``postgres`` BYPASSRLS or
    mocked tests, but once Fly cuts over to ``trajan_app`` the
    enumeration will return zero rows silently unless the caller has
    already set an RLS context that admits
    ``OrgDigestPreference`` SELECTs.

    Queries OrgDigestPreference rows, filters to users whose local time
    matches their configured digest_hour, then sends one email per org
    with product-level access enforcement. For weekly: only on the
    configured digest day. For daily: every day.
    """
    label = f"{frequency}-digest"
    report = WeeklyDigestReport()
    start = time.monotonic()

    if not settings.postmark_enabled:
        logger.warning(f"[{label}] Skipped — Postmark not configured")
        report.duration_seconds = time.monotonic() - start
        return report

    # Find all org preferences with this frequency enabled
    all_prefs = await org_digest_preference_ops.get_all_active_for_frequency(
        db, frequency
    )

    report.users_checked = len(all_prefs)
    logger.info(
        f"[{label}] Found {len(all_prefs)} org-preferences "
        f"with {frequency} digest enabled"
    )

    # Filter to prefs whose local time matches right now
    now_utc = datetime.now(UTC)
    target_day = settings.weekly_digest_day  # e.g. "fri"

    eligible: list[OrgDigestPreference] = []
    for pref in all_prefs:
        try:
            tz = ZoneInfo(pref.digest_timezone or "UTC")
        except (KeyError, ValueError):
            tz = ZoneInfo("UTC")
        local_now = now_utc.astimezone(tz)
        hour_match = local_now.hour == (
            pref.digest_hour if pref.digest_hour is not None else 17
        )

        if frequency == "weekly":
            day_abbrev = local_now.strftime("%a").lower()
            if day_abbrev == target_day and hour_match:
                eligible.append(pref)
        elif frequency == "daily" and hour_match:
            eligible.append(pref)

    logger.info(f"[{label}] {len(eligible)} org-preferences eligible for current hour")

    if not eligible:
        report.duration_seconds = round(time.monotonic() - start, 2)
        return report

    # Batch-load users and orgs to avoid N+1 queries
    user_ids = {pref.user_id for pref in eligible}
    org_ids = {pref.organization_id for pref in eligible}

    user_stmt = select(User).where(User.id.in_(user_ids))  # type: ignore[attr-defined]
    user_result = await db.execute(user_stmt)
    users_by_id: dict[uuid_pkg.UUID, User] = {
        u.id: u for u in user_result.scalars().all()
    }

    org_stmt = select(Organization).where(
        Organization.id.in_(org_ids)  # type: ignore[attr-defined]
    )
    org_result = await db.execute(org_stmt)
    orgs_by_id: dict[uuid_pkg.UUID, Organization] = {
        o.id: o for o in org_result.scalars().all()
    }

    # Track which users actually received at least one email
    users_emailed: set[uuid_pkg.UUID] = set()

    async with postmark_service.batch():
        for pref in eligible:
            user = users_by_id.get(pref.user_id)
            if not user or not user.email:
                report.skipped_reasons["no_email"] = (
                    report.skipped_reasons.get("no_email", 0) + 1
                )
                continue

            org = orgs_by_id.get(pref.organization_id)
            if not org:
                report.skipped_reasons["no_org"] = (
                    report.skipped_reasons.get("no_org", 0) + 1
                )
                continue

            # Resolve the user's role in this org for access checks
            role = await organization_ops.get_member_role(
                db, pref.organization_id, pref.user_id
            )
            if role is None:
                report.skipped_reasons["not_member"] = (
                    report.skipped_reasons.get("not_member", 0) + 1
                )
                continue

            try:
                sent = await _send_digest_for_org(
                    db,
                    pref,
                    user.email,
                    org.name,
                    role.value if hasattr(role, "value") else str(role),
                    report,
                    frequency,
                )
                if sent:
                    users_emailed.add(pref.user_id)
            except Exception:
                logger.exception(
                    f"[{label}] Error processing user {pref.user_id} "
                    f"org {pref.organization_id}"
                )
                report.errors += 1

    report.users_emailed = len(users_emailed)
    report.duration_seconds = round(time.monotonic() - start, 2)

    logger.info(
        f"[{label}] Done: {report.users_emailed} users emailed, "
        f"{report.emails_sent} emails sent, {report.errors} errors, "
        f"{report.duration_seconds}s"
    )

    return report


async def send_weekly_digests(db: AsyncSession) -> WeeklyDigestReport:
    """Backwards-compatible wrapper: send weekly digests only."""
    return await send_digests(db, frequency="weekly")
