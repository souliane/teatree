"""The known-red QUARANTINE registry: parse, validate, expire, escape (#4173).

Selection is section-scoped, so while a scenario is red every PR touching the doctrine
section it grades reds its eval lane. The quarantine suppresses a tracked known-red from
the bounded selective-PR lane; these tests pin the registry's contract — a required
tracking issue and expiry date, a loud parse failure, an expiry that stops suppressing,
and the three ways an entry becomes a lie (the scenario is gone, it started passing, or
the run never carried it).
"""

from datetime import date
from pathlib import Path

import pytest

from teatree.eval.discovery import discover_specs
from teatree.eval.quarantine import (
    QUARANTINE_PATH,
    Quarantine,
    QuarantineEntry,
    QuarantineError,
    load_quarantine,
    suppressed_scenario_names,
)

ISSUE = "https://github.com/souliane/teatree/issues/4172"


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def _registry(tmp_path: Path, *, until: str = "2999-01-01", scenario: str = "flaky_one") -> Path:
    return _write(
        tmp_path / "quarantine.yaml",
        f"scenarios:\n  {scenario}:\n    issue: {ISSUE}\n    until: {until}\n    reason: tracked known red\n",
    )


class TestLoading:
    def test_parses_an_entry_into_its_typed_fields(self, tmp_path: Path) -> None:
        quarantine = load_quarantine(_registry(tmp_path, until="2026-09-04"))
        assert quarantine.entries == (
            QuarantineEntry(
                scenario="flaky_one",
                issue=ISSUE,
                until=date(2026, 9, 4),
                reason="tracked known red",
            ),
        )

    def test_a_missing_file_is_an_empty_quarantine_not_an_error(self, tmp_path: Path) -> None:
        # The sanctioned degraded state: an overlay with no registry of its own suppresses
        # nothing, which is byte-identical to the pre-quarantine selection.
        assert load_quarantine(tmp_path / "absent.yaml").entries == ()

    def test_an_empty_scenarios_map_is_an_empty_quarantine(self, tmp_path: Path) -> None:
        assert load_quarantine(_write(tmp_path / "q.yaml", "scenarios: {}\n")).entries == ()

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ("- not a mapping\n", "top-level mapping"),
            ("scenarios: [a, b]\n", "must be a mapping"),
            ("scenarios:\n  x: just a string\n", "mapping"),
            ("scenarios:\n  x:\n    until: 2999-01-01\n    reason: r\n", "issue"),
            (f"scenarios:\n  x:\n    issue: {ISSUE}\n    reason: r\n", "until"),
            (f"scenarios:\n  x:\n    issue: {ISSUE}\n    until: 2999-01-01\n", "reason"),
            (f"scenarios:\n  x:\n    issue: {ISSUE}\n    until: soon\n    reason: r\n", "until"),
            ("scenarios:\n  x:\n    issue: nope\n    until: 2999-01-01\n    reason: r\n", "issue"),
            (f"scenarios:\n  x:\n    issue: {ISSUE}\n    until: 2999-01-01\n    reason: r\n    typo: 1\n", "typo"),
        ],
    )
    def test_a_malformed_registry_fails_loud(self, tmp_path: Path, body: str, expected: str) -> None:
        # A present-but-malformed registry must never degrade to "suppress nothing" — a
        # typo'd entry would silently stop protecting the PR lane it was added for.
        with pytest.raises(QuarantineError, match=expected):
            load_quarantine(_write(tmp_path / "q.yaml", body))


class TestSuppression:
    def test_an_unexpired_entry_suppresses_its_scenario(self, tmp_path: Path) -> None:
        quarantine = load_quarantine(_registry(tmp_path, until="2026-09-04"))
        assert quarantine.suppressed(as_of=date(2026, 9, 4)) == frozenset({"flaky_one"})

    def test_an_expired_entry_stops_suppressing(self, tmp_path: Path) -> None:
        # Expiry is self-enforcing: the scenario re-arms and blocks again exactly as it did
        # before quarantine, so the list can never rot into a permanent skip.
        quarantine = load_quarantine(_registry(tmp_path, until="2026-09-04"))
        assert quarantine.suppressed(as_of=date(2026, 9, 5)) == frozenset()
        assert [entry.scenario for entry in quarantine.expired(as_of=date(2026, 9, 5))] == ["flaky_one"]

    def test_suppressed_scenario_names_reads_the_registry(self, tmp_path: Path) -> None:
        names = suppressed_scenario_names(path=_registry(tmp_path), as_of=date(2026, 9, 4))
        assert names == frozenset({"flaky_one"})


class TestEntriesThatBecameLies:
    def test_a_quarantined_scenario_that_passed_has_escaped(self, tmp_path: Path) -> None:
        quarantine = load_quarantine(_registry(tmp_path))
        escaped = quarantine.escaped(passing={"flaky_one", "unrelated"})
        assert [entry.scenario for entry in escaped] == ["flaky_one"]

    def test_a_still_failing_scenario_has_not_escaped(self, tmp_path: Path) -> None:
        quarantine = load_quarantine(_registry(tmp_path))
        assert quarantine.escaped(passing={"unrelated"}) == ()
        assert [entry.scenario for entry in quarantine.still_red(failing={"flaky_one"})] == ["flaky_one"]

    def test_an_entry_naming_no_such_scenario_is_unknown(self, tmp_path: Path) -> None:
        quarantine = load_quarantine(_registry(tmp_path))
        assert [entry.scenario for entry in quarantine.unknown(catalog={"other"})] == ["flaky_one"]
        assert quarantine.unknown(catalog={"flaky_one"}) == ()

    def test_a_scenario_the_run_never_carried_is_absent(self, tmp_path: Path) -> None:
        quarantine = load_quarantine(_registry(tmp_path))
        assert [entry.scenario for entry in quarantine.absent(ran={"other"})] == ["flaky_one"]
        assert quarantine.absent(ran={"flaky_one"}) == ()


class TestShippedRegistry:
    """The checked-in ``evals/quarantine.yaml`` is the gate's own input — it must stay honest.

    Expiry is deliberately NOT asserted here. An expired entry already stops suppressing
    (``TestSuppression``), so the scenario re-arms on its own; reddening the whole test
    suite on a date would re-create the every-PR blast radius #4173 exists to remove
    (`tests/teatree_cli/eval/test_quarantine_cli.py::TestCheck` carries the matching
    regression). The loud channel for a stale entry is ``t3 eval quarantine audit``,
    which the heal lane runs beside ``green-proof``; the selector's own note never
    mentions an expired entry, since :meth:`Quarantine.suppressed` excludes it.
    """

    def test_it_is_checked_in_where_the_loader_looks(self) -> None:
        # A missing file loads as empty, so an absent registry would make the whole
        # mechanism silently inert rather than fail.
        assert QUARANTINE_PATH.is_file()

    def test_it_parses(self) -> None:
        assert isinstance(load_quarantine(), Quarantine)

    def test_every_entry_names_a_real_scenario(self) -> None:
        catalog = {spec.name for spec in discover_specs()}
        assert load_quarantine().unknown(catalog=catalog) == ()
