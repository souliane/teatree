"""Tests for teatree.cli_reference — introspection-based CLI doc generation."""

import typer

from teatree.cli import app as real_app
from teatree.cli_reference import build_cli_reference_from_app


def _make_test_app() -> typer.Typer:
    app = typer.Typer(name="demo", no_args_is_help=True)

    @app.command()
    def hello(name: str = "world") -> None:
        """Say hello."""

    sub = typer.Typer(help="Sub commands.")
    app.add_typer(sub, name="sub")

    @sub.command()
    def greet() -> None:
        """Greet someone."""

    return app


class TestBuildCliReferenceFromApp:
    def test_includes_heading(self) -> None:
        result = build_cli_reference_from_app(_make_test_app(), base_name="demo")
        assert result.startswith("# CLI Reference\n")

    def test_includes_all_commands(self) -> None:
        result = build_cli_reference_from_app(_make_test_app(), base_name="demo")
        assert "## `demo`" in result
        assert "### `demo hello`" in result
        assert "### `demo sub`" in result
        assert "#### `demo sub greet`" in result

    def test_includes_help_text(self) -> None:
        result = build_cli_reference_from_app(_make_test_app(), base_name="demo")
        assert "Say hello" in result
        assert "Sub commands" in result
        assert "Greet someone" in result

    def test_help_blocks_are_fenced(self) -> None:
        result = build_cli_reference_from_app(_make_test_app(), base_name="demo")
        assert "```\n" in result

    def test_walks_real_t3_app(self) -> None:
        result = build_cli_reference_from_app(real_app)
        assert "# CLI Reference" in result
        assert "`t3`" in result
        assert "`t3 config`" in result

    def test_resolves_overlay_proxy_leaves_to_real_typer_app(self) -> None:
        """Overlay proxy leaves tagged with ``overlay_proxy`` render real leaf options."""
        import django  # noqa: PLC0415

        django.setup()
        from teatree.cli import register_overlay_commands  # noqa: PLC0415

        register_overlay_commands(allowlist={"t3-teatree"})
        result = build_cli_reference_from_app(real_app)
        assert "`t3 teatree e2e project`" in result
        assert "--update-snapshots" in result
