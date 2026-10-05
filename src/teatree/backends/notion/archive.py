"""Move a Notion page to the trash or take it back out, and refuse to report success unless the re-read agrees.

Notion's page PATCH takes one boolean and answers ``200`` whether or not it applied, so the verdict is read
back from the page itself: an archived page is the one thing the liveness probe calls DEAD.
"""

from teatree.backends.notion.client import NotionClient
from teatree.backends.notion.errors import NotionWriteNotLandedError
from teatree.backends.notion.liveness import Liveness


class PageArchiver:
    def __init__(self, client: NotionClient) -> None:
        self._client = client

    def write(self, page_id: str, *, archived: bool) -> None:
        self._client.update_page(page_id, {"archived": archived})
        state = self._client.page_liveness(page_id).state
        wanted_state = Liveness.DEAD if archived else Liveness.LIVE
        if state is not wanted_state:
            now, wanted = ("live", "archived") if archived else ("archived", "live")
            if state is Liveness.UNKNOWN:
                now = "unknown (liveness cannot be proven)"
            msg = (
                f"the write reported success but page {page_id} still reads as {now}, not {wanted} "
                "— treat the write as failed."
            )
            raise NotionWriteNotLandedError(msg)
