"""``t3 doctor`` Notion-credential gate — absent, rejected, or shared onto nothing.

A box with no Notion token reads healthy right up until a headless read fails at
point of use, because nothing else asks. Three faults reach that same dead end by
different routes and are fixed three different ways, so the check reports which one
it found rather than a single "Notion is broken".

Scoped to overlays that DECLARE a ``notion_token_pass_key``: silent where Notion is
unrouted, a hard FAIL where it is expected.
"""

import dataclasses
from enum import StrEnum

import httpx
import typer

from teatree.utils.secrets import SecretStoreError


class NotionCredentialState(StrEnum):
    OK = "ok"
    ABSENT = "absent"
    REJECTED = "rejected"
    SHARED_ONTO_NOTHING = "shared_onto_nothing"
    UNREACHABLE = "unreachable"


#: One diagnosis per fault, each naming its OWN fix — minting a token, replacing a
#: rejected one and sharing a page with a working integration are three different acts.
DIAGNOSES: dict[NotionCredentialState, str] = {
    NotionCredentialState.ABSENT: (
        "no token resolves from $NOTION_TOKEN or `pass {pass_key}`, so every Notion read fails at "
        "point of use. Mint and store one: `t3 notion setup --overlay {overlay}`."
    ),
    NotionCredentialState.REJECTED: (
        "the stored token is invalid, revoked, or belongs to a deleted integration ({detail}). "
        "Re-mint it: `t3 notion setup --overlay {overlay} --reset`."
    ),
    NotionCredentialState.SHARED_ONTO_NOTHING: (
        "the token authenticates ({detail}) but NOTHING is shared with it, so every read answers 404 "
        "as if the page did not exist. Open each page in Notion, use ... -> Connections to add the "
        "integration, then re-check with `t3 notion doctor <page-url> --overlay {overlay}`."
    ),
    NotionCredentialState.UNREACHABLE: (
        "the credential could not be probed ({detail}). A probe that FAILED is not a credential that "
        "is absent — fix the fault rather than reading this as configured."
    ),
}


@dataclasses.dataclass(frozen=True, slots=True)
class NotionCredential:
    """One overlay's Notion routing, probed end to end."""

    overlay: str
    pass_key: str
    state: NotionCredentialState
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.state is NotionCredentialState.OK

    def line(self) -> str:
        diagnosis = DIAGNOSES[self.state].format(overlay=self.overlay, pass_key=self.pass_key, detail=self.detail)
        return f"FAIL  Notion [{self.overlay}]: {diagnosis}"


def notion_routed_overlays() -> list[tuple[str, str]]:
    """``(overlay, pass key)`` for every registered overlay that routes a Notion token."""
    from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415 — deferred: needs apps

    return [
        (name, pass_key)
        for name, overlay in sorted(get_all_overlays().items())
        if (pass_key := overlay.config.secret_pass_key("notion_token"))
    ]


def probe_notion_credential(overlay: str, pass_key: str) -> NotionCredential:
    """Resolve, authenticate and search — the same three stages a headless read walks."""
    from teatree.backends.notion.credentials import build_notion_client  # noqa: PLC0415 — deferred: needs apps
    from teatree.backends.notion.errors import (  # noqa: PLC0415 — deferred: needs apps
        NotionBadTokenError,
        NotionError,
        NotionTokenMissingError,
    )

    def verdict(state: NotionCredentialState, detail: str = "") -> NotionCredential:
        return NotionCredential(overlay=overlay, pass_key=pass_key, state=state, detail=detail)

    try:
        client = build_notion_client(overlay)
        identity = client.describe_identity()
        shared = client.any_object_shared()
    except NotionTokenMissingError:
        return verdict(NotionCredentialState.ABSENT)
    except NotionBadTokenError as exc:
        return verdict(NotionCredentialState.REJECTED, str(exc))
    # ValueError: a 200 with a non-JSON body raises out of response.json(), killing every OTHER finding.
    except (NotionError, SecretStoreError, httpx.HTTPError, ValueError) as exc:
        return verdict(NotionCredentialState.UNREACHABLE, f"{exc.__class__.__name__}: {exc}")
    if not shared:
        return verdict(NotionCredentialState.SHARED_ONTO_NOTHING, identity)
    return verdict(NotionCredentialState.OK, identity)


def _check_notion_credentials() -> bool:
    """Hard-FAIL when an overlay routes a Notion token that does not work end to end."""
    findings = [
        credential
        for overlay, pass_key in notion_routed_overlays()
        if not (credential := probe_notion_credential(overlay, pass_key)).ok
    ]
    for credential in findings:
        typer.echo(credential.line())
    return not findings
