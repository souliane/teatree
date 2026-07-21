r"""Config-aware, per-account routing factory for the Anthropic credentials.

The domain-layer bridge between the foundation-pure credential mechanics
(``teatree.llm.credentials``), the rate-limit reader (``teatree.llm.rate_limits``),
the DB-backed config store (``ConfigSetting``) and the routing state
(``AnthropicTokenUsage`` health cache + ``AnthropicActivePick`` sticky pointer). The
foundation credential module MUST NOT import the domain models, so the per-account
``pass_path`` override is SELECTED here and INJECTED into the credential as a plain
string.

Each credential *kind* (subscription OAuth / metered API key) reads an ORDERED LIST
of candidate ``pass`` entries from the config store (keys
``anthropic_oauth_pass_paths`` / ``anthropic_api_key_pass_paths``, overlay scope then
global). :class:`PassPathSelector` then routes to the first account that is not
exhausted:

*   An empty list for the REQUESTED scope falls back to the cross-scope union, so a bare
    eval shell (no active overlay → the global scope) still routes to an overlay-scoped
    account without a manual env export. Only when the union is empty (nothing configured
    in ANY scope) does the selector return ``None``. The metered API-key credential then
    keeps its built-in ``pass_path`` (the pre-routing default; the HOT path for
    metered/eval consumers). The subscription OAuth credential has NO built-in default, so
    :func:`resolve_subscription_credential` instead fails loud (unless
    ``CLAUDE_CODE_OAUTH_TOKEN`` is set in the env) — it never lands on a dead entry.
*   A sticky pick whose health-cache row is fresh and non-exhausted is reused with NO
    probe — the hot path reads the CACHED table only, never the network.
*   On a cache miss / expiry each candidate is probed once (the reader), its health
    row upserted, and the first non-exhausted one is pinned as the new sticky pick.
*   When the overlay's own accounts are all exhausted the selector falls back to ANY
    other configured account (across overlays); when every account's CACHED verdict still
    reads exhausted, a last-ditch live re-probe (:meth:`PassPathSelector._rescue_reprobe`,
    cooldown-throttled) runs first — a rolling 5h window frees capacity before its recorded
    reset, so a recovered account is rescued rather than stranding the loop. Only when the
    rescue also finds nothing usable does it raise :class:`AllTokensExhaustedError` (a
    :class:`CredentialError`) naming the earliest reset, halting agent work loudly.

The token that signs a probe is never logged or returned; only its ``pass_path`` and
parsed health are persisted.
"""

import datetime as dt
import logging
import os
from enum import StrEnum
from typing import TYPE_CHECKING

from django.utils import timezone

from teatree.core.models.anthropic_active_pick import AnthropicActivePick
from teatree.core.models.anthropic_token_usage import REJECTED_STATUS, AnthropicTokenUsage, TokenHealthReading
from teatree.core.models.config_setting import GLOBAL_SCOPE, ConfigSetting
from teatree.llm.anthropic_limits import ALL_TOKENS_EXHAUSTED_SIGNATURE
from teatree.llm.credentials import (
    AnthropicApiKeyCredential,
    AnthropicSubscriptionCredential,
    Credential,
    CredentialError,
)
from teatree.llm.rate_limits import (
    MeteredKeyReader,
    MeteredKeySnapshot,
    RateLimitProbeError,
    RateLimitReader,
    RateLimitSnapshot,
    read_api_key_status,
    read_rate_limits,
)
from teatree.utils.eval_container import in_container

if TYPE_CHECKING:
    from teatree.config.agent_enums import EvalCredential

logger = logging.getLogger(__name__)

#: A cached-exhausted verdict is trusted until its recorded window reset, but a rolling
#: 5h window frees capacity continuously — well before that reset. When every account
#: reads exhausted, :meth:`PassPathSelector._rescue_reprobe` re-probes live before
#: declaring a hard stall; this cooldown skips any account probed more recently than
#: this, so a spinning all-exhausted dispatch loop cannot storm the rate-limit API.
_RESCUE_REPROBE_COOLDOWN = dt.timedelta(seconds=60)


