"""Proactive rotation of the Slack app-configuration token pair (``xoxe``).

Slack issues app-configuration credentials as an access/refresh pair, and the
access half expires 12 hours after issue. ``tooling.tokens.rotate`` is one-shot:
it spends the refresh token it is given and returns a brand-new pair, so the
only way to stay authenticated is to rotate *before* expiry.

Rotating reactively — on the ``invalid_auth`` a dead access token produces —
cannot recover anything. Both halves are minted together, so by the time the
access half is rejected the stored refresh half is exactly as old and equally
dead; the pair is then unrecoverable without a human minting a fresh one in the
Slack UI. A 12-hour credential refreshed only on failure is guaranteed to reach
that dead end, and the stored pair did after 12 days.

Two invariants make that unreachable once a pair is seeded:

1. **Rotate on age, not on failure.** :func:`ensure_fresh_config_token` rotates
   as soon as the pair is older than :data:`ROTATE_AFTER` — a third of Slack's
   lifetime, so several consecutive missed runs still leave hours of margin. Its
   caller is ``t3 doctor``, which SessionStart runs (``bootstrap-cli.sh``) and
   which the deployed stack's watchdog re-runs every five minutes — a cadence
   far inside the window from two independent triggers, so a box whose worker
   has been down for a day still self-heals the moment the owner opens a
   session. An unknown age counts as stale: assuming freshness is precisely the
   assumption that let the stored pair run to expiry.

2. **Persist before the old pair is considered spent.** Slack has already
   invalidated the previous pair by the time ``rotate`` returns, so a lost
   response bricks the credential permanently.
   :meth:`ConfigTokenStore.persist` writes the REFRESH half first — it is the
   half that can mint another pair, so a crash between the two writes leaves a
   store the next rotation recovers from, whereas the reverse order would leave
   a live access token guarding a burned refresh token: dead in 12 hours with no
   way back. Every write is verified by reading it back, and a failure raises
   :class:`SlackConfigTokenPersistError` rather than returning a value a caller
   can discard.

The reactive path in :func:`~teatree.cli.slack.setup._export_with_rotation`
remains as the complementary backstop for a pair invalidated early (revoked, or
reseeded out of band) rather than by the clock; it persists through this
module's store so the two paths cannot drift.
"""

import datetime as dt
from dataclasses import dataclass
from enum import Enum
from uuid import uuid4

import httpx

from teatree.cli.slack.manifest import _CONFIG_REFRESH_REF, _CONFIG_TOKEN_REF, SlackManifestError, rotate_config_token
from teatree.utils.secrets import read_pass, remove_pass, write_pass

#: Where the pair's issue time is recorded, so age is knowable without calling
#: Slack. It lives beside the tokens in ``pass`` rather than in the config DB so
#: the credential and its metadata are one backup/restore unit, and so this
#: module needs no Django.
_CONFIG_ISSUED_AT_REF = "teatree/slack-app-config-issued-at"

#: Slack's documented lifetime for an app-configuration ACCESS token.
CONFIG_TOKEN_LIFETIME = dt.timedelta(hours=12)

#: Rotate once the pair is older than this. A third of the lifetime, so a missed
#: tick — or a worker down for hours — still leaves margin before expiry.
ROTATE_AFTER = dt.timedelta(hours=4)

#: Attempts allowed for a ``pass`` WRITE. Never applied to the rotate itself: a
#: second rotate would spend a second refresh token to fix a storage fault.
_PERSIST_ATTEMPTS = 3

_REMINT_URL = "https://api.slack.com/apps"


class SlackConfigTokenPersistError(RuntimeError):
    """A rotated app-config pair could not be stored, and the old pair is already spent.

    Named and raised rather than returned because there is no safe degraded
    behaviour left: Slack invalidated the previous pair when it issued this one,
    so a caller that ignored a boolean would leave the credential dead with the
    only copy of its replacement lost to garbage collection.
    """


class SlackConfigTokenStoreUnwritableError(RuntimeError):
    """The store failed its write-ahead round-trip, so NO rotation was attempted.

    Distinct from :class:`SlackConfigTokenPersistError` in the one way that
    matters to whoever reads it: nothing was spent. The stored pair is exactly as
    it was, and fixing the store is all that is needed — no re-minting.
    """


