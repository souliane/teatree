r"""One Anthropic account's rendered health row, and the table they render into.

The PRESENTATION half of ``t3 tokens`` — :mod:`teatree.token_report` gathers the rows,
this module decides what each one looks like. Split so neither concern has to be read to
change the other.

A cell is honest about what was never measured: a ``None`` utilization renders ``—``,
never ``0 %``. No row ever carries a token value — a ``STORE`` row's ``account`` is its
``pass`` entry and an ad-hoc row's is its ``token[N]`` position.
"""

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import TypedDict

from django.utils import timezone
from rich.console import Console
from rich.table import Table

from teatree.core.models.anthropic_token_usage import (
    DEFAULT_WARNING_THRESHOLDS,
    AnthropicTokenUsage,
    TokenHealthReading,
    WarningThresholds,
    warning_windows,
)
from teatree.core.models.config_setting import GLOBAL_SCOPE
from teatree.credential_config import TokenKind
from teatree.llm.rate_limits import MeteredKeySnapshot, OverageUsage, used_fraction

_RENDER_WIDTH = 200


class TokenSource(StrEnum):
    """Where a rendered row's account came from — the ``--json`` discriminator.

    ``STORE`` rows are sourced from a configured ``pass`` entry (``account`` is that
    entry); ``AD_HOC`` rows are ad-hoc ``--token`` probes (``account`` is the ``token[N]``
    label, never the token value). The wire values are ``"pass"`` / ``"token"``.
    """

    STORE = "pass"
    AD_HOC = "token"


class TokenStatus(StrEnum):
    """One account's rendered health verdict.

    ``OUT_OF_CREDITS`` is the metered-API-key twin of ``EXHAUSTED`` (a depleted prepaid
    balance) — both are alarming and block routing. ``UNCACHED`` is reachable only under
    ``--cached``: the router holds no verdict for this account.
    """

    HEALTHY = "healthy"
    WARNING = "warning"
    EXHAUSTED = "exhausted"
    OUT_OF_CREDITS = "out_of_credits"
    MISSING = "missing"
    UNREACHABLE = "unreachable"
    UNCACHED = "uncached"

    @property
    def is_measured(self) -> bool:
        """Whether a reading exists at all (the three no-reading verdicts have none)."""
        return self not in {TokenStatus.MISSING, TokenStatus.UNREACHABLE, TokenStatus.UNCACHED}


@dataclass(frozen=True)
class RowIdentity:
    """Which account a row speaks for and where it came from — never a token value.

    For a ``STORE`` row ``account`` IS the configured ``pass`` entry; for an ad-hoc row it
    is the ``token[N]`` label.
    """

    account: str
    kind: TokenKind
    source: TokenSource
    scopes: tuple[str, ...] = ()


class TokenAccountPayload(TypedDict):
    """The token-free JSON shape of one account row (``t3 tokens --json``).

    OAuth rows carry ``utilization_*`` / ``next_window_reset`` (5h) / ``weekly_reset``
    (7d) / the ``overage_*`` set; API-key rows carry the per-minute ``requests_*`` /
    ``tokens_remaining`` instead — the inapplicable set is ``None`` on each kind.
    ``account`` is the ``pass`` entry for a ``pass`` row and the ``token[N]`` label for an
    ad-hoc ``--token`` row; ``source`` discriminates the two. ``checked_at`` is when the
    rendered reading was taken.
    """

    account: str
    source: str
    kind: str
    overlays: list[str]
    organization_id: str
    utilization_5h: float | None
    utilization_7d: float | None
    next_window_reset: str | None
    weekly_reset: str | None
    overage_status: str | None
    overage_utilization: float | None
    overage_reset: str | None
    overage_in_use: bool | None
    overage_disabled_reason: str | None
    fallback: str | None
    requests_remaining: int | None
    requests_limit: int | None
    tokens_remaining: int | None
    checked_at: str | None
    status: str


