"""``on_behalf_gate_active`` resolves the gate; it never degrades to "gate off".

The helper used to wrap its ``teatree.on_behalf_gate`` import in
``except ModuleNotFoundError: return False`` — a safety gate failing OPEN. The
module is a first-party leaf importing only ``enum`` and ``teatree.config``, so
that branch was unreachable; these tests pin that an unimportable-gate world now
surfaces rather than silently un-gating an approve/unapprove.
"""

import builtins
import sys
from typing import Any

import pytest
from django.test import TestCase

from teatree.cli.review.on_behalf import on_behalf_gate_active
from teatree.core.models import ConfigSetting

_GATE_MODULE = "teatree.on_behalf_gate"


class TestGateResolutionNeverFailsOpen:
    def test_an_unimportable_gate_module_raises_rather_than_reporting_gate_off(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        real_import = builtins.__import__

        def refuse(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == _GATE_MODULE:
                detail = f"No module named {name!r}"
                raise ModuleNotFoundError(detail, name=name)
            return real_import(name, *args, **kwargs)

        monkeypatch.delitem(sys.modules, _GATE_MODULE, raising=False)
        monkeypatch.setattr(builtins, "__import__", refuse)

        with pytest.raises(ModuleNotFoundError):
            on_behalf_gate_active()


class TestGateResolutionFollowsTheMode(TestCase):
    """``approve`` is a non-draft action: gated under both blocking modes."""

    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in ("T3_OVERLAY_NAME", "T3_ON_BEHALF_POST_MODE", "T3_ON_BEHALF_AUTO_ACTIONS"):
            monkeypatch.delenv(env, raising=False)

    def test_ask_reports_the_gate_active(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "ask")
        assert on_behalf_gate_active() is True

    def test_draft_or_ask_reports_the_gate_active(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "draft_or_ask")
        assert on_behalf_gate_active() is True

    def test_immediate_reports_the_gate_inactive(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate")
        assert on_behalf_gate_active() is False
