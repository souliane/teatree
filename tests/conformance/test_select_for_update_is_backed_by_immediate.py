"""Every locked read sits where ``BEGIN IMMEDIATE`` actually covers it (#4226).

On the production engine ``select_for_update()`` is a **silent no-op**:
``teatree.db.sqlite3_boundary`` reports ``has_select_for_update = False``, so
Django drops the ``FOR UPDATE`` clause without error. Write exclusion comes
entirely from ``transaction_mode: "IMMEDIATE"``
(``settings.SQLITE_WRITE_SERIALIZATION_OPTIONS``), which makes every
``transaction.atomic()`` take SQLite's reserved write lock from BEGIN through
COMMIT.

No behavioural test can reach that coupling: under Django's ``TestCase``
``atomic()`` degrades to a savepoint and the clause is dropped anyway, so a test
written against a call site exercises the **re-read** half and never the **lock**
half. This lane pins the two properties the coupling rests on instead.

*   A ``select_for_update()`` **outside** ``transaction.atomic()`` has no
    exclusion at all *and* raises nothing — Django's own "cannot be used outside
    of a transaction" guard is itself gated on ``has_select_for_update``, so on
    this backend the mistake is doubly silent.
*   ``skip_locked`` / ``nowait`` are semantics ``IMMEDIATE`` cannot reproduce: it
    blocks where they promise to return. Both are silently dropped.

The census the live gate re-derives on every run **is** the audit record: a
reader asking "how many locked reads are there, and is each one covered?" runs
this lane rather than repeating the sweep by hand.

The settings half of the coupling — that ``transaction_mode`` is still
``IMMEDIATE`` — is pinned by ``tests/test_sqlite_write_serialization.py``.
"""

import re
from pathlib import Path

from django.db.backends.sqlite3.base import DatabaseWrapper as StockSqliteWrapper

