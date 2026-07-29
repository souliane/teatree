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

Beyond the explicit ``SECRET_SETTINGS`` denylist (customer/brand keys a rule cannot
derive), two rule-driven classes are withheld from a shared export so no second
hand-kept list can drift out of date:

- **Credential coordinates** — a setting that names WHERE a secret lives (a ``pass``
    entry / token reference), matched by :func:`is_credential_reference`. This is the
    SAME suffix rule the dashboard's credential band uses, so export and dashboard
    agree on "this is a credential coordinate" from one source of truth.
- **Personal identifiers** — :data:`PERSONAL_IDENTIFIERS`, an operator's own account
    handles / channel / schedule that carry no brand term but are not shareable.
"""

import re

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

#: A credential-reference setting NAMES where a secret lives (a ``pass`` entry / token
#: reference), never the secret itself. Matched by suffix so a renamed reference keeps
#: its classification with no second registration. The single source of truth for
#: "is this a credential coordinate": the dashboard credential band AND the config
#: export withhold-set both resolve through it (F2).
CREDENTIAL_REFERENCE_RE = re.compile(r"(pass_path|pass_paths|pass_key|token_ref|credential_entry)$")

#: An operator's OWN personal identifiers — account handle, channel, schedule. They
#: carry no customer/brand term (so the banned-term scan misses them) and are not a
#: credential coordinate (so the suffix rule misses them), yet must not reach a shared
#: export. Kept explicit because there is no derivable rule for "this is personal" (F2).
PERSONAL_IDENTIFIERS: frozenset[str] = frozenset(
    {
        "availability_schedule",
        "slack_user_channel",
        "slack_user_id",
    }
)


def is_credential_reference(name: str) -> bool:
    """Whether *name* is a credential-coordinate setting (it names where a secret lives)."""
    return bool(CREDENTIAL_REFERENCE_RE.search(name))
