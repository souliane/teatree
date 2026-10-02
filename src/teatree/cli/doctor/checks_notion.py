"""The Notion credential probe ``t3 doctor`` gates on and ``t3 setup`` reports.

A venue with no working Notion token reads healthy right up until a headless read fails
at point of use, because nothing else asks. Several faults reach that same dead end by
different routes and are fixed different ways, so the probe reports which one it found.
It reads the token from the venue it runs in, then checks every in-flight ticket page and
every configured database/write root the factory requires.

Scoped to overlays that need Notion (``required_third_party_services``) or route a
``notion_token_pass_key``: silent where Notion is unrouted, a hard FAIL where it is expected.
"""

import dataclasses
import functools
import os
from collections.abc import Callable, Iterator
from enum import StrEnum
from typing import TYPE_CHECKING

import httpx
import typer

from teatree.backends.types import Service
from teatree.config.credential_pass_key import pass_key_setting
from teatree.core.gates.notion_write_roots import notion_write_roots
from teatree.utils.secrets import SecretStoreError

if TYPE_CHECKING:  # pragma: no cover — deferred with the other app-dependent imports
    from teatree.backends.notion.client import NotionClient
    from teatree.core.overlay import OverlayConfig

_CREDENTIAL = "notion_token"


class NotionCredentialState(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    UNCONFIGURED = "unconfigured"
    ABSENT = "absent"
    REJECTED = "rejected"
    SHARED_ONTO_NOTHING = "shared_onto_nothing"
    UNGRANTED = "ungranted"
    UNREACHABLE = "unreachable"


#: One diagnosis per fault, each naming its OWN fix.
DIAGNOSES: dict[NotionCredentialState, str] = {
    NotionCredentialState.UNCONFIGURED: (
        "the overlay needs Notion but no token entry is routed on this venue ({detail}). Store the token in "
        "`pass`, then route the credential to that entry: "
        "`t3 {overlay} config_setting set {setting} '\"<entry>\"' --overlay {overlay}`."
    ),
    NotionCredentialState.ABSENT: (
        "no token resolves from $NOTION_TOKEN or `pass {pass_key}` on this venue, so every Notion read fails at "
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
    NotionCredentialState.UNGRANTED: (
        "the token authenticates ({detail}) but required pages/databases are not shared with it: "
        "{ungranted}. Add the integration under ... -> Connections on each object or its teamspace, then re-check with "
        "`t3 notion doctor <page-url> --overlay {overlay}`."
    ),
    NotionCredentialState.UNREACHABLE: (
        "the credential could not be probed ({detail}). A probe that FAILED is not a credential that "
        "is absent — fix the fault rather than reading this as configured."
    ),
}


@dataclasses.dataclass(frozen=True, slots=True)
class NotionCredential:
    """One overlay's Notion routing, probed end to end from the venue it runs in."""

    overlay: str
    pass_key: str
    state: NotionCredentialState
    detail: str = ""
    source: str = ""
    ungranted: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.state is NotionCredentialState.OK

    def line(self) -> str:
        if self.state in {NotionCredentialState.OK, NotionCredentialState.PARTIAL}:
            origin = "$NOTION_TOKEN" if not self.pass_key else f"`pass {self.pass_key}` ({self.source})"
            if self.state is NotionCredentialState.PARTIAL:
                return (
                    f"INFO  Notion [{self.overlay}]: {self.detail}, token from {origin}; "
                    "required page/database grants not checked — run `t3 doctor check`."
                )
            return f"OK    Notion [{self.overlay}]: {self.detail}, token from {origin}"
        diagnosis = DIAGNOSES[self.state].format(
            overlay=self.overlay,
            pass_key=self.pass_key,
            detail=self.detail,
            setting=pass_key_setting(_CREDENTIAL),
            ungranted=", ".join(self.ungranted),
        )
        return f"FAIL  Notion [{self.overlay}]: {diagnosis}"


def notion_routed_overlays() -> "list[tuple[str, OverlayConfig]]":
    """``(overlay, config)`` for every registered overlay that needs Notion or routes a token entry."""
    from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415 — deferred: needs apps

    return [
        (name, overlay.config)
        for name, overlay in sorted(get_all_overlays().items())
        if Service.NOTION in overlay.config.required_third_party_services or overlay.config.secret_pass_key(_CREDENTIAL)
    ]


def tracked_notion_pages(overlay: str) -> list[tuple[str, int]]:
    """``(page id, ticket pk)`` for every page an in-flight ticket tracks — of *overlay*, or of any when empty."""
    from teatree.backends.notion.errors import NotionError, normalize_object_id  # noqa: PLC0415 — deferred
    from teatree.core.models import Ticket  # noqa: PLC0415 — deferred: needs apps

    tickets = Ticket.objects.in_flight()
    pages: dict[str, int] = {}
    for pk, extra in (tickets.filter(overlay=overlay) if overlay else tickets).values_list("pk", "extra"):
        if url := (extra or {}).get("notion_url", ""):
            try:
                pages.setdefault(normalize_object_id(url), pk)
            except NotionError:
                continue
    return sorted(pages.items())


def _ungranted_pages(client: "NotionClient", overlay: str) -> Iterator[str]:
    from teatree.backends.notion.errors import NotionNotSharedError  # noqa: PLC0415 — deferred: needs apps

    for page_id, ticket_pk in tracked_notion_pages(overlay):
        try:
            client.get_page(page_id)
        except NotionNotSharedError:
            yield f"page {page_id} (ticket {ticket_pk})"


def _required_configured_objects(overlay: str, config: "OverlayConfig") -> list[tuple[str, str, str]]:
    """Authoritative configured Notion roots/databases that must be granted."""
    from teatree.backends.notion.errors import NotionError, normalize_object_id  # noqa: PLC0415 — deferred

    declared: list[tuple[str, str, str]] = []
    if database_id := config.notion_database_id:
        declared.append((database_id, "database {id} (notion_database_id)", "database"))
    allowed, _denied = notion_write_roots(overlay)
    declared.extend((root, "page/database {id} (notion_write_allowed_roots)", "block") for root in allowed)
    required: dict[str, tuple[str, str]] = {}
    for raw_id, label, kind in declared:
        try:
            object_id = normalize_object_id(raw_id)
        except NotionError:
            continue
        required.setdefault(object_id, (label.format(id=object_id), kind))
    return [(object_id, label, kind) for object_id, (label, kind) in required.items()]


def _ungranted_configured_objects(client: "NotionClient", overlay: str, config: "OverlayConfig") -> Iterator[str]:
    from teatree.backends.notion.errors import NotionNotSharedError  # noqa: PLC0415 — deferred

    for object_id, label, kind in _required_configured_objects(overlay, config):
        try:
            if kind == "database":
                client.get_database(object_id)
            else:
                try:
                    client.get_block(object_id)
                except NotionNotSharedError:
                    client.get_database(object_id)
        except NotionNotSharedError:
            yield label


def probe_notion_credential(
    overlay: str, config: "OverlayConfig", *, check_required_grants: bool = True
) -> NotionCredential:
    """Route, authenticate and check every required Notion object's grant."""
    from teatree.backends.notion.client import NotionTokenCredential  # noqa: PLC0415 — deferred: needs apps

    resolution = config.resolve_pass_key(_CREDENTIAL)
    from_env = bool(os.environ.get(NotionTokenCredential.spec.env_var))
    verdict = functools.partial(
        NotionCredential, overlay, "" if from_env else resolution.value, source=str(resolution.source)
    )
    if not resolution.value and not from_env:
        return verdict(NotionCredentialState.UNCONFIGURED, str(resolution.source))
    return _probe_the_api(overlay, config, verdict, check_required_grants=check_required_grants)


def _probe_the_api(
    overlay: str,
    config: "OverlayConfig",
    verdict: Callable[..., NotionCredential],
    *,
    check_required_grants: bool,
) -> NotionCredential:
    from teatree.backends.notion.credentials import build_notion_client  # noqa: PLC0415 — deferred: needs apps
    from teatree.backends.notion.errors import (  # noqa: PLC0415 — deferred: needs apps
        NotionBadTokenError,
        NotionError,
        NotionTokenMissingError,
    )

    try:
        client = build_notion_client(overlay)
        identity = client.describe_identity()
        shared = client.any_object_shared()
        ungranted = (
            (
                *tuple(_ungranted_pages(client, overlay)),
                *tuple(_ungranted_configured_objects(client, overlay, config)),
            )
            if shared and check_required_grants
            else ()
        )
    except NotionTokenMissingError:
        return verdict(NotionCredentialState.ABSENT)
    except NotionBadTokenError as exc:
        return verdict(NotionCredentialState.REJECTED, str(exc))
    # ValueError: a 200 with a non-JSON body raises out of response.json(), killing every OTHER finding.
    except (NotionError, SecretStoreError, httpx.HTTPError, ValueError) as exc:
        return verdict(NotionCredentialState.UNREACHABLE, f"{exc.__class__.__name__}: {exc}")
    if not shared:
        return verdict(NotionCredentialState.SHARED_ONTO_NOTHING, identity)
    if not check_required_grants:
        return verdict(NotionCredentialState.PARTIAL, identity)
    state = NotionCredentialState.UNGRANTED if ungranted else NotionCredentialState.OK
    return verdict(state, identity, ungranted=ungranted)


def report_notion_connections(echo: Callable[[str], None]) -> bool:
    """Print a bounded credential check; ``t3 doctor`` verifies each required grant."""
    verdicts = [
        probe_notion_credential(overlay, config, check_required_grants=False)
        for overlay, config in notion_routed_overlays()
    ]
    for credential in verdicts:
        echo(credential.line())
    return all(credential.ok or credential.state is NotionCredentialState.PARTIAL for credential in verdicts)


def _check_notion_credentials() -> bool:
    """Hard-FAIL when an overlay needing Notion has a credential that does not work end to end."""
    findings = [
        credential
        for overlay, config in notion_routed_overlays()
        if not (credential := probe_notion_credential(overlay, config)).ok
    ]
    for credential in findings:
        typer.echo(credential.line())
    return not findings
