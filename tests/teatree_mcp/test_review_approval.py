from unittest.mock import patch

from asgiref.sync import async_to_sync

from teatree.mcp.server import build_server
from tests.teatree_mcp._call_tool_result import structured


class _Seam:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def approve(self, repo: str, mr: int) -> tuple[str, int]:
        self.calls.append(("approve", repo, mr))
        return "approved", 0

    def unapprove(self, repo: str, mr: int) -> tuple[str, int]:
        self.calls.append(("unapprove", repo, mr))
        return "unapproved", 0


def test_review_approval_tools_use_the_gated_review_service() -> None:
    seam = _Seam()
    with (
        patch("teatree.mcp.server.get_all_overlays", return_value={}),
        patch("teatree.mcp.review_write_tools.review_post_seam", return_value=seam),
    ):
        server = build_server()
        approved = structured(async_to_sync(server.call_tool)("review_approve", {"repo": "acme/widgets", "mr": 7}))
        revoked = structured(async_to_sync(server.call_tool)("review_unapprove", {"repo": "acme/widgets", "mr": 7}))

    assert approved == {"message": "approved", "code": 0}
    assert revoked == {"message": "unapproved", "code": 0}
    assert seam.calls == [("approve", "acme/widgets", 7), ("unapprove", "acme/widgets", 7)]
