from typing import cast

import httpx


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