class RotationOutcome(Enum):
    """What :func:`ensure_fresh_config_token` did, or could not do."""

    #: No refresh token stored — this box never seeded an app-config pair.
    NOT_CONFIGURED = "not_configured"
    #: The pair is inside :data:`ROTATE_AFTER`; no call was made.
    FRESH = "fresh"
    #: A new pair was issued and durably persisted.
    ROTATED = "rotated"
    #: Slack refused the rotation — the refresh half is spent or revoked, and
    #: only a human minting a fresh pair in the Slack UI can restore it.
    UNRECOVERABLE = "unrecoverable"
    #: The store could not round-trip a throwaway value, so the rotation was
    #: REFUSED before Slack was called. Nothing was spent; the pair is intact.
    STORE_UNWRITABLE = "store_unwritable"


@dataclass(frozen=True, slots=True)
class RotationReport:
    """The outcome of one :func:`ensure_fresh_config_token` call plus its reason."""

    outcome: RotationOutcome
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ConfigTokenStore:
    """The three ``pass`` slots holding the app-config pair and its issue time.

    The keys are constructor arguments so a test can point the whole lifecycle
    at throwaway slots without patching module globals.
    """

    access_key: str = _CONFIG_TOKEN_REF
    refresh_key: str = _CONFIG_REFRESH_REF
    issued_at_key: str = _CONFIG_ISSUED_AT_REF

    def read_issued_at(self) -> dt.datetime | None:
        """The recorded issue time, or ``None`` when absent or unparseable.

        A naive stored value is read as UTC — every writer here emits an
        aware UTC timestamp, so a naive one can only come from hand-editing.
        """
        raw = read_pass(self.issued_at_key)
        if not raw:
            return None
        try:
            parsed = dt.datetime.fromisoformat(raw)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)

    def age(self, *, now: dt.datetime) -> dt.timedelta | None:
        issued_at = self.read_issued_at()
        return None if issued_at is None else now - issued_at

    @property
    def writability_probe_key(self) -> str:
        """The throwaway slot the write-ahead round-trip uses. Never holds a credential."""
        return f"{self.issued_at_key}-writability-probe"

    def assert_writable(self) -> None:
        """Prove a real write-then-read round-trip BEFORE any irreversible rotation.

        Ordering is the whole point. ``tooling.tokens.rotate`` invalidates the
        previous pair the instant it issues a new one, so a store discovered to
        be unwritable AFTER the call has already destroyed the credential. The
        proof therefore has to happen first, and it has to be a real round trip:
        ``pass insert`` needs only the public key, so it exits 0 in a venue where
        ``pass show`` cannot start ``gpg-agent``/``keyboxd`` and fails — a store
        that accepts every write and returns nothing readable. Checking
        permissions, or trusting the write's exit code, both pass there.

        The value is freshly random per call so a leftover file from an earlier
        run can never be mistaken for this write landing, and the slot is removed
        afterwards whatever happens.

        Raises :class:`SlackConfigTokenStoreUnwritableError` when the round trip
        fails, which the caller turns into a refusal rather than a rotation.
        """
        witness = f"teatree-writability-probe-{uuid4().hex}"
        try:
            if not write_pass(self.writability_probe_key, witness):
                raise SlackConfigTokenStoreUnwritableError(self._unwritable_message("the write was rejected"))
            if read_pass(self.writability_probe_key) != witness:
                raise SlackConfigTokenStoreUnwritableError(
                    self._unwritable_message("the write reported success but could not be read back")
                )
        finally:
            remove_pass(self.writability_probe_key)

    def _unwritable_message(self, reason: str) -> str:
        return (
            f"refusing to rotate the Slack app-config token: {reason} for the throwaway slot "
            f"`pass {self.writability_probe_key}`, so a rotated pair could not have been stored. "
            f"Slack invalidates the previous pair the moment it issues a new one, so rotating "
            f"against a store in this state would destroy the credential irrecoverably. "
            f"NOTHING WAS SPENT — the stored pair is untouched. Fix the `pass`/GPG environment "
            f"(a container venue whose GNUPGHOME cannot start gpg-agent is the usual cause) and "
            f"the next run will rotate normally."
        )

    def persist(self, *, access: str, refresh: str, issued_at: dt.datetime) -> None:
        """Durably store a freshly-rotated pair, refresh half first.

        Ordering is the whole guarantee. Slack spent the previous pair to mint
        this one, so the store must never end up holding a burned refresh token:
        the refresh half lands (and is read back) FIRST, because a crash after
        it is written leaves a store the next rotation can recover from, while
        the reverse order leaves a live access token guarding a dead refresh
        token — unrecoverable once the access half expires.

        Raises :class:`SlackConfigTokenPersistError` on any write that cannot be
        read back, so a storage fault surfaces at the point of failure instead of
        silently discarding the only copy of the new credential.
        """
        self._write_verified(self.refresh_key, refresh, "refresh token")
        self._write_verified(self.access_key, access, "access token")
        self._write_verified(self.issued_at_key, issued_at.isoformat(), "issue time")

    @staticmethod
    def _write_verified(key: str, value: str, what: str) -> None:
        for _ in range(_PERSIST_ATTEMPTS):
            if write_pass(key, value) and read_pass(key) == value:
                return
        message = (
            f"could not persist the rotated Slack app-config {what} to `pass {key}` "
            f"after {_PERSIST_ATTEMPTS} attempts. Slack already invalidated the previous "
            f"pair when it issued this one, so the credential must be re-minted at {_REMINT_URL}"
        )
        raise SlackConfigTokenPersistError(message)


