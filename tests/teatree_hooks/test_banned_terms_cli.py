"""Tests for the shared banned-terms source resolver (config-unify, task #36).

``resolve_banned_terms`` is the single source-resolution every banned-terms
scanner shares so they cannot diverge on WHERE the term list comes from. It is
the fail-closed, secret-aware twin of the config-unify cold-hook readers: a
SECRET cold setting resolves env-override → ``[teatree].banned_terms`` TOML, with
NO DB tier (the customer/brand terms must never reach the exportable
``ConfigSetting`` store). A genuinely-unset-but-present config RAISES rather than
silently degrading to an empty ban list — the anti-vacuity contract that keeps
the security gate from going inert on a load bug.

All terms here are SYNTHETIC (``acme`` / ``widget-margin``) — no real customer
value, so this public test leaks nothing.
"""

from pathlib import Path

import pytest

from teatree.hooks.banned_terms_cli import resolve_banned_terms
from teatree.hooks.banned_terms_tree_scan import BannedTermsUnsetError

_SYNTHETIC_TERMS = ("acme", "widget-margin")


def _write_config(tmp_path: Path, body: str) -> Path:
    config = tmp_path / ".teatree.toml"
    config.write_text(body, encoding="utf-8")
    return config


class TestResolveBannedTerms:
    def test_toml_list_is_honoured(self, tmp_path: Path) -> None:
        config = _write_config(tmp_path, '[teatree]\nbanned_terms = ["acme", "widget-margin"]\n')
        assert resolve_banned_terms(config) == _SYNTHETIC_TERMS

    def test_env_override_wins_over_toml(self, tmp_path: Path) -> None:
        config = _write_config(tmp_path, '[teatree]\nbanned_terms = ["from-toml"]\n')
        assert resolve_banned_terms(config, env_value="acme, widget-margin") == _SYNTHETIC_TERMS

    def test_env_override_without_a_config_file(self, tmp_path: Path) -> None:
        assert resolve_banned_terms(tmp_path / "absent.toml", env_value="acme,widget-margin") == _SYNTHETIC_TERMS

    def test_missing_config_file_is_a_clean_no_op(self, tmp_path: Path) -> None:
        assert resolve_banned_terms(tmp_path / "absent.toml") == ()

    def test_explicit_empty_list_is_a_deliberate_no_op(self, tmp_path: Path) -> None:
        config = _write_config(tmp_path, "[teatree]\nbanned_terms = []\n")
        assert resolve_banned_terms(config) == ()

    def test_present_but_unset_raises_rather_than_silently_empty(self, tmp_path: Path) -> None:
        # The anti-vacuity contract: a config that exists but never declares the
        # key is a load-bug-shaped UNSET, not a deliberate no-terms choice, so it
        # must RAISE — never degrade to an empty ban list that disables the gate.
        config = _write_config(tmp_path, '[teatree]\nprivate_repos = ["acme/widget"]\n')
        with pytest.raises(BannedTermsUnsetError):
            resolve_banned_terms(config)

    def test_unreadable_config_raises_rather_than_silently_empty(self, tmp_path: Path) -> None:
        config = _write_config(tmp_path, "[teatree]\nbanned_terms = [unclosed\n")
        with pytest.raises(BannedTermsUnsetError):
            resolve_banned_terms(config)

    def test_blank_env_value_falls_through_to_toml(self, tmp_path: Path) -> None:
        config = _write_config(tmp_path, '[teatree]\nbanned_terms = ["acme"]\n')
        assert resolve_banned_terms(config, env_value="   ") == ("acme",)
