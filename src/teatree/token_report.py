r"""Per-account Anthropic token-health report (``t3 tokens``).

A read-oriented diagnostic over the SAME routing state the per-account selector
(``teatree.credential_config``) drives: it enumerates every configured ``pass``
entry — the per-overlay OAuth + API-key candidate lists across all scopes plus
global — and reports each account's org id + health status, rendered per credential
kind because Anthropic exposes their headroom differently:

*   An **OAuth** row shows the account's unified 5h / weekly utilization + weekly
    reset. For it the report reuses a FRESH cached :class:`AnthropicTokenUsage` row
    with no network; on a cache miss / expiry it reads the token from ``pass`` and
    probes once through :func:`~teatree.llm.rate_limits.read_rate_limits`, upserting the
    same health cache the selector reads.
*   An **API-key** row shows the metered credit state (HEALTHY when funded /
    OUT_OF_CREDITS when depleted) + per-minute requests / tokens remaining — NOT weekly
    utilization, and NOT a dollar balance (unavailable via a standard key). A metered
    key emits no unified windows and cannot be represented in the unified cache, so it
    is probed fresh each run through :func:`~teatree.llm.rate_limits.read_api_key_status`.

Alongside the configured ``pass`` rows, the ``--token`` option adds one row per ad-hoc
token (``ad_hoc_tokens``), labelled ``token[1]``, ``token[2]``, … in the order given
(deduped, first-seen order). This health-probes a freshly-minted token BEFORE it is
written into ``pass`` — its recovery flow. An ad-hoc token has no ``pass`` entry, so it
is probed FRESH and never reads from or writes to the :class:`AnthropicTokenUsage` cache;
its kind is auto-detected from the token prefix (``sk-ant-oat01`` → OAuth,
``sk-ant-api03`` → metered key). An empty token renders ``MISSING``; an unrecognised
prefix renders ``UNREACHABLE`` WITHOUT transmitting the token anywhere (the auth scheme
is unknowable, so no probe is attempted). The token value itself is never the account
label — only the ``token[N]`` position is — so, like a ``pass`` row, it is never
rendered, logged, or returned.

The token that signs a probe is read only to sign it — it is never rendered, logged,
or returned (a MISSING account is one whose ``pass`` entry is empty; an UNREACHABLE one
is a transport/HTTP failure). The readers and secret reader are injected (defaults: the
real ``read_rate_limits`` / ``read_api_key_status`` / ``read_pass``) so a test drives
canned health + tokens with no network or ``pass``.
"""

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from itertools import starmap
from typing import TypedDict

from django.utils import timezone
from rich.console import Console
from rich.table import Table

from teatree.core.models.anthropic_token_usage import AnthropicTokenUsage, TokenHealthReading
from teatree.core.models.config_setting import GLOBAL_SCOPE, ConfigSetting
from teatree.credential_config import LIST_SETTING, TokenKind, reading_from
from teatree.llm.rate_limits import (
    MeteredKeyReader,
    MeteredKeySnapshot,
    RateLimitProbeError,
    RateLimitReader,
    read_api_key_status,
    read_rate_limits,
)
from teatree.utils.secrets import read_pass

# Utilization at/above which a healthy account is flagged as a warning — below the
# routing exhaustion limits (5h ≥ 0.95 / 7d ≥ 0.99) so a warning precedes exhaustion.
WARNING_5H = 0.80
WARNING_7D = 0.90

_RENDER_WIDTH = 200

#: A ``pass``-store reader: given a ``pass`` entry, return its token (``""`` when absent).
type SecretReader = Callable[[str], str]

