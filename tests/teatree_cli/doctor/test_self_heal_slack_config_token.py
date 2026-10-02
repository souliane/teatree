"""``check_slack_config_token_fresh`` — the SessionStart auto-rotation of the Slack app-config pair.

Runs on a plain ``t3 doctor`` (no ``--repair``) because the owner's rule is that the
doctor fixes Slack tokens silently. Surfacing-only on every outcome except the two
STORE faults: the app-config token authorises manifest edits during ``t3 setup`` and
nothing on the delivery path, so a dead one must not redden a box whose factory is
healthy — but a ``pass`` store teatree can neither write nor read back is teatree's
own credential plane failing, and it freezes the pair on a 12-hour fuse.
"""

import io
from collections.abc import Callable
from contextlib import redirect_stdout
from unittest.mock import patch

from teatree.cli.doctor.self_heal_slack_config_token import check_slack_config_token_fresh
from teatree.cli.slack.config_token import RotationOutcome, RotationReport, SlackConfigTokenPersistError

_TARGET = "teatree.cli.doctor.self_heal_slack_config_token.ensure_fresh_config_token"
_OVERLAYS = "teatree.cli.doctor.self_heal_slack_config_token._slack_overlays"


def _echoes_unguarded(check: Callable[[], bool]) -> tuple[bool, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok = check()
    return ok, buf.getvalue()


def _echoes(check: Callable[[], bool]) -> tuple[bool, str]:
    buf = io.StringIO()
    with patch(_OVERLAYS, return_value=["t3"]), redirect_stdout(buf):
        ok = check()
    return ok, buf.getvalue()


class TestSlackConfigTokenSelfHeal:
    def test_a_rotation_is_announced_and_never_reddens_the_run(self) -> None:
        report = RotationReport(RotationOutcome.ROTATED, "rotated a pair that was 4.2h old")

        with patch(_TARGET, return_value=report):
            ok, out = _echoes(check_slack_config_token_fresh)

        assert ok is True
        assert out.startswith("WARN")
        assert "Auto-rotated" in out

    def test_a_fresh_pair_says_nothing(self) -> None:
        """Silent when nothing was due — the doctor must not grow a line per healthy run."""
        with patch(_TARGET, return_value=RotationReport(RotationOutcome.FRESH, "pair is 0.5h old")):
            ok, out = _echoes(check_slack_config_token_fresh)

        assert ok is True
        assert out == ""

    def test_an_unseeded_box_says_nothing(self) -> None:
        """Slack is optional; a box that never seeded a pair is not in a degraded state."""
        with patch(_TARGET, return_value=RotationReport(RotationOutcome.NOT_CONFIGURED, "no refresh token")):
            ok, out = _echoes(check_slack_config_token_fresh)

        assert ok is True
        assert out == ""

    def test_an_unrecoverable_pair_warns_loudly_but_stays_green(self) -> None:
        report = RotationReport(RotationOutcome.UNRECOVERABLE, "Slack refused; mint a fresh pair at ...")

        with patch(_TARGET, return_value=report):
            ok, out = _echoes(check_slack_config_token_fresh)

        assert ok is True
        assert out.startswith("WARN")
        assert "cannot self-heal" in out

    def test_a_lost_write_is_the_one_hard_failure(self) -> None:
        """Rotating and then losing the new pair is teatree's own fault, and bricks the credential."""
        with patch(_TARGET, side_effect=SlackConfigTokenPersistError("pass insert failed")):
            ok, out = _echoes(check_slack_config_token_fresh)

        assert ok is False
        assert out.startswith("FAIL")

    def test_an_unwritable_store_reports_and_fails(self) -> None:
        """A refused rotation must never pass silently — the pair is frozen on a 12h fuse.

        Nothing was spent, so the pair is intact right now; but nothing CAN be
        rotated either, so staying green here is a green that holds until the
        credential expires for good and needs a hand-minted replacement.
        """
        report = RotationReport(RotationOutcome.STORE_UNWRITABLE, "the write reported success but ... gpg-agent")

        with patch(_TARGET, return_value=report):
            ok, out = _echoes(check_slack_config_token_fresh)

        assert ok is False
        assert out.startswith("FAIL")
        assert "gpg-agent" in out

    def test_an_unexpected_crash_never_breaks_the_doctor_run(self) -> None:
        with patch(_TARGET, side_effect=RuntimeError("gpg-agent unavailable")):
            ok, out = _echoes(check_slack_config_token_fresh)

        assert ok is True
        assert out.startswith("WARN")

    def test_a_box_with_no_slack_overlay_never_reads_a_credential(self) -> None:
        """A box with no Slack overlay must not read a credential or reach the network.

        There is no manifest to edit, so checking on the pair every doctor run
        would be pure cost — and a live gpg + HTTP call on a path this hot is
        exactly what broke the loop-tick placement this check replaced.
        """
        with (
            patch(_OVERLAYS, return_value=[]),
            patch(_TARGET, side_effect=AssertionError) as rotate,
        ):
            ok, out = _echoes_unguarded(check_slack_config_token_fresh)

        assert ok is True
        assert out == ""
        rotate.assert_not_called()
