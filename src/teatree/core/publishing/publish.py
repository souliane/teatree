"""Publish one text to a forge: scan it, write it through the send-proxy, read it back, scan what is live."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlparse

from teatree.core.publishing.live_text import LiveTextVerdict, scan_live_text
from teatree.core.send_proxy import OutboundBlockedError, forge_from_url, route_forge_write
from teatree.core.verify_by_reread import verify_by_reread
from teatree.utils.url_slug import slug_from_issue_or_pr_url


class PublishKind(StrEnum):
    PR_BODY = "pr-body"
    ISSUE_BODY = "issue-body"
    COMMENT = "comment"


class PublishRefusedError(Exception):
    """The text was not published, or what is live is not what was approved."""


@dataclass(frozen=True, slots=True)
class PublishRequest:
    kind: PublishKind
    target: str
    text: str
    closes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PublishResult:
    kind: PublishKind
    target: str
    text: str


class PublishHost(Protocol):
    def write(self, kind: PublishKind, target: str, text: str) -> str: ...  # pragma: no branch

    def read_back(self, kind: PublishKind, target: str, handle: str) -> str | None: ...  # pragma: no branch


def publish_text(request: PublishRequest, host: PublishHost) -> PublishResult:
    """Write *request* once and return what is live; raise :class:`PublishRefusedError` unless that text is clean."""
    if not request.text.strip():
        msg = f"refusing to publish to {request.target}: the text is empty"
        raise PublishRefusedError(msg)
    repo = slug_from_issue_or_pr_url(urlparse(request.target).path)
    if not repo:
        msg = f"refusing to publish: {request.target} is not a PR or issue URL"
        raise PublishRefusedError(msg)
    forge = forge_from_url(request.target)
    _refuse_unless_clean(
        scan_live_text(request.text, repo, forge, request.closes), f"the proposed text for {request.target}"
    )
    try:
        sent = route_forge_write(
            forge=forge, repo=repo, text=request.text, action=f"publish-{request.kind.value}", target=request.target
        )
    except OutboundBlockedError as exc:
        msg = f"the send-proxy refused {request.target}: {exc}"
        raise PublishRefusedError(msg) from exc
    handle = host.write(request.kind, request.target, sent)
    live = _read_live(host, request, handle)
    _refuse_unless_clean(scan_live_text(live, repo, forge, request.closes), f"the live text at {request.target}")
    if _normalized(live) != _normalized(sent):
        msg = f"the live text at {request.target} differs from the text published; review it on the forge"
        raise PublishRefusedError(msg)
    return PublishResult(kind=request.kind, target=request.target, text=live)


def _read_live(host: PublishHost, request: PublishRequest, handle: str) -> str:
    seen: list[str | None] = []

    def reread() -> bool:
        seen.append(host.read_back(request.kind, request.target, handle))
        return seen[-1] is not None

    outcome = verify_by_reread(label=f"{request.kind.value} {request.target}", reread=reread)
    if not outcome.confirmed or seen[-1] is None:
        msg = f"the live text at {request.target} could not be read back ({outcome.reason}); it is unverified"
        raise PublishRefusedError(msg)
    return seen[-1]


def _refuse_unless_clean(verdict: LiveTextVerdict, subject: str) -> None:
    if not verdict.clean:
        msg = f"refusing: {subject} fails the publish scan\n{verdict.render()}"
        raise PublishRefusedError(msg)


def _normalized(text: str) -> str:
    return text.replace("\r\n", "\n").strip()
