r"""Per-account Anthropic token-health report (``t3 tokens``).

A read-oriented diagnostic over the SAME routing state the per-account selector
(``teatree.credential_config``) drives: it enumerates every configured ``pass``
entry — the per-overlay OAuth + API-key candidate lists across all scopes plus
global — and reports each account's org id + health status, rendered per credential
kind because Anthropic exposes their headroom differently:

*   An **OAuth** row shows the account's unified 5h / weekly utilization, its extra-usage
    (overage) balance, the 5h next-window reset and the weekly reset. It reads the token
    from ``pass`` and probes it through
    :func:`~teatree.llm.rate_limits.read_rate_limits`, upserting the cache the selector reads.
*   An **API-key** row shows the metered credit state (HEALTHY when funded /
    OUT_OF_CREDITS when depleted) + per-minute requests / tokens remaining — NOT weekly
    utilization, and NOT a dollar balance (unavailable via a standard key). It is probed
    through :func:`~teatree.llm.rate_limits.read_api_key_status`.

**The default report ALWAYS probes and never renders a cached row.** An operator asking
for token health is asking what is true NOW, and a cached row can be wrong in every field
— it is written by the reactive exhaustion path, whose verdict is about whichever account
was sticky at the time. ``from_cache`` (``t3 tokens --cached``) is the opt-in inverse:
render the stored verdict, probe nothing, and say so — the view of what the ROUTER
believes. An account with no stored verdict renders :attr:`TokenStatus.UNCACHED`, never a
fabricated zero.

Rows are ordered best-account-first (:func:`_routing_preference`) so the top row is the
one a new task should run on. Probes are independent and network-bound, so they run
concurrently; the ORM is touched only back on the calling thread.

Alongside the configured ``pass`` rows, the ``--token`` option adds one row per ad-hoc
token (``ad_hoc_tokens``), labelled ``token[1]``, ``token[2]``, … in first-seen order
(deduped), kept LAST and unsorted. This probes a freshly-minted token BEFORE it is
written into ``pass`` (its recovery flow): probed FRESH, never touching the
:class:`AnthropicTokenUsage` cache, its kind auto-detected from the prefix
(``sk-ant-oat01`` → OAuth, ``sk-ant-api03`` → metered key). An empty token renders
``MISSING``; an unrecognised prefix renders ``UNREACHABLE`` WITHOUT transmitting the
token (the auth scheme is unknowable).

The token that signs a probe is read only to sign it — never rendered, logged, or
returned; only the ``token[N]`` position is ever the account label, never the token
value. The readers and secret reader are injected (defaults: the real
``read_rate_limits`` / ``read_api_key_status`` / ``read_pass``) so a test drives canned
health + tokens with no network or ``pass``.
"""

import datetime as dt
from concurrent.futures import ThreadPoolExecutor

from django.utils import timezone

from teatree.core.models.anthropic_token_usage import (
    DEFAULT_WARNING_THRESHOLDS,
    AnthropicTokenUsage,
    WarningThresholds,
    fingerprint_token,
)
from teatree.core.models.config_setting import ConfigSetting
from teatree.credential_config import LIST_SETTING, TokenKind, reading_from
from teatree.llm.rate_limits import (
    MeteredKeyReader,
    MeteredKeySnapshot,
    RateLimitProbeError,
    RateLimitReader,
    RateLimitSnapshot,
    read_api_key_status,
    read_rate_limits,
)
from teatree.token_rows import (
    LiveProbeFacts,
    RowIdentity,
    TokenAccountPayload,
    TokenAccountRow,
    TokenSource,
    TokenStatus,
    blank_row,
    metered_row,
    oauth_row,
    render_table,
)
from teatree.utils.secrets import SecretReader, read_pass

__all__ = [
    "RowIdentity",
    "TokenAccountPayload",
    "TokenAccountRow",
    "TokenReport",
    "TokenSource",
    "TokenStatus",
    "render_table",
]

# Probes are independent HTTP round trips against different accounts; run serially the
# five-account report measured 5m36s of wall clock.
_MAX_PROBE_WORKERS = 8

# Anthropic token prefixes the ad-hoc ``--token`` path routes on: an OAuth subscription
# token probes the unified windows; a metered API key probes its credit state.
_OAUTH_PREFIX = "sk-ant-oat01"
_API_KEY_PREFIX = "sk-ant-api03"

#: One account's probe result. A :class:`TokenStatus` means there is no reading at all
#: (no stored token, or the probe failed) — the union keeps the network phase ORM-free.
type ProbeOutcome = RateLimitSnapshot | MeteredKeySnapshot | TokenStatus


