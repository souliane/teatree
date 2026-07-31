"""``git_env_without_overrides`` keeps non-``GIT_*`` env — incl. TMPDIR.

The headless ``claude`` child env (``teatree.agents._headless_env``) is built on
this base when a Layer-2 credential provider is pinned. Routing runtime temp to
disk relies on the spawned child inheriting the ``TMPDIR`` the entrypoint exports,
so this locks the contract that the base strips ONLY ``GIT_*`` overrides and never
drops ``TMPDIR`` (or any other non-git var).
"""

import pytest

from teatree.utils.git_run import git_env_non_interactive, git_env_without_overrides


class TestGitEnvNonInteractive:
    """The credential-prompt kill switch every remote-touching git call runs under (#3927)."""

    def test_disables_every_interactive_credential_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GIT_ASKPASS", "/usr/bin/some-gui-askpass")
        env = git_env_non_interactive()
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_ASKPASS"] == ""
        assert env["SSH_ASKPASS"] == ""
        assert env["GCM_INTERACTIVE"] == "never"

    def test_still_strips_git_overrides_and_keeps_the_rest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GIT_DIR", "/somewhere/.git")
        monkeypatch.setenv("TMPDIR", "/var/tmp")
        env = git_env_non_interactive()
        assert "GIT_DIR" not in env
        assert env["TMPDIR"] == "/var/tmp"


class TestGitEnvWithoutOverrides:
    def test_preserves_tmpdir_and_strips_git_vars(self, monkeypatch) -> None:
        monkeypatch.setenv("TMPDIR", "/var/tmp")
        monkeypatch.setenv("PYTEST_DEBUG_TEMPROOT", "/var/tmp")
        monkeypatch.setenv("GIT_DIR", "/somewhere/.git")
        env = git_env_without_overrides()
        # The disk-temp routing survives into the child env.
        assert env["TMPDIR"] == "/var/tmp"
        assert env["PYTEST_DEBUG_TEMPROOT"] == "/var/tmp"
        # GIT_* overrides are the only thing stripped.
        assert "GIT_DIR" not in env
