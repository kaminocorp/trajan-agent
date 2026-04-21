"""Expected RLS policy coverage per RLS-enabled public table.

Drives the Phase C CI tripwire in
`tests/integration/test_rls_policy_coverage.py`: if any RLS-enabled table's
`pg_policies.cmd` set is narrower than the allowlist says it should be,
the class of bug fixed in v0.31.9 is regressing — a user-request code path
writing the table will fail with `InsufficientPrivilegeError` (surface as
500) for every INSERT/UPDATE/DELETE the policy doesn't permit.

The allowlist is a **lower bound**: `pg_policies` may define strictly more
policies than listed here (e.g., multiple narrow SELECT policies instead of
one broad one), but it must not define fewer verbs. Tighter exact-match
enforcement is out of scope — the goal is catching accidental drops /
newly-enabled RLS without write policies.

`ALL` in `pg_policies.cmd` is pre-expanded to the four verbs when the test
compares coverage, matching Postgres's policy-evaluation semantics.

Adding a new RLS-enabled table requires adding an entry here; the tripwire
fails CI until you do. This is intentional — it forces a conscious
decision about which verbs the new table's policies must cover.
"""

from __future__ import annotations

ALL_VERBS: frozenset[str] = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})


RLS_POLICY_ALLOWLIST: dict[str, frozenset[str]] = {
    # ─── Tenant-scoped, full CRUD coverage ────────────────────────────────
    # Each of these has per-verb policies predicated on org membership or
    # product access. Silent-zero failures here would break core workflows
    # immediately, so the existing integration suite already catches most
    # regressions; this tripwire is belt-and-braces.
    "app_info": ALL_VERBS,
    "changelog_commits": ALL_VERBS,
    "changelog_entries": ALL_VERBS,
    "code_edges": ALL_VERBS,
    "code_nodes": ALL_VERBS,
    "custom_doc_jobs": ALL_VERBS,
    "dashboard_shipped_summary": ALL_VERBS,
    "dashboard_stats_cache": ALL_VERBS,
    "document_sections": ALL_VERBS,
    "document_subsections": ALL_VERBS,
    "documents": ALL_VERBS,
    "github_app_installation_repos": ALL_VERBS,
    "github_app_installations": ALL_VERBS,
    "infra_components": ALL_VERBS,
    "org_digest_preferences": ALL_VERBS,
    "organization_members": ALL_VERBS,
    "organizations": ALL_VERBS,
    "product_access": ALL_VERBS,
    "product_api_keys": ALL_VERBS,
    "products": ALL_VERBS,
    "progress_summary": ALL_VERBS,
    "repositories": ALL_VERBS,
    "subscriptions": ALL_VERBS,
    "team_contributor_summary": ALL_VERBS,
    "user_preferences": ALL_VERBS,
    "users": ALL_VERBS,
    "work_items": ALL_VERBS,
    # ─── Shared public caches: INSERT + SELECT ───────────────────────────
    # commit_stats_cache is keyed by (repository_full_name, commit_sha).
    # SHAs are immutable so UPDATE has no semantic meaning; cache entries
    # are never deleted (no TTL). If that changes, add the verb here AND
    # ship the matching policy in one migration.
    "commit_stats_cache": frozenset({"INSERT", "SELECT"}),
    # ─── Write-once / append-only audit records ─────────────────────────
    # billing_events is the immutable audit log for subscription activity.
    # UPDATE/DELETE on audit rows defeats the purpose; if a cleanup cron
    # ever appears it must run on `trajan_cron` (BYPASSRLS), not widen
    # the policy set here. The v0.31.8 incident (member INSERT policy
    # needed) expanded this from {SELECT} to {INSERT, SELECT}; further
    # expansion should be equally deliberate.
    "billing_events": frozenset({"INSERT", "SELECT"}),
    # referral_codes: one-time codes. App code has no edit/revoke path;
    # expired codes stay on the row for audit. If a revoke flow ever
    # ships, add UPDATE (soft-revoke) or DELETE here.
    "referral_codes": frozenset({"INSERT", "SELECT"}),
    # discount_redemptions: write-once. Creation happens at checkout,
    # deletion happens via org-cascade. No mutation path.
    "discount_redemptions": frozenset({"INSERT", "SELECT", "DELETE"}),
    # ─── Admin-managed via Supabase dashboard ───────────────────────────
    # discount_codes: managed by platform admins through the Supabase
    # dashboard (which connects as `postgres`, BYPASSRLS). No in-app
    # INSERT or DELETE path; UPDATE happens inline when a code is
    # redeemed (to increment `uses`). If an in-app admin UI appears,
    # add INSERT/DELETE here AND ship the matching admin-only policies.
    "discount_codes": frozenset({"SELECT", "UPDATE"}),
    # ─── Soft-revoke via UPDATE (no hard DELETE path in app code) ──────
    # feedback: tickets are archived via `status`, never DELETEd. If a
    # hard-delete button ever ships, add DELETE here + a matching
    # (admin-only) policy.
    "feedback": frozenset({"INSERT", "SELECT", "UPDATE"}),
    # org_api_keys: revocation sets `revoked_at` rather than DELETEing,
    # mirroring how `product_api_keys` worked pre-v0.31.9. If a hard-
    # delete path appears, parallel the `product_api_keys_admin_delete`
    # policy added in migration 36e4127be7d3.
    "org_api_keys": frozenset({"INSERT", "SELECT", "UPDATE"}),
    # ─── Admin-only / dormant tables ─────────────────────────────────
    # announcement: managed by platform admins on the Supabase dashboard.
    # No app code writes. If we ever build an in-app admin UI, that
    # migration needs to add INSERT/UPDATE/DELETE policies AND update
    # this entry in the same PR.
    "announcement": frozenset({"SELECT"}),
    # usage_snapshots: zero writers in the codebase as of v0.31.9. The
    # table was created for a billing metric that was never wired up.
    # If a monthly-aggregation cron ever ships it must run on
    # `trajan_cron` (BYPASSRLS); keep SELECT-only until then.
    "usage_snapshots": frozenset({"SELECT"}),
}