class TokenReport:
    """Build the per-account health rows from the configured ``pass`` lists.

    Probes every configured account live and upserts the shared health cache, so the
    rendered numbers are measured rather than remembered. ``from_cache`` renders the
    stored routing verdict instead and performs NO probe. Both readers and the secret
    reader are injectable for a network-free test.
    """

    def __init__(
        self,
        *,
        reader: RateLimitReader | None = None,
        secret_reader: SecretReader | None = None,
        api_key_reader: MeteredKeyReader | None = None,
        ad_hoc_tokens: list[str] | None = None,
        from_cache: bool = False,
    ) -> None:
        self._reader = reader or read_rate_limits
        self._secret_reader = secret_reader or read_pass
        self._api_key_reader = api_key_reader or read_api_key_status
        self._ad_hoc_tokens = _dedup_tokens(ad_hoc_tokens or [])
        self._from_cache = from_cache

    def rows(self) -> list[TokenAccountRow]:
        now = timezone.now()
        ad_hoc = [self._ad_hoc_row(index, token, now) for index, token in enumerate(self._ad_hoc_tokens, start=1)]
        return sorted(self._pass_rows(now), key=_routing_preference) + ad_hoc

    def render(self) -> str:
        return render_table(self.rows(), from_cache=self._from_cache)

    def _pass_rows(self, now: dt.datetime) -> list[TokenAccountRow]:
        accounts = _configured()
        if self._from_cache:
            return [_cached_row(account) for account in accounts]
        probes = self._probe_all(accounts)
        return [
            _row_from(account, outcome, now, fingerprint=fingerprint)
            for account, (outcome, fingerprint) in zip(accounts, probes, strict=True)
        ]

    def _probe_all(self, accounts: list[RowIdentity]) -> list[tuple[ProbeOutcome, str]]:
        """Every account's probe outcome, in the accounts' own order — ORM untouched here."""
        if not accounts:
            return []
        with ThreadPoolExecutor(max_workers=min(_MAX_PROBE_WORKERS, len(accounts))) as pool:
            kinds = [account.kind for account in accounts]
            paths = [account.account for account in accounts]
            return list(pool.map(self._probe, kinds, paths))

    def _probe(self, kind: TokenKind, pass_path: str) -> tuple[ProbeOutcome, str]:
        """Read *pass_path*'s token, probe it the way its *kind* authenticates, and fingerprint it.

        Deliberately ORM-free so :meth:`_probe_all` can run it on a worker thread — a
        Django connection is per-thread and would sit outside the caller's transaction. The
        fingerprint binds the cached verdict to the credential that produced it (#4736).
        """
        token = self._secret_reader(pass_path)
        if not token:
            return TokenStatus.MISSING, ""
        fingerprint = fingerprint_token(token)
        try:
            if kind is TokenKind.API_KEY:
                return self._api_key_reader(token), fingerprint
            return self._reader(token, is_oauth=True), fingerprint
        except RateLimitProbeError:
            return TokenStatus.UNREACHABLE, fingerprint

    def _ad_hoc_row(self, index: int, token: str, now: dt.datetime) -> TokenAccountRow:
        """One ``--token`` row: probed FRESH (never cache-backed), labelled ``token[N]``.

        The token is never the account label — only its position is. An empty token is
        ``MISSING``; an unrecognised prefix is ``UNREACHABLE`` without transmitting the
        token (the auth scheme is unknowable). A recognised token is probed the way its
        detected kind authenticates and rendered from the live snapshot, bypassing the
        :class:`AnthropicTokenUsage` cache entirely (an ad-hoc token has no ``pass`` key).
        Under ``--cached`` nothing is probed, and an ad-hoc token the router has never
        seen is ``UNCACHED``.
        """
        kind = _detect_kind(token)
        identity = RowIdentity(f"token[{index}]", kind or TokenKind.OAUTH, TokenSource.AD_HOC)
        if self._from_cache:
            return blank_row(identity, TokenStatus.UNCACHED)
        if kind is None:
            return blank_row(identity, TokenStatus.MISSING if not token else TokenStatus.UNREACHABLE)
        if kind is TokenKind.API_KEY:
            try:
                snapshot = self._api_key_reader(token)
            except RateLimitProbeError:
                return blank_row(identity, TokenStatus.UNREACHABLE)
            return metered_row(identity, snapshot, checked_at=now)
        try:
            oauth_snapshot = self._reader(token, is_oauth=True)
        except RateLimitProbeError:
            return blank_row(identity, TokenStatus.UNREACHABLE)
        return oauth_row(identity, reading_from(oauth_snapshot), checked_at=now, probe=_live_facts(oauth_snapshot))


