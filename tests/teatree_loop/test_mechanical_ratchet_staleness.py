"""The reference-ratchet staleness handler (#4451) — it reports, and it changes nothing."""

from unittest.mock import patch

import django.test

from teatree.loop.mechanical_ratchet_staleness import report_ratchet_staleness

_ROWS = [
    ["python_prose", "src/teatree/loop/scanners/self_update_ci.py", "teatree.loop.scanners.pr_sweep.GhPrApiClient"]
]


class ReportRatchetStalenessTests(django.test.TestCase):
    def test_it_names_the_pins_and_the_one_command_repair(self) -> None:
        with patch("teatree.loop.mechanical_ratchet_staleness.notify_user", return_value=True) as notify:
            report_ratchet_staleness({"repo": "/clone", "stale": _ROWS})

        text = notify.call_args.args[0]
        assert "teatree.loop.scanners.pr_sweep.GhPrApiClient" in text
        assert "t3 tool ratchet-prune --write" in text, "a report without the repair is the #4451 diagnosis cost again"
        assert "/clone" in text

    def test_an_empty_stale_set_notifies_nothing(self) -> None:
        with patch("teatree.loop.mechanical_ratchet_staleness.notify_user") as notify:
            report_ratchet_staleness({"repo": "/clone", "stale": []})

        notify.assert_not_called()

    def test_the_key_tracks_the_stale_set_not_the_tick(self) -> None:
        # The same condition across ticks must dedupe; a GROWN condition must re-notify.
        with patch("teatree.loop.mechanical_ratchet_staleness.notify_user", return_value=True) as notify:
            report_ratchet_staleness({"repo": "/clone", "stale": _ROWS})
            report_ratchet_staleness({"repo": "/clone", "stale": _ROWS})
            report_ratchet_staleness({"repo": "/clone", "stale": [*_ROWS, ["charter", "BLUEPRINT.md", "teatree.gone"]]})

        keys = [call.kwargs["idempotency_key"] for call in notify.call_args_list]
        assert keys[0] == keys[1], "the same stale set must reuse one key"
        assert keys[2] != keys[0], "a grown stale set must not be suppressed by the earlier key"

    def test_a_messaging_failure_never_raises_into_the_tick(self) -> None:
        with patch("teatree.loop.mechanical_ratchet_staleness.notify_user", side_effect=RuntimeError("slack down")):
            report_ratchet_staleness({"repo": "/clone", "stale": _ROWS})

    def test_a_long_list_is_truncated_with_an_honest_trailer(self) -> None:
        rows = [["python_prose", f"src/teatree/m{i}.py", f"teatree.m{i}.GONE"] for i in range(14)]
        with patch("teatree.loop.mechanical_ratchet_staleness.notify_user", return_value=True) as notify:
            report_ratchet_staleness({"repo": "/clone", "stale": rows})

        text = notify.call_args.args[0]
        assert "14 stale" in text, "the count must be the real one, not the listed one"
        assert "… and 4 more" in text