_ALARMING = {TokenStatus.EXHAUSTED, TokenStatus.OUT_OF_CREDITS, TokenStatus.MISSING, TokenStatus.UNREACHABLE}
_ROW_STYLE: dict[TokenStatus, str] = {
    TokenStatus.EXHAUSTED: "bold red",
    TokenStatus.OUT_OF_CREDITS: "bold red",
    TokenStatus.MISSING: "red",
    TokenStatus.UNREACHABLE: "red",
    TokenStatus.UNCACHED: "dim",
    TokenStatus.WARNING: "yellow",
    TokenStatus.HEALTHY: "green",
}

# Anthropic's overage ``disabled-reason`` words, rendered for the "extra usage" cell.
_OVERAGE_REASON_LABELS = {
    "org_level_disabled": "off",
    "out_of_credits": "out of credits",
    "org_spend_cap_reached": "spend cap",
}


@dataclass(frozen=True)
class TokenAccountRow:
    """One configured account's health, ready to render — never carries the token.

    OAuth rows populate ``utilization_*`` / ``next_window_reset`` (5h) / ``weekly_reset``
    (7d) / :attr:`overage`; API-key rows populate the per-minute ``requests_*`` /
    ``tokens_remaining`` instead. The ``col_*`` cells render the applicable set per kind so
    the shared table stays honest, and a ``None`` utilization renders ``—`` rather than
    ``0 %``. ``account`` is the ``pass`` entry for a ``pass`` row and the ``token[N]`` label
    for an ad-hoc row — never a token value; ``source`` says which.
    """

    account: str
    kind: TokenKind
    source: TokenSource
    scopes: tuple[str, ...]
    organization_id: str
    utilization_5h: float | None
    utilization_7d: float | None
    weekly_reset: dt.datetime | None
    status: TokenStatus
    next_window_reset: dt.datetime | None = None
    overage: OverageUsage | None = None
    #: The API's own warning bands when the row came from a live probe; the shipped
    #: defaults for a cached row, which carries no thresholds.
    thresholds: WarningThresholds = DEFAULT_WARNING_THRESHOLDS
    fallback: str = ""
    frees_up_at: dt.datetime | None = None
    checked_at: dt.datetime | None = None
    requests_remaining: int | None = None
    requests_limit: int | None = None
    tokens_remaining: int | None = None

    @property
    def is_api_key(self) -> bool:
        return self.kind is TokenKind.API_KEY

    @property
    def overlay_labels(self) -> tuple[str, ...]:
        return tuple("global" if scope == GLOBAL_SCOPE else scope for scope in self.scopes)

    @property
    def overlays_label(self) -> str:
        return ", ".join(self.overlay_labels) or "—"

    @property
    def headroom(self) -> float:
        """The binding window's free fraction — 0.0 when no window was measured."""
        measured = [used for used in (self.utilization_5h, self.utilization_7d) if used is not None]
        return 1.0 - max(measured) if measured else 0.0

    @property
    def utilization_5h_pct(self) -> str:
        return _pct(self.utilization_5h)

    @property
    def utilization_7d_pct(self) -> str:
        return _pct(self.utilization_7d)

    @property
    def col_5h(self) -> str:
        """The "5h" cell: OAuth 5h utilization, or an API-key's requests-remaining."""
        if self.is_api_key:
            return _remaining_cell("req", self.requests_remaining, self.requests_limit)
        return self.utilization_5h_pct

    @property
    def col_7d(self) -> str:
        """The "7d" cell: OAuth weekly utilization, or an API-key's tokens-remaining."""
        if self.is_api_key:
            return _remaining_cell("tok", self.tokens_remaining, None)
        return self.utilization_7d_pct

    @property
    def col_extra_usage(self) -> str:
        """The "extra usage" cell: overage utilization, or why extra usage is unavailable."""
        if self.overage is None:
            return "—"
        if not self.overage.is_available:
            reason = self.overage.disabled_reason
            return _OVERAGE_REASON_LABELS.get(reason, reason)
        return _pct(self.overage.utilization)

    @property
    def col_next_window(self) -> str:
        """The "5h reset" cell — the unified 5h window's next-window reset (OAuth only)."""
        return "—" if self.is_api_key else _local_time(self.next_window_reset)

    @property
    def col_reset(self) -> str:
        """The "weekly reset" cell — inapplicable to per-minute API-key limits."""
        return "—" if self.is_api_key else _local_time(self.weekly_reset)

    def as_dict(self) -> TokenAccountPayload:
        oauth_measured = self.status.is_measured and not self.is_api_key
        overage = self.overage
        return TokenAccountPayload(
            account=self.account,
            source=self.source.value,
            kind=self.kind.value,
            overlays=list(self.overlay_labels),
            organization_id=self.organization_id,
            utilization_5h=self.utilization_5h if oauth_measured else None,
            utilization_7d=self.utilization_7d if oauth_measured else None,
            next_window_reset=_iso(self.next_window_reset),
            weekly_reset=_iso(self.weekly_reset),
            overage_status=overage.status if overage is not None else None,
            overage_utilization=overage.utilization if overage is not None else None,
            overage_reset=_iso(overage.reset) if overage is not None else None,
            overage_in_use=overage.in_use if overage is not None else None,
            overage_disabled_reason=overage.disabled_reason if overage is not None else None,
            fallback=self.fallback or None,
            requests_remaining=self.requests_remaining,
            requests_limit=self.requests_limit,
            tokens_remaining=self.tokens_remaining,
            checked_at=_iso(self.checked_at),
            status=self.status.value,
        )


