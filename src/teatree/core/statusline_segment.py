"""The contributed-statusline-segment data contract (#3237).

A loop or overlay generates named, pre-rendered inline segments; core assembles
them into the statusline. The segment crosses the Python→shell seam via
``tick-meta.json``'s ``segments`` list, generalizing the single hardcoded
``cost_chip`` into a registry: producers generate segments, core owns coloring
and placement.

Core-owned so the ``OverlayBase`` producer hook can type its return without a
core→loop dependency (loop and overlays import it downward).
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StatuslineSegment:
    """One inline statusline segment a producer contributes.

    ``id`` is the unique segment name (dedup + ``after:<id>`` placement anchor).
    ``text`` is the pre-rendered value (plain text; core applies ``color``).
    ``color`` is a small semantic palette (``green``/``yellow``/``red``);
    ``None`` renders in the neutral default so a broken palette never errors.
    ``placement`` is a named anchor — ``header`` (next to the repo-freshness
    segments), ``usage`` (the group the cost chip sits in), or ``after:<id>``;
    an unknown placement degrades gracefully to end-of-line.
    """

    id: str
    text: str
    color: str | None = None
    placement: str = "header"

    def as_meta(self) -> dict[str, str]:
        meta = {"id": self.id, "text": self.text, "placement": self.placement}
        if self.color:
            meta["color"] = self.color
        return meta
