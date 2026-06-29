# test-path: cross-cutting
"""The cold-hook integer budgets resolve from the DB store, TOML-fallback (config-unify PR4).

The integer sibling of ``test_teatree_settings_db_flip``. PR4 flips the three
``hook_router`` budgets — the deny-circuit-breaker threshold and the orchestrator
turn / wall-clock budgets — off their inline ``tomllib`` reads and onto the new
``teatree_settings.teatree_int_setting`` adapter, which resolves from the canonical
``ConfigSetting`` store FIRST (seeded by ``t3 setup``), falls back to the
``[teatree]`` TOML value, then the per-budget default.

These integration tests build a REAL ``teatree_config_setting`` sqlite file (the
exact Django-migration shape, JSON-encoded values) and read it back through the
LIVE ``hook_router`` reader functions — the actual repointed consumers — so the
DB-precedence, never-lockout TOML fallback, bool-rejection, and minimum/zero
semantics are exercised end to end against real sqlite and a real ``~/.teatree.toml``.
"""

import json
import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import NamedTuple

import pytest

import hooks.scripts.hook_router as router
from teatree.config import cold_reader

Row = tuple[str, str, object]


class Budget(NamedTuple):
    key: str
    reader_attr: str
    default: int
    minimum: int

    @property
    def reader(self) -> Callable[[], int]:
        return getattr(router, self.reader_attr)


_BUDGETS: list[Budget] = [
    Budget("deny_circuit_breaker_threshold", "_deny_circuit_breaker_threshold", 3, 1),
    Budget("orchestrator_turn_budget", "_orchestrator_turn_budget", 25, 0),
    Budget("orchestrator_turn_wall_clock_seconds", "_orchestrator_turn_wall_clock_threshold", 180, 0),
]


def _make_config_db(path: Path, rows: Iterable[Row]) -> None:
    """Build a real ``teatree_config_setting`` DB matching the Django migration."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE teatree_config_setting ("
            "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', "
            "key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES (?, ?, ?)",
            [(scope, key, json.dumps(value)) for scope, key, value in rows],
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A clean ``$HOME`` so a TOML fallback only fires when the test plants the file.

    Clearing ``T3_CONFIG_DB`` / ``XDG_DATA_HOME`` means the cold reader resolves
    under the isolated ``$HOME`` and never reads a host DB, so DB-precedence
    assertions are not masked by stray host config.
    """
    home_dir = tmp_path / "home"
    home_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.delenv("T3_CONFIG_DB", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    return home_dir


def _seed_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rows: Iterable[Row]) -> None:
    db = tmp_path / "db.sqlite3"
    _make_config_db(db, rows)
    monkeypatch.setenv("T3_CONFIG_DB", str(db))


def _write_teatree_toml(home: Path, body: str) -> None:
    (home / ".teatree.toml").write_text(f"[teatree]\n{body}", encoding="utf-8")


class TestBudgetReadersResolveDbFirst:
    """Each repointed ``hook_router`` budget reader is DB-first, TOML-fallback, default."""

    @pytest.mark.parametrize("budget", _BUDGETS, ids=lambda b: b.key)
    def test_seeded_db_row_is_honoured(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, budget: Budget
    ) -> None:
        # (a) a seeded DB row wins over the in-code default.
        _seed_db(monkeypatch, tmp_path, [("", budget.key, 7)])
        assert budget.reader() == 7

    @pytest.mark.parametrize("budget", _BUDGETS, ids=lambda b: b.key)
    def test_toml_value_honoured_when_db_absent(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, budget: Budget
    ) -> None:
        # (b) never-lockout: a present DB with no row for the budget falls back to
        # the ``[teatree]`` TOML value — the fail-open path that keeps a configured
        # budget working without a seeded row.
        _seed_db(monkeypatch, tmp_path, [("", "some_other_key", 1)])
        _write_teatree_toml(home, f"{budget.key} = 9\n")
        assert budget.reader() == 9

    @pytest.mark.parametrize("budget", _BUDGETS, ids=lambda b: b.key)
    def test_neither_db_nor_toml_returns_default(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, budget: Budget
    ) -> None:
        # (c) no DB file and no TOML → the in-code default.
        assert budget.reader() == budget.default

    @pytest.mark.parametrize("budget", _BUDGETS, ids=lambda b: b.key)
    def test_bool_db_value_is_rejected_and_falls_back(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, budget: Budget
    ) -> None:
        # (d) a stored bool is NOT a budget — a bool subclasses int but must never be
        # read as one, so it falls through to the TOML value (here, present).
        _seed_db(monkeypatch, tmp_path, [("", budget.key, True)])
        _write_teatree_toml(home, f"{budget.key} = 11\n")
        assert budget.reader() == 11

    @pytest.mark.parametrize("budget", _BUDGETS, ids=lambda b: b.key)
    def test_non_int_db_value_is_rejected_and_falls_back_to_default(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, budget: Budget
    ) -> None:
        # (d) a JSON string in the DB is not an int → rejected → default (no TOML).
        _seed_db(monkeypatch, tmp_path, [("", budget.key, "13")])
        assert budget.reader() == budget.default

    @pytest.mark.parametrize("budget", _BUDGETS, ids=lambda b: b.key)
    def test_zero_survives_only_when_minimum_is_zero(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, budget: Budget
    ) -> None:
        # (e) ``0`` is an explicit "off" for the orchestrator budgets (minimum=0) and
        # MUST survive; for the deny-circuit-breaker threshold (minimum=1) a value
        # ``< 1`` is malformed and falls back to the default.
        _seed_db(monkeypatch, tmp_path, [("", budget.key, 0)])
        expected = 0 if budget.minimum == 0 else budget.default
        assert budget.reader() == expected

    @pytest.mark.parametrize("budget", _BUDGETS, ids=lambda b: b.key)
    def test_below_minimum_value_falls_back_to_default(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, budget: Budget
    ) -> None:
        # (e) a value strictly below the minimum is always rejected to the default —
        # a negative budget can never be mistyped into disabling the guard.
        _seed_db(monkeypatch, tmp_path, [("", budget.key, budget.minimum - 1)])
        assert budget.reader() == budget.default


