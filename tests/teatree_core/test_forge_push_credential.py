"""Unit tests for the forge-write credential seam (souliane/teatree#3927).

Split out of ``test_forge_push`` alongside the module it mirrors. Fixture secrets
are assembled at runtime so this file carries no literal token.
"""

import os
from unittest.mock import patch

from teatree.core.forge_push_credential import (
    CredentialSource,
    ForgeCredential,
    credential_failure_hint,
    remote_url_embeds_credential,
    resolve_forge_credential,
    scrub_token,
)

FAKE_TOKEN = "gh" + "p_" + "x" * 36


class TestResolveForgeCredential:
    def test_prefers_gh_token_over_teatree_gh_token(self) -> None:
        with patch.dict(os.environ, {"GH_TOKEN": FAKE_TOKEN, "TEATREE_GH_TOKEN": "other"}, clear=False):
            credential = resolve_forge_credential()
        assert credential.token == FAKE_TOKEN
        assert credential.source is CredentialSource.GH_TOKEN

    def test_falls_back_to_teatree_gh_token(self) -> None:
        env = {"TEATREE_GH_TOKEN": FAKE_TOKEN}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("GH_TOKEN", None)
            credential = resolve_forge_credential()
        assert credential.token == FAKE_TOKEN
        assert credential.source is CredentialSource.TEATREE_GH_TOKEN

    def test_falls_back_to_overlay_pass_store(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=False),
            patch("teatree.core.forge_push_credential._overlay_github_token", return_value=FAKE_TOKEN),
        ):
            os.environ.pop("GH_TOKEN", None)
            os.environ.pop("TEATREE_GH_TOKEN", None)
            credential = resolve_forge_credential()
        assert credential.token == FAKE_TOKEN
        assert credential.source is CredentialSource.OVERLAY_PASS_STORE

    def test_no_credential_falls_through_to_ambient_helper(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=False),
            patch("teatree.core.forge_push_credential._overlay_github_token", return_value=""),
        ):
            os.environ.pop("GH_TOKEN", None)
            os.environ.pop("TEATREE_GH_TOKEN", None)
            credential = resolve_forge_credential()
        assert credential.token == ""
        assert credential.source is CredentialSource.AMBIENT


class TestRemoteUrlEmbedsCredential:
    def test_flags_a_token_in_the_userinfo(self) -> None:
        assert remote_url_embeds_credential(f"https://{FAKE_TOKEN}@github.com/acme/app.git")

    def test_flags_a_password_component(self) -> None:
        url = "https://user:hunter2@github.com/acme/app.git"  # privacy-scan:allow (fake test credential, not PII)
        assert remote_url_embeds_credential(url)

    def test_leaves_a_plain_https_remote_alone(self) -> None:
        assert not remote_url_embeds_credential("https://github.com/acme/app.git")

    def test_leaves_an_scp_style_ssh_remote_alone(self) -> None:
        assert not remote_url_embeds_credential("git@github.com:acme/app.git")

    def test_a_malformed_url_is_not_flagged_and_does_not_raise(self) -> None:
        url = "https://[oops@github.com/acme/app.git"  # privacy-scan:allow (malformed fixture URL, not PII)
        assert not remote_url_embeds_credential(url)


class TestCredentialFailureHint:
    def test_names_the_token_sources_when_none_resolved(self) -> None:
        credential = ForgeCredential(token="", source=CredentialSource.AMBIENT)
        hint = credential_failure_hint("fatal: could not read Username for 'https://github.com'", credential)
        assert "TEATREE_GH_TOKEN" in hint
        assert "pass store" in hint

    def test_names_the_helper_wiring_when_a_token_was_supplied(self) -> None:
        credential = ForgeCredential(token=FAKE_TOKEN, source=CredentialSource.TEATREE_GH_TOKEN)
        hint = credential_failure_hint("fatal: Authentication failed for 'https://github.com'", credential)
        assert "gh auth setup-git" in hint
        assert FAKE_TOKEN not in hint

    def test_is_silent_for_a_failure_that_is_not_about_credentials(self) -> None:
        credential = ForgeCredential(token=FAKE_TOKEN, source=CredentialSource.GH_TOKEN)
        assert credential_failure_hint("! [rejected] feature -> feature (non-fast-forward)", credential) == ""


class TestScrubToken:
    def test_replaces_every_occurrence(self) -> None:
        assert scrub_token(f"a {FAKE_TOKEN} b {FAKE_TOKEN}", FAKE_TOKEN) == "a <redacted> b <redacted>"

    def test_empty_token_is_a_no_op(self) -> None:
        assert scrub_token("nothing to hide", "") == "nothing to hide"
