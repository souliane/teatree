# test-path: cross-cutting — one contract over the whole declared settings surface
"""Every declared setting is READ by something — nobody's key is nobody's answer.

The surface-size problem turns on a question the loop-ownership map cannot answer: it
says which loop owns the keys a loop owns, and nothing about the rest. *Which settings
does nobody own?* is what decides whether a registry is maintainable, and an assertion is
what keeps that answer from rotting — a derivation nothing asserts is descriptive, and
descriptive things drift here.

Ownership is total by construction, so the assertion is one-sided: the UNREAD set is
empty. An empty answer is also exactly what a broken walk produces, which is why the
control below is not optional — it plants a key nothing reads and requires the walk to
find it. Three narrower reaches were each measured producing a false absence: a walk
without ``hooks/``, one blind to ``.sh`` readers, and one excluding ``config/`` whole
rather than only its declaration modules.
"""

import dataclasses
from pathlib import Path

import pytest

import teatree
from teatree.config.setting_registries import READER_LESS_SETTINGS
from teatree.config.settings import UserSettings
from teatree.quality.setting_readership import DECLARATION_MODULES, UnreadableRootError, read_sites, unread_settings

_PACKAGE_ROOT = Path(teatree.__file__).parent
#: The package and the hook tree, which is outside it and is the ONLY reader of some keys.
_ROOTS = (_PACKAGE_ROOT, _PACKAGE_ROOT.parents[1] / "hooks")


def _declared_keys() -> tuple[str, ...]:
    return tuple(field.name for field in dataclasses.fields(UserSettings))


class TestEverySettingHasAReader:
    def test_no_declared_setting_is_unread(self) -> None:
        unread = tuple(
            k
            for k in unread_settings(_ROOTS, _declared_keys(), package_root=_PACKAGE_ROOT)
            if k not in READER_LESS_SETTINGS
        )

        assert unread == (), (
            "these settings are declared and nothing reads them — each is a surface with no "
            "owner, so it is a B8 ask (activate it, or delete it with the owner's decision), "
            "never a key to leave sitting:\n" + "\n".join(f"  {key}" for key in unread)
        )

    def test_every_excused_key_is_genuinely_unread(self) -> None:
        """The allowlist is not a resting place: an excused key that gains a reader leaves it.

        Without this, `READER_LESS_SETTINGS` could quietly grow to cover keys that DO have
        owners, and the assertion above would pass over a surface nobody checked.
        """
        unread = set(unread_settings(_ROOTS, _declared_keys(), package_root=_PACKAGE_ROOT))
        declared = {k for k in READER_LESS_SETTINGS if k in set(_declared_keys())}

        assert declared <= unread, sorted(declared - unread)

    def test_the_surface_it_accounts_for_is_the_whole_declared_one(self) -> None:
        keys = _declared_keys()
        sites = read_sites(_ROOTS, keys, package_root=_PACKAGE_ROOT)

        assert set(sites) == set(keys), "the walk must answer for EVERY declared key, not a subset"
        assert len(keys) > 100, "a shrunken key set would make the assertion above trivially true"


class TestTheWalkCanActuallyFail:
    """The control. Without it, a walk that reads nothing reports the same clean answer."""

    def test_a_key_nothing_reads_is_reported(self) -> None:
        planted = "a_setting_no_file_anywhere_reads"

        unread = unread_settings(_ROOTS, [*_declared_keys(), planted], package_root=_PACKAGE_ROOT)

        # Both directions: the planted key is caught, AND a key with a real reader is not —
        # a walk that reported everything unread would satisfy the first alone.
        assert planted in unread
        assert "agent_harness" not in unread
        assert set(unread) - {planted} <= READER_LESS_SETTINGS

    def test_prose_naming_a_key_is_not_a_read(self) -> None:
        """Three of the six keys a narrower walk called unread are named only in docstrings.

        Counting prose would make the assertion pass for a key with no consumer at all,
        which is the failure this whole check exists to catch.
        """
        module = _PACKAGE_ROOT / "quality" / "setting_readership.py"
        prose_only = "a_key_named_only_in_a_docstring"
        assert prose_only not in module.read_text(encoding="utf-8")

        sites = read_sites([module.parent], [prose_only], package_root=_PACKAGE_ROOT)

        assert sites[prose_only] == set()

    def test_an_unreadable_root_refuses_rather_than_answering_empty(self, tmp_path: Path) -> None:
        with pytest.raises(UnreadableRootError):
            unread_settings([tmp_path / "not-a-directory"], _declared_keys(), package_root=_PACKAGE_ROOT)


class TestDeclarationsAreNotReadership:
    def test_the_schema_that_declares_a_key_does_not_count_as_reading_it(self) -> None:
        # Every key appears in `schema.py` by construction, so counting it would make the
        # UNREAD set empty for a tree with no consumers at all.
        sites = read_sites([_PACKAGE_ROOT / "config"], _declared_keys(), package_root=_PACKAGE_ROOT)
        declaring = {_PACKAGE_ROOT / "config" / name for name in DECLARATION_MODULES}

        assert not declaring & {path for found in sites.values() for path in found}
