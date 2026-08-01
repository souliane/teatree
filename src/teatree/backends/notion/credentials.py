"""Resolve the Notion integration token for the active overlay, failing loud.

The domain-layer half of the credential seam: :class:`NotionTokenCredential` is
foundation-pure (env var, then a ``pass`` path), and this module supplies the
per-overlay ``pass`` entry as the injected override — exactly the split
``teatree.credential_config`` uses for the Anthropic accounts.

Resolution order, first non-empty wins:

1. the ``NOTION_TOKEN`` environment variable — a rotated value always beats a stale store entry;
2. the active overlay's ``NOTION_TOKEN_PASS_KEY`` entry in the ``pass`` store;
3. the default ``pass`` entry ``notion/integration-token``.

An unresolvable token raises :class:`~teatree.backends.notion.errors.NotionTokenMissingError`
naming the whole setup — including the part no code can do, which is sharing the
integration onto each page it must reach. The value is read at point of use and
never lands in argv, a config file, or the transcript.
"""

from django.core.exceptions import ImproperlyConfigured

from teatree.backends.notion.client import NotionClient, NotionTokenCredential
from teatree.backends.notion.errors import NotionTokenMissingError
from teatree.core.overlay_loader import get_overlay
from teatree.llm.credentials import CredentialError

SETUP_HELP = (
    "Set up headless Notion access once:\n"
    "  1. create an internal integration at https://www.notion.so/profile/integrations "
    "with the read/update/insert-content and read-comment capabilities;\n"
    "  2. store its secret: `pass insert notion/integration-token` "
    "(or point NOTION_TOKEN_PASS_KEY at your own entry);\n"
    "  3. share every page and database it must reach WITH the integration "
    "(page ••• -> Connections -> add it) — an integration sees nothing until that grant exists."
)


def overlay_notion_pass_key(overlay_name: str | None = None) -> str:
    """The ``pass`` entry the active overlay routes its Notion token to, or ``""``."""
    try:
        overlay = get_overlay(overlay_name or None)
    except ImproperlyConfigured:
        return ""
    return overlay.config.secret_pass_key("notion_token")


def resolve_notion_token(overlay_name: str | None = None) -> str:
    """Return the Notion integration token, or raise :class:`NotionTokenMissingError`."""
    credential = NotionTokenCredential(pass_path_override=overlay_notion_pass_key(overlay_name) or None)
    try:
        return credential.resolve()
    except CredentialError as exc:
        msg = f"{exc}\n\n{SETUP_HELP}"
        raise NotionTokenMissingError(msg) from exc


def build_notion_client(overlay_name: str | None = None, *, version: str = "") -> NotionClient:
    """Build a token-authenticated client, failing loud when no token resolves.

    The fail-loud counterpart to
    :func:`teatree.core.backend_factory.notion_client_from_overlay`, which
    returns ``None`` on an absent token because its caller (the runtime status
    sync) is meant to no-op. A headless read or write must never no-op silently,
    so this raises instead.
    """
    token = resolve_notion_token(overlay_name)
    return NotionClient(token=token, version=version) if version else NotionClient(token=token)
