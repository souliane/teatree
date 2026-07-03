import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from teatree.backends.figma import FigmaComponentMetadata, FigmaFrameRef
from teatree.cli import app

runner = CliRunner()


class TestFigmaClientFactory:
    def test_exits_with_hint_when_no_token_stored(self) -> None:
        with patch("teatree.cli.figma_tools.read_pass", return_value=""):
            result = runner.invoke(app, ["tool", "figma-frames", "abc123", "1:1"])

        assert result.exit_code == 1
        assert "pass show figma/pat" in result.output
        assert "pass insert figma/pat" in result.output


class TestFigmaScreenshotCLI:
    def test_saves_screenshot_and_reports_size(self, tmp_path: Path) -> None:
        dest = tmp_path / "shot.png"
        dest.write_bytes(b"PNG-BYTES")

        with (
            patch("teatree.cli.figma_tools.read_pass", return_value="tok"),
            patch("teatree.cli.figma_tools.FigmaClient") as client_cls,
        ):
            client_cls.return_value.get_screenshot.return_value = dest
            result = runner.invoke(
                app, ["tool", "figma-screenshot", "abc123", "1:2", "--dest", str(dest), "--scale", "3.0"]
            )

        assert result.exit_code == 0, result.output
        assert "Saved:" in result.output
        client_cls.return_value.get_screenshot.assert_called_once_with("abc123", "1:2", dest, scale=3.0)


class TestFigmaFramesCLI:
    def test_lists_child_frames(self) -> None:
        frames = [
            FigmaFrameRef(node_id="1:2", name="Header", node_type="FRAME"),
            FigmaFrameRef(node_id="1:3", name="Body", node_type="GROUP"),
        ]
        with (
            patch("teatree.cli.figma_tools.read_pass", return_value="tok"),
            patch("teatree.cli.figma_tools.FigmaClient") as client_cls,
        ):
            client_cls.return_value.list_frame_children.return_value = frames
            result = runner.invoke(app, ["tool", "figma-frames", "abc123", "1:1"])

        assert result.exit_code == 0, result.output
        assert "1:2" in result.output
        assert "Header" in result.output
        assert "1:3" in result.output
        assert "Body" in result.output

    def test_reports_when_no_children(self) -> None:
        with (
            patch("teatree.cli.figma_tools.read_pass", return_value="tok"),
            patch("teatree.cli.figma_tools.FigmaClient") as client_cls,
        ):
            client_cls.return_value.list_frame_children.return_value = []
            result = runner.invoke(app, ["tool", "figma-frames", "abc123", "1:1"])

        assert result.exit_code == 0, result.output
        assert "No child frames found." in result.output


class TestFigmaCommentsCLI:
    def test_fetches_all_comments_by_default(self) -> None:
        with (
            patch("teatree.cli.figma_tools.read_pass", return_value="tok"),
            patch("teatree.cli.figma_tools.FigmaClient") as client_cls,
        ):
            client_cls.return_value.get_comments.return_value = [{"id": "c1", "message": "hi"}]
            result = runner.invoke(app, ["tool", "figma-comments", "abc123"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == [{"id": "c1", "message": "hi"}]
        client_cls.return_value.get_comments.assert_called_once_with("abc123")
        client_cls.return_value.get_node_comments.assert_not_called()

    def test_filters_by_node_id_when_given(self) -> None:
        with (
            patch("teatree.cli.figma_tools.read_pass", return_value="tok"),
            patch("teatree.cli.figma_tools.FigmaClient") as client_cls,
        ):
            client_cls.return_value.get_node_comments.return_value = [{"id": "c1"}]
            result = runner.invoke(app, ["tool", "figma-comments", "abc123", "--node-id", "1:2"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == [{"id": "c1"}]
        client_cls.return_value.get_node_comments.assert_called_once_with("abc123", "1:2")
        client_cls.return_value.get_comments.assert_not_called()


class TestFigmaComponentsCLI:
    def test_prints_component_metadata_as_json(self) -> None:
        metadata = FigmaComponentMetadata(
            components={"1:2": {"name": "Button"}},
            component_sets={},
            styles={"S:1": {"name": "Primary"}},
            variant_properties={"1:1": {"Size": {"type": "VARIANT", "variantOptions": ["Small", "Large"]}}},
        )
        with (
            patch("teatree.cli.figma_tools.read_pass", return_value="tok"),
            patch("teatree.cli.figma_tools.FigmaClient") as client_cls,
        ):
            client_cls.return_value.get_component_metadata.return_value = metadata
            result = runner.invoke(app, ["tool", "figma-components", "abc123"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {
            "components": {"1:2": {"name": "Button"}},
            "component_sets": {},
            "styles": {"S:1": {"name": "Primary"}},
            "variant_properties": {"1:1": {"Size": {"type": "VARIANT", "variantOptions": ["Small", "Large"]}}},
        }


class TestFigmaCompareCLI:
    def test_builds_side_by_side_comparison(self, tmp_path: Path) -> None:
        design = tmp_path / "design.png"
        actual = tmp_path / "actual.png"
        dest = tmp_path / "out.png"
        design.write_bytes(b"design")
        actual.write_bytes(b"actual")

        fake_compare = MagicMock(return_value=dest)
        dest.write_bytes(b"combined")
        with patch("teatree.cli.figma_tools.build_side_by_side_comparison", fake_compare):
            result = runner.invoke(app, ["tool", "figma-compare", str(design), str(actual), "--dest", str(dest)])

        assert result.exit_code == 0, result.output
        assert "Saved:" in result.output
        fake_compare.assert_called_once_with(design, actual, dest)
