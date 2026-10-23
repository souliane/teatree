from typer.testing import CliRunner

from teatree.cli.overlay import OverlayAppBuilder


def test_overlay_exposes_secrets_group() -> None:
    app = OverlayAppBuilder("teatree", None).build()
    result = CliRunner().invoke(app, ["secrets", "--help"])
    assert result.exit_code == 0, result.output
    assert "status" in result.output
    assert "ls" in result.output
    assert "import-pass" in result.output
