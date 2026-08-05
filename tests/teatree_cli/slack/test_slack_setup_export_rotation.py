"""``_export_with_rotation`` — the reactive config-token rotation shared by three commands.

``t3 slack setup``, ``t3 slack provision`` and ``t3 slack socket-doctor`` all reach
Slack's manifest API through this one function, so every failure it can raise is a
failure all three surface. A store fault must therefore leave through a diagnostic
and a clean exit code, never a traceback whose top frame is a ``pass`` probe.
"""

import io
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import pytest
import typer

from teatree.cli.slack.config_token import (
    ConfigTokenStore,
    SlackConfigTokenPersistError,
    SlackConfigTokenStoreUnwritableError,
)
from teatree.cli.slack.manifest import SlackManifestError
from teatree.cli.slack.setup import _export_with_rotation

_STORE = "teatree.cli.slack.setup.ConfigTokenStore"
_EXPORT = "teatree.cli.slack.setup.export_manifest"
_READ_PASS = "teatree.cli.slack.setup.read_pass"
_ROTATE = "teatree.cli.slack.setup.rotate_config_token"


def _exports_with_expired_token(store: MagicMock) -> tuple[int, str]:
    """Drive the rotation branch — the live token is rejected, a refresh half exists."""
    buf = io.StringIO()
    with (
        patch(_READ_PASS, return_value="xoxe-stored"),
        patch(_EXPORT, side_effect=SlackManifestError("invalid_auth")),
        patch(_STORE, return_value=store),
        patch(_ROTATE, return_value=("xoxe-new", "xoxe-new-refresh")),
        redirect_stdout(buf),
        pytest.raises(typer.Exit) as exit_info,
    ):
        _export_with_rotation(app_id="A123")
    return exit_info.value.exit_code, buf.getvalue()


class TestExportWithRotationStoreFaults:
    def test_an_unwritable_store_exits_with_a_diagnostic_not_a_traceback(self) -> None:
        store = MagicMock(spec=ConfigTokenStore)
        store.assert_writable.side_effect = SlackConfigTokenStoreUnwritableError(
            "refusing to rotate: gpg-agent could not start. NOTHING WAS SPENT"
        )

        code, out = _exports_with_expired_token(store)

        assert code == 1
        assert out.startswith("ERROR")
        assert "NOTHING WAS SPENT" in out

    def test_an_unwritable_store_never_spends_the_rotation(self) -> None:
        """The write-ahead exists to keep Slack's one-shot rotate unspent — prove it stays unspent."""
        store = MagicMock(spec=ConfigTokenStore)
        store.assert_writable.side_effect = SlackConfigTokenStoreUnwritableError("unwritable")

        with patch(_ROTATE, side_effect=AssertionError("rotate must not be called")) as rotate:
            buf = io.StringIO()
            with (
                patch(_READ_PASS, return_value="xoxe-stored"),
                patch(_EXPORT, side_effect=SlackManifestError("invalid_auth")),
                patch(_STORE, return_value=store),
                redirect_stdout(buf),
                pytest.raises(typer.Exit),
            ):
                _export_with_rotation(app_id="A123")

        rotate.assert_not_called()
        store.persist.assert_not_called()

    def test_a_lost_write_exits_with_the_re_mint_instruction(self) -> None:
        store = MagicMock(spec=ConfigTokenStore)
        store.persist.side_effect = SlackConfigTokenPersistError("could not persist ... re-mint at https://slack")

        code, out = _exports_with_expired_token(store)

        assert code == 1
        assert out.startswith("ERROR")
        assert "re-mint" in out