@dataclass(frozen=True)
class LiveProbeFacts:
    """What only a LIVE probe response carries — the cache stores none of it.

    Bundled because the three arrive together on one response and are read together on
    one row; a cached row takes the all-default instance and renders them as unknown.
    """

    overage: OverageUsage | None = None
    thresholds: WarningThresholds = DEFAULT_WARNING_THRESHOLDS
    fallback: str = ""


NO_LIVE_PROBE = LiveProbeFacts()


def oauth_row(
    account: RowIdentity,
    reading: AnthropicTokenUsage | TokenHealthReading,
    *,
    checked_at: dt.datetime | None = None,
    probe: LiveProbeFacts = NO_LIVE_PROBE,
) -> TokenAccountRow:
    """An OAuth row from a live probe's reading OR a cached ``pass`` usage row.

    Both sources expose the same utilization / reset / exhaustion / re-arm interface, so a
    live reading and a stored verdict classify identically through one builder. *overage*
    rides only the live path — the cache does not carry it.
    """
    return TokenAccountRow(
        account=account.account,
        kind=account.kind,
        source=account.source,
        scopes=account.scopes,
        organization_id=reading.organization_id,
        utilization_5h=reading.utilization_5h,
        utilization_7d=reading.utilization_7d,
        next_window_reset=reading.reset_5h,
        weekly_reset=reading.reset_7d,
        overage=probe.overage,
        thresholds=probe.thresholds,
        fallback=probe.fallback,
        frees_up_at=reading.frees_up_at,
        checked_at=checked_at,
        status=_status_for(
            reading.utilization_5h,
            reading.utilization_7d,
            exhausted=reading.is_exhausted,
            thresholds=probe.thresholds,
        ),
    )


def metered_row(
    account: RowIdentity, snapshot: MeteredKeySnapshot, *, checked_at: dt.datetime | None = None
) -> TokenAccountRow:
    status = TokenStatus.OUT_OF_CREDITS if snapshot.out_of_credits else TokenStatus.HEALTHY
    return TokenAccountRow(
        account=account.account,
        kind=TokenKind.API_KEY,
        source=account.source,
        scopes=account.scopes,
        organization_id=snapshot.organization_id,
        utilization_5h=None,
        utilization_7d=None,
        weekly_reset=None,
        checked_at=checked_at,
        status=status,
        requests_remaining=snapshot.requests_remaining,
        requests_limit=snapshot.requests_limit,
        tokens_remaining=snapshot.tokens_remaining,
    )


def blank_row(account: RowIdentity, status: TokenStatus) -> TokenAccountRow:
    """A row with no reading at all — every measured cell renders ``—``."""
    return TokenAccountRow(
        account=account.account,
        kind=account.kind,
        source=account.source,
        scopes=account.scopes,
        organization_id="",
        utilization_5h=None,
        utilization_7d=None,
        weekly_reset=None,
        status=status,
    )


