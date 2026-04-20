import teatree.cli.overlay_dev
from teatree.cli.overlay_dev import overlay_dev_app


class TestOverlayDevModule:
    def test_module_importable(self) -> None:
        assert teatree.cli.overlay_dev is not None

    def test_has_typer_app(self) -> None:
        assert overlay_dev_app is not None
