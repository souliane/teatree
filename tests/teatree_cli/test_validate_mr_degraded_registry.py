"""``t3 tool validate-mr`` must not pass an MR it never graded.

The command's last arm treats "no overlay resolvable" as "nothing to check" and
exits 0. That arm cannot tell a genuinely-unconfigured install from one whose
``overlays`` registry read FAILED — and an unreadable store makes every
configured overlay vanish, so the pre-push hook's green means only that the
config was unreadable. A control confirmed it: the title ``wibble wobble not a
conventional commit`` exited 0.

These tests pin the distinction :func:`teatree.config.cold_reader.read_setting_confirmed`
already draws for :func:`teatree.config.loader._inject_db_registries`:

- a READABLE store with no overlays keeps the skip (a fresh install still pushes);
- an UNREADABLE store denies, naming the degraded read rather than the metadata; and
- neither path changes a verdict when an overlay DOES resolve.
"""

from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli import app
from teatree.config.cold_reader import SettingRead

runner = CliRunner()

_BAD_TITLE = "wibble wobble not a conventional commit"
_MODULE = "teatree.cli.tools"
_COLD = "teatree.config.cold_reader"


def _invoke(title: str):
    return runner.invoke(app, ["tool", "validate-mr", "--title", title, "--description", "## What\nbody"])


class TestDegradedRegistryFailsClosed:
    def test_unreadable_overlays_registry_denies(self) -> None:
        with (
            patch(f"{_MODULE}.get_overlay", return_value=None),
            patch(f"{_MODULE}.get_overlay_for_repo", return_value=None),
            patch(f"{_MODULE}.get_all_overlays", return_value={}),
            patch(f"{_COLD}.read_setting_confirmed", return_value=SettingRead(None, readable=False)),
        ):
            result = _invoke(_BAD_TITLE)
        assert result.exit_code != 0, "an unreadable overlay registry must never report a clean metadata check"
        assert "degraded" in result.output.lower()

    def test_readable_empty_registry_still_skips(self) -> None:
        with (
            patch(f"{_MODULE}.get_overlay", return_value=None),
            patch(f"{_MODULE}.get_overlay_for_repo", return_value=None),
            patch(f"{_MODULE}.get_all_overlays", return_value={}),
            patch(f"{_COLD}.read_setting_confirmed", return_value=SettingRead(None, readable=True)),
        ):
            result = _invoke(_BAD_TITLE)
        assert result.exit_code == 0, "a genuinely unconfigured install must keep skipping (never-lockout)"
        assert "skipping metadata check" in result.output


class TestResolvedOverlayVerdictUnchanged:
    def test_resolving_overlay_still_denies_a_bad_title(self) -> None:
        with (
            patch(f"{_MODULE}.get_overlay", return_value=object()),
            patch(f"{_MODULE}._validation_errors", return_value=["Title is not conventional."]),
        ):
            result = _invoke(_BAD_TITLE)
        assert result.exit_code != 0
        assert "Title is not conventional." in result.output

    def test_resolving_overlay_still_passes_a_good_title(self) -> None:
        with (
            patch(f"{_MODULE}.get_overlay", return_value=object()),
            patch(f"{_MODULE}._validation_errors", return_value=[]),
        ):
            result = _invoke("fix(gates): a conventional title")
        assert result.exit_code == 0