class TokenKind(StrEnum):
    """The two Anthropic credential kinds the selector routes independently."""

    OAUTH = "oauth"
    API_KEY = "api_key"


LIST_SETTING: dict[TokenKind, str] = {
    TokenKind.OAUTH: "anthropic_oauth_pass_paths",
    TokenKind.API_KEY: "anthropic_api_key_pass_paths",
}
_CREDENTIAL_CLASS: dict[TokenKind, type[Credential]] = {
    TokenKind.OAUTH: AnthropicSubscriptionCredential,
    TokenKind.API_KEY: AnthropicApiKeyCredential,
}


class AllTokensExhaustedError(CredentialError):
    """Every configured account for a credential kind is exhausted.

    A :class:`CredentialError` subclass so the existing headless / eval
    ``except CredentialError`` handlers record it as a loud dispatch refusal. The
    message names the earliest known reset so the operator knows when work can resume;
    :attr:`earliest_reset` carries the same instant as a datetime so the headless
    quiesce-and-auto-resume park (multi-account #C2) can key its release on it.
    """

    def __init__(self, message: str, *, earliest_reset: dt.datetime | None = None) -> None:
        super().__init__(message)
        #: The soonest an account frees up — the MINIMUM over exhausted accounts of each
        #: account's own ``frees_up_at`` (itself the LATEST of that account's blocking-window
        #: resets, since all of them must clear before it is usable). ``None`` when no
        #: blocking window has a known reset. NOT a token; a schedule instant.
        self.earliest_reset = earliest_reset