def ensure_fresh_config_token(
    *,
    store: ConfigTokenStore | None = None,
    now: dt.datetime | None = None,
) -> RotationReport:
    """Rotate the app-config pair when it is older than :data:`ROTATE_AFTER`.

    The proactive entry point, safe to call on every ``t3 doctor`` run: it reads
    one timestamp and returns without decrypting a secret or touching the
    network unless the pair is actually due.

    A Slack refusal is reported, never raised — the app-config token gates only
    manifest reads/writes during setup, so a dead one must not fail a doctor
    run. A persistence failure IS raised: that one means teatree lost a
    credential it had just been handed.
    """
    store = store or ConfigTokenStore()
    now = now or dt.datetime.now(dt.UTC)

    # Age first, so the overwhelmingly common tick reads one timestamp and stops
    # without decrypting either secret. Captured once: after the rotation the
    # stored issue time is the NEW one, which would report every pair as 0h old.
    age = store.age(now=now)
    if not _is_due(age):
        return RotationReport(RotationOutcome.FRESH, f"pair is {_render_age(age)}")

    refresh = read_pass(store.refresh_key)
    if not refresh:
        return RotationReport(
            RotationOutcome.NOT_CONFIGURED,
            f"no refresh token at `pass {store.refresh_key}`",
        )

    # Write-ahead. The rotation below is irreversible, so the store's ability to
    # keep its result is proven FIRST — on a store that cannot, the correct
    # outcome is a loud refusal with the pair intact, never a spent credential.
    try:
        store.assert_writable()
    except SlackConfigTokenStoreUnwritableError as exc:
        return RotationReport(RotationOutcome.STORE_UNWRITABLE, str(exc))

    try:
        access, new_refresh = rotate_config_token(refresh_token=refresh)
    except (SlackManifestError, httpx.HTTPError) as exc:
        return RotationReport(
            RotationOutcome.UNRECOVERABLE,
            f"Slack refused tooling.tokens.rotate ({exc}); mint a fresh pair at {_REMINT_URL}",
        )

    store.persist(access=access, refresh=new_refresh, issued_at=now)
    return RotationReport(RotationOutcome.ROTATED, f"rotated a pair that was {_render_age(age)}")


def _is_due(age: dt.timedelta | None) -> bool:
    """True when the pair is older than :data:`ROTATE_AFTER`, or its age is unknown.

    Unknown age rotates NOW rather than waiting. A pair with no recorded issue
    time was seeded by hand or stored before this module existed, and could be
    any age at all; rotating early costs one API call, whereas assuming it is
    fresh is the exact assumption that lets a pair run to expiry.
    """
    return age is None or age >= ROTATE_AFTER


def _render_age(age: dt.timedelta | None) -> str:
    if age is None:
        return "of unrecorded age"
    return f"{age.total_seconds() / 3600:.1f}h old"


__all__ = [
    "CONFIG_TOKEN_LIFETIME",
    "ROTATE_AFTER",
    "ConfigTokenStore",
    "RotationOutcome",
    "RotationReport",
    "SlackConfigTokenPersistError",
    "SlackConfigTokenStoreUnwritableError",
    "ensure_fresh_config_token",
]
