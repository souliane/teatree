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

import os
import re
import subprocess
from pathlib import Path

import pytest

DEPLOY_DIR = Path(__file__).resolve().parents[1] / "deploy"
DOCKERFILE = DEPLOY_DIR / "Dockerfile"
WRAPPER = DEPLOY_DIR / "t3"


class TestImageRewritesSshRemotesToHttps:
    """The image gives git a transport it actually has, for both SSH spellings."""

    @pytest.fixture(scope="class")
    @classmethod
    def dockerfile(cls) -> str:
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


class TestTheHelperAnswersEvenWithoutAToken:
    """An unset token does NOT make the helper stand down — it authenticates as nobody.

    The comment above the ``RUN`` used to claim the helper "emits nothing and git falls
    through exactly as if it were not registered". Measured, an unset ``GITLAB_TOKEN``
    yields the same 26 bytes as an empty one — ``username=oauth2`` with an empty
    ``password=`` — which GitLab answers with ``HTTP Basic: Access denied``. A reader who
    believed the fall-through would look for the missing helper rather than the missing
    token, which is the whole cost of the wrong claim.
    """

    HELPER_RE = re.compile(r"'!(f\(\) \{.*?\}; f)'", re.DOTALL)

    @pytest.fixture(scope="class")
    @classmethod
    def helper_body(cls) -> str:
        match = cls.HELPER_RE.search(DOCKERFILE.read_text(encoding="utf-8"))
        assert match, "the baked credential helper must stay a git shell-helper (`!f() { ... }; f`)"
        return match.group(1)

    def _answer(self, helper_body: str, env: dict[str, str]) -> str:
        return subprocess.run(
            ["sh", "-c", f"{helper_body} get"],  # noqa: S607 — `sh` from PATH is the helper's own interpreter
            capture_output=True,
            text=True,
            check=True,
            env=env,
            timeout=30,
        ).stdout

    def test_an_unset_token_still_answers_with_an_empty_password(self, helper_body: str) -> None:
        answer = self._answer(helper_body, env={"PATH": os.environ.get("PATH", "")})

        assert answer == "username=oauth2\npassword=\n", repr(answer)

    def test_the_unset_answer_is_byte_identical_to_the_empty_one(self, helper_body: str) -> None:
        """So no caller downstream can tell "no credential" from "a blank credential"."""
        base = {"PATH": os.environ.get("PATH", "")}

        assert self._answer(helper_body, env=base) == self._answer(helper_body, env={**base, "GITLAB_TOKEN": ""})

    def test_a_present_token_is_what_actually_changes_the_answer(self, helper_body: str) -> None:
        """The control: the helper is not simply inert."""
        base = {"PATH": os.environ.get("PATH", "")}
        answer = self._answer(helper_body, env={**base, "GITLAB_TOKEN": "a-value"})

        assert answer == "username=oauth2\npassword=a-value\n", repr(answer)

    def test_the_comment_does_not_promise_a_fall_through(self) -> None:
        """The prose is what a reader debugging `Access denied` consults first."""
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")

        assert "emits nothing" not in dockerfile, "the helper emits 26 bytes with no token — it never stands down"
        assert "falls through" not in dockerfile


class TestExecVenueResolvesTheToken:
    """`compose exec` and the one-off `run --rm` both inherit neither export."""

    @pytest.fixture(scope="class")
    @classmethod
    def wrapper(cls) -> str:
        return WRAPPER.read_text(encoding="utf-8")

    @pytest.fixture(scope="class")
    @classmethod
    def prologue(cls, wrapper: str) -> str:
        match = re.search(r"CONTAINER_CREDENTIAL_PROLOGUE='(.*?)'\n", wrapper, re.DOTALL)
        assert match, "the credential prologue must exist and be single-quoted"
        return match.group(1)

    def test_prologue_reads_the_token_from_the_box_pass_store(self, prologue: str) -> None:
        # Bound to a variable rather than spelled inline at the read: the wedge
        # diagnostic below it names the same path, and two literals would drift.
        assert 'pass_path="${TEATREE_GITLAB_TOKEN_PASS_PATH:-}"' in prologue
        assert 'pass show "$pass_path"' in prologue
        assert "export GITLAB_TOKEN" in prologue

    def test_an_already_set_token_wins(self, prologue: str) -> None:
        """The operator's forwarded value, and the entrypoint's own tree, must not be clobbered."""
        assert 'if [ -z "${GITLAB_TOKEN:-}" ]' in prologue

    def test_the_read_is_bounded(self, prologue: str) -> None:
        """`pass show` execs gpg, which blocks with no deadline of its own on a keybox lock.

        This replaces an assertion that the read came AFTER a GNUPGHOME repair. That repair
        is retired: the image bakes one container-local ``GNUPGHOME`` and the entrypoint
        seeds it, so no venue has one to perform (
        ``tests/test_deploy_gnupg_lock_isolation.py``). What still has to hold here is that
        the read TERMINATES — an unbounded one left 322 orphaned ``pass``/``gpg`` pairs.
        """
        assert 'deadline="${TEATREE_SECRET_READ_DEADLINE_SECONDS:-20}"' in prologue
        assert 'timeout "$deadline"' in prologue
        assert prologue.index("timeout ") < prologue.index("pass show")

    def test_a_wedged_store_refuses_rather_than_exporting_an_empty_token(self, prologue: str) -> None:
        """An EMPTY token authenticates as nobody: GitLab reports it as a missing branch.

        The refusal is the stronger form of leaving it unset — an unset token still reaches
        the command, which then fails four layers down wearing someone else's error.
        """
        wedge = prologue[prologue.index('"$rc" -eq 124') :]

        assert "exit 1" in wedge, "a store that did not ANSWER must stop here, not export an empty value"
        assert wedge.index("exit 1") < wedge.index("GITLAB_TOKEN=")

    def test_an_absent_store_is_not_treated_as_a_wedged_one(self, prologue: str) -> None:
        """CI and a laptop that never set `pass` up are not outages — they must not be refused."""
        assert "command -v pass" in prologue
        assert '"$rc" -eq 127' in prologue

    def test_the_token_never_reaches_the_host_argv(self, wrapper: str) -> None:
        """The read is INSIDE the container; a host-side `pass show` would leak it to `ps`.

        Comment lines are stripped first: the prose above the prologue names ``pass show``
        while explaining why the read is bounded, and a prose mention is the opposite of a
        host-side invocation.
        """
        host_side = wrapper.split("CONTAINER_CREDENTIAL_PROLOGUE=", maxsplit=1)[0]
        code = [line for line in host_side.splitlines() if not line.lstrip().startswith("#")]
        assert "pass show" not in "\n".join(code)

    def test_every_dispatch_path_runs_the_prologue(self, wrapper: str) -> None:
        """The exec path AND the one-off `run --rm` fallback — the fallback had no coverage."""
        # Backslash continuations first: a dispatch split across physical lines would
        # otherwise read as two half-lines and match neither side of the assertion.
        logical = wrapper.replace("\\\n", " ")
        dispatches = [line for line in logical.splitlines() if line.strip().startswith("exec docker compose")]
        assert len(dispatches) >= 3, f"expected the two exec dispatches and the one-off run, got {len(dispatches)}"
        for line in dispatches:
            assert "$CONTAINER_CREDENTIAL_PROLOGUE" in line, f"dispatch bypasses the prologue: {line.strip()}"
