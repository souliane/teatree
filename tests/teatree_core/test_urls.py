from importlib.util import find_spec

import pytest
from django.urls import Resolver404, resolve


def test_slack_hook_route_is_gone() -> None:
    with pytest.raises(Resolver404):
        resolve("/hooks/slack/")
    assert find_spec("teatree.core.views.slack_webhook") is None
