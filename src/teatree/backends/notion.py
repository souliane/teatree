import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import unquote

import httpx

from teatree.types import RawAPIDict

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_SIGNED_MARKERS = ("X-Amz-Signature", "expirationTimestamp", "signature=")


def _brave_cookies(domain: str) -> httpx.Cookies:
    """Extract cookies for *domain* from the running Brave browser."""
    import browser_cookie3  # noqa: PLC0415

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


def _property_name_value(prop: object) -> str | None:
    """Read the option name from a Notion ``status``- or ``select``-typed property."""
    if not isinstance(prop, dict):
        return None
    typed = cast("RawAPIDict", prop)
    for key in ("status", "select"):
        value = typed.get(key)
        if isinstance(value, dict):
            name = cast("RawAPIDict", value).get("name")
            if isinstance(name, str):
                return name
    return None


class NotionClient:
    _BASE = "https://api.notion.com/v1"

    def __init__(self, *, token: str, version: str = "2022-06-28") -> None:
        self.token = token
        self.version = version

    def _client(self) -> httpx.Client:
        return httpx.Client(
            headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": self.version,
            },
            timeout=10.0,
        )

    def get_page(self, page_id: str) -> RawAPIDict:
        with self._client() as client:
            response = client.get(f"{self._BASE}/pages/{page_id}")
            response.raise_for_status()
            return cast("RawAPIDict", response.json())

    def get_page_status(self, page_id: str, *, property_name: str = "Status") -> str | None:
        properties = self.get_page(page_id).get("properties")
        if not isinstance(properties, dict):
            return None
        return _property_name_value(cast("RawAPIDict", properties).get(property_name))

    def query_database(
        self, database_id: str, *, db_filter: RawAPIDict | None = None, page_size: int = 100
    ) -> list[RawAPIDict]:
        results: list[RawAPIDict] = []
        cursor: str | None = None
        with self._client() as client:
            while True:
                payload: RawAPIDict = {"page_size": page_size}
                if db_filter is not None:
                    payload["filter"] = db_filter
                if cursor:
                    payload["start_cursor"] = cursor
                response = client.post(f"{self._BASE}/databases/{database_id}/query", json=payload)
                response.raise_for_status()
                body = response.json()
                results.extend(body.get("results", []))
                cursor = body.get("next_cursor")
                if not body.get("has_more") or not cursor:
                    return results

    def update_page_status(self, page_id: str, *, property_name: str, value: str) -> RawAPIDict:
        with self._client() as client:
            response = client.patch(
                f"{self._BASE}/pages/{page_id}",
                json={"properties": {property_name: {"status": {"name": value}}}},
            )
            response.raise_for_status()
            return cast("RawAPIDict", response.json())