class PassPathSelector:
    """Route a credential *kind* to a healthy account's ``pass`` entry.

    The reader is injectable (default: :func:`~teatree.llm.rate_limits.read_rate_limits`)
    so selector tests drive canned health with no network. All DB reads/writes go
    through the health cache and sticky pointer, so the steady-state hot path (a fresh
    sticky pick) never probes.
    """

    def __init__(
        self, *, reader: RateLimitReader | None = None, api_key_reader: MeteredKeyReader | None = None
    ) -> None:
        self._reader = reader
        self._api_key_reader = api_key_reader

    def select(self, kind: TokenKind, scope: str = GLOBAL_SCOPE) -> str | None:
        """The ``pass_path`` override for *kind* in *scope*, or ``None`` for the built-in.

        Reuses a fresh, non-exhausted sticky pick with no probe; else selects the first
        non-exhausted account from the overlay's list, then falls back across overlays.
        When the REQUESTED scope has no routing configured, falls back to the cross-scope
        union — a bare eval shell (no active overlay → :data:`GLOBAL_SCOPE`) still routes
        to an overlay-scoped account without a manual env export. An empty union (nothing
        configured anywhere) returns ``None`` so the caller fails loud downstream.
        When every account's CACHED verdict reads exhausted, a last-ditch live
        :meth:`_rescue_reprobe` runs before the hard fail — a rolling 5h window frees
        capacity before its recorded reset, so a recovered account is rescued here rather
        than after its full reset. Raises :class:`AllTokensExhaustedError` only when the
        rescue re-probe also finds no usable account anywhere.
        """
        configured = self._configured_paths(kind, scope)
        fell_back_across_scopes = False
        if not configured:
            configured = self._all_configured_paths(kind)
            if not configured:
                return None
            fell_back_across_scopes = True
        now = timezone.now()

        sticky = AnthropicActivePick.objects.pick_for(kind.value, scope)
        if sticky is not None and self._sticky_is_usable(sticky, now):
            return sticky

        chosen = self._first_usable(kind, configured, now)
        if chosen is None:
            others = [path for path in self._all_configured_paths(kind) if path not in configured]
            chosen = self._first_usable(kind, others, now)
        if chosen is None:
            chosen = self._rescue_reprobe(kind, configured, now)
        if chosen is None:
            raise self._all_exhausted_error(kind)

        AnthropicActivePick.objects.set_pick(kind.value, scope, chosen)
        if fell_back_across_scopes:
            # Make the auto-resolution visible: the requested scope had no routing, so a
            # cross-scope account was selected and pinned. The pass_path is a store path
            # (already persisted), never the token value.
            logger.info(
                "credential routing: no %s account configured for requested scope %r; auto-resolved cross-scope to %s",
                kind.value,
                scope,
                chosen,
            )
        return chosen

    @staticmethod
    def _sticky_is_usable(pass_path: str, now: dt.datetime) -> bool:
        row = AnthropicTokenUsage.objects.filter(pass_path=pass_path).first()
        return row is not None and row.is_fresh(now) and not row.is_exhausted

    def _first_usable(self, kind: TokenKind, candidates: list[str], now: dt.datetime) -> str | None:
        """The first candidate whose cached-or-freshly-probed health is not exhausted."""
        for pass_path in candidates:
            row = AnthropicTokenUsage.objects.filter(pass_path=pass_path).first()
            if row is not None and row.is_fresh(now):
                if not row.is_exhausted:
                    return pass_path
                continue
            probed = self._probe(kind, pass_path, now)
            if probed is not None and not probed.is_exhausted:
                return pass_path
        return None

    def _rescue_reprobe(self, kind: TokenKind, configured: list[str], now: dt.datetime) -> str | None:
        """Last-ditch live re-probe before declaring every account exhausted (#3406).

        The normal path trusts a FRESH cached-exhausted verdict without re-probing, and an
        exhausted row is cached until its recorded window reset. But a subscription 5h
        window is ROLLING — capacity returns continuously, well before that reset — so a
        cached-exhausted verdict can outlive the real exhaustion and strand the loop on
        "all accounts exhausted" while an account has in fact recovered. When every
        candidate reads exhausted, this re-probes each account LIVE (ignoring cache
        freshness) and returns the first that recovered, bounding the stall to
        :data:`_RESCUE_REPROBE_COOLDOWN` past real recovery rather than the full reset. The
        cooldown skips any account probed within that window, so a spinning all-exhausted
        dispatch loop cannot storm the rate-limit API — one live re-probe per account per
        cooldown is enough to catch a rolling-window recovery.
        """
        candidates = configured + [path for path in self._all_configured_paths(kind) if path not in configured]
        for pass_path in candidates:
            row = AnthropicTokenUsage.objects.filter(pass_path=pass_path).first()
            if row is not None and (now - row.checked_at) < _RESCUE_REPROBE_COOLDOWN:
                continue
            probed = self._probe(kind, pass_path, now)
            if probed is not None and not probed.is_exhausted:
                return pass_path
        return None

    def _probe(self, kind: TokenKind, pass_path: str, now: dt.datetime) -> AnthropicTokenUsage | None:
        """Resolve *pass_path*'s token, read its live health, and upsert the cache row.

        Returns the upserted row, or ``None`` when the token cannot be resolved
        (no credential stored) or the probe transport fails — either makes the
        candidate unusable, so the selector moves on. The token never leaves this scope.
        """
        credential = _CREDENTIAL_CLASS[kind](pass_path_override=pass_path)
        try:
            token = credential.resolve()
        except CredentialError:
            return None
        try:
            reading = self._health_reading(kind, token)
        except RateLimitProbeError:
            return None
        return AnthropicTokenUsage.objects.record(pass_path, reading, now=now)

    def _health_reading(self, kind: TokenKind, token: str) -> TokenHealthReading:
        """Probe *token* the way its *kind* authenticates and fold it into a cache reading.

        OAuth reads the unified 5h/7d windows; a metered API key reads its credit state
        (funded / out-of-credits), mapped onto the same exhaustion signal so routing
        refuses a depleted key.
        """
        if kind is TokenKind.API_KEY:
            api_key_reader = self._api_key_reader or read_api_key_status
            return reading_from_metered(api_key_reader(token))
        reader = self._reader or read_rate_limits
        return reading_from(reader(token, is_oauth=True))

    @staticmethod
    def _configured_paths(kind: TokenKind, scope: str) -> list[str]:
        """The candidate list for *kind*, overlay scope then global (overlay wins whole)."""
        setting = LIST_SETTING[kind]
        stored = ConfigSetting.objects.get_effective(setting, scope=scope)
        if not stored and scope != GLOBAL_SCOPE:
            stored = ConfigSetting.objects.get_effective(setting)
        return _as_path_list(stored)

    @staticmethod
    def _all_configured_paths(kind: TokenKind) -> list[str]:
        """Every configured ``pass`` entry for *kind* across all scopes, order-preserving deduped."""
        setting = LIST_SETTING[kind]
        seen: dict[str, None] = {}
        for stored in ConfigSetting.objects.filter(key=setting).values_list("value", flat=True):
            for path in _as_path_list(stored):
                seen.setdefault(path, None)
        return list(seen)

    @staticmethod
    def _all_exhausted_error(kind: TokenKind) -> AllTokensExhaustedError:
        candidates = PassPathSelector._all_configured_paths(kind)
        rows = AnthropicTokenUsage.objects.filter(pass_path__in=candidates)
        # ``frees_up_at``, not ``earliest_reset``: the answer is when an account actually
        # re-arms (its blocking windows all clear), not the soonest reset it happens to
        # have on record. An account rejected on its 7-day window whose idle 5h window
        # rolls over every few minutes would otherwise report a reset in the PAST, and the
        # caller would park behind an instant that has already happened.
        resets = [row.frees_up_at for row in rows if row.is_exhausted and row.frees_up_at is not None]
        earliest = min(resets) if resets else None
        when = f" — earliest reset {earliest.isoformat()}" if earliest is not None else ""
        # Name the candidate accounts so the operator knows exactly which ones to
        # refill/check. A ``pass_path`` is a store path (already persisted, logged in
        # ``select``), never the token value — safe to surface in the loud error.
        accounts = f" (accounts: {', '.join(candidates)})" if candidates else ""
        message = f"all configured Anthropic {kind.value} {ALL_TOKENS_EXHAUSTED_SIGNATURE}{accounts}{when}"
        return AllTokensExhaustedError(message, earliest_reset=earliest)


