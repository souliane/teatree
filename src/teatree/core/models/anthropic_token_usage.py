r"""Cached per-account Anthropic rate-limit health, keyed by ``pass`` entry.

The health cache the routing selector (``teatree.credential_config``) reads on the
HOT path instead of the network: one row per ``pass_path`` records the account's
last-probed unified 5h / 7d utilization + status + reset, when it was
:attr:`checked_at`, and how long the verdict is trusted (:attr:`valid_until`). The
selector reuses a fresh, non-exhausted row with NO probe; it re-probes and
:meth:`AnthropicTokenUsageManager.record`\ s a fresh row only on a cache miss /
expiry.

This module is DOMAIN and stays free of ``teatree.llm``: :meth:`record` takes a
:class:`TokenHealthReading` value object (the already-parsed primitive fields), not a
``RateLimitSnapshot`` — the selector (which knows both the reader and this cache)
builds the reading at the boundary. The :attr:`valid_until` policy lives HERE so it
has one home: a healthy verdict expires after :data:`HEALTH_TTL` (re-probe
occasionally), an exhausted one is trusted until its blocking window(s) reset (so an
exhausted account is NOT re-probed until it can free up) — unless the verdict is
UNVERIFIED, when :data:`HEALTH_TTL` caps it instead.

Which windows block is one function, :func:`blocking_windows`, so the exhaustion verdict
and the re-arm instant can never disagree about which window matters. The whole threshold
ladder lives here for the same reason — :func:`warning_windows` names the STRAINED band
one step below, the trigger that makes a sticky routing pick re-rank before it is spent.

A verdict is bound to the CREDENTIAL it was probed with (:attr:`token_fingerprint`): an
exhausted row outlives a rotation of the token at its ``pass_path``, so trusting it by age
alone reports the previous account's exhaustion as the new one's.
"""

import datetime as dt
import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from django.db import models
from django.utils import timezone

from teatree.core.models.anthropic_active_pick import AnthropicActivePick

UTILIZATION_5H_LIMIT = 0.95
UTILIZATION_7D_LIMIT = 0.99

#: The STRAINED band, below the exhaustion limits above: a warning precedes exhaustion.
#: Routing re-ranks a sticky pick that reaches it, and ``t3 tokens`` renders it WARNING.
WARNING_5H = 0.80
WARNING_7D = 0.90

REJECTED_STATUS = "rejected"
HEALTH_TTL = dt.timedelta(minutes=5)


def fingerprint_token(token: str) -> str:
    """The stored form of a probed token — a hash, so the cache never holds the secret.

    ``""`` for an empty token is the UNKNOWN marker.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest() if token else ""


class Window(StrEnum):
    """The two unified windows, spelled as Anthropic's ``representative-claim`` values."""

    FIVE_HOUR = "five_hour"
    SEVEN_DAY = "seven_day"


_CLAIMED_WINDOWS: dict[str, Window] = {window.value: window for window in Window}


@dataclass(frozen=True)
class UnifiedVerdict:
    """Anthropic's account-wide status plus the window that status speaks for.

    The two travel together: a ``representative-claim`` only attributes anything while the
    verdict is ``rejected``, which is why the gating lives here rather than at each reader.
    """

    status: str = ""
    representative_claim: str = ""

    @property
    def rejected_window(self) -> Window | None:
        """The window a rejected account-wide verdict blames, when it names a known one."""
        if self.status != REJECTED_STATUS:
            return None
        return _CLAIMED_WINDOWS.get(self.representative_claim)


def _window_is_spent(utilization: float | None, limit: float, status: str) -> bool:
    """Whether ONE window is spent — an absent utilization is unknown, never spent."""
    return status == REJECTED_STATUS or (utilization is not None and utilization >= limit)


@dataclass(frozen=True)
class WarningThresholds:
    """Where each window's warning band starts.

    Defaults are teatree's own shipped bands; a live probe overrides them with the
    thresholds the API reports for itself, which are the authoritative same line.
    """

    five_hour: float = WARNING_5H
    seven_day: float = WARNING_7D


DEFAULT_WARNING_THRESHOLDS = WarningThresholds()


def warning_windows(
    *,
    utilization_5h: float | None,
    utilization_7d: float | None,
    thresholds: WarningThresholds = DEFAULT_WARNING_THRESHOLDS,
) -> frozenset[Window]:
    """The windows at or above the WARNING band — the trigger to re-rank a sticky pick.

    Deliberately NUMERIC-ONLY, unlike :func:`blocking_windows`: a ``rejected`` status is
    DEFINITIVE (it exhausts the account outright whatever the number says), while a
    warning is a matter of degree the utilization already expresses.
    """
    return frozenset(
        window
        for window, utilization, threshold in (
            (Window.FIVE_HOUR, utilization_5h, thresholds.five_hour),
            (Window.SEVEN_DAY, utilization_7d, thresholds.seven_day),
        )
        if utilization is not None and utilization >= threshold
    )


