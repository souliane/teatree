"""Overlay ``db_import`` call-site contract (regression for teatree#783).

teatree#783 added a keyword-only ``approve_remote_dump`` to the core
``OverlayBase.db_import`` contract and a call site that passes it. An overlay
override that does not accept a kwarg the core passes raises ``TypeError`` at
runtime (``t3 <ov> db refresh --fresh-dump`` broke for a registered overlay this
way, live on main).

This pins the contract structurally: **every concrete ``db_import`` override —
the base, every in-tree subclass, and every entry-point-registered overlay —
must accept every keyword the core call sites pass.** It is signature-based
(``inspect``), so it catches a missing kwarg without invoking the real,
side-effecting import. A new core ``db_import`` kwarg that an override forgets
to accept turns this RED.
"""

import inspect
from collections.abc import Iterator

import pytest

from teatree.core.overlay import OverlayBase

# The union of keyword arguments the core passes to ``overlay.db_import(...)``.
# Derived from the in-tree call sites:
#   - core/runners/worktree_provision.py  → slow_import
#   - core/management/commands/db.py      → force, dslr_snapshot, dump_path,
#                                           approve_remote_dump (teatree#783)
# Keep in sync with the base ``OverlayBase.db_import`` signature; the
# ``test_base_signature_is_the_superset`` guard fails if they drift apart.
_CORE_DB_IMPORT_KWARGS = frozenset(
    {"force", "slow_import", "dslr_snapshot", "dump_path", "approve_remote_dump"},
)


def _accepts_kwargs(func: object, required: frozenset[str]) -> set[str]:
    """Return the *missing* required kwargs ``func`` cannot accept by name.

    A ``**kwargs`` catch-all accepts anything (empty set). Otherwise every
    name in ``required`` must be a declared parameter the caller may pass by
    keyword (POSITIONAL_OR_KEYWORD or KEYWORD_ONLY).
    """
    sig = inspect.signature(func)
    params = sig.parameters.values()
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
        return set()
    nameable = {
        p.name for p in params if p.kind in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
    }
    return set(required) - nameable


def _all_concrete_db_import_overrides() -> Iterator[tuple[str, object]]:
    """Yield ``(label, db_import_callable)`` for the base + every subclass.

    Walks ``OverlayBase.__subclasses__()`` transitively so an
    entry-point-registered overlay whose module has
    been imported is covered, alongside the base default itself.
    """
    seen: set[type] = set()

    def _walk(cls: type) -> Iterator[type]:
        for sub in cls.__subclasses__():
            if sub in seen:
                continue
            seen.add(sub)
            yield sub
            yield from _walk(sub)

    yield ("OverlayBase", OverlayBase.db_import)
    for cls in _walk(OverlayBase):
        # Test-only fixture overlays (defined in this test package) are
        # exercised directly by the detector-proof unit tests below; the
        # registry-walking contract scan must cover *production* overlays
        # only, never a deliberately-broken fixture.
        if cls.__module__.startswith("tests."):
            continue
        # Only overrides defined on the class itself matter; inherited ones
        # are the base callable already covered above.
        if "db_import" in cls.__dict__:
            yield (cls.__qualname__, cls.__dict__["db_import"])


def test_base_signature_is_the_superset() -> None:
    """The base ``db_import`` must itself accept every core call-site kwarg.

    If teatree#783 (or a later change) adds a core-passed kwarg without
    adding it to the base contract, this fails first — pointing at the
    contract, not at each overlay.
    """
    missing = _accepts_kwargs(OverlayBase.db_import, _CORE_DB_IMPORT_KWARGS)
    assert missing == set(), f"OverlayBase.db_import is missing core kwargs: {sorted(missing)}"


def test_every_overlay_db_import_accepts_core_call_site_kwargs() -> None:
    """Every registered/in-tree overlay override must accept the core kwargs.

    This is the test that would have caught the live-main break: a
    registered overlay's override lacked ``approve_remote_dump`` and had no
    ``**kwargs``, so ``t3 <ov> db refresh --fresh-dump`` raised ``TypeError``.
    """
    # Import installed overlays so their subclasses are registered. An
    # entry-point overlay registers via ``teatree.overlays``; loading all
    # overlays imports its module and thus its ``OverlayBase`` subclass.
    from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415

    get_all_overlays()

    offenders: dict[str, list[str]] = {}
    for label, func in _all_concrete_db_import_overrides():
        missing = _accepts_kwargs(func, _CORE_DB_IMPORT_KWARGS)
        if missing:
            offenders[label] = sorted(missing)

    assert offenders == {}, (
        "These db_import overrides cannot accept kwargs the core call sites "
        f"pass (add the explicit param, not bare **kwargs): {offenders}"
    )


@pytest.mark.parametrize("kwarg", sorted(_CORE_DB_IMPORT_KWARGS))
def test_each_core_kwarg_is_individually_required(kwarg: str) -> None:
    """Each core kwarg, alone, must be acceptable by every override.

    Parametrised so a single missing kwarg names itself in the failure
    rather than hiding inside an aggregate set.
    """
    from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415

    get_all_overlays()

    one = frozenset({kwarg})
    bad = [label for label, func in _all_concrete_db_import_overrides() if _accepts_kwargs(func, one)]
    assert bad == [], f"db_import overrides not accepting {kwarg!r}: {bad}"


# ── Anti-vacuous guard ────────────────────────────────────────────────
#
# The contract tests above can only fail when an offending override is
# *importable in this env*. teatree's own venv may not install every
# entry-point overlay package, so without this the suite would pass even though the
# detection were broken — the exact "vacuous green" trap behind the
# live-main break. These pin the *detector itself* against the precise
# break shape, independent of which overlays happen to be installed.


class _BrokenPreContractOverlay(OverlayBase):
    """Reproduces the live-main break shape.

    A ``db_import`` override with the pre-#783 signature — no
    ``approve_remote_dump``, no ``**kwargs``.
    """

    def get_repos(self):
        return []

    def get_provision_steps(self, worktree):
        return []

    def db_import(  # ty: ignore[invalid-method-override] — the narrower-than-base signature IS the break this fixture reproduces.
        self,
        worktree,
        *,
        force: bool = False,
        slow_import: bool = False,
        dslr_snapshot: str = "",
        dump_path: str = "",
    ) -> bool:
        return False


class _FixedWithExplicitParam(OverlayBase):
    """The forward-fix shape: explicit ``approve_remote_dump`` param."""

    def get_repos(self):
        return []

    def get_provision_steps(self, worktree):
        return []

    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def db_import(  # noqa: PLR0913 — deliberately mirrors the 6-arg core OverlayBase.db_import contract.
        self,
        worktree,
        *,
        force: bool = False,
        slow_import: bool = False,
        dslr_snapshot: str = "",
        dump_path: str = "",
        approve_remote_dump: bool = False,
    ) -> bool:
        return False


def test_detector_flags_the_exact_live_break_shape() -> None:
    """RED-before proof: the pre-#783 override shape MUST be detected.

    If this ever returns an empty set the detector is vacuous and the
    contract tests above are worthless — this is the guard the live break
    slipped past.
    """
    missing = _accepts_kwargs(_BrokenPreContractOverlay.__dict__["db_import"], _CORE_DB_IMPORT_KWARGS)
    assert missing == {"approve_remote_dump"}


def test_detector_passes_the_forward_fix_shape() -> None:
    """GREEN-after proof: the explicit-param fix shape must NOT be flagged."""
    missing = _accepts_kwargs(_FixedWithExplicitParam.__dict__["db_import"], _CORE_DB_IMPORT_KWARGS)
    assert missing == set()