from teatree.db.sqlite3_boundary.base import DatabaseWrapper as BoundaryWrapper
from teatree.quality.catalog import AntiPatternEntry, load_catalog
from teatree.quality.select_for_update_audit import (
    CALLER_ATOMIC_PRAGMA,
    UNEMULATABLE_OPTIONS,
    Coverage,
    audit_source,
    audit_tree,
    render,
    violation_reason,
    violations,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src" / "teatree"

#: Call sites whose enclosing helper documents that its CALLER owns the
#: ``transaction.atomic()``. Pinned by name so a new pragma exemption cannot be
#: added without a reviewed edit here — the pragma is deliberately cheap to
#: write and must not be cheap to normalise.
_DECLARED_CALLER_CONTRACT_SITES = frozenset({"locked_get_or_create_ticket", "_lock_directive"})

#: The sweep found 40 locked reads. A floor well under that catches a scanner
#: that silently stops matching (a renamed AST field, a swallowed parse error)
#: without turning every new call site into a test edit.
_CENSUS_FLOOR = 30


def _reasons(source: str) -> list[str]:
    return [violation_reason(site) for site in audit_source(source, Path("<test>")) if violation_reason(site)]


def _coverages(source: str) -> list[Coverage]:
    return [site.coverage for site in audit_source(source, Path("<test>"))]


class TestTheLockClauseIsInert:
    """The premise: on this backend the row lock the call sites name does not exist."""

    def test_boundary_engine_inherits_the_stock_sqlite_features(self) -> None:
        assert issubclass(BoundaryWrapper, StockSqliteWrapper)

    def test_no_row_level_lock_is_available(self) -> None:
        features = BoundaryWrapper.features_class
        assert features.has_select_for_update is False, (
            "select_for_update() would now emit a real FOR UPDATE — re-derive the audit before trusting it"
        )

    def test_the_unemulatable_options_are_the_ones_the_backend_drops(self) -> None:
        features = BoundaryWrapper.features_class
        assert features.has_select_for_update_skip_locked is False
        assert features.has_select_for_update_nowait is False
        assert {"skip_locked", "nowait"} == UNEMULATABLE_OPTIONS


class TestLiveTreeIsCovered:
    """The gate: every locked read in ``src/teatree`` is covered by IMMEDIATE."""

    def test_no_locked_read_escapes_begin_immediate(self) -> None:
        offenders = violations(audit_tree([_SRC]))
        rendered = "\n".join(f"  {render(site)}" for site in offenders)
        assert not offenders, (
            "locked read(s) whose exclusion transaction_mode=IMMEDIATE does not supply:\n"
            f"{rendered}\n"
            "Wrap the read-modify-write in transaction.atomic(), or declare the caller's "
            f"contract with a same-line `# {CALLER_ATOMIC_PRAGMA}` pragma."
        )

    def test_the_census_is_the_audit_record(self) -> None:
        census = audit_tree([_SRC])
        assert len(census) >= _CENSUS_FLOOR, f"the sweep found only {len(census)} locked reads — is the scanner blind?"

    def test_caller_contract_exemptions_are_the_declared_set(self) -> None:
        exempt = {site.enclosing for site in audit_tree([_SRC]) if site.coverage is Coverage.CALLER_CONTRACT}
        assert exempt == _DECLARED_CALLER_CONTRACT_SITES

    def test_scan_skips_a_nonexistent_root(self) -> None:
        assert audit_tree([_REPO_ROOT / "does_not_exist"]) == []


class TestUncoveredReadsAreFlagged:
    """Anti-vacuity: the exact bug shapes go RED."""

    def test_module_level_locked_read_is_flagged(self) -> None:
        assert _reasons("row = Task.objects.select_for_update().get(pk=1)\n")

    def test_locked_read_in_a_plain_function_is_flagged(self) -> None:
        source = "def claim(pk):\n    return Task.objects.select_for_update().get(pk=pk)\n"
        assert _reasons(source) == [
            "outside transaction.atomic() — BEGIN IMMEDIATE is never taken, so nothing excludes a concurrent writer"
        ]

    def test_a_nested_function_does_not_inherit_the_outer_atomic(self) -> None:
        # The closure may be called long after the with-block has committed.
        source = (
            "def outer(pk):\n"
            "    with transaction.atomic():\n"
            "        def inner():\n"
            "            return Task.objects.select_for_update().get(pk=pk)\n"
            "        return inner\n"
        )
        assert _reasons(source)

    def test_a_lambda_does_not_inherit_the_outer_atomic(self) -> None:
        source = (
            "def outer(pk):\n"
            "    with transaction.atomic():\n"
            "        return lambda: Task.objects.select_for_update().get(pk=pk)\n"
        )
        assert _coverages(source) == [Coverage.NONE]

    def test_an_unrelated_with_block_does_not_cover(self) -> None:
        source = "def claim(pk):\n    with suppress(Error):\n        Task.objects.select_for_update().get(pk=pk)\n"
        assert _reasons(source)


class TestUnemulatableOptionsAreFlagged:
    """``skip_locked`` / ``nowait`` are flagged even inside a correct atomic block."""

    def test_skip_locked_inside_atomic_is_still_flagged(self) -> None:
        source = (
            "def sweep():\n    with transaction.atomic():\n        Task.objects.select_for_update(skip_locked=True)\n"
        )
        assert _reasons(source) == [
            "passes skip_locked — IMMEDIATE blocks where it promises to return, and the kwarg is silently dropped"
        ]

    def test_nowait_inside_atomic_is_still_flagged(self) -> None:
        source = "def sweep():\n    with transaction.atomic():\n        Task.objects.select_for_update(nowait=True)\n"
        assert _reasons(source) == [
            "passes nowait — IMMEDIATE blocks where it promises to return, and the kwarg is silently dropped"
        ]

    def test_both_options_are_reported_together(self) -> None:
        source = (
            "def sweep():\n"
            "    with transaction.atomic():\n"
            "        Task.objects.select_for_update(skip_locked=True, nowait=True)\n"
        )
        assert "nowait, skip_locked" in _reasons(source)[0]

    def test_an_option_immediate_does_supply_is_not_flagged(self) -> None:
        # ``of=`` narrows the lock; IMMEDIATE takes a strictly wider one, so nothing is lost.
        source = 'def sweep():\n    with transaction.atomic():\n        Task.objects.select_for_update(of=("self",))\n'
        assert _reasons(source) == []


class TestCoveredReadsAreClean:
    """Anti-vacuity's other half: every covered shape stays GREEN."""

    def test_with_transaction_atomic_covers(self) -> None:
        source = "def claim(pk):\n    with transaction.atomic():\n        Task.objects.select_for_update().get(pk=pk)\n"
        assert _coverages(source) == [Coverage.ATOMIC_BLOCK]

    def test_atomic_with_a_using_alias_covers(self) -> None:
        source = (
            "def claim(pk):\n"
            '    with transaction.atomic(using="control"):\n'
            "        Task.objects.select_for_update().get(pk=pk)\n"
        )
        assert _coverages(source) == [Coverage.ATOMIC_BLOCK]

    def test_a_bare_imported_atomic_covers(self) -> None:
        source = "def claim(pk):\n    with atomic():\n        Task.objects.select_for_update().get(pk=pk)\n"
        assert _coverages(source) == [Coverage.ATOMIC_BLOCK]

    def test_an_async_with_atomic_covers(self) -> None:
        source = (
            "async def claim(pk):\n"
            "    async with transaction.atomic():\n"
            "        Task.objects.select_for_update().get(pk=pk)\n"
        )
        assert _coverages(source) == [Coverage.ATOMIC_BLOCK]

    def test_a_second_context_manager_beside_atomic_covers(self) -> None:
        source = (
            "def claim(pk):\n"
            "    with transaction.atomic(), conn.cursor() as cur:\n"
            "        Task.objects.select_for_update().get(pk=pk)\n"
        )
        assert _coverages(source) == [Coverage.ATOMIC_BLOCK]

    def test_the_atomic_decorator_covers(self) -> None:
        source = "@transaction.atomic\ndef claim(pk):\n    return Task.objects.select_for_update().get(pk=pk)\n"
        assert _coverages(source) == [Coverage.ATOMIC_DECORATOR]

    def test_the_called_atomic_decorator_covers(self) -> None:
        source = '@atomic(using="control")\ndef claim(pk):\n    return Task.objects.select_for_update().get(pk=pk)\n'
        assert _coverages(source) == [Coverage.ATOMIC_DECORATOR]

    def test_an_async_def_atomic_decorator_covers(self) -> None:
        source = "@transaction.atomic\nasync def claim(pk):\n    return Task.objects.select_for_update().get(pk=pk)\n"
        assert _coverages(source) == [Coverage.ATOMIC_DECORATOR]

    def test_a_source_with_no_locked_read_yields_no_sites(self) -> None:
        assert audit_source("def claim(pk):\n    return Task.objects.get(pk=pk)\n", Path("<test>")) == []


class TestCallerContractPragma:
    """The declared-contract escape, and the shapes it must not silently widen."""

    _PRAGMA_SOURCE = (
        "def locked_get(pk):\n"
        '    """Caller must be inside ``transaction.atomic()``."""\n'
        f"    return Task.objects.select_for_update().get(pk=pk)  # {CALLER_ATOMIC_PRAGMA}\n"
    )

    def test_the_pragma_clears_an_otherwise_uncovered_read(self) -> None:
        assert _coverages(self._PRAGMA_SOURCE) == [Coverage.CALLER_CONTRACT]
        assert _reasons(self._PRAGMA_SOURCE) == []

    def test_the_pragma_is_honoured_anywhere_in_a_multi_line_call(self) -> None:
        # The call's own lines, not just the one ``node.lineno`` reports.
        source = (
            "def locked_get(pk):\n"
            "    return Task.objects.select_for_update(\n"
            '        of=("self",),\n'
            f"    ).get(pk=pk)  # {CALLER_ATOMIC_PRAGMA}\n"
        )
        assert _coverages(source) == [Coverage.CALLER_CONTRACT]

    def test_the_pragma_does_not_excuse_an_unemulatable_option(self) -> None:
        source = f"def sweep():\n    Task.objects.select_for_update(skip_locked=True)  # {CALLER_ATOMIC_PRAGMA}\n"
        assert _reasons(source)

    def test_an_unrelated_trailing_comment_does_not_clear_the_read(self) -> None:
        source = "def claim(pk):\n    return Task.objects.select_for_update().get(pk=pk)  # locked re-read\n"
        assert _coverages(source) == [Coverage.NONE]


class TestTheCatalogEntryPointsHere:
    """The anti-pattern catalog names this lane as the entry's mechanizer.

    ``tests/quality/test_catalog.py`` can only check that the name is
    allowlisted; this is the other half — the entry exists, is greppable, and its
    hint matches the option shapes this lane rejects.
    """

    _ENTRY_ID = "locked-read-the-backend-cannot-honour"

    def _entry(self) -> AntiPatternEntry:
        return next(entry for entry in load_catalog() if entry.id == self._ENTRY_ID)

    def test_the_entry_names_this_lane_as_its_linter(self) -> None:
        assert self._entry().linter == "select-for-update-audit"

    def test_the_grep_hint_matches_every_unemulatable_option(self) -> None:
        hint = self._entry().grep_hint
        assert hint is not None
        pattern = re.compile(hint)
        for option in sorted(UNEMULATABLE_OPTIONS):
            assert pattern.search(f"Task.objects.select_for_update({option}=True)"), option

    def test_the_grep_hint_leaves_a_plain_locked_read_alone(self) -> None:
        hint = self._entry().grep_hint
        assert hint is not None
        assert not re.compile(hint).search("Task.objects.select_for_update().get(pk=pk)")


class TestRendering:
    def test_render_names_the_file_the_function_and_the_reason(self) -> None:
        site = audit_source("def claim(pk):\n    Task.objects.select_for_update()\n", Path("a/b.py"))[0]
        assert render(site) == f"a/b.py:2 in claim() — {violation_reason(site)}"

    def test_render_of_a_covered_site_states_it_is_covered(self) -> None:
        source = "def claim(pk):\n    with transaction.atomic():\n        Task.objects.select_for_update()\n"
        assert "covered by transaction.atomic()" in render(audit_source(source, Path("a/b.py"))[0])

    def test_a_module_level_read_renders_its_scope(self) -> None:
        site = audit_source("Task.objects.select_for_update()\n", Path("a/b.py"))[0]
        assert site.enclosing == "<module>"
