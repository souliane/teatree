"""The forge-read external-outcome measure (#4506).

Every signal in :mod:`teatree.core.factory.factory_signals` is derived from
teatree's own ledgers, so all five can read healthy through a window in which
nothing reached ``main`` — tasks reporting ``completed`` is not evidence that
anything shipped. This module reads the one number the factory cannot write for
itself: how many pull requests the FORGE says were merged in the trailing window.

Three states, kept apart on purpose. ``OK`` is a completed read. ``NO_FORGE`` is
a box with no code host or no declared repos — unmeasured, which is not the same
as zero. A read that FAILS raises :class:`ExternalOutcomeReadError` rather than
returning an empty list, because downstream an empty list is the alarm condition:
a rate-limited read degrading to "zero merges" would manufacture the very finding
this measure exists to raise honestly.

:func:`refresh_if_stale` is the cadence seam. ``t3 doctor`` runs many times a day
and the read costs a network round trip per repo, so a snapshot younger than
:data:`EXTERNAL_OUTCOME_TTL` is served without touching the network.

Imported lazily by its callers (the doctor's reconciliation ledger), so the
module-level ORM and backend imports below resolve after the app registry is up.
"""

import dataclasses
import datetime as dt
import enum
from collections.abc import Mapping
from typing import TYPE_CHECKING, TypedDict

from django.utils import timezone

from teatree.core.backend_factory import code_host_from_overlay
from teatree.core.models.external_outcome_snapshot import ExternalOutcomeSnapshot
from teatree.core.overlay_loader import get_overlay
from teatree.core.overlay_repos import owned_repo_slugs
from teatree.types import RawAPIDict

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend

#: The trailing window the measure is read over. A single day is too short: a
#: legitimately quiet Sunday shows zero merges, so a 24h window would alarm on
#: healthy idleness. Over a week, zero merges alongside sustained internal
#: success is unambiguous.
DEFAULT_EXTERNAL_WINDOW_DAYS = 7

#: How long a recorded read stays authoritative before the network is consulted again.
EXTERNAL_OUTCOME_TTL = dt.timedelta(hours=6)


class ExternalOutcomeStatus(enum.StrEnum):
    """Whether the trailing-window read produced a number, and why not when it did not."""

    OK = "ok"
    NO_FORGE = "no_forge"


class ExternalOutcomeReadError(RuntimeError):
    """A configured forge's merged-PR read failed — indeterminate, never zero."""


class MergedPrRefDict(TypedDict):
    """The JSON-serialised shape of one :class:`MergedPrRef`."""

    slug: str
    number: int
    url: str


@dataclasses.dataclass(frozen=True, slots=True)
class MergedPrRef:
    """One pull request the forge reports as merged inside the window."""

    slug: str
    number: int
    url: str = ""

    def to_dict(self) -> MergedPrRefDict:
        return {"slug": self.slug, "number": self.number, "url": self.url}


@dataclasses.dataclass(frozen=True, slots=True)
class Forge:
    """Where the external numbers come from: a code host, and the repos it is read over.

    Resolution is separated from the read so an absent host is a value the caller
    passes deliberately, never an argument the reader silently fills in for itself.
    """

    host: "CodeHostBackend | None"
    repo_slugs: tuple[str, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class ExternalOutcomes:
    """One trailing-window read of what actually landed, across the overlay's repos."""

    window_days: int
    generated_at: dt.datetime
    repo_slugs: tuple[str, ...]
    merged_prs: tuple[MergedPrRef, ...]
    status: ExternalOutcomeStatus

    @property
    def merged_pr_count(self) -> int:
        return len(self.merged_prs)


def _pr_number(item: RawAPIDict) -> int | None:
    """GitHub search calls it ``number``, GitLab calls it ``iid``; anything else is unusable."""
    raw = item.get("number")
    if raw is None:
        raw = item.get("iid")
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _merged_pr_ref(slug: str, item: object) -> MergedPrRef | None:
    if not isinstance(item, Mapping):
        return None
    fields: RawAPIDict = {str(key): value for key, value in item.items()}
    number = _pr_number(fields)
    if number is None:
        return None
    url = fields.get("html_url") or fields.get("web_url") or ""
    return MergedPrRef(slug=slug, number=number, url=str(url))


def resolve_forge(overlay: str = "") -> Forge:
    """The overlay's code host and the repo slugs whose merges count as its output."""
    return Forge(
        host=code_host_from_overlay(overlay or None),
        repo_slugs=owned_repo_slugs(get_overlay(overlay or None)),
    )


def read_external_outcomes(
    forge: Forge,
    *,
    window_days: int = DEFAULT_EXTERNAL_WINDOW_DAYS,
    now: dt.datetime | None = None,
) -> ExternalOutcomes:
    """Read the merges *forge* recorded in the trailing window.

    Raises :class:`ExternalOutcomeReadError` when a present host's read fails — an
    indeterminate read must surface, never masquerade as zero output.
    """
    moment = now or timezone.now()
    if forge.host is None or not forge.repo_slugs:
        return ExternalOutcomes(
            window_days=window_days,
            generated_at=moment,
            repo_slugs=forge.repo_slugs,
            merged_prs=(),
            status=ExternalOutcomeStatus.NO_FORGE,
        )
    since = (moment - dt.timedelta(days=window_days)).date().isoformat()
    merged: list[MergedPrRef] = []
    for slug in forge.repo_slugs:
        try:
            hits = forge.host.list_merged_prs_since(repo=slug, since=since)
        except Exception as exc:
            msg = f"merged-PR read failed for {slug!r} since {since}: {exc.__class__.__name__}: {exc}"
            raise ExternalOutcomeReadError(msg) from exc
        merged.extend(ref for ref in (_merged_pr_ref(slug, hit) for hit in hits) if ref is not None)
    return ExternalOutcomes(
        window_days=window_days,
        generated_at=moment,
        repo_slugs=forge.repo_slugs,
        merged_prs=tuple(merged),
        status=ExternalOutcomeStatus.OK,
    )


def refresh_if_stale(
    *,
    window_days: int = DEFAULT_EXTERNAL_WINDOW_DAYS,
    overlay: str = "",
    ttl: dt.timedelta = EXTERNAL_OUTCOME_TTL,
    now: dt.datetime | None = None,
    forge: Forge | None = None,
) -> ExternalOutcomeSnapshot:
    """Serve the newest snapshot younger than *ttl*, else read the forge and record one."""
    moment = now or timezone.now()
    fresh = ExternalOutcomeSnapshot.objects.latest_fresh(overlay=overlay, ttl=ttl, now=moment)
    if fresh is not None:
        return fresh
    outcomes = read_external_outcomes(
        forge if forge is not None else resolve_forge(overlay),
        window_days=window_days,
        now=moment,
    )
    return ExternalOutcomeSnapshot.objects.record(outcomes, overlay=overlay)
