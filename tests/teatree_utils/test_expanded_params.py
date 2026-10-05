"""A dataclass parameter stands for its fields in the signature a framework reads, and arrives whole."""

import asyncio
import dataclasses
import inspect
import re
from pathlib import Path
from typing import Annotated

import pytest
import typer
import typer.testing
from asgiref.sync import async_to_sync
from mcp.server.mcpserver import MCPServer

from teatree.utils.expanded_params import Expand, expand_dataclass_params

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@dataclasses.dataclass(frozen=True, slots=True)
class _Text:
    file: Annotated[Path | None, typer.Option(help="File holding it.")] = None
    text: Annotated[str | None, typer.Option(help="Inline and verbatim.")] = None


@dataclasses.dataclass(frozen=True, slots=True)
class _Comment:
    page: str
    body: str
    marker: str = ""


class TestOnATyperCommand:
    @staticmethod
    def _runner() -> tuple[typer.Typer, typer.testing.CliRunner]:
        app = typer.Typer()

        @app.command()
        @expand_dataclass_params
        def append(page: str, *, body: Annotated[_Text, Expand("body_")], overlay: str = "") -> None:
            typer.echo(f"{page}|{overlay}|{body.file}|{body.text}")

        return app, typer.testing.CliRunner()

    def test_the_fields_become_prefixed_options_and_the_handler_receives_the_dataclass(self, tmp_path: Path) -> None:
        app, runner = self._runner()
        source = tmp_path / "b.md"

        inline = runner.invoke(app, ["p1", "--body-text", "hello", "--overlay", "o"])
        filed = runner.invoke(app, ["p1", "--body-file", str(source)])

        assert inline.stdout.strip() == "p1|o|None|hello"
        assert filed.stdout.strip() == f"p1||{source}|None"

    def test_the_fields_keep_the_help_their_annotation_carries(self) -> None:
        app, runner = self._runner()

        help_text = " ".join(_ANSI.sub("", runner.invoke(app, ["--help"]).stdout).split())

        assert "--body-file" in help_text
        assert "File holding it." in help_text
        assert "Inline and verbatim." in help_text

    def test_the_decorated_handler_reports_the_expanded_signature_and_the_original_keeps_its_own(self) -> None:
        def handler(page: str, *, body: Annotated[_Text, Expand("body_")]) -> None:
            _ = page, body

        expanded = expand_dataclass_params(handler)

        assert list(inspect.signature(expanded).parameters) == ["page", "body_file", "body_text"]
        assert list(inspect.signature(handler).parameters) == ["page", "body"]


class TestOnAnMcpTool:
    def test_an_unprefixed_dataclass_becomes_flat_tool_arguments_with_its_required_fields_required(self) -> None:
        async def comment(request: _Comment, *, dry_run: bool = True) -> dict[str, str]:
            await asyncio.sleep(0)
            return {"page": request.page, "marker": request.marker, "dry": str(dry_run)}

        server = MCPServer("probe")
        server.add_tool(expand_dataclass_params(comment), name="comment")

        schema = asyncio.run(server.list_tools())[0].input_schema
        assert set(schema["properties"]) == {"page", "body", "marker", "dry_run"}
        assert set(schema["required"]) == {"page", "body"}

    def test_the_async_handler_is_awaited_and_receives_the_dataclass(self) -> None:
        async def comment(request: _Comment, *, dry_run: bool = True) -> dict[str, str]:
            await asyncio.sleep(0)
            return {"page": request.page, "marker": request.marker, "dry": str(dry_run)}

        server = MCPServer("probe")
        server.add_tool(expand_dataclass_params(comment), name="comment")

        result = async_to_sync(server.call_tool)("comment", {"page": "p", "body": "b", "marker": "m", "dry_run": False})

        assert result.structured_content == {"page": "p", "marker": "m", "dry": "False"}


def test_two_dataclasses_whose_fields_collide_are_refused_at_decoration_time() -> None:
    def handler(first: _Text, second: _Text) -> None:
        _ = first, second

    with pytest.raises(ValueError, match="duplicate"):
        expand_dataclass_params(handler)


@dataclasses.dataclass(frozen=True, slots=True)
class _Listed:
    names: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True, slots=True)
class _Derived:
    given: str = ""
    computed: str = dataclasses.field(init=False, default="")


def test_a_default_factory_field_is_refused_at_decoration_time() -> None:
    def handler(request: _Listed) -> None:
        _ = request

    with pytest.raises(ValueError, match=r"_Listed\.names: an expanded field needs init=True"):
        expand_dataclass_params(handler)


def test_an_init_false_field_is_refused_at_decoration_time() -> None:
    def handler(request: _Derived) -> None:
        _ = request

    with pytest.raises(ValueError, match=r"_Derived\.computed: an expanded field needs init=True"):
        expand_dataclass_params(handler)
