"""A recording stand-in for the forge a publish writes to."""

from teatree.core.publishing.publish import PublishKind


class RecordingHost:
    def __init__(self, *, appended: str = "", replaced_with: str | None = None, unreadable: bool = False) -> None:
        self.writes: list[tuple[PublishKind, str, str]] = []
        self.live = ""
        self._appended = appended
        self._replaced_with = replaced_with
        self._unreadable = unreadable

    def write(self, kind: PublishKind, target: str, text: str) -> str:
        self.writes.append((kind, target, text))
        self.live = text + self._appended
        return "handle-1"

    def read_back(self, kind: PublishKind, target: str, handle: str) -> str | None:
        if self._unreadable:
            return None
        return self.live if self._replaced_with is None else self._replaced_with