# Anthropic token prefixes the ad-hoc ``--token`` path routes on: an OAuth subscription
# token probes the unified windows; a metered API key probes its credit state.
_OAUTH_PREFIX = "sk-ant-oat01"
_API_KEY_PREFIX = "sk-ant-api03"


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
    balance) — both are alarming and block routing.
    """

    HEALTHY = "healthy"
    WARNING = "warning"
    EXHAUSTED = "exhausted"
    OUT_OF_CREDITS = "out_of_credits"
    MISSING = "missing"
    UNREACHABLE = "unreachable"

    @property
    def is_measured(self) -> bool:
        """Whether a live/cached reading exists (``MISSING`` / ``UNREACHABLE`` have none)."""
        return self not in {TokenStatus.MISSING, TokenStatus.UNREACHABLE}


class TokenAccountPayload(TypedDict):
    """The token-free JSON shape of one account row (``t3 tokens --json``).

    OAuth rows carry ``utilization_*`` / ``weekly_reset``; API-key rows carry the
    per-minute ``requests_*`` / ``tokens_remaining`` instead — the inapplicable set is
    ``None`` on each kind. ``account`` is the ``pass`` entry for a ``pass`` row and the
    ``token[N]`` label for an ad-hoc ``--token`` row; ``source`` discriminates the two.
    """

    account: str
    source: str
    kind: str
    overlays: list[str]
    organization_id: str
    utilization_5h: float | None
    utilization_7d: float | None
    weekly_reset: str | None
    requests_remaining: int | None
    requests_limit: int | None
    tokens_remaining: int | None
    status: str


_ALARMING = {TokenStatus.EXHAUSTED, TokenStatus.OUT_OF_CREDITS, TokenStatus.MISSING, TokenStatus.UNREACHABLE}
_ROW_STYLE: dict[TokenStatus, str] = {
    TokenStatus.EXHAUSTED: "bold red",
    TokenStatus.OUT_OF_CREDITS: "bold red",
    TokenStatus.MISSING: "red",
    TokenStatus.UNREACHABLE: "red",
    TokenStatus.WARNING: "yellow",
    TokenStatus.HEALTHY: "green",
}


@dataclass(frozen=True)
class TokenAccountRow:
    """One configured account's health, ready to render — never carries the token.

    OAuth rows populate ``utilization_*`` / ``weekly_reset``; API-key rows populate the
    per-minute ``requests_*`` / ``tokens_remaining`` instead. The ``col_*`` cells render
    the applicable set per kind so the shared table stays honest. ``account`` is the
    ``pass`` entry for a ``pass`` row and the ``token[N]`` label for an ad-hoc row —
    never a token value; ``source`` says which.
    """

    account: str
    kind: TokenKind
    source: TokenSource
    scopes: tuple[str, ...]
    organization_id: str
    utilization_5h: float
    utilization_7d: float
    weekly_reset: dt.datetime | None
    status: TokenStatus
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
    def utilization_5h_pct(self) -> str:
        return _pct(self.utilization_5h) if self.status.is_measured else "—"

    @property
    def utilization_7d_pct(self) -> str:
        return _pct(self.utilization_7d) if self.status.is_measured else "—"

    @property
    def weekly_reset_local(self) -> str:
        if self.weekly_reset is None:
            return "—"
        return self.weekly_reset.astimezone().strftime("%Y-%m-%d %H:%M %Z")

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
    def col_reset(self) -> str:
        """The "weekly reset" cell — inapplicable to per-minute API-key limits."""
        return "—" if self.is_api_key else self.weekly_reset_local

    def as_dict(self) -> TokenAccountPayload:
        oauth_measured = self.status.is_measured and not self.is_api_key
        return TokenAccountPayload(
            account=self.account,
            source=self.source.value,
            kind=self.kind.value,
            overlays=list(self.overlay_labels),
            organization_id=self.organization_id,
            utilization_5h=self.utilization_5h if oauth_measured else None,
            utilization_7d=self.utilization_7d if oauth_measured else None,
            weekly_reset=self.weekly_reset.astimezone().isoformat() if self.weekly_reset else None,
            requests_remaining=self.requests_remaining,
            requests_limit=self.requests_limit,
            tokens_remaining=self.tokens_remaining,
            status=self.status.value,
        )


class TokenReport:
    """Build the per-account health rows from the configured ``pass`` lists.

    Reuses a fresh cached health row with no probe; else reads the account token
    from ``pass`` and probes it once, upserting the shared health cache. Both the
    reader and the secret reader are injectable for a network-free test.
    """

    def __init__(
        self,
        *,
        reader: RateLimitReader | None = None,
        secret_reader: SecretReader | None = None,
        api_key_reader: MeteredKeyReader | None = None,
        ad_hoc_tokens: list[str] | None = None,
    ) -> None:
        self._reader = reader or read_rate_limits
        self._secret_reader = secret_reader or read_pass
        self._api_key_reader = api_key_reader or read_api_key_status
        self._ad_hoc_tokens = _dedup_tokens(ad_hoc_tokens or [])

    def rows(self) -> list[TokenAccountRow]:
        now = timezone.now()
        pass_rows = [self._row_for(kind, pass_path, scopes, now) for (kind, pass_path), scopes in _configured().items()]
        ad_hoc_rows = list(starmap(self._ad_hoc_row, enumerate(self._ad_hoc_tokens, start=1)))
        return pass_rows + ad_hoc_rows

    def render(self) -> str:
        return render_table(self.rows())

    def _row_for(self, kind: TokenKind, pass_path: str, scopes: tuple[str, ...], now: dt.datetime) -> TokenAccountRow:
        if kind is TokenKind.API_KEY:
            return self._api_key_row(pass_path, scopes)
        cached = AnthropicTokenUsage.objects.filter(pass_path=pass_path).first()
        if cached is not None and cached.is_fresh(now):
            return _row_from_usage(kind, pass_path, scopes, cached)
        token = self._secret_reader(pass_path)
        if not token:
            return _blank_row(kind, pass_path, scopes, TokenStatus.MISSING, source=TokenSource.STORE)
        try:
            snapshot = self._reader(token, is_oauth=True)
        except RateLimitProbeError:
            return _blank_row(kind, pass_path, scopes, TokenStatus.UNREACHABLE, source=TokenSource.STORE)
        probed = AnthropicTokenUsage.objects.record(pass_path, reading_from(snapshot), now=now)
        return _row_from_usage(kind, pass_path, scopes, probed)

    def _api_key_row(self, pass_path: str, scopes: tuple[str, ...]) -> TokenAccountRow:
        """A metered API-key row: probe fresh (no unified cache) and render the credit signal.

        A metered key emits no unified windows, so it cannot be represented in the
        shared ``AnthropicTokenUsage`` cache — it is probed each run and rendered from
        the live :class:`~teatree.llm.rate_limits.MeteredKeySnapshot`.
        """
        token = self._secret_reader(pass_path)
        if not token:
            return _blank_row(TokenKind.API_KEY, pass_path, scopes, TokenStatus.MISSING, source=TokenSource.STORE)
        try:
            snapshot = self._api_key_reader(token)
        except RateLimitProbeError:
            return _blank_row(TokenKind.API_KEY, pass_path, scopes, TokenStatus.UNREACHABLE, source=TokenSource.STORE)
        return _metered_row(pass_path, scopes, snapshot, source=TokenSource.STORE)

    def _ad_hoc_row(self, index: int, token: str) -> TokenAccountRow:
        """One ``--token`` row: probed FRESH (never cache-backed), labelled ``token[N]``.

        The token is never the account label — only its position is. An empty token is
        ``MISSING``; an unrecognised prefix is ``UNREACHABLE`` without transmitting the
        token (the auth scheme is unknowable). A recognised token is probed the way its
        detected kind authenticates and rendered from the live snapshot, bypassing the
        :class:`AnthropicTokenUsage` cache entirely (an ad-hoc token has no ``pass`` key).
        """
        account = f"token[{index}]"
        kind = _detect_kind(token)
        if kind is None:
            status = TokenStatus.MISSING if not token else TokenStatus.UNREACHABLE
            return _blank_row(TokenKind.OAUTH, account, (), status, source=TokenSource.AD_HOC)
        if kind is TokenKind.API_KEY:
            try:
                snapshot = self._api_key_reader(token)
            except RateLimitProbeError:
                return _blank_row(TokenKind.API_KEY, account, (), TokenStatus.UNREACHABLE, source=TokenSource.AD_HOC)
            return _metered_row(account, (), snapshot, source=TokenSource.AD_HOC)
        try:
            oauth_snapshot = self._reader(token, is_oauth=True)
        except RateLimitProbeError:
            return _blank_row(TokenKind.OAUTH, account, (), TokenStatus.UNREACHABLE, source=TokenSource.AD_HOC)
        return _oauth_token_row(account, reading_from(oauth_snapshot))


def _configured() -> dict[tuple[TokenKind, str], tuple[str, ...]]:
    """Every configured ``(kind, pass_path)`` mapped to the scopes that list it.

    Reads the routing config keys directly so the report's account set is the same
    one the selector routes over; ``""`` scope is global, any other is an overlay.
    """
    scopes_by_account: dict[tuple[TokenKind, str], list[str]] = {}
    for kind in TokenKind:
        rows = ConfigSetting.objects.filter(key=LIST_SETTING[kind]).values_list("scope", "value")
        for scope, value in rows:
            for pass_path in _as_path_list(value):
                scopes = scopes_by_account.setdefault((kind, pass_path), [])
                if scope not in scopes:
                    scopes.append(scope)
    return {account: tuple(sorted(scopes)) for account, scopes in scopes_by_account.items()}


def _row_from_usage(
    kind: TokenKind, account: str, scopes: tuple[str, ...], usage: AnthropicTokenUsage
) -> TokenAccountRow:
    return TokenAccountRow(
        account=account,
        kind=kind,
        source=TokenSource.STORE,
        scopes=scopes,
        organization_id=usage.organization_id,
        utilization_5h=usage.utilization_5h,
        utilization_7d=usage.utilization_7d,
        weekly_reset=usage.reset_7d,
        status=_status_for(usage.utilization_5h, usage.utilization_7d, exhausted=usage.is_exhausted),
    )


def _oauth_token_row(account: str, reading: TokenHealthReading) -> TokenAccountRow:
    """An ad-hoc OAuth row built from a live reading — no ``AnthropicTokenUsage`` touch.

    Uses the same exhaustion rule and status mapping as a cached ``pass`` row (via the
    shared :class:`TokenHealthReading`), so an ad-hoc OAuth token classifies identically
    to a configured one while never reading or writing the health cache.
    """
    return TokenAccountRow(
        account=account,
        kind=TokenKind.OAUTH,
        source=TokenSource.AD_HOC,
        scopes=(),
        organization_id=reading.organization_id,
        utilization_5h=reading.utilization_5h,
        utilization_7d=reading.utilization_7d,
        weekly_reset=reading.reset_7d,
        status=_status_for(reading.utilization_5h, reading.utilization_7d, exhausted=reading.is_exhausted),
    )


def _metered_row(
    account: str, scopes: tuple[str, ...], snapshot: MeteredKeySnapshot, *, source: TokenSource
) -> TokenAccountRow:
    status = TokenStatus.OUT_OF_CREDITS if snapshot.out_of_credits else TokenStatus.HEALTHY
    return TokenAccountRow(
        account=account,
        kind=TokenKind.API_KEY,
        source=source,
        scopes=scopes,
        organization_id=snapshot.organization_id,
        utilization_5h=0.0,
        utilization_7d=0.0,
        weekly_reset=None,
        status=status,
        requests_remaining=snapshot.requests_remaining,
        requests_limit=snapshot.requests_limit,
        tokens_remaining=snapshot.tokens_remaining,
    )


def _blank_row(
    kind: TokenKind, account: str, scopes: tuple[str, ...], status: TokenStatus, *, source: TokenSource
) -> TokenAccountRow:
    return TokenAccountRow(
        account=account,
        kind=kind,
        source=source,
        scopes=scopes,
        organization_id="",
        utilization_5h=0.0,
        utilization_7d=0.0,
        weekly_reset=None,
        status=status,
    )


def _detect_kind(token: str) -> TokenKind | None:
    """The credential kind a token's prefix names, or ``None`` when unrecognised/empty."""
    if token.startswith(_OAUTH_PREFIX):
        return TokenKind.OAUTH
    if token.startswith(_API_KEY_PREFIX):
        return TokenKind.API_KEY
    return None