#: How the three no-reading verdicts rank against each other, most actionable first: a
#: MISSING account is fixed by adding the secret, an UNREACHABLE one needs the probe
#: diagnosed, and an UNCACHED one only says this run was `--cached`. Every measured status
#: shares rank 0 and is already ahead of all three, so this never reorders them.
_NO_READING_RANK: dict[TokenStatus, int] = {
    TokenStatus.MISSING: 1,
    TokenStatus.UNREACHABLE: 2,
    TokenStatus.UNCACHED: 3,
}


def _routing_preference(row: TokenAccountRow) -> tuple[bool, int, bool, float, bool, float, bool]:
    """Best-account-first ordering: the top row is the one a new task should run on.

    Unmeasurable rows sink to the bottom (they tell an operator nothing about headroom) and
    are ordered among themselves by :data:`_NO_READING_RANK` — without it all three shared
    one key and fell to the input order of an unordered queryset; OAuth precedes metered
    keys because a subscription draws no per-token bill; then the most headroom on the
    binding window, with the soonest re-arm breaking a tie among spent accounts; a funded
    key precedes an out-of-credits one.
    """
    frees_up = row.frees_up_at
    return (
        not row.status.is_measured,
        _NO_READING_RANK.get(row.status, 0),
        row.is_api_key,
        -row.headroom,
        frees_up is None,
        frees_up.timestamp() if frees_up is not None else 0.0,
        row.status is TokenStatus.OUT_OF_CREDITS,
    )


def _configured() -> list[RowIdentity]:
    """Every configured ``(kind, pass_path)`` as a row identity carrying its scopes.

    Reads the routing config keys directly so the report's account set is the same
    one the selector routes over; ``""`` scope is global, any other is an overlay.
    """
    scopes_by_account: dict[tuple[TokenKind, str], list[str]] = {}
    for kind in TokenKind:
        # Ordered: this feeds a stable sort, so an unordered queryset leaves any pair the
        # preference key ties on to the database's row order.
        rows = (
            ConfigSetting.objects.filter(key=LIST_SETTING[kind]).order_by("scope", "pk").values_list("scope", "value")
        )
        for scope, value in rows:
            for pass_path in _as_path_list(value):
                scopes = scopes_by_account.setdefault((kind, pass_path), [])
                if scope not in scopes:
                    scopes.append(scope)
    return [
        RowIdentity(pass_path, kind, TokenSource.STORE, tuple(sorted(scopes)))
        for (kind, pass_path), scopes in scopes_by_account.items()
    ]


def _row_from(account: RowIdentity, outcome: ProbeOutcome, now: dt.datetime, *, fingerprint: str) -> TokenAccountRow:
    """One probed account's row; an OAuth reading also refreshes the shared health cache."""
    if isinstance(outcome, TokenStatus):
        return blank_row(account, outcome)
    if isinstance(outcome, MeteredKeySnapshot):
        return metered_row(account, outcome, checked_at=now)
    reading = reading_from(outcome)
    AnthropicTokenUsage.objects.record(account.account, reading, now=now, token_fingerprint=fingerprint)
    return oauth_row(account, reading, checked_at=now, probe=_live_facts(outcome))


def _live_facts(snapshot: RateLimitSnapshot) -> LiveProbeFacts:
    """The live-only fields of *snapshot*, with each unreported warning band defaulted."""
    return LiveProbeFacts(
        overage=snapshot.overage,
        fallback=snapshot.fallback,
        thresholds=WarningThresholds(
            five_hour=_band(snapshot.warn_above_5h, DEFAULT_WARNING_THRESHOLDS.five_hour),
            seven_day=_band(snapshot.warn_above_7d, DEFAULT_WARNING_THRESHOLDS.seven_day),
        ),
    )


def _band(reported: float | None, shipped: float) -> float:
    """The API's own warning band where it reports one, else teatree's shipped guess."""
    return shipped if reported is None else reported


def _cached_row(account: RowIdentity) -> TokenAccountRow:
    """What the ROUTER believes about this account — the stored verdict, no probe.

    Overage is absent by design: it is display-only, so the cache carries no column for it.
    """
    cached = AnthropicTokenUsage.objects.filter(pass_path=account.account).first()
    if cached is None:
        return blank_row(account, TokenStatus.UNCACHED)
    return oauth_row(account, cached, checked_at=cached.checked_at)


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


def _as_path_list(stored: object) -> list[str]:
    if not isinstance(stored, list):
        return []
    seen: dict[str, None] = {}
    for item in stored:
        text = str(item).strip()
        if text:
            seen.setdefault(text, None)
    return list(seen)
