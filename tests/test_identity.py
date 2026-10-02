"""Tests for the user-on-behalf signature policy.

``agent_signature`` is DB-home (#1775): the default-off behaviour resolves from
the dataclass default (no row needed), and enabling it is a GLOBAL-scope
``ConfigSetting`` row — a ``[teatree] agent_signature`` TOML key is ignored on
read. Integration-first per the Test-Writing Doctrine: ``get_effective_settings``
is exercised end-to-end through ``agent_signature_enabled`` / ``_suffix``.
"""

from django.test import TestCase

from teatree.core.models import ConfigSetting
from teatree.identity import agent_signature_enabled, agent_signature_suffix


class TestSignatureDisabledByDefault(TestCase):
    """With no ``agent_signature`` row the default-off resolves from the dataclass default."""

    def test_signature_disabled_by_default(self) -> None:
        assert agent_signature_enabled() is False
        assert agent_signature_suffix("\n— Sent using Claude") == ""

    def test_signature_disabled_when_unset(self) -> None:
        assert agent_signature_enabled() is False
        assert agent_signature_suffix("\nCo-Authored-By: agent <a@b>") == ""


class TestSignatureEnabled(TestCase):
    """Enabling the signature is the DB-home ``agent_signature`` row (#1775)."""

    def test_signature_enabled_passes_suffix_through(self) -> None:
        ConfigSetting.objects.set_value("agent_signature", value=True)
        assert agent_signature_enabled() is True
        assert agent_signature_suffix("\n— from the assistant") == "\n— from the assistant"
