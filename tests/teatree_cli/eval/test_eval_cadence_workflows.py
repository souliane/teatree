"""Static checks for the three-tier eval cadence workflows (owner-refined 2026-07-23).

PER-PR (`eval-pr.yml` + the reusable) runs only the changed scenarios and is
zero-is-RED; NIGHTLY (`eval-nightly.yml`) runs a small smoke slice ONLY on a day
something merged; WEEKLY (`eval.yml`) runs the full catalog on its own cron,
unchanged. These guard that contract against a future edit silently removing a
piece of it.
"""

from pathlib import Path

_WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"
_PR = _WORKFLOWS / "eval-pr.yml"
_PR_REUSABLE = _WORKFLOWS / "eval-pr-reusable.yml"
_NIGHTLY = _WORKFLOWS / "eval-nightly.yml"
_WEEKLY = _WORKFLOWS / "eval.yml"


class TestPerPrLane:
    def test_pr_lane_still_selects_only_the_changed_scenarios(self) -> None:
        text = _PR.read_text(encoding="utf-8")
        assert "scenarios_for_changed.py" in text
        assert "if: needs.detect.outputs.run == 'true'" in text

    def test_pr_lane_is_zero_is_red_on_a_run_that_executed_nothing(self) -> None:
        # detect promised >=1 scenario (run=true); the eval loop asserts it executed
        # at least one — a run that was supposed to execute scenarios but executed
        # ZERO must fail, never pass green.
        text = _PR.read_text(encoding="utf-8")
        assert "ran=$((ran + 1))" in text
        assert 'if [ "$ran" -eq 0 ]; then' in text

    def test_pr_lane_wires_the_advisory_reachability_check(self) -> None:
        # F12 / #3566: the reachability check gets a live production caller.
        text = _PR.read_text(encoding="utf-8")
        assert "t3 eval reachability" in text

    def test_reusable_pr_lane_carries_the_same_two_guards(self) -> None:
        text = _PR_REUSABLE.read_text(encoding="utf-8")
        assert 'if [ "$ran" -eq 0 ]; then' in text
        assert "t3 eval reachability" in text


class TestNightlySmokeLane:
    def test_nightly_runs_on_a_daily_cron(self) -> None:
        text = _NIGHTLY.read_text(encoding="utf-8")
        assert 'cron: "0 5 * * *"' in text

    def test_nightly_is_conditional_on_a_same_day_merge(self) -> None:
        # NOT an unconditional nightly: it runs only when >=1 PR merged in the last
        # day (a 1-day window, vs the weekly's 7). No merge → skip, no token spend.
        text = _NIGHTLY.read_text(encoding="utf-8")
        assert "merged_prs_since.py" in text
        assert "--days 1" in text
        assert "if: needs.prepare.outputs.run_eval == 'true'" in text

    def test_nightly_runs_a_bounded_cheap_smoke_slice(self) -> None:
        text = _NIGHTLY.read_text(encoding="utf-8")
        assert "--lane clean_room --shard 1/16" in text
        assert "--require-executed" in text

    def test_nightly_defaults_to_the_no_per_token_subscription_credential(self) -> None:
        text = _NIGHTLY.read_text(encoding="utf-8")
        assert "T3_AGENT_HARNESS_PROVIDER: ${{ inputs.credential || 'subscription_oauth' }}" in text


class TestWeeklyCatalogUntouched:
    def test_weekly_still_runs_the_full_catalog_on_its_own_cron(self) -> None:
        # The full-catalog weekly stays on its existing Monday 06:00 cadence — the
        # additive PER-PR + NIGHTLY tiers do not replace it.
        text = _WEEKLY.read_text(encoding="utf-8")
        assert 'cron: "0 6 * * 1"' in text
        assert "--days 7" in text
