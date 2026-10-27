"""Routed metered keys under the plans-first composite, with the plans capped until a known reset."""

import os
from collections.abc import Sequence
from datetime import datetime, timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import ConfigSetting, TaskAttempt, UsageWindowState
from teatree.llm.anthropic_limits import LimitCause

KEY_1 = "anthropic/key-1/api-key"
KEY_2 = "anthropic/key-2/api-key"
SCOPE = ""


def cap_the_plans(*, until: datetime | None = None) -> datetime:
    reset = until or timezone.now() + timedelta(days=3)
    UsageWindowState.record_limit(
        lane=TaskAttempt.Lane.SUBSCRIPTION, cause=LimitCause.SUBSCRIPTION_WEEKLY.value, resets_at=reset
    )
    return reset


def route_metered_keys(test: TestCase, keys: Sequence[str], *, provider: str = "subscription_then_api_key") -> None:
    """Configure *keys* as the routed API keys; an empty *keys* leaves the ambient ``ANTHROPIC_API_KEY`` alone."""
    ConfigSetting.objects.set_value("agent_harness_provider", provider)
    if keys:
        ConfigSetting.objects.set_value("anthropic_api_key_pass_paths", list(keys))
        test.enterContext(patch.dict(os.environ))
        os.environ.pop("ANTHROPIC_API_KEY", None)
        test.enterContext(patch("teatree.llm.credentials.read_pass", side_effect=lambda path: path))
