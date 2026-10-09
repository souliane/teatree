"""A stateful MR for the bulk-publish read-backs: one pending draft until ``bulk_publish`` lands it."""


class BulkPublishMR:
    """The ``draft_notes`` / ``notes`` lists before and after a publish that landed one note by *author*."""

    def __init__(self, author: str = "souliane") -> None:
        self.author = author
        self.published = False

    def saw_post(self, endpoint: str) -> None:
        self.published = self.published or endpoint.rstrip("/").endswith("/bulk_publish")

    def listing(self, endpoint: str) -> list[dict[str, object]] | None:
        """The list *endpoint* reads, or ``None`` when it is not one of the two bulk-publish lists."""
        path = endpoint.split("?", 1)[0].rstrip("/")
        if path.endswith("/draft_notes"):
            return [] if self.published else [{"id": 1, "note": "pending"}]
        if path.endswith("/notes"):
            return [{"id": 99, "system": False, "author": {"username": self.author}}] if self.published else []
        return None
