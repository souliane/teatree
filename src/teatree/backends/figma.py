"""Figma backend — direct REST API client for large files.

The claude.ai Figma MCP integration times out or truncates data on large
files. This wraps the Figma REST API directly (``X-Figma-Token`` personal
access token auth) as a lightweight, reliable alternative for design-to-code
workflows: mockup screenshots, frame navigation, review comments, and
component/style ("design token") metadata. Callers resolve the token via
``teatree.utils.secrets.read_pass("figma/pat")``.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast

import httpx
from PIL import Image


class FigmaComponentPropertyDefinition(TypedDict, total=False):
    """A single variant property definition on a Figma ``COMPONENT_SET`` node."""

    type: str
    defaultValue: object
    variantOptions: list[str]


class FigmaNode(TypedDict, total=False):
    """Subset of a Figma document node that teatree reads."""

    id: str
    name: str
    type: str
    children: list["FigmaNode"]
    componentPropertyDefinitions: dict[str, FigmaComponentPropertyDefinition]


class FigmaComponentEntry(TypedDict, total=False):
    """Subset of a Figma component/component-set entry that teatree reads."""

    key: str
    name: str
    description: str


class FigmaStyleEntry(TypedDict, total=False):
    """Subset of a Figma style ("design token") entry that teatree reads."""

    key: str
    name: str
    description: str
    styleType: str


class FigmaCommentClientMeta(TypedDict, total=False):
    """The anchor of a Figma comment — a node ID when pinned to a layer."""

    node_id: str


class FigmaComment(TypedDict, total=False):
    """Subset of a Figma comment (designer annotation / review feedback) that teatree reads."""

    id: str
    message: str
    client_meta: FigmaCommentClientMeta


class _FigmaFileResponse(TypedDict, total=False):
    """Subset of the ``GET /v1/files/:key`` response that teatree reads."""

    document: FigmaNode
    components: dict[str, FigmaComponentEntry]
    componentSets: dict[str, FigmaComponentEntry]
    styles: dict[str, FigmaStyleEntry]


class _FigmaNodeEntry(TypedDict, total=False):
    document: FigmaNode


class _FigmaNodesResponse(TypedDict, total=False):
    """Subset of the ``GET /v1/files/:key/nodes`` response that teatree reads."""

    nodes: dict[str, _FigmaNodeEntry]


class _FigmaImagesResponse(TypedDict, total=False):
    """Subset of the ``GET /v1/images/:key`` response that teatree reads."""

    err: str | None
    images: dict[str, str]


class _FigmaCommentsResponse(TypedDict, total=False):
    """Subset of the ``GET /v1/files/:key/comments`` response that teatree reads."""

    comments: list[FigmaComment]


@dataclass(frozen=True)
class FigmaFrameRef:
    """A child node of a listed frame — enough to navigate or re-fetch it."""

    node_id: str
    name: str
    node_type: str


@dataclass(frozen=True)
class FigmaComponentMetadata:
    """Component/style metadata embedded in a file's ``GET /v1/files/:key`` response.

    ``styles`` covers Figma's color/text/effect/grid styles, the closest REST-level
    equivalent to design tokens (the ``variables`` API is Enterprise-only).
    ``variant_properties`` maps each ``COMPONENT_SET`` node id to its
    ``componentPropertyDefinitions`` — the root ``componentSets`` map carries only
    key/name/description, so the variant properties themselves are read from the
    document tree, keyed by node id.
    """

    components: dict[str, FigmaComponentEntry]
    component_sets: dict[str, FigmaComponentEntry]
    styles: dict[str, FigmaStyleEntry]
    variant_properties: dict[str, dict[str, FigmaComponentPropertyDefinition]]


class FigmaClient:
    """Figma REST API client — one HTTP call per method, no local state."""

    def __init__(self, *, token: str, base_url: str = "https://api.figma.com") -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")

    def get_file(self, file_key: str) -> _FigmaFileResponse:
        with self._client() as client:
            response = client.get(f"/v1/files/{file_key}")
            response.raise_for_status()
            return cast("_FigmaFileResponse", response.json())

    def get_node(self, file_key: str, node_id: str) -> FigmaNode:
        with self._client() as client:
            response = client.get(f"/v1/files/{file_key}/nodes", params={"ids": node_id})
            response.raise_for_status()
            body = cast("_FigmaNodesResponse", response.json())
        entry = body.get("nodes", {}).get(node_id)
        if not entry:
            msg = f"Figma node {node_id} not found in file {file_key}"
            raise ValueError(msg)
        return entry["document"]

    def list_frame_children(self, file_key: str, node_id: str) -> list[FigmaFrameRef]:
        document = self.get_node(file_key, node_id)
        return [
            FigmaFrameRef(node_id=child["id"], name=child["name"], node_type=child["type"])
            for child in document.get("children", [])
        ]

    def get_image_urls(
        self, file_key: str, node_ids: list[str], *, scale: float = 1.0, image_format: str = "png"
    ) -> dict[str, str]:
        with self._client() as client:
            response = client.get(
                f"/v1/images/{file_key}",
                params={"ids": ",".join(node_ids), "scale": scale, "format": image_format},
            )
            response.raise_for_status()
            body = cast("_FigmaImagesResponse", response.json())
        if body.get("err"):
            msg = f"Figma image render failed: {body['err']}"
            raise RuntimeError(msg)
        return body.get("images") or {}

    def get_screenshot(
        self, file_key: str, node_id: str, dest: Path, *, scale: float = 2.0, image_format: str = "png"
    ) -> Path:
        urls = self.get_image_urls(file_key, [node_id], scale=scale, image_format=image_format)
        url = urls.get(node_id)
        if not url:
            msg = f"Figma returned no rendered image URL for node {node_id}"
            raise RuntimeError(msg)
        return download_image(url, dest)

    def get_comments(self, file_key: str) -> list[FigmaComment]:
        with self._client() as client:
            response = client.get(f"/v1/files/{file_key}/comments")
            response.raise_for_status()
            body = cast("_FigmaCommentsResponse", response.json())
        return body.get("comments") or []

    def get_node_comments(self, file_key: str, node_id: str) -> list[FigmaComment]:
        return [
            comment
            for comment in self.get_comments(file_key)
            if comment.get("client_meta", {}).get("node_id") == node_id
        ]

    def get_component_metadata(self, file_key: str) -> FigmaComponentMetadata:
        file_data = self.get_file(file_key)
        document = file_data.get("document")
        return FigmaComponentMetadata(
            components=file_data.get("components") or {},
            component_sets=file_data.get("componentSets") or {},
            styles=file_data.get("styles") or {},
            variant_properties=_collect_variant_properties(document) if document else {},
        )

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            headers={"X-Figma-Token": self.token},
            timeout=30.0,
        )


def _collect_variant_properties(node: FigmaNode) -> dict[str, dict[str, FigmaComponentPropertyDefinition]]:
    """Recursively collect ``componentPropertyDefinitions`` from every ``COMPONENT_SET`` node."""
    found: dict[str, dict[str, FigmaComponentPropertyDefinition]] = {}
    if node.get("type") == "COMPONENT_SET":
        definitions = node.get("componentPropertyDefinitions")
        if definitions:
            found[node["id"]] = definitions
    for child in node.get("children", []):
        found.update(_collect_variant_properties(child))
    return found


def download_image(url: str, dest: Path) -> Path:
    """Download a rendered image URL (from :meth:`FigmaClient.get_image_urls`) to *dest*."""
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(response.content)
    return dest


def build_side_by_side_comparison(design_image: Path, actual_screenshot: Path, dest: Path) -> Path:
    """Combine a Figma mockup and a Playwright screenshot side by side for MR evidence."""
    with Image.open(design_image) as design, Image.open(actual_screenshot) as actual:
        design_rgb = design.convert("RGB")
        actual_rgb = actual.convert("RGB")
        height = max(design_rgb.height, actual_rgb.height)
        combined = Image.new("RGB", (design_rgb.width + actual_rgb.width, height), color="white")
        combined.paste(design_rgb, (0, 0))
        combined.paste(actual_rgb, (design_rgb.width, 0))
    dest.parent.mkdir(parents=True, exist_ok=True)
    combined.save(dest)
    return dest
