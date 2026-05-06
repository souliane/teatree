"""Scan a Notion view for tickets that have not yet been routed to a code host.

The walkthrough is overlay-config-driven: the user wires the `notion_view`
config (token + database id) on their overlay, and the scanner queries
items in the configured view that lack a code-host reference field. Each
hit triggers an n8n webhook (also overlay-configured) so the code-host
issue gets created with the right project routing and templating.

When the overlay does not configure Notion, the scanner is a no-op — it
stays installed but emits no signals.
"""

from dataclasses import dataclass
from typing import Any, Protocol

from teatree.loop.scanners.base import ScanSignal

type NotionItem = dict[str, Any]


class NotionLike(Protocol):
    def list_unrouted(self) -> list[NotionItem]: ...  # pragma: no branch

    def trigger_webhook(self, item: NotionItem) -> None: ...  # pragma: no branch


@dataclass(slots=True)
class NotionViewScanner:
    """Surface ``notion.unrouted`` signals for items missing a code-host link."""

    client: NotionLike | None
    name: str = "notion_view"

    def scan(self) -> list[ScanSignal]:
        if self.client is None:
            return []
        signals: list[ScanSignal] = []
        for item in self.client.list_unrouted():
            title = item.get("title") if isinstance(item.get("title"), str) else ""
            signals.append(
                ScanSignal(
                    kind="notion.unrouted",
                    summary=f"Notion item to route: {title}",
                    payload={"item": item},
                )
            )
        return signals
