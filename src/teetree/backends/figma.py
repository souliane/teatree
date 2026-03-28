import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

_FIGMA_API = "https://api.figma.com/v1"


@dataclass(frozen=True)
class FigmaChildNode:
    id: str
    name: str


class FigmaBackend:
    def __init__(self, *, token: str = "") -> None:
        self.token = token or os.environ.get("FIGMA_TOKEN", "")

    def _headers(self) -> dict[str, str]:
        return {"X-FIGMA-TOKEN": self.token}

    def get_file(self, file_key: str) -> dict[str, object]:
        resp = httpx.get(f"{_FIGMA_API}/files/{file_key}", headers=self._headers(), timeout=30.0)
        resp.raise_for_status()
        return resp.json()

    def get_node_image(self, file_key: str, node_id: str, *, scale: float = 2.0, fmt: str = "png") -> bytes:
        resp = httpx.get(
            f"{_FIGMA_API}/images/{file_key}",
            params={"ids": node_id, "scale": scale, "format": fmt},
            headers=self._headers(),
            timeout=30.0,
        )
        resp.raise_for_status()
        images = resp.json().get("images", {})
        image_url = images.get(node_id, "")
        if not image_url:
            return b""
        img_resp = httpx.get(image_url, timeout=30.0)
        img_resp.raise_for_status()
        return img_resp.content

    def list_children(self, file_key: str, node_id: str) -> list[FigmaChildNode]:
        data = self.get_file(file_key)
        node = _find_node(data.get("document", {}), node_id)
        if not node:
            return []
        return [FigmaChildNode(id=c.get("id", ""), name=c.get("name", "")) for c in node.get("children", [])]

    def get_comments(self, file_key: str) -> list[dict[str, object]]:
        resp = httpx.get(f"{_FIGMA_API}/files/{file_key}/comments", headers=self._headers(), timeout=30.0)
        resp.raise_for_status()
        return resp.json().get("comments", [])


def _find_node(tree: dict[str, object], node_id: str) -> dict[str, object] | None:
    if tree.get("id") == node_id:
        return tree
    for child in tree.get("children", []):
        found = _find_node(child, node_id)
        if found:
            return found
    return None
