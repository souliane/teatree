"""Shared checking report renderers."""

from dataclasses import dataclass, field
from typing import TypedDict, cast

from teatree.core.ref_render import render_ref

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


class RemainingItemDict(CheckItemDict):
    kind: str
    steps: list[str]
    eta: dict


class RemainingGroupDict(CheckGroupDict):
    eta: dict
    queue: dict
    durations: list[dict]
    holds: dict


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
        ref = render_ref(self.label, title=self.title, url=self.url)
        return f"  - {ref} — {self.detail}" if self.detail else f"  - {ref}"


@dataclass(frozen=True, slots=True)
class CheckGroup:
    """One titled group of items, with the full pre-cap total.

    ``total`` is the count before the cap, so the renderer can append
    "…and X more" without re-querying. RemainingGroup excludes its meta rows.
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

    def has_table_content(self) -> bool:
        return self.total > 0

    def table_items(self, *, cap: int) -> list[CheckItem]:
        return self.items[: max(0, cap)]

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


@dataclass(frozen=True, slots=True)
class RemainingItem(CheckItem):
    kind: str = ""
    steps: tuple[str, ...] = ()
    eta: dict = field(default_factory=dict)
    duration_phases: tuple[str, ...] = ()
    ticket_id: int | None = None
    hold: str = ""

    def to_dict(self) -> RemainingItemDict:
        return cast(
            "RemainingItemDict",
            {**CheckItem.to_dict(self), "kind": self.kind, "steps": list(self.steps), "eta": self.eta},
        )


@dataclass(frozen=True, slots=True)
class RemainingGroup(CheckGroup):
    eta: dict = field(default_factory=dict)
    queue: dict = field(default_factory=dict)
    durations: list[dict] = field(default_factory=list)
    holds: dict = field(default_factory=dict)

    def has_table_content(self) -> bool:
        return bool(self.items)

    def table_items(self, *, cap: int) -> list[CheckItem]:
        return [*self.items[:2], *self.items[2 : 2 + max(0, cap)]]

    def render(self, *, cap: int = DEFAULT_CAP) -> list[str]:
        if not self.items:
            return []
        shown = self.items[2 : 2 + max(0, cap)]
        lines = [self.title, *(item.render() for item in self.items[:2]), *(item.render() for item in shown)]
        if self.total > len(shown):
            lines.append(f"  …and {self.total - len(shown)} more")
        return lines

    def to_dict(self) -> RemainingGroupDict:
        return RemainingGroupDict(
            title=self.title,
            items=[item.to_dict() for item in self.items],
            total=self.total,
            eta=self.eta,
            queue=self.queue,
            durations=self.durations,
            holds=self.holds,
        )