def reading_from(snapshot: RateLimitSnapshot) -> TokenHealthReading:
    """Translate a foundation ``RateLimitSnapshot`` into the domain cache's value object."""
    return TokenHealthReading(
        organization_id=snapshot.organization_id,
        utilization_5h=snapshot.unified_5h_utilization,
        utilization_7d=snapshot.unified_7d_utilization,
        status_5h=snapshot.unified_5h_status,
        status_7d=snapshot.unified_7d_status,
        reset_5h=snapshot.unified_5h_reset,
        reset_7d=snapshot.unified_7d_reset,
    )


def reading_from_metered(snapshot: MeteredKeySnapshot) -> TokenHealthReading:
    """Translate a metered API-key status into the domain cache's value object.

    A standard key exposes no dollar balance and no unified windows, so the routing
    verdict rides the credit flag: an out-of-credits key is recorded with a rejected 7d
    status — exactly the exhaustion signal the selector already refuses to route to.
    """
    return TokenHealthReading(
        organization_id=snapshot.organization_id,
        utilization_5h=0.0,
        utilization_7d=0.0,
        status_5h="",
        status_7d=REJECTED_STATUS if snapshot.out_of_credits else "",
        reset_5h=None,
        reset_7d=None,
    )


def _as_path_list(stored: object) -> list[str]:
    """Coerce a stored config value (a JSON list) to a deduped, order-preserving ``list[str]``."""
    if not isinstance(stored, list):
        return []
    seen: dict[str, None] = {}
    for item in stored:
        text = str(item).strip()
        if text:
            seen.setdefault(text, None)
    return list(seen)


_SELECTOR = PassPathSelector()


def _record_account_exhausted(pass_path: str, *, resets_at: dt.datetime, weekly: bool, now: dt.datetime) -> None:
    """Cache *pass_path* as exhausted until *resets_at* so the selector routes off it.

    A weekly hit blocks the 7d window, else the 5h one; the verdict is trusted until the
    reset. Only the ``pass_path`` health verdict is written — the token is never read here.
    """
    reading = TokenHealthReading(
        organization_id="",
        utilization_5h=0.0 if weekly else 1.0,
        utilization_7d=1.0 if weekly else 0.0,
        status_5h="",
        status_7d=REJECTED_STATUS if weekly else "",
        reset_5h=None if weekly else resets_at,
        reset_7d=resets_at if weekly else None,
    )
    AnthropicTokenUsage.objects.record(pass_path, reading, now=now)