def _dedup_tokens(tokens: list[str]) -> list[str]:
    """Strip and dedup ad-hoc tokens, preserving first-seen order (mirrors ``_as_path_list``).

    Unlike ``_as_path_list`` an empty entry is KEPT (once): an empty ``--token`` is a
    ``MISSING`` row, not a silently-dropped one.
    """
    seen: dict[str, None] = {}
    for token in tokens:
        seen.setdefault(token.strip(), None)
    return list(seen)


def _status_for(utilization_5h: float, utilization_7d: float, *, exhausted: bool) -> TokenStatus:
    if exhausted:
        return TokenStatus.EXHAUSTED
    if utilization_5h >= WARNING_5H or utilization_7d >= WARNING_7D:
        return TokenStatus.WARNING
    return TokenStatus.HEALTHY


def _status_cell(status: TokenStatus) -> str:
    label = status.value.upper()
    return f"! {label}" if status in _ALARMING else label


def _pct(fraction: float) -> str:
    return f"{fraction * 100:.0f}%"


def _remaining_cell(label: str, remaining: int | None, limit: int | None) -> str:
    """A metered per-minute cell: ``label remaining/limit`` (or ``label remaining``), else ``—``."""
    if remaining is None:
        return "—"
    if limit is not None:
        return f"{label} {remaining}/{limit}"
    return f"{label} {remaining}"


