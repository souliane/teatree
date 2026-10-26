"""Shared checking report renderers."""

from dataclasses import dataclass, field
from typing import TypedDict

#: Per-group item cap; beyond it the renderer appends "…and X more".
DEFAULT_CAP = 5


class CheckItemDict(TypedDict):
    label: str
    url: str
    detail: str
    title: str


class CheckGroupDict(TypedDict):
    title: str
    items: list[CheckItemDict]
    total: int


class OwnerWaitItemDict(CheckItemDict):
    kind: str
    age_days: int
    row_id: int | None
    owner_only: bool


@dataclass(frozen=True, slots=True)
class CheckItem:
    """One line in a group — a clickable reference plus optional detail.

    ``url`` is the clickable target (a PR/issue/ticket URL, or an actionable
    command for a deferred question). ``title`` is the human-readable
    description rendered inline next to the id (#2092), so a reader never sees a
    bare/link-only ``#N`` they can't interpret. The shared
    :func:`teatree.core.ref_render.render_ref` chokepoint produces the
    ``[#N (short title)](url)`` shape every listing surface uses.
    """

    label: str
    url: str
    detail: str = ""
    title: str = ""

    def to_dict(self) -> CheckItemDict:
        return CheckItemDict(label=self.label, url=self.url, detail=self.detail, title=self.title)

    def render(self) -> str:
        from teatree.core.ref_render import render_ref  # noqa: PLC0415 — deferred: call-time import, kept lazy

        ref = render_ref(self.label, title=self.title, url=self.url)
        return f"  - {ref} — {self.detail}" if self.detail else f"  - {ref}"


@dataclass(frozen=True, slots=True)
class OwnerWaitItem(CheckItem):
    kind: str = ""
    age_days: int = 0
    row_id: int | None = None
    owner_only: bool = False

    def to_dict(self) -> OwnerWaitItemDict:
        return OwnerWaitItemDict(
            label=self.label,
            url=self.url,
            detail=self.detail,
            title=self.title,
            kind=self.kind,
            age_days=self.age_days,
            row_id=self.row_id,
            owner_only=self.owner_only,
        )


@dataclass(frozen=True, slots=True)
class CheckGroup:
    """One titled group of items, with the full pre-cap total.

    ``total`` is the count before the cap (``total >= len(items)``) so the
    renderer can append "…and X more" without re-querying.
    """

    title: str
    items: list[CheckItem] = field(default_factory=list)
    total: int = 0

    def to_dict(self) -> CheckGroupDict:
        return CheckGroupDict(
            title=self.title,
            items=[item.to_dict() for item in self.items],
            total=self.total,
        )

    def render(self, *, cap: int = DEFAULT_CAP) -> list[str]:
        """Render the group header + capped items, or ``[]`` when empty.

        An empty group is omitted entirely (returns no lines). A group with
        more than *cap* items appends a ``…and X more`` line.
        """
        if self.total == 0:
            return []
        lines = [self.title]
        lines.extend(item.render() for item in self.items[:cap])
        if self.total > cap:
            lines.append(f"  …and {self.total - cap} more")
        return lines