def record_reactive_exhaustion_and_reselect(
    *, scope: str, resets_at: dt.datetime, weekly: bool, now: dt.datetime | None = None
) -> str | None:
    """Record the CURRENT subscription account exhausted after a mid-run limit, then re-select.

    A mid-run 5h/weekly limit is observed by the SDK, NOT the health cache — so without
    recording it the selector's sticky pick would route the SAME spent account again. This
    marks the sticky OAuth account for *scope* exhausted (its ``resets_at`` cached so the
    verdict is trusted until the window re-arms) and re-consults the selector:

    * returns the next healthy account's ``pass_path`` — another account is available, so the
        caller REQUEUES the task to rotate onto it rather than parking the whole lane;
    * raises :class:`AllTokensExhaustedError` (carrying the soonest instant any account frees
        up) when every account is now exhausted, so the caller parks the lane for auto-resume;
    * returns ``None`` when nothing is routed (no sticky account — an ambient-env or single
        unrouted credential), so the caller falls back to the existing lane park unchanged.
    """
    moment = now or timezone.now()
    current = AnthropicActivePick.objects.pick_for(TokenKind.OAUTH.value, scope)
    if current is None:
        return None
    _record_account_exhausted(current, resets_at=resets_at, weekly=weekly, now=moment)
    return _SELECTOR.select(TokenKind.OAUTH, scope)


def resolve_subscription_credential(*, scope: str = GLOBAL_SCOPE) -> AnthropicSubscriptionCredential:
    """The subscription OAuth credential, routed to its selected account's ``pass`` entry.

    The subscription credential has NO built-in ``pass`` path. The selector reads *scope*
    (overlay then global) and, when that is empty, falls back to the cross-scope union, so
    an overlay-scoped account routes even for a global-scope request. Only when NO OAuth
    account is configured in any scope does the selector return no override, so the
    returned credential resolves ONLY from ``CLAUDE_CODE_OAUTH_TOKEN``; if that too is
    absent, :meth:`~teatree.llm.credentials.Credential.resolve` fails loud with a
    :class:`CredentialError` naming ``anthropic_oauth_pass_paths`` AND the empty scope —
    it never lands on a dead default. The scope is threaded in as ``missing_context`` so
    the loud error names it without this factory itself having to fail eagerly (a caller
    that only inspects or patches the credential is never blocked at construction).

    Inside the ephemeral eval container (:func:`~teatree.utils.eval_container.in_container`),
    the per-account DB routing is short-circuited: the HOST already selected an
    account and forwarded its resolved ``CLAUDE_CODE_OAUTH_TOKEN`` via ``docker
    run -e`` (see ``teatree.cli.eval.docker``), so the credential's own
    env-then-``pass`` resolution picks that up with no DB read — the container's
    SQLite has zero tables (never migrated), so a DB query there is a guaranteed
    ``OperationalError``, not a degraded-but-safe read.
    """
    if in_container():
        return AnthropicSubscriptionCredential()
    override = _SELECTOR.select(TokenKind.OAUTH, scope)
    missing_context = None if override is not None else _empty_routing_note("OAuth", scope)
    return AnthropicSubscriptionCredential(pass_path_override=override, missing_context=missing_context)


def _empty_routing_note(credential_label: str, scope: str) -> str:
    """A failure-message note: nothing is configured in *scope* nor in any other scope.

    Reached only when the cross-scope union is empty (the selector already tried the
    fallback), so a global-scope request names "any scope" and an overlay-scope request
    names its own scope plus "nor in any other scope".
    """
    where = "in any scope" if scope == GLOBAL_SCOPE else f"for scope {scope!r} (nor in any other scope)"
    return f"(no {credential_label} account is configured {where})"


