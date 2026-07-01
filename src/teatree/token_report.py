r"""Per-account Anthropic token-health report (``t3 tokens``).

A read-oriented diagnostic over the SAME routing state the per-account selector
(``teatree.credential_config``) drives: it enumerates every configured ``pass``
entry — the per-overlay OAuth + API-key candidate lists across all scopes plus
global — and reports each account's unified 5h / weekly utilization, org id,
weekly reset, and a health status.

For each account it reuses a FRESH cached :class:`AnthropicTokenUsage` row with no
network; on a cache miss / expiry it reads the account's token from ``pass`` and
probes it once through the foundation reader
(:func:`~teatree.llm.rate_limits.read_rate_limits`), upserting the same health
cache the selector reads. The token that signs a probe is read only to sign it —
it is never rendered, logged, or returned (a MISSING account is one whose ``pass``
entry is empty; an UNREACHABLE one is a transport/HTTP failure).

The reader and secret reader are injected (defaults: the real ``read_rate_limits``
/ ``read_pass``) so a test drives canned health + tokens with no network or ``pass``.
"""

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TypedDict

from django.utils import timezone
from rich.console import Console
from rich.table import Table

from teatree.core.models.anthropic_token_usage import AnthropicTokenUsage
from teatree.core.models.config_setting import GLOBAL_SCOPE, ConfigSetting
from teatree.credential_config import LIST_SETTING, TokenKind, reading_from
from teatree.llm.rate_limits import RateLimitProbeError, RateLimitReader, read_rate_limits
from teatree.utils.secrets import read_pass

# Utilization at/above which a healthy account is flagged as a warning — below the
# routing exhaustion limits (5h ≥ 0.95 / 7d ≥ 0.99) so a warning precedes exhaustion.
WARNING_5H = 0.80
WARNING_7D = 0.90

_RENDER_WIDTH = 200

#: A ``pass``-store reader: given a ``pass`` entry, return its token (``""`` when absent).
type SecretReader = Callable[[str], str]


class TokenStatus(StrEnum):
    """One account's rendered health verdict."""

    HEALTHY = "healthy"
    WARNING = "warning"
    EXHAUSTED = "exhausted"
    MISSING = "missing"
    UNREACHABLE = "unreachable"

    @property
    def is_measured(self) -> bool:
        """Whether a live/cached reading exists (``MISSING`` / ``UNREACHABLE`` have none)."""
        return self not in {TokenStatus.MISSING, TokenStatus.UNREACHABLE}


class TokenAccountPayload(TypedDict):
    """The token-free JSON shape of one account row (``t3 tokens --json``)."""

    pass_path: str
    kind: str
    overlays: list[str]
    organization_id: str
    utilization_5h: float | None
    utilization_7d: float | None
    weekly_reset: str | None
    status: str


_ALARMING = {TokenStatus.EXHAUSTED, TokenStatus.MISSING, TokenStatus.UNREACHABLE}
_ROW_STYLE: dict[TokenStatus, str] = {
    TokenStatus.EXHAUSTED: "bold red",
    TokenStatus.MISSING: "red",
    TokenStatus.UNREACHABLE: "red",
    TokenStatus.WARNING: "yellow",
    TokenStatus.HEALTHY: "green",
}


@dataclass(frozen=True)
class TokenAccountRow:
    """One configured account's health, ready to render — never carries the token."""

    pass_path: str
    kind: TokenKind
    scopes: tuple[str, ...]
    organization_id: str
    utilization_5h: float
    utilization_7d: float
    weekly_reset: dt.datetime | None
    status: TokenStatus

    @property
    def overlay_labels(self) -> tuple[str, ...]:
        return tuple("global" if scope == GLOBAL_SCOPE else scope for scope in self.scopes)

    @property
    def overlays_label(self) -> str:
        return ", ".join(self.overlay_labels)

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

    def as_dict(self) -> TokenAccountPayload:
        return TokenAccountPayload(
            pass_path=self.pass_path,
            kind=self.kind.value,
            overlays=list(self.overlay_labels),
            organization_id=self.organization_id,
            utilization_5h=self.utilization_5h if self.status.is_measured else None,
            utilization_7d=self.utilization_7d if self.status.is_measured else None,
            weekly_reset=self.weekly_reset.astimezone().isoformat() if self.weekly_reset else None,
            status=self.status.value,
        )


class TokenReport:
    """Build the per-account health rows from the configured ``pass`` lists.

    Reuses a fresh cached health row with no probe; else reads the account token
    from ``pass`` and probes it once, upserting the shared health cache. Both the
    reader and the secret reader are injectable for a network-free test.
    """

    def __init__(self, *, reader: RateLimitReader | None = None, secret_reader: SecretReader | None = None) -> None:
        self._reader = reader or read_rate_limits
        self._secret_reader = secret_reader or read_pass

    def rows(self) -> list[TokenAccountRow]:
        now = timezone.now()
        return [self._row_for(kind, pass_path, scopes, now) for (kind, pass_path), scopes in _configured().items()]

    def render(self) -> str:
        return render_table(self.rows())

    def _row_for(self, kind: TokenKind, pass_path: str, scopes: tuple[str, ...], now: dt.datetime) -> TokenAccountRow:
        cached = AnthropicTokenUsage.objects.filter(pass_path=pass_path).first()
        if cached is not None and cached.is_fresh(now):
            return _row_from_usage(kind, pass_path, scopes, cached)
        token = self._secret_reader(pass_path)
        if not token:
            return _blank_row(kind, pass_path, scopes, TokenStatus.MISSING)
        try:
            snapshot = self._reader(token, is_oauth=kind is TokenKind.OAUTH)
        except RateLimitProbeError:
            return _blank_row(kind, pass_path, scopes, TokenStatus.UNREACHABLE)
        probed = AnthropicTokenUsage.objects.record(pass_path, reading_from(snapshot), now=now)
        return _row_from_usage(kind, pass_path, scopes, probed)


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
    kind: TokenKind, pass_path: str, scopes: tuple[str, ...], usage: AnthropicTokenUsage
) -> TokenAccountRow:
    return TokenAccountRow(
        pass_path=pass_path,
        kind=kind,
        scopes=scopes,
        organization_id=usage.organization_id,
        utilization_5h=usage.utilization_5h,
        utilization_7d=usage.utilization_7d,
        weekly_reset=usage.reset_7d,
        status=_status_for(usage.utilization_5h, usage.utilization_7d, exhausted=usage.is_exhausted),
    )


def _blank_row(kind: TokenKind, pass_path: str, scopes: tuple[str, ...], status: TokenStatus) -> TokenAccountRow:
    return TokenAccountRow(
        pass_path=pass_path,
        kind=kind,
        scopes=scopes,
        organization_id="",
        utilization_5h=0.0,
        utilization_7d=0.0,
        weekly_reset=None,
        status=status,
    )


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


def render_table(rows: list[TokenAccountRow]) -> str:
    if not rows:
        return "No Anthropic accounts configured (set anthropic_oauth_pass_paths / anthropic_api_key_pass_paths)."
    table = Table(title="Anthropic account token health")
    for column in _COLUMNS:
        table.add_column(column, no_wrap=column in {"account", "overlays", "org"})
    for row in rows:
        table.add_row(
            row.pass_path,
            row.kind.value,
            row.overlays_label,
            row.organization_id or "—",
            row.utilization_5h_pct,
            row.utilization_7d_pct,
            row.weekly_reset_local,
            _status_cell(row.status),
            style=_ROW_STYLE.get(row.status),
        )
    console = Console(width=_RENDER_WIDTH)
    with console.capture() as capture:
        console.print(table)
    return capture.get()
