"""SVG geometry for the dashboard's charts — arithmetic here, markup in the template.

The dashboard is server-rendered Django templates with vendored htmx and no charting
library, so a chart is an inline ``<svg>`` whose rectangles and polylines are computed
here. That keeps the frontend dependency set unchanged and makes every coordinate
assertable from a plain test, which a canvas-drawing library would not be.

``tone`` is an FSM-rail group slug (``building``, ``reviewing``, …) that the template
puts on ``data-group``, so a chart inherits the same ``--col-hue`` the board columns
and drawer chips use rather than introducing a second palette.
"""

from collections.abc import Sequence
from dataclasses import dataclass

#: The plotted viewBox. Charts scale to their container through ``preserveAspectRatio``,
#: so these are relative units rather than pixels an operator's screen ever sees.
BAR_WIDTH = 1000.0
TREND_WIDTH = 1000.0
TREND_HEIGHT = 260.0
TREND_PADDING = 12.0


@dataclass(frozen=True, slots=True)
class BarInput:
    """One contribution to a stacked bar, before it is positioned."""

    label: str
    tone: str
    seconds: float
    #: Drawn faintly — the same hue at low opacity, for a piece that is the ABSENCE of
    #: the work the hue stands for (a phase's queue wait against its working stretch).
    muted: bool = False


@dataclass(frozen=True, slots=True)
class BarPiece:
    """One drawn rectangle of a stacked bar, positioned along the axis."""

    label: str
    tone: str
    muted: bool
    x: float
    width: float
    seconds: float
    share: float


@dataclass(frozen=True, slots=True)
class StackedBar:
    label: str
    href: str
    total_seconds: float
    pieces: tuple[BarPiece, ...]


@dataclass(frozen=True, slots=True)
class LinePoint:
    x: float
    y: float
    seconds: float
    label: str


@dataclass(frozen=True, slots=True)
class LineSeries:
    label: str
    tone: str
    points: tuple[LinePoint, ...]

    @property
    def polyline(self) -> str:
        return " ".join(f"{point.x:.2f},{point.y:.2f}" for point in self.points)


def stacked_bar(*, label: str, href: str, pieces: Sequence[BarInput], scale_seconds: float) -> StackedBar:
    """Lay *pieces* end to end against a SHARED *scale_seconds*.

    Every bar on the page is measured against the same scale rather than its own total,
    so a fast ticket renders as a short bar. Normalising each bar to its own width would
    make every ticket look identical and hide exactly the comparison the chart is for.
    """
    total = sum(piece.seconds for piece in pieces)
    span = scale_seconds if scale_seconds > 0 else 1.0
    drawn: list[BarPiece] = []
    cursor = 0.0
    for piece in pieces:
        width = BAR_WIDTH * max(0.0, piece.seconds) / span
        drawn.append(
            BarPiece(
                label=piece.label,
                tone=piece.tone,
                muted=piece.muted,
                x=cursor,
                width=width,
                seconds=piece.seconds,
                share=piece.seconds / total if total > 0 else 0.0,
            )
        )
        cursor += width
    return StackedBar(label=label, href=href, total_seconds=total, pieces=tuple(drawn))


def line_series(
    *,
    label: str,
    tone: str,
    points: Sequence[tuple[str, float]],
    axis: Sequence[str],
    scale_seconds: float,
) -> LineSeries:
    """Plot (bucket, seconds) points against the SHARED *axis*, oldest bucket at the left.

    The x position comes from the bucket's slot in *axis* rather than from the point's
    index in this series: two edges rarely have samples in the same buckets, and packing
    each series' own points left-to-right would draw them against different time scales
    while the page shows one set of labels. A bucket a series has no samples in yields no
    point — a zero there would read as an edge that got instantaneous.
    """
    span = scale_seconds if scale_seconds > 0 else 1.0
    usable = TREND_WIDTH - 2 * TREND_PADDING
    height = TREND_HEIGHT - 2 * TREND_PADDING
    slots = {bucket: index for index, bucket in enumerate(axis)}
    step = usable / (len(axis) - 1) if len(axis) > 1 else 0.0
    plotted = tuple(
        LinePoint(
            x=TREND_PADDING + slots[point_label] * step,
            y=TREND_PADDING + height * (1.0 - min(1.0, max(0.0, seconds / span))),
            seconds=seconds,
            label=point_label,
        )
        for point_label, seconds in points
        if point_label in slots
    )
    return LineSeries(label=label, tone=tone, points=plotted)