class TestIntHelperSemantics:
    """Helper-level behaviour the per-reader parametrization does not cover."""

    def test_db_value_wins_over_a_conflicting_toml_value(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from hooks.scripts import teatree_settings  # noqa: PLC0415

        _seed_db(monkeypatch, tmp_path, [("", "orchestrator_turn_budget", 40)])
        _write_teatree_toml(home, "orchestrator_turn_budget = 5\n")
        assert teatree_settings.teatree_int_setting("orchestrator_turn_budget", default=25, minimum=0) == 40

    def test_quoted_numeric_string_in_toml_is_rejected(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from hooks.scripts import teatree_settings  # noqa: PLC0415

        _write_teatree_toml(home, 'orchestrator_turn_budget = "5"\n')
        assert teatree_settings.teatree_int_setting("orchestrator_turn_budget", default=25, minimum=0) == 25

    def test_non_teatree_section_reads_its_toml_value_not_the_global_db_row(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from hooks.scripts import teatree_settings  # noqa: PLC0415

        _seed_db(monkeypatch, tmp_path, [("", "budget", 99)])  # a GLOBAL row that must NOT leak in
        (home / ".teatree.toml").write_text("[mysection]\nbudget = 7\n", encoding="utf-8")
        assert teatree_settings.section_int_setting("mysection", "budget", default=3, minimum=0) == 7

    def test_non_teatree_section_missing_returns_default(self, home: Path) -> None:
        from hooks.scripts import teatree_settings  # noqa: PLC0415

        assert teatree_settings.section_int_setting("mysection", "budget", default=3) == 3

    def test_no_minimum_allows_any_int(self, home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from hooks.scripts import teatree_settings  # noqa: PLC0415

        _seed_db(monkeypatch, tmp_path, [("", "budget", -42)])
        assert teatree_settings.teatree_int_setting("budget", default=3) == -42


class TestDelegatesToColdReader:
    """Anti-vacuous: patching ``cold_reader.read_setting`` flips the int reader output."""

    def test_reader_routes_through_cold_reader(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from hooks.scripts import teatree_settings  # noqa: PLC0415

        seen: list[tuple[str, str]] = []

        def _fake(name: str, *, scope: str = "", **_: object) -> object:
            seen.append((name, scope))
            return 17

        monkeypatch.setattr(cold_reader, "read_setting", _fake)
        assert teatree_settings.teatree_int_setting("orchestrator_turn_budget", default=25, minimum=0) == 17
        assert ("orchestrator_turn_budget", "") in seen

    def test_reader_fails_open_when_the_db_layer_raises(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from hooks.scripts import teatree_settings  # noqa: PLC0415

        def _boom(*_args: object, **_kwargs: object) -> object:
            msg = "db layer exploded"
            raise RuntimeError(msg)

        monkeypatch.setattr(cold_reader, "read_setting", _boom)
        assert teatree_settings.teatree_int_setting("orchestrator_turn_budget", default=25, minimum=0) == 25
