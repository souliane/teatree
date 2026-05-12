from pathlib import Path
from typing import cast
from urllib.parse import urlencode

import httpx


def _brave_cookies(domain: str) -> httpx.Cookies:
    """Extract cookies for *domain* from the running Brave browser."""
    import browser_cookie3  # noqa: PLC0415

    jar = browser_cookie3.brave(domain_name=domain)
    cookies = httpx.Cookies()
    for c in jar:
        cookies.set(c.name, c.value, domain=c.domain)
    return cookies


def download_notion_file(
    *,
    space_id: str,
    attachment_id: str,
    block_id: str,
    filename: str,
    dest: Path,
) -> Path:
    """Download a Notion file attachment using browser cookies."""
    base = f"https://file.notion.so/f/f/{space_id}/{attachment_id}/{filename}"
    params = urlencode({"table": "block", "id": block_id, "spaceId": space_id})
    url = f"{base}?{params}"
    cookies = _brave_cookies("notion.so")

    with httpx.Client(
        cookies=cookies,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
        follow_redirects=True,
        timeout=30.0,
    ) as client:
        resp = client.get(url)
        resp.raise_for_status()

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)
    return dest


class NotionClient:
    def __init__(self, *, token: str, version: str = "2022-06-28") -> None:
        self.token = token
        self.version = version

    def get_page(self, page_id: str) -> dict[str, object]:
        with httpx.Client(
            headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": self.version,
            },
            timeout=10.0,
        ) as client:
            response = client.get(f"https://api.notion.com/v1/pages/{page_id}")
            response.raise_for_status()
            return cast("dict[str, object]", response.json())