def resolve_api_key_credential(*, scope: str = GLOBAL_SCOPE) -> AnthropicApiKeyCredential:
    """The metered API-key credential, routed to its selected account's ``pass`` entry.

    Like the subscription credential, this has NO built-in ``pass`` path: when the
    ``anthropic_api_key_pass_paths`` routing list is empty for *scope* the returned
    credential resolves only from ``ANTHROPIC_API_KEY``; absent that too,
    :meth:`~teatree.llm.credentials.Credential.resolve` fails loud naming the setting
    (and the empty scope, threaded in via ``missing_context``) rather than reading a
    dead default.

    Inside the ephemeral eval container, the per-account DB routing is
    short-circuited the same way as :func:`resolve_subscription_credential` —
    the HOST-forwarded ``ANTHROPIC_API_KEY`` env var is reused instead.
    """
    if in_container():
        return AnthropicApiKeyCredential()
    override = _SELECTOR.select(TokenKind.API_KEY, scope)
    missing_context = None if override is not None else _empty_routing_note("API-key", scope)
    return AnthropicApiKeyCredential(pass_path_override=override, missing_context=missing_context)


def _active_overlay_scope() -> str:
    """The active overlay's routing scope, read from ``T3_OVERLAY_NAME``.

    Empty (the :data:`GLOBAL_SCOPE` sentinel) when no overlay is active, so the
    selector's overlay→global fallback lands on the global routing list unchanged.
    """
    return os.environ.get("T3_OVERLAY_NAME", "") or GLOBAL_SCOPE


def resolve_eval_credential(*, kind: "EvalCredential | None" = None, scope: str | None = None) -> Credential:
    """The credential the automated eval lane rides, selected by the ``eval_credential`` knob.

    THE single seam that reverses #2707's metered-exclusive lock: the eval backend,
    the judge, and the Docker auth-passthrough all resolve their credential HERE, so
    flipping the knob switches every eval chokepoint at once (never a per-call-site
    edit). ``kind`` (an explicit :class:`~teatree.config.enums.EvalCredential`) wins;
    ``None`` (the default) reads the DB-home ``eval_credential`` setting via
    :func:`~teatree.config.get_effective_settings` (``T3_EVAL_CREDENTIAL`` env → the
    ``ConfigSetting`` store → the default :attr:`EvalCredential.SUBSCRIPTION_OAUTH`).

    ``scope`` (``None``, the default) resolves to the ACTIVE OVERLAY (``T3_OVERLAY_NAME``)
    via :func:`_active_overlay_scope`, so the per-account routing reads the overlay-scoped
    ``anthropic_oauth_pass_paths`` first and the selector's overlay→global fallback covers
    the global list. The eval lane is a teatree-overlay eval, so its account routing is
    configured at the overlay scope — defaulting to :data:`GLOBAL_SCOPE` here made a
    bare eval abort with :class:`~teatree.llm.credentials.CredentialError` whenever the
    routing lived only at the overlay scope. An explicit *scope* (including
    :data:`GLOBAL_SCOPE`) overrides the active-overlay default. Even when ``scope``
    resolves to :data:`GLOBAL_SCOPE` (no active overlay), the selector's cross-scope union
    fallback still finds an overlay-scoped account — so a bare ``t3 eval`` shell routes
    without a manual ``CLAUDE_CODE_OAUTH_TOKEN`` export.

    :attr:`EvalCredential.SUBSCRIPTION_OAUTH` → :func:`resolve_subscription_credential`
    (per-account OAuth routing via ``anthropic_oauth_pass_paths`` for the same
    *scope*, spreading a right-sized lane across accounts so its usage window is not
    throttled). :attr:`EvalCredential.METERED_API_KEY` → :func:`resolve_api_key_credential`.
    Imported at call time so the eval CLI import chain stays Django-free until Django
    is up (the resolvers already require it).
    """
    from teatree.config import EvalCredential, get_effective_settings  # noqa: PLC0415 — deferred: call-time import

    if scope is None:
        scope = _active_overlay_scope()
    if kind is None:
        kind = get_effective_settings().eval_credential
    if kind is EvalCredential.METERED_API_KEY:
        return resolve_api_key_credential(scope=scope)
    return resolve_subscription_credential(scope=scope)
