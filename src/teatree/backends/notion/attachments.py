"""Notion file-attachment download — Brave browser cookies for the file CDN.

The one part of Notion the public API cannot serve: a file block's bytes sit
behind a short-lived S3 signature only a ``notion.so`` web session can mint, so
this path needs a logged-in browser and is therefore NOT headless. Everything a
headless run needs — pages, blocks, comments, databases — goes through the
integration-token client in :mod:`teatree.backends.notion.client`.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import unquote

import httpx

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_SIGNED_MARKERS = ("X-Amz-Signature", "expirationTimestamp", "signature=")


def _brave_cookies(domain: str) -> httpx.Cookies:
    """Extract cookies for *domain* from the running Brave browser."""
    import browser_cookie3  # noqa: PLC0415 — deferred: heavy/optional dep at call site

    jar = browser_cookie3.brave(domain_name=domain)
    cookies = httpx.Cookies()
    for c in jar:
        cookies.set(c.name, c.value, domain=c.domain)
    return cookies


@dataclass(frozen=True)
class NotionFileRef:
    """A Notion file attachment, resolvable to a signed download URL."""

    space_id: str
    attachment_id: str
    filename: str
    block_id: str

    @property
    def stored_url(self) -> str:
        # getSignedFileUrls validates against the S3 origin form; the
        # file.notion.so/f/f/… display form is rejected ("Invalid secure
        # file URL").
        return (
            f"https://prod-files-secure.s3.us-west-2.amazonaws.com/{self.space_id}/{self.attachment_id}/{self.filename}"
        )

    @classmethod
    def from_fetch_src(cls, src: str) -> "NotionFileRef | None":
        """Parse the ``file://%7B…%7D`` src string emitted by ``notion-fetch``.

        Decodes to ``{"source":"attachment:<aid>:<name>","permissionRecord":
        {"table":"block","id":"<blockId>","spaceId":"<spaceId>"}}``.
        """
        payload = src.removeprefix("file://")
        try:
            data = json.loads(unquote(payload))
        except (json.JSONDecodeError, ValueError):
            return None
        source = str(data.get("source", ""))
        record = data.get("permissionRecord") or {}
        if not source.startswith("attachment:") or not record.get("id"):
            return None
        _, attachment_id, filename = source.split(":", 2)
        return cls(
            space_id=str(record.get("spaceId", "")),
            attachment_id=attachment_id,
            filename=filename,
            block_id=str(record["id"]),
        )


def _is_signed(url: str) -> bool:
    return any(marker in url for marker in _SIGNED_MARKERS)


def resolve_signed_url(ref: NotionFileRef, cookies: httpx.Cookies) -> str:
    """Resolve a signed download URL via Notion's internal getSignedFileUrls API.

    This is what the Notion web app calls when a file is clicked; driving it
    directly with the browser session cookies removes the need for a manual
    browser click to obtain the signature.
    """
    with httpx.Client(
        cookies=cookies,
        headers={"User-Agent": _UA, "Content-Type": "application/json"},
        timeout=30.0,
    ) as client:
        resp = client.post(
            "https://www.notion.so/api/v3/getSignedFileUrls",
            json={
                "urls": [
                    {
                        "url": ref.stored_url,
                        "permissionRecord": {"table": "block", "id": ref.block_id},
                    },
                ],
            },
        )
        resp.raise_for_status()
        body = resp.json()
    for key in ("signedUrls", "signedGetUrls", "signed_urls"):
        urls = body.get(key)
        if urls:
            return cast("str", urls[0])
    msg = f"getSignedFileUrls returned no signed URL: {body}"
    raise RuntimeError(msg)


def download_notion_file(
    *,
    url: str = "",
    ref: NotionFileRef | None = None,
    dest: Path,
) -> Path:
    """Download a Notion file attachment using the Brave browser session.

    Pass either a *ref* (resolved server-side via getSignedFileUrls — no
    browser click needed) or a pre-signed *url*. A non-signed plain URL is
    rejected with a clear message because file.notion.so returns 400 without
    the signature params.
    """
    cookies = _brave_cookies("notion.so")
    if ref is not None and not _is_signed(url):
        url = resolve_signed_url(ref, cookies)
    elif not _is_signed(url):
        msg = f"URL is not signed and no NotionFileRef given to resolve it: {url}"
        raise ValueError(msg)

    with httpx.Client(
        cookies=cookies,
        headers={"User-Agent": _UA},
        follow_redirects=True,
        timeout=60.0,
    ) as client:
        resp = client.get(url)
        resp.raise_for_status()

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)
    return dest