def _as_path_list(stored: object) -> list[str]:
    if not isinstance(stored, list):
        return []
    seen: dict[str, None] = {}
    for item in stored:
        text = str(item).strip()
        if text:
            seen.setdefault(text, None)
    return list(seen)


_COLUMNS: tuple[str, ...] = ("account", "kind", "overlays", "org", "5h", "7d", "weekly reset", "status")

#: Rendered below the table only when an api_key row is present, so the shared 5h/7d
#: columns and the missing dollar balance are not misread.
_API_KEY_CAPTION = (
    "api_key rows: '5h' → requests remaining, '7d' → tokens remaining (per-minute); "
    "status is credit state. Exact prepaid $ balance isn't available via a standard key."
)


def render_table(rows: list[TokenAccountRow]) -> str:
    if not rows:
        return "No Anthropic accounts configured (set anthropic_oauth_pass_paths / anthropic_api_key_pass_paths)."
    caption = _API_KEY_CAPTION if any(row.is_api_key for row in rows) else None
    table = Table(title="Anthropic account token health", caption=caption)
    for column in _COLUMNS:
        table.add_column(column, no_wrap=column in {"account", "overlays", "org"})
    for row in rows:
        table.add_row(
            row.account,
            row.kind.value,
            row.overlays_label,
            row.organization_id or "—",
            row.col_5h,
            row.col_7d,
            row.col_reset,
            _status_cell(row.status),
            style=_ROW_STYLE.get(row.status),
        )
    console = Console(width=_RENDER_WIDTH)
    with console.capture() as capture:
        console.print(table)
    return capture.get()