def _status_for(
    utilization_5h: float | None,
    utilization_7d: float | None,
    *,
    exhausted: bool,
    thresholds: WarningThresholds = DEFAULT_WARNING_THRESHOLDS,
) -> TokenStatus:
    if exhausted:
        return TokenStatus.EXHAUSTED
    if warning_windows(
        utilization_5h=used_fraction(utilization_5h),
        utilization_7d=used_fraction(utilization_7d),
        thresholds=thresholds,
    ):
        return TokenStatus.WARNING
    return TokenStatus.HEALTHY


def _status_cell(status: TokenStatus) -> str:
    label = status.value.upper()
    return f"! {label}" if status in _ALARMING else label


def _pct(fraction: float | None) -> str:
    """A 0.0-1.0 fraction as a whole percent, or ``—`` when it was never reported."""
    return "—" if fraction is None else f"{fraction * 100:.0f}%"


def _iso(moment: dt.datetime | None) -> str | None:
    return moment.astimezone().isoformat() if moment is not None else None


def _local_time(moment: dt.datetime | None) -> str:
    """A reset instant rendered in the local zone, or ``—`` when unknown."""
    if moment is None:
        return "—"
    return moment.astimezone().strftime("%Y-%m-%d %H:%M %Z")


_AGE_UNITS = ((86400, "d"), (3600, "h"), (60, "m"))


def _age(moment: dt.datetime | None, now: dt.datetime) -> str:
    """How long ago *moment* was, coarsely — ``—`` when unknown."""
    if moment is None:
        return "—"
    seconds = max(0.0, (now - moment).total_seconds())
    for size, suffix in _AGE_UNITS:
        if seconds >= size:
            return f"{int(seconds // size)}{suffix} ago"
    return "just now"


def _remaining_cell(label: str, remaining: int | None, limit: int | None) -> str:
    """A metered per-minute cell: ``label remaining/limit`` (or ``label remaining``), else ``—``."""
    if remaining is None:
        return "—"
    if limit is not None:
        return f"{label} {remaining}/{limit}"
    return f"{label} {remaining}"


_COLUMNS: tuple[str, ...] = (
    "account",
    "kind",
    "overlays",
    "org",
    "5h",
    "7d",
    "extra usage",
    "5h reset",
    "weekly reset",
    "status",
)
_AGE_COLUMN = "as of"

_LIVE_TITLE = "Anthropic account token health"
_CACHED_TITLE = "Anthropic account token health — CACHED routing view, not probed"

#: Rendered below the table only when an api_key row is present, so the shared 5h/7d
#: columns and the missing dollar balance are not misread.
_API_KEY_CAPTION = (
    "api_key rows: '5h' → requests remaining, '7d' → tokens remaining (per-minute); "
    "status is credit state. Exact prepaid $ balance isn't available via a standard key."
)


def render_table(rows: list[TokenAccountRow], *, from_cache: bool = False) -> str:
    if not rows:
        return "No Anthropic accounts configured (set anthropic_oauth_pass_paths / anthropic_api_key_pass_paths)."
    columns = (*_COLUMNS, _AGE_COLUMN) if from_cache else _COLUMNS
    caption = _API_KEY_CAPTION if any(row.is_api_key for row in rows) else None
    table = Table(title=_CACHED_TITLE if from_cache else _LIVE_TITLE, caption=caption)
    for column in columns:
        table.add_column(column, no_wrap=column in {"account", "overlays", "org"})
    now = timezone.now()
    for row in rows:
        cells = [
            row.account,
            row.kind.value,
            row.overlays_label,
            row.organization_id or "—",
            row.col_5h,
            row.col_7d,
            row.col_extra_usage,
            row.col_next_window,
            row.col_reset,
            _status_cell(row.status),
        ]
        if from_cache:
            cells.append(_age(row.checked_at, now))
        table.add_row(*cells, style=_ROW_STYLE.get(row.status))
    console = Console(width=_RENDER_WIDTH)
    with console.capture() as capture:
        console.print(table)
    return capture.get()
