"""Pin shipped loop defaults to the seeds that materialize them."""

import re
import tomllib
from pathlib import Path

from teatree.config.resolution import effective_default
from teatree.loops.preset_seed import DEFAULT_ACTIVE_SCHEDULE
from teatree.loops.registry import iter_loops

DEFAULTS = Path(__file__).resolve().parents[2] / "src" / "teatree" / "config" / "defaults.toml"

ACTIVE_SCHEDULE = "standard"


class TestActiveLoopScheduleShipsUnsetAndIsMaterialisedBySeed:
    """`standard` reaches a box through the SEED, and the shipped file stays the sentinel.

    Every box carrying `active_loop_schedule = standard` reads like a default each operator
    had to correct. None did: `t3 setup` writes that row itself. Shipping `standard` in the
    file instead would break the cold-key rule that a shipped value is the UNSET sentinel
    (`test_declared_default_base.TestEveryShippedKeyIsPinned`), and would name a schedule that
    does not exist yet on a box whose seed has not run — logging `names unknown schedule`
    on every resolution until it does.
    """

    def test_the_fresh_install_pin_is_the_working_hours_calendar(self) -> None:
        assert DEFAULT_ACTIVE_SCHEDULE == ACTIVE_SCHEDULE

    def test_the_shipped_file_stays_the_unset_sentinel(self) -> None:
        assert effective_default("active_loop_schedule") == ""


class TestArchReviewRunsDaily:
    """A codebase-wide review sub-agent may not be re-checked eight times a day.

    The scanner already gates firing on `architectural_review_cadence_hours` (168) and a
    12h post-failure backoff, so this row is only how often that gate is CHECKED — but a
    3h interval spent seven wakeups a day proving the weekly gate had not elapsed, on the
    live tick. `daily_at` is the same mechanism `dream` and `news` use to sit off it.
    """

    def _loop(self) -> dict:
        return tomllib.loads(DEFAULTS.read_text("utf-8"))["loops"]["arch_review"]

    def test_it_is_a_daily_loop(self) -> None:
        assert self._loop()["delay_seconds"] == 86400

    def test_it_fires_off_the_live_tick_at_a_fixed_hour(self) -> None:
        # A bare 86400 interval drifts with whenever the worker last restarted; `daily_at`
        # is what pins it to a quiet hour, which is the whole point for a review sub-agent.
        assert self._loop()["daily_at"].isoformat() == "04:00:00"

    def test_its_description_no_longer_advertises_the_old_interval(self) -> None:
        assert "every 3h" not in self._loop()["description"]


class TestShippedDescriptionsAreTrue:
    """A shipped description is what a reader trusts when deciding whether to enable a loop.

    Both guards are INVARIANTS, not string equality: pinning the literal text would go
    stale on the next legitimate reword, and neither would have caught the defect that
    prompted them. `arch_review` claimed "off the live tick" while carrying no
    `off_live_tick`, because `daily_at` changes when a loop is DUE and never which driver
    runs it.
    """

    def _loops(self) -> dict:
        return tomllib.loads(DEFAULTS.read_text("utf-8"))["loops"]

    def _tables(self) -> list[dict]:
        shipped = tomllib.loads(DEFAULTS.read_text("utf-8"))
        return [shipped[table] for table in ("loops", "modes", "schedules")]

    def test_only_a_declared_off_tick_loop_may_claim_it_runs_off_the_live_tick(self) -> None:
        declared = {loop.name for loop in iter_loops() if loop.off_live_tick}
        claiming = {name for name, row in self._loops().items() if "off the live tick" in row["description"].lower()}
        assert claiming == declared, f"claimed {sorted(claiming)} but code declares {sorted(declared)}"

    def test_a_loop_scoped_to_our_own_repos_says_so_where_an_operator_reads_it(self) -> None:
        # The scope lives as a condition at the scanner factory, which no operator reads.
        # `t3 loops list`, the dash and the doctor all render this line instead.
        assert "only for t3-teatree owned repos" in self._loops()["issue_disposition"]["description"]

    def test_no_shipped_description_carries_an_internal_issue_or_directive_reference(self) -> None:
        # These render in `t3 loops list`; an operator cannot resolve a bare `#3895`.
        archeology = re.compile(r"#\d+|\bdirective \d+\b", re.IGNORECASE)
        found = {
            name: archeology.findall(row["description"])
            for table in self._tables()
            for name, row in table.items()
            if archeology.search(row["description"])
        }
        assert not found, f"operator-facing descriptions carry internal references: {found}"
