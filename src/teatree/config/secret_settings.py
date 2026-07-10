"""Settings whose VALUES are private (customer/brand secrets) — the export-leak guard.

These keys carry customer codenames, internal namespaces, or secret references that
must NEVER reach a SHARED config export (the future "auto-configure a fresh teatree
for a new user" path). The DB store itself is private to the operator, so a secret
LIVING in the DB is fine; the leak surface is the EXPORT, not the storage.

The guard is deliberately two complementary defenses (both required — neither alone
is sufficient):

1.  This explicit denylist — the per-setting ``private`` flag, expressed as set
    membership. ``config_migration.export_db_to_toml`` drops these keys by default.
2.  An active banned-term CONTENT scan over every exported key+value (in
    ``config_migration``), which catches a NON-listed key whose value happens to
    contain a customer/brand term (e.g. ``ban_close_trailers_on_namespaces =
    ['acme-engineering/*']``) — the case a static keylist can never enumerate.

``config_setting export`` filters both by default and WARNS what it dropped; the
``--include-private`` flag exports everything for a PERSONAL (never-shared) backup.
"""

# Membership == the ``private`` flag. Keep alphabetised.
SECRET_SETTINGS: frozenset[str] = frozenset(
    {
        "banned_brands",
        "banned_term_registry",
        "banned_terms",
        "banned_terms_allowlist",
        "github_token_pass_key",
        "internal_publish_namespaces",
        "overlay_leak_terms",
        "private_repos",
        "private_tests",
        "slack_token_ref",
        "user_token_ref",
    }
)