def blocking_windows(
    *,
    utilization_5h: float | None,
    utilization_7d: float | None,
    status_5h: str,
    status_7d: str,
    verdict: UnifiedVerdict,
) -> frozenset[Window]:
    """The windows currently BLOCKING an account — the one rule exhaustion and re-arm share.

    A window blocks only when that window is itself spent: an idle 5h window does NOT hold
    the account back even though its reset is the sooner of the two. *verdict* adds the
    window Anthropic itself calls binding, so a rejected account is attributed to that one
    rather than to whichever threshold happens to trip.
    """
    blocking = {
        window
        for window, spent in (
            (Window.FIVE_HOUR, _window_is_spent(utilization_5h, UTILIZATION_5H_LIMIT, status_5h)),
            (Window.SEVEN_DAY, _window_is_spent(utilization_7d, UTILIZATION_7D_LIMIT, status_7d)),
        )
        if spent
    }
    claimed = verdict.rejected_window
    if claimed is not None:
        blocking.add(claimed)
    return frozenset(blocking)


def _shown(utilization: float | None) -> str:
    """A window's utilization for a human, or ``?`` when nobody measured it."""
    return "?" if utilization is None else f"{utilization:.2f}"


def re_arms_at(
    windows: frozenset[Window], *, reset_5h: dt.datetime | None, reset_7d: dt.datetime | None
) -> dt.datetime | None:
    """When an account blocked on *windows* re-arms — the LATEST of their known resets.

    A ``max``, not a ``min``: every blocking window must clear first, so an account
    rejected on its 7-day window is not freed by its idle 5h window rolling over.
    ``None`` when nothing blocks, or when no blocking window reported a reset.
    """
    by_window = {Window.FIVE_HOUR: reset_5h, Window.SEVEN_DAY: reset_7d}
    resets = [reset for window in windows if (reset := by_window[window]) is not None]
    return max(resets) if resets else None


@dataclass(frozen=True)
class TokenHealthReading:
    """One account's parsed rate-limit fields, handed to the cache at the boundary.

    The DOMAIN-side twin of ``teatree.llm.rate_limits.RateLimitSnapshot`` (minus the
    token-signing concern): the selector translates a snapshot into this so the cache
    never imports the foundation reader. A ``None`` utilization is an unreported window,
    not an idle one. ``verified`` is ``False`` for a verdict nobody measured — a
    synthesis reached because the account could not be probed — and only
    :meth:`valid_until` reads it.
    """

    organization_id: str
    utilization_5h: float | None
    utilization_7d: float | None
    status_5h: str
    status_7d: str
    reset_5h: dt.datetime | None
    reset_7d: dt.datetime | None
    verdict: UnifiedVerdict = UnifiedVerdict()
    verified: bool = True

    @property
    def blocking(self) -> frozenset[Window]:
        return blocking_windows(
            utilization_5h=self.utilization_5h,
            utilization_7d=self.utilization_7d,
            status_5h=self.status_5h,
            status_7d=self.status_7d,
            verdict=self.verdict,
        )

    @property
    def is_exhausted(self) -> bool:
        return bool(self.blocking)

    @property
    def is_warning(self) -> bool:
        return bool(warning_windows(utilization_5h=self.utilization_5h, utilization_7d=self.utilization_7d))

    def valid_until(self, now: dt.datetime) -> dt.datetime:
        """When this reading's verdict stops being trusted.

        Healthy: the sooner of ``now + HEALTH_TTL`` and the nearest window reset, so a
        healthy token re-probes occasionally. Exhausted: the LATEST reset among the
        windows currently blocking it (all must clear before it frees up), so it is not
        re-probed until then; ``HEALTH_TTL`` is the floor when no reset is known, and the
        cap when the verdict is UNVERIFIED — an unmeasured refusal must self-correct in
        minutes rather than strand an account for its whole window.
        """
        ttl_bound = now + HEALTH_TTL
        blocking = self.blocking
        if not blocking:
            resets = [reset for reset in (self.reset_5h, self.reset_7d) if reset is not None]
            return min([ttl_bound, *resets]) if resets else ttl_bound
        if not self.verified:
            return ttl_bound
        return self.frees_up_at or ttl_bound

    @property
    def frees_up_at(self) -> dt.datetime | None:
        """When this account re-arms, or ``None`` when nothing blocks it."""
        return re_arms_at(self.blocking, reset_5h=self.reset_5h, reset_7d=self.reset_7d)


