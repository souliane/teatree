"""Every container venue must be able to reach GitLab over git — with no ssh client.

Two independent gaps made a `compose exec`-run `t3` unable to clone a private GitLab
repo at all, while the same clone from the worker's own loop succeeded:

* the image ships **no ssh client** (deliberately — an ssh remote would need the
    operator's private key inside a container that already holds the docker socket), so a
    hard-coded ``git@gitlab.com:`` remote dies on ``cannot run ssh``; and
* ``GITLAB_TOKEN`` is exported by ``deploy/entrypoint.sh`` into the ROLE's process tree
    only, so the baked https credential helper answers an exec'd process with an empty
    password. The entrypoint's mitigation for that — persisting the login into ``glab``'s
    config — is dead code, because ``glab`` is deliberately absent from the image.

The measured consequence was not a loud failure: a tool four layers down reported its
own generic error, the tenant config was never written, and the stack came up minus its
frontend service. These assert the two halves that close it.
"""

import re
from pathlib import Path

import pytest

DEPLOY_DIR = Path(__file__).resolve().parents[1] / "deploy"
DOCKERFILE = DEPLOY_DIR / "Dockerfile"
WRAPPER = DEPLOY_DIR / "t3"


class TestImageRewritesSshRemotesToHttps:
    """The image gives git a transport it actually has, for both SSH spellings."""

    @pytest.fixture(scope="class")
    def dockerfile(self) -> str:
        return DOCKERFILE.read_text(encoding="utf-8")

    def test_scp_like_remote_is_rewritten(self, dockerfile: str) -> None:
        assert re.search(
            r'git config --system[^\n]*"url\.https://gitlab\.com/\.insteadOf"\s+"git@gitlab\.com:"',
            dockerfile,
        ), "a hard-coded git@gitlab.com: remote must resolve over https — the image has no ssh"

    def test_explicit_ssh_scheme_is_rewritten(self, dockerfile: str) -> None:
        assert 'git config --system --add "url.https://gitlab.com/.insteadOf" "ssh://git@gitlab.com/"' in dockerfile

    def test_no_ssh_client_is_installed(self, dockerfile: str) -> None:
        """The rewrite is the fix; shipping ssh would be the wrong one."""
        assert "openssh-client" not in dockerfile

    def test_the_https_credential_helper_still_backs_the_rewrite(self, dockerfile: str) -> None:
        """A rewrite onto an unauthenticated transport would only change the error."""
        assert "credential.https://gitlab.com.helper" in dockerfile


class TestExecVenueResolvesTheToken:
    """`compose exec` and the one-off `run --rm` both inherit neither export."""

    @pytest.fixture(scope="class")
    def wrapper(self) -> str:
        return WRAPPER.read_text(encoding="utf-8")

    @pytest.fixture(scope="class")
    def prologue(self, wrapper: str) -> str:
        match = re.search(r"CONTAINER_CREDENTIAL_PROLOGUE='(.*?)'\n", wrapper, re.DOTALL)
        assert match, "the credential prologue must exist and be single-quoted"
        return match.group(1)

    def test_prologue_reads_the_token_from_the_box_pass_store(self, prologue: str) -> None:
        assert 'pass show "${TEATREE_GITLAB_TOKEN_PASS_PATH:-gitlab/pat}"' in prologue
        assert "export GITLAB_TOKEN" in prologue

    def test_an_already_set_token_wins(self, prologue: str) -> None:
        """The operator's forwarded value, and the entrypoint's own tree, must not be clobbered."""
        assert 'if [ -z "${GITLAB_TOKEN:-}" ]' in prologue

    def test_the_read_happens_after_gnupg_is_repaired(self, prologue: str) -> None:
        """`pass` cannot decrypt until GNUPGHOME points at a home gpg can bind sockets in."""
        assert prologue.index("export GNUPGHOME") < prologue.index("pass show")

    def test_the_token_never_reaches_the_host_argv(self, wrapper: str) -> None:
        """The read is INSIDE the container; a host-side `pass show` would leak it to `ps`."""
        host_side = wrapper.split("CONTAINER_CREDENTIAL_PROLOGUE=", maxsplit=1)[0]
        assert "pass show" not in host_side

    def test_every_dispatch_path_runs_the_prologue(self, wrapper: str) -> None:
        """The exec path AND the one-off `run --rm` fallback — the fallback had no coverage."""
        # Backslash continuations first: a dispatch split across physical lines would
        # otherwise read as two half-lines and match neither side of the assertion.
        logical = wrapper.replace("\\\n", " ")
        dispatches = [line for line in logical.splitlines() if line.strip().startswith("exec docker compose")]
        assert len(dispatches) >= 3, f"expected the two exec dispatches and the one-off run, got {len(dispatches)}"
        for line in dispatches:
            assert "$CONTAINER_CREDENTIAL_PROLOGUE" in line, f"dispatch bypasses the prologue: {line.strip()}"
