"""UserSettings field ↔ ≥1 real reader — the dead-config totality lane (SELFCATCH-4).

``test_removed_dead_settings.py`` guards exactly two historically-removed fields
(``branch_prefix``, ``ask_before_post_on_behalf``, #2731) and ``test_feature_flags``
governs only flag lifecycle. Neither is total: a NEW ``UserSettings`` field that
nothing reads — the dead-toggle class — ships green, an operator-facing knob that
silently does nothing.

This lane makes the guard total by introspection — but a reader must be a REAL
settings read, not a coincidental token match. A bare ``django.utils.timezone``
attribute (receiver is a module, not a settings object) and a keyword string
``"privacy"`` in an unrelated intent catalog would each falsely "read" a field
that the codebase itself documents as reader-less, hollowing the lane out. So a
read is one of: a ``.<field>`` access whose RECEIVER resolves to a settings object
(``get_effective_settings()`` / a var bound to it / a
high-confidence ``settings``/``cfg``/``config`` receiver), a ``getattr(settings,
"<field>")`` / cold-reader key read, a ``<NAME>_KEY = "<field>"`` config-key
constant, a field-name string literal inside a settings-RESOLUTION module (where
such a string IS a config key — ``getattr(settings, key)`` over a dict/tuple of
key names, but NOT a name used only as a bare-dict ``.get("<key>")`` sub-key of
a bespoke structured table nor a dict-literal ``{"<key>": ...}`` key, both
coincidental collisions), or the field on a
non-comment line of a ``hooks/*.sh`` cold-read script. A field read by none is dead config, unless named in
``FIELDS_WITHOUT_SRC_READER`` — the reviewable allowlist of fields consumed by
agent-prose / documentation rather than ``src`` code, or documented reader-less -
or in ``FIELDS_AWAITING_DECLARED_CONSUMER``, declared ahead of the change that
reads it and forced back out of the allowlist as soon as that reader lands.
"""

import ast
import dataclasses
from collections.abc import Callable
from functools import cache

from teatree.config import UserSettings
from teatree.config.setting_registries import CODE_PINNED_SETTINGS
from teatree.quality.setting_readership import file_settings_reads
from tests.conformance._src_tree import REPO_ROOT, SRC_DIR, parsed_modules

_REPO_ROOT = REPO_ROOT
_SETTINGS_DEF = (SRC_DIR / "config" / "settings.py").resolve()
_PY_ROOTS = (SRC_DIR, _REPO_ROOT / "hooks")
_SH_ROOT = _REPO_ROOT / "hooks"


# Fields whose ONLY consumer is not ``src`` Python — documented, reviewable.
# ``e2e_confidence_threshold`` is documentation-driven by design (BLUEPRINT §
# configuration: "the typed field is the shared source of truth for the doc value
# and any future programmatic consumer"); the ``/t3:e2e`` verify↔review loop is
# agent prose, not a deterministic gate, so it reads the value from the skill.
# The other three entries this list carried — ``issue_implementer_cadence_hours``,
# ``privacy``, ``timezone`` — were reader-less rather than prose-consumed, so #4203
# retired them instead of excusing them. A NEW dead field is NOT on this list and
# fails until it gains a real reader or a conscious allowlist entry.
FIELDS_WITHOUT_SRC_READER: frozenset[str] = frozenset({"e2e_confidence_threshold"})

# Fields DECLARED ahead of the change that reads them, every one INERT by default
# (empty list / false / a bound nothing evaluates yet), so no behaviour depends on
# the reader not being there yet. Distinct from the allowlist above: those are
# reader-less by design, these are waiting on a consumer that is being written.
# NOT a resting place: ``test_allowlisted_fields_genuinely_lack_a_src_reader`` fails the
# moment an entry gains a real reader, so wiring one forces its removal from here.
# Read from the registry the dash renders its refusal from, so a key cannot be pinned in
# code on one surface and read as ordinarily-owned on the other.
FIELDS_AWAITING_DECLARED_CONSUMER: frozenset[str] = frozenset(CODE_PINNED_SETTINGS)

#: Every field the lane excuses, whichever reason it was excused for.
_ALLOWLISTED_FIELDS: frozenset[str] = FIELDS_WITHOUT_SRC_READER | FIELDS_AWAITING_DECLARED_CONSUMER


@cache
def _field_names() -> frozenset[str]:
    return frozenset(field.name for field in dataclasses.fields(UserSettings))


@cache
def _walk_py_files(reader: Callable[[ast.Module, set[str]], set[str]], field_names: frozenset[str]) -> frozenset[str]:
    read: set[str] = set()
    for root in _PY_ROOTS:
        for path, tree in parsed_modules(root):
            if path.resolve() == _SETTINGS_DEF:
                continue
            # Migration files are FROZEN ORM state, never live settings readers. A
            # squash migration collapses the data-op bodies of the whole chain
            # into one file, so a ``ConfigSetting = apps.get_model(...)`` reference
            # (a resolution marker) sits alongside every frozen model field-name
            # string literal — flipping ``_module_resolves_settings`` True and
            # falsely counting field-name strings (``"timezone"``, ``"payload"``) as
            # settings reads. Excluding ``migrations/`` scopes the matcher to live
            # code; no real reader lives in a migration (proven: only the coincidental
            # ``timezone`` string was ever migration-only).
            if "migrations" in path.parts:
                continue
            read |= reader(tree, field_names)
    return frozenset(read)