class AnthropicTokenUsageManager(models.Manager["AnthropicTokenUsage"]):
    """Upsert helper for the per-``pass_path`` health cache."""

    def record(
        self,
        pass_path: str,
        reading: TokenHealthReading,
        *,
        now: dt.datetime | None = None,
        token_fingerprint: str | None = None,
    ) -> "AnthropicTokenUsage":
        """Upsert the health row for *pass_path* from a fresh probe's *reading*.

        Idempotent on the unique ``pass_path``: a re-probe updates the one row. The
        stored :attr:`valid_until` follows the reading's TTL/reset policy so a healthy
        token re-probes after :data:`HEALTH_TTL` and an exhausted one waits out its reset.

        An EXHAUSTED reading also unpins the account from every scope holding it. This is
        the one place both writers converge — the reactive refusal path and the operator's
        own probe — so no future writer can forget the sweep. WARNING never unpins: that is
        a per-scope re-rank, taken at that scope's own next selection from its own list;
        exhaustion is the only verdict provably wrong for every scope simultaneously.

        *token_fingerprint* binds the verdict to the credential that produced it; ``None``
        keeps whatever is stored, for a writer holding a verdict but no token (the reactive
        exhaustion recorder observes a mid-run limit, never the secret).
        """
        moment = now or timezone.now()
        row, _ = self.update_or_create(
            pass_path=pass_path,
            defaults={
                "organization_id": reading.organization_id,
                "utilization_5h": reading.utilization_5h,
                "utilization_7d": reading.utilization_7d,
                "status_5h": reading.status_5h,
                "status_7d": reading.status_7d,
                "reset_5h": reading.reset_5h,
                "reset_7d": reading.reset_7d,
                "checked_at": moment,
                "valid_until": reading.valid_until(moment),
                **({"token_fingerprint": token_fingerprint} if token_fingerprint is not None else {}),
            },
        )
        if reading.is_exhausted:
            AnthropicActivePick.objects.unpin_account(pass_path)
        return row

    def expire_all(self, now: dt.datetime | None = None) -> int:
        """Stale every cached verdict, returning how many rows were expired.

        Expire rather than delete: the readings stay renderable while nothing trusts them,
        and the governor reads a stale row as "not currently known-blocked" — so a fleet
        cached as spent stops denying dispatch the moment the operator switches account.
        """
        return self.update(valid_until=now or timezone.now())


class AnthropicTokenUsage(models.Model):
    """One ``pass`` account's cached unified rate-limit health.

    Keyed by the unique :attr:`pass_path` (the credential's routed ``pass`` entry).
    :attr:`is_exhausted` is the routing verdict, and :meth:`is_fresh` gates whether the
    cache may be trusted without a re-probe.
    """

    pass_path = models.CharField(max_length=255, unique=True)
    organization_id = models.CharField(max_length=255, blank=True, default="")
    utilization_5h = models.FloatField(null=True, blank=True, default=None)
    utilization_7d = models.FloatField(null=True, blank=True, default=None)
    status_5h = models.CharField(max_length=64, blank=True, default="")
    status_7d = models.CharField(max_length=64, blank=True, default="")
    reset_5h = models.DateTimeField(null=True, blank=True)
    reset_7d = models.DateTimeField(null=True, blank=True)
    checked_at = models.DateTimeField(default=timezone.now)
    valid_until = models.DateTimeField()
    token_fingerprint = models.CharField(max_length=64, blank=True, default="")

    objects: ClassVar[AnthropicTokenUsageManager] = AnthropicTokenUsageManager()

    class Meta:
        db_table = "teatree_anthropic_token_usage"
        ordering: ClassVar = ["pass_path"]

    def __str__(self) -> str:
        return f"anthropic-usage<{self.pass_path} 5h={_shown(self.utilization_5h)} 7d={_shown(self.utilization_7d)}>"

    @property
    def blocking(self) -> frozenset[Window]:
        """The windows blocking this row — no ``unified_status``/claim is stored, by design."""
        return blocking_windows(
            utilization_5h=self.utilization_5h,
            utilization_7d=self.utilization_7d,
            status_5h=self.status_5h,
            status_7d=self.status_7d,
            verdict=UnifiedVerdict(),
        )

    @property
    def is_exhausted(self) -> bool:
        """Whether this account is spent: either window at its limit or rejected."""
        return bool(self.blocking)

    @property
    def is_warning(self) -> bool:
        """Whether either window is STRAINED — spent enough that routing should re-rank."""
        return bool(warning_windows(utilization_5h=self.utilization_5h, utilization_7d=self.utilization_7d))

    @property
    def is_measured(self) -> bool:
        """Whether BOTH windows carry a real reading — never rank a candidate we can't."""
        return self.utilization_5h is not None and self.utilization_7d is not None

    def is_fresh(self, now: dt.datetime | None = None) -> bool:
        """Whether the cached verdict is still trusted (``valid_until`` in the future)."""
        return self.valid_until > (now or timezone.now())

    @property
    def earliest_reset(self) -> dt.datetime | None:
        """The soonest window reset on record, or ``None`` when neither is known.

        A display read (the dash health band). Callers deciding WHEN AN EXHAUSTED ACCOUNT
        RE-ARMS must use :attr:`frees_up_at` instead — this one ignores which window is
        actually blocking and so can point at an idle window's imminent reset.
        """
        resets = [reset for reset in (self.reset_5h, self.reset_7d) if reset is not None]
        return min(resets) if resets else None

    @property
    def frees_up_at(self) -> dt.datetime | None:
        """When this account re-arms — the LATEST reset among the windows blocking it.

        Every blocking window must clear before the account is usable again, so this is a
        ``max``, not a ``min``: an account rejected on its 7-day window is not freed by its
        idle 5h window rolling over. ``None`` when the account is not blocked, or when no
        blocking window reported a reset — there is nothing to re-arm to, so a caller must
        not park behind it.
        """
        return re_arms_at(self.blocking, reset_5h=self.reset_5h, reset_7d=self.reset_7d)
