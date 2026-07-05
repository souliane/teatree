"""UserSettings field ↔ ≥1 reader — the dead-config totality lane (SELFCATCH-4).

``test_removed_dead_settings.py`` guards exactly two historically-removed fields
(``branch_prefix``, ``ask_before_post_on_behalf``, #2731) and ``test_feature_flags``
governs only flag lifecycle. Neither is total: a NEW ``UserSettings`` field that
nothing reads — the dead-toggle class — ships green, an operator-facing knob that
silently does nothing.

This lane makes the guard total by introspection. Every :class:`UserSettings`
field must be READ by at least one consumer: a ``.<field>`` attribute access or an
exact ``"<field>"`` string constant somewhere under ``src/teatree`` /
``hooks/*.py`` (AST — so a docstring merely *mentioning* the name, being a longer
string, never counts as a read), or the field name on a non-comment line of a
``hooks/*.sh`` script (the pre-Django cold-read seam). A field read by none of
these is dead config and fails, unless it is named in ``FIELDS_WITHOUT_SRC_READER``
— the reviewable allowlist of fields consumed by agent-prose / documentation
rather than ``src`` code.
"""

import ast
import dataclasses
from pathlib import Path

from teatree.config import UserSettings

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SETTINGS_DEF = (_REPO_ROOT / "src" / "teatree" / "config" / "settings.py").resolve()
_PY_ROOTS = (_REPO_ROOT / "src" / "teatree", _REPO_ROOT / "hooks")
_SH_ROOT = _REPO_ROOT / "hooks"

# Fields whose ONLY consumer is not ``src`` Python — documented, reviewable.
# ``e2e_confidence_threshold`` is documentation-driven by design (BLUEPRINT §
# configuration: "the typed field is the shared source of truth for the doc value
# and any future programmatic consumer"); the ``/t3:e2e`` verify↔review loop is
# agent prose, not a deterministic gate, so it reads the value from the skill.
# ``issue_implementer_cadence_hours`` is the documented cadence default the
# issue-implementer loop mirrors as a literal (``default_cadence_seconds=3600``);
# the field is not yet a live reader-driven knob. A NEW dead field is NOT on this
# list and fails until it gains a reader or a conscious allowlist entry.
FIELDS_WITHOUT_SRC_READER: frozenset[str] = frozenset(
    {
        "e2e_confidence_threshold",
        "issue_implementer_cadence_hours",
    }
)


def _field_names() -> set[str]:
    return {field.name for field in dataclasses.fields(UserSettings)}


def _python_readers(field_names: set[str]) -> set[str]:
    """Fields read by ``src``/``hooks`` Python: a ``.<field>`` access or exact ``"<field>"`` literal.

    AST-based, so a comment or a prose docstring naming a field never counts — an
    ``Attribute`` node's ``attr`` is the real access, and a ``Constant`` must equal
    the field name EXACTLY (a docstring mentioning it is a longer, unequal string).
    """
    read: set[str] = set()
    for root in _PY_ROOTS:
        for path in root.rglob("*.py"):
            if path.resolve() == _SETTINGS_DEF:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"), filename=str(path))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in field_names:
                    read.add(node.attr)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in field_names:
                    read.add(node.value)
    return read


def _shell_readers(field_names: set[str]) -> set[str]:
    """Fields named on a non-comment line of a ``hooks/*.sh`` script (the cold-read seam).

    ``statusline_chain`` is read by ``statusline.sh`` via a direct ``sqlite3`` query
    on the ConfigSetting store before Django is up; that is a real reader a Python
    walk cannot see. Comment lines (``#``-led) are skipped so a mention in prose
    does not count as a read.
    """
    read: set[str] = set()
    for path in _SH_ROOT.rglob("*.sh"):
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.lstrip().startswith("#"):
                continue
            for name in field_names:
                if name in line:
                    read.add(name)
    return read


def unread_fields() -> set[str]:
    """Every ``UserSettings`` field with no reader and no allowlist entry — the lane core."""
    names = _field_names()
    read = _python_readers(names) | _shell_readers(names)
    return names - read - FIELDS_WITHOUT_SRC_READER


class TestEveryUserSettingsFieldIsRead:
    """A setting nothing reads is dead config — every field has a consumer."""

    def test_no_user_settings_field_is_dead_config(self) -> None:
        unread = sorted(unread_fields())
        assert not unread, (
            "UserSettings field(s) that no src/hooks consumer reads (dead config) — "
            "wire a reader or add to FIELDS_WITHOUT_SRC_READER on purpose: " + str(unread)
        )

    def test_allowlisted_fields_are_still_real_fields(self) -> None:
        # A stale allowlist entry (a field that was removed) is dead surface.
        stale = sorted(FIELDS_WITHOUT_SRC_READER - _field_names())
        assert not stale, f"FIELDS_WITHOUT_SRC_READER entries that are not UserSettings fields: {stale}"

    def test_allowlisted_fields_genuinely_lack_a_src_reader(self) -> None:
        # Keep the allowlist honest: an allowlisted field that DID gain a src reader
        # should be removed from the allowlist, not left masking coverage.
        names = _field_names()
        read = _python_readers(names) | _shell_readers(names)
        redundant = sorted(FIELDS_WITHOUT_SRC_READER & read)
        assert not redundant, f"FIELDS_WITHOUT_SRC_READER entries that now HAVE a src reader (drop them): {redundant}"


class TestUserSettingsReadersCardinalityFloors:
    """Anti-vacuity — a broken reader walk that finds nothing must not pass green."""

    def test_field_and_reader_floors(self) -> None:
        names = _field_names()
        read = _python_readers(names) | _shell_readers(names)
        assert len(names) >= 100, len(names)
        # The overwhelming majority of fields must resolve a reader, else the walk broke.
        assert len(read) >= 100, len(read)


class TestUserSettingsReadersFiresRed:
    """Anti-vacuity — a synthetic never-read field must be reported."""

    def test_a_synthetic_unread_field_is_named(self) -> None:
        # Model the dead-toggle class directly: a field name no reader mentions and
        # no allowlist names is exactly what the lane must flag.
        names = _field_names() | {"synthetic_dead_toggle_nothing_reads"}
        read = _python_readers(names) | _shell_readers(names)
        unread = names - read - FIELDS_WITHOUT_SRC_READER
        assert "synthetic_dead_toggle_nothing_reads" in unread

    def test_a_real_read_field_is_not_flagged(self) -> None:
        # Positive control: a field with a real ``.<field>`` reader resolves a reader
        # and is therefore never in the unread set.
        names = _field_names()
        assert "max_concurrent_local_stacks" in names
        read = _python_readers(names) | _shell_readers(names)
        assert "max_concurrent_local_stacks" in read