def _python_readers(field_names: frozenset[str]) -> frozenset[str]:
    """Fields read by a REAL settings read across ``src``/``hooks`` Python.

    Introspection, not a hand-list; AST-based so a comment or prose docstring
    naming a field never counts. A read is a settings-object attribute access, a
    ``getattr``/cold-reader key literal, a ``*_KEY`` config-key constant, or a
    field-name string inside a settings-resolution module (where it is a key).
    """
    return _walk_py_files(file_settings_reads, field_names)


@cache
def _shell_readers(field_names: frozenset[str]) -> frozenset[str]:
    """Fields named on a non-comment line of a ``hooks/*.sh`` script (the cold-read seam).

    ``statusline_chain`` is read by ``statusline.sh`` via a direct ``sqlite3`` query
    on the ConfigSetting store before Django is up — a real reader a Python walk
    cannot see. Comment lines (``#``-led) are skipped.
    """
    read: set[str] = set()
    for path in _SH_ROOT.rglob("*.sh"):
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.lstrip().startswith("#"):
                continue
            for name in field_names:
                if name in line:
                    read.add(name)
    return frozenset(read)


def _all_readers(field_names: frozenset[str]) -> frozenset[str]:
    return _python_readers(field_names) | _shell_readers(field_names)


def unread_fields() -> frozenset[str]:
    """Every ``UserSettings`` field with no real reader and no allowlist entry — the lane core."""
    names = _field_names()
    return names - _all_readers(names) - _ALLOWLISTED_FIELDS


class TestEveryUserSettingsFieldIsRead:
    """A setting nothing reads is dead config — every field has a real consumer."""

    def test_no_user_settings_field_is_dead_config(self) -> None:
        unread = sorted(unread_fields())
        assert not unread, (
            "UserSettings field(s) that no src/hooks consumer reads (dead config) — "
            "wire a reader or add to FIELDS_WITHOUT_SRC_READER on purpose: " + str(unread)
        )

    def test_allowlisted_fields_are_still_real_fields(self) -> None:
        # A stale allowlist entry (a field that was removed) is dead surface.
        stale = sorted(_ALLOWLISTED_FIELDS - _field_names())
        assert not stale, f"Allowlisted entries that are not UserSettings fields: {stale}"

    def test_allowlisted_fields_genuinely_lack_a_src_reader(self) -> None:
        # The teeth of the tightening: each allowlisted field must be UNfound by the
        # REAL-reader matcher (the loose matcher counted privacy/timezone on a
        # coincidental token). An allowlisted field that now has a real reader must
        # be dropped from the allowlist, not left masking coverage.
        redundant = sorted(_ALLOWLISTED_FIELDS & _all_readers(_field_names()))
        assert not redundant, f"Allowlisted entries that now HAVE a real reader (drop them): {redundant}"


class TestUserSettingsReadersCardinalityFloors:
    """Anti-vacuity — a broken reader walk that finds nothing must not pass green."""

    def test_field_and_reader_floors(self) -> None:
        names = _field_names()
        read = _all_readers(names)
        assert len(names) >= 100, len(names)
        # The overwhelming majority of fields must resolve a REAL reader, else the walk broke.
        assert len(read) >= 100, len(read)


class TestUserSettingsReadersFiresRed:
    """Anti-vacuity — a synthetic never-read field is caught, and the tightening has teeth."""

    def test_a_synthetic_unread_field_is_named(self) -> None:
        # A field name no reader mentions and no allowlist names is exactly the
        # dead-toggle class the lane must flag.
        names = _field_names() | {"synthetic_dead_toggle_nothing_reads"}
        unread = names - _all_readers(names) - _ALLOWLISTED_FIELDS
        assert "synthetic_dead_toggle_nothing_reads" in unread

    def test_tightening_catches_a_coincidentally_matched_dead_field(self) -> None:
        # THE fix: a dead field whose common name collides with an unrelated token
        # (``.payload`` on a ScanSignal, never a settings-object attribute) is
        # counted by a LOOSE matcher but caught by the REAL-reader matcher.
        loose = _loose_readers(_field_names() | {"payload"})
        tight = _all_readers(_field_names() | {"payload"})
        assert "payload" in loose, "expected the loose matcher to falsely count the coincidental token"
        assert "payload" not in tight, "the tightened matcher must NOT count a coincidental non-settings token"

    def test_the_tightening_catches_the_two_real_world_coincidences(self) -> None:
        # privacy/timezone are the cases MEASURED in the wild: a `.privacy` attribute whose
        # receiver is not a settings object, and a `"timezone"` string in an intent catalog.
        # #4203 retired both as fields, so they are INJECTED — asserting their absence from
        # a set the walk can only return a subset of would hold for free.
        names = _field_names() | {"privacy", "timezone"}
        loose = _loose_readers(names)
        tight = _all_readers(names)
        for token in ("privacy", "timezone"):
            assert token in loose, f"expected the loose matcher to falsely count {token!r}"
            assert token not in tight, f"the tightened matcher must NOT count the coincidental {token!r}"

    def test_a_real_read_field_is_not_flagged(self) -> None:
        # Positive control: a field with a real ``settings.<field>`` reader resolves.
        names = _field_names()
        assert "max_concurrent_local_stacks" in names
        assert "max_concurrent_local_stacks" in _all_readers(names)


def _loose_file(tree: ast.Module, names: set[str]) -> set[str]:
    read: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in names:
            read.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in names:
            read.add(node.value)
    return read


def _loose_readers(field_names: frozenset[str]) -> frozenset[str]:
    """The OLD loose matcher (any ``.<field>`` attr OR exact ``"<field>"`` string).

    Retained ONLY to prove — in ``TestUserSettingsReadersFiresRed`` — that the
    tightened matcher rejects the coincidental token matches the loose one accepted.
    """
    return _walk_py_files(_loose_file, field_names)
