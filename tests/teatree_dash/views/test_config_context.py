"""The config page's typed context — its declared shape and the consumer that builds it (#3664)."""

from django.test import TestCase

from teatree.dash.config_surface import ConfigView
from teatree.dash.views.config import ConfigContext, _config_context


class TestConfigContext(TestCase):
    def test_the_context_declares_the_config_view_shape(self) -> None:
        assert ConfigContext.__annotations__ == {"config": ConfigView}

    def test_the_consumer_builds_a_context_carrying_the_config_view(self) -> None:
        ctx: ConfigContext = _config_context()
        assert set(ctx) == {"config"}
        assert isinstance(ctx["config"], ConfigView)
