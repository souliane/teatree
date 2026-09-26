"""Resolve the Notion integration token for the active overlay, failing loud.

The domain-layer half of the credential seam: :class:`NotionTokenCredential` is
foundation-pure (env var, then a ``pass`` path), and this module supplies the
per-overlay ``pass`` entry as the injected override — exactly the split
``teatree.credential_config`` uses for the Anthropic accounts.

Resolution order, first non-empty wins:

1. the ``NOTION_TOKEN`` environment variable — a rotated value always beats a stale store entry;
2. the ``pass`` entry the ``notion_token_pass_key`` setting routes to on this venue.

There is no default entry. An unresolvable token raises :class:`~teatree.backends.notion.errors.NotionTokenMissingError`
naming the whole setup — including the part no code can do, which is sharing the
integration onto each page it must reach. The value is read at point of use and
never lands in argv, a config file, or the transcript.
"""

from teatree.backends.notion.client import NotionClient, NotionTokenCredential
from teatree.backends.notion.errors import NotionTokenMissingError
from teatree.core.overlays.overlay_credentials import overlay_pass_key
from teatree.llm.credentials import CredentialError

SETUP_HELP = (
    "Set up headless Notion access once:\n"
    "  1. create an internal integration at https://www.notion.so/profile/integrations "
    "with the read/update/insert-content and read-comment capabilities;\n"
    "  2. store its secret in `pass` and route the credential to that entry: "
    "`t3 <overlay> config_setting set notion_token_pass_key '\"<entry>\"' --overlay <overlay>` "
    "(or run `t3 setup`, which reports what is missing);\n"
    "  3. share every page and database it must reach WITH the integration "
    "(page ••• -> Connections -> add it) — an integration sees nothing until that grant exists."
)


def overlay_notion_pass_key(overlay_name: str | None = None) -> str:
    return overlay_pass_key("notion_token", overlay_name)


def resolve_notion_token(overlay_name: str | None = None) -> str:
    """Return the Notion integration token, or raise :class:`NotionTokenMissingError`."""
    pass_key = overlay_notion_pass_key(overlay_name)
    try:
        return NotionTokenCredential(pass_path_override=pass_key or None).resolve()
    except CredentialError as exc:
        found = f"`pass {pass_key}` is empty on this venue" if pass_key else "no notion_token_pass_key is configured"
        msg = f"no NOTION_TOKEN in the environment and {found}.\n\n{SETUP_HELP}"
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
    if version:
        return NotionClient(token=token, version=version, overlay=overlay_name)
    return NotionClient(token=token, overlay=overlay_name)
