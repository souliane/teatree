"""``_check_repos_claimed_by_disagreeing_overlays`` — the contested-repo advisory.

Two overlays declaring one repo is legitimate, and ``infer_overlay_for_url`` is ambiguity-safe
about it. What is NOT safe is two claimants that configure the guard rails differently: the
verdict for a guarded action then depends on which tier matched, or on which overlay the caller's
cwd anchored to, and both answers look correct. The instance that motivated it: a fork repo whose
root ``manage.py`` anchored one overlay and whose vendored ``manage.py`` one directory down
anchored the other, so the same approve proceeded from the root and refused from ``vendor/``.

Advisory only: it must not gate the exit code and must not crash a doctor run.

The predicate is pure and the wrapper's two reads are both patched, so nothing here needs a
database — these are plain classes rather than ``django.test.TestCase`` subclasses so the
suite does not provision one for a check that never queries.
"""

import io
from contextlib import redirect_stdout
from unittest.mock import patch

from teatree.cli.doctor.checks_overlay_claims import _check_repos_claimed_by_disagreeing_overlays, contested_repos

_MODULE = "teatree.cli.doctor.checks_overlay_claims"


def _run() -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        _check_repos_claimed_by_disagreeing_overlays()
    return buf.getvalue()


class TestTheContestPredicate:
    """The pure half: which claimed repos are contested by claimants that disagree."""

    def test_two_claimants_disagreeing_on_a_gate_are_reported(self) -> None:
        found = contested_repos(
            claims=[("t3-a", ["acme/widget"]), ("t3-b", ["acme/widget"])],
            gates={"t3-a": {"on_behalf_post_mode": "immediate"}, "t3-b": {"on_behalf_post_mode": "ask"}},
        )
        assert len(found) == 1
        assert found[0].slug == "widget"
        assert found[0].claimants == ("t3-a", "t3-b")
        assert found[0].disagreements == (("on_behalf_post_mode", (("t3-a", "immediate"), ("t3-b", "ask"))),)

    def test_a_bare_name_and_a_full_slug_are_the_same_claim(self) -> None:
        # The two shapes ``infer_overlay_for_url`` arbitrates between. Comparing the raw
        # strings would miss every contest the two-tier resolver exists for.
        found = contested_repos(
            claims=[("t3-a", ["widget"]), ("t3-b", ["acme-eng/platform/widget"])],
            gates={"t3-a": {"autonomy": "full"}, "t3-b": {"autonomy": "notify"}},
        )
        assert [f.slug for f in found] == ["widget"]

    def test_two_claimants_that_agree_are_silent(self) -> None:
        found = contested_repos(
            claims=[("t3-a", ["acme/widget"]), ("t3-b", ["acme/widget"])],
            gates={"t3-a": {"on_behalf_post_mode": "ask"}, "t3-b": {"on_behalf_post_mode": "ask"}},
        )
        assert found == ()

    def test_a_single_claimant_is_never_contested(self) -> None:
        found = contested_repos(
            claims=[("t3-a", ["acme/widget"]), ("t3-b", ["acme/other"])],
            gates={"t3-a": {"on_behalf_post_mode": "immediate"}, "t3-b": {"on_behalf_post_mode": "ask"}},
        )
        assert found == ()

    def test_a_divergence_outside_the_gate_set_is_not_a_finding(self) -> None:
        # An unrelated per-overlay difference changes no verdict; reporting it would bury
        # the findings that do.
        found = contested_repos(
            claims=[("t3-a", ["acme/widget"]), ("t3-b", ["acme/widget"])],
            gates={"t3-a": {"slack_channel": "#a"}, "t3-b": {"slack_channel": "#b"}},
        )
        assert found == ()

    def test_a_claimant_whose_settings_did_not_resolve_disagrees_with_everyone(self) -> None:
        found = contested_repos(
            claims=[("t3-a", ["acme/widget"]), ("t3-b", ["acme/widget"])],
            gates={"t3-a": {"on_behalf_post_mode": "ask"}},
        )
        assert found[0].disagreements == (("on_behalf_post_mode", (("t3-a", "ask"), ("t3-b", "<unresolved>"))),)

    def test_a_bool_gate_renders_and_compares_as_the_same_string(self) -> None:
        found = contested_repos(
            claims=[("t3-a", ["acme/widget"]), ("t3-b", ["acme/widget"])],
            gates={
                "t3-a": {"require_human_approval_to_merge": True},
                "t3-b": {"require_human_approval_to_merge": False},
            },
        )
        assert found[0].disagreements == (("require_human_approval_to_merge", (("t3-a", "True"), ("t3-b", "False"))),)


class TestTheAdvisoryOutput:
    """The wrapper: what a doctor run prints, and that it can never take the run down."""

    def test_a_contested_repo_is_named_with_the_gate_the_claimants_dispute(self) -> None:
        with (
            patch(
                f"{_MODULE}._declared_claims",
                return_value=[("t3-a", ["acme/widget"]), ("t3-b", ["widget"])],
            ),
            patch(
                f"{_MODULE}._resolved_gates",
                return_value={
                    "t3-a": {"on_behalf_post_mode": "immediate"},
                    "t3-b": {"on_behalf_post_mode": "ask"},
                },
            ),
        ):
            output = _run()
        assert "widget is claimed by t3-a and t3-b" in output
        assert "on_behalf_post_mode: t3-a=immediate, t3-b=ask" in output

    def test_no_contest_prints_nothing(self) -> None:
        with (
            patch(f"{_MODULE}._declared_claims", return_value=[("t3-a", ["acme/widget"])]),
            patch(f"{_MODULE}._resolved_gates", return_value={"t3-a": {"on_behalf_post_mode": "ask"}}),
        ):
            assert _run() == ""

    def test_a_crashing_probe_degrades_to_one_warn_line(self) -> None:
        with patch(f"{_MODULE}._declared_claims", side_effect=RuntimeError("registry unreadable")):
            output = _run()
        assert "Contested-repo check crashed: RuntimeError: registry unreadable" in output
