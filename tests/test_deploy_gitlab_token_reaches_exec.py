"""The GitLab token must reach a `docker exec`, not just the role's process tree.

`deploy/entrypoint.sh` reads the token from ``pass`` and ``export``s it — but an
export reaches only the process tree of the role it ran for. ``docker exec`` starts
from the CONTAINER's create-time environment, so an exec'd process saw an unset
``GITLAB_TOKEN`` while the role process had it the whole time. The baked credential
helper then interpolated the empty value and authenticated with an EMPTY password,
which GitLab reports as ``HTTP Basic: Access denied`` — indistinguishable, from the
outside, from a branch that does not exist.

Two halves close it, and this module pins both. The compose files DECLARE
``GITLAB_TOKEN`` per service, which is what an exec inherits; and the host-side
entry points resolve it only from the SAME explicitly configured bootstrap
``pass`` key the entrypoint uses, so no legacy default can bypass a DB route.

The compose half is asserted against the parsed YAML rather than a `docker compose
config` render: the subject is the declaration, and a render would make the check
require a working daemon. The wrapper half runs the genuine ``deploy/t3`` against a
``pass`` stub, the same way `test_deploy_host_glab_token_forwarding.py` runs it
against a ``glab`` stub.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml
from _deploy_forwarded_env import CREDENTIAL_PROBE, ENV_REPORT, argv, forwarded

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
COMPOSE = DEPLOY / "docker-compose.yml"
WRAPPER = DEPLOY / "t3"
DEPLOY_SH = DEPLOY / "deploy.sh"
ENTRYPOINT = DEPLOY / "entrypoint.sh"

# An explicit bootstrap route for the deployment boundary. There is deliberately no
# implicit default: runtime credential routes live in the DB.
DEFAULT_PASS_KEY = "gitlab/pat"
PASS_KEY_OVERRIDE = "TEATREE_GITLAB_TOKEN_PASS_PATH"

# Every service an operator or the CLI wrapper `docker exec`s into. The watchdog is
# excluded here and asserted separately: it never runs teatree, it CARRIES the value
# so its own `compose up` repair can interpolate it.
EXEC_TARGET_SERVICES = ["teatree-init", "teatree-worker", "teatree-admin", "teatree-slack-listener"]

SYSTEM_PATH = os.defpath.strip(os.pathsep)
FAKE_TOKEN = "stub-pass-store-000000000000"

pytestmark = pytest.mark.skipif(
    shutil.which("bash", path=SYSTEM_PATH) is None,
    reason="needs a system bash (present on macOS, in the deploy image, and in CI)",
)

DOCKER_STUB = (
    """#!/usr/bin/env bash
for arg in "$@"; do
    [ "$arg" = ps ] && exit 0
done
printf 'ARG %s\\n' "$@"
"""
    + ENV_REPORT
)

# Models `pass show <key>`, recording every key it was asked for so a test can prove
# WHICH key the wrapper read rather than only that it read something.
PASS_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$2" >>"$PASS_STUB_CALLS"
if [ "$2" = "$PASS_STUB_KEY" ]; then
    printf '%s\\n' "$PASS_STUB_TOKEN"
    exit 0
fi
echo "Error: $2 is not in the password store." >&2
exit 1
"""

# A store that is PRESENT and BROKEN: gpg's own failure rc, the shape a locked
# `pubring.db.lock` or an orphan keyboxd under a split GNUPGHOME produces.
WEDGED_PASS_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$2" >>"$PASS_STUB_CALLS"
echo "gpg: decryption failed: No Keybox daemon running" >&2
exit 2
"""

# The other wedge: a store that never answers at all. `exec` so the deadline's TERM
# reaches the sleeper itself — an orphan child would hold the read's stdout pipe open
# and the caller would wait out the full sleep the bound exists to cut short.
UNANSWERING_PASS_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$2" >>"$PASS_STUB_CALLS"
exec sleep 30
"""

# An authenticated host `glab`, so tests prove it cannot replace an explicit pass
# route or paper over a wedge.
GLAB_STUB = """#!/usr/bin/env bash
echo "  ✓ Token found: stub-glab-fallback-0000" >&2
exit 0
"""


@pytest.fixture(scope="module")
def compose_doc() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _service_env(compose_doc: dict, service: str) -> dict[str, str]:
    return compose_doc["services"][service].get("environment") or {}


def _write_stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _invoke(
    tmp_path: Path,
    *,
    pass_stub: str | None = PASS_STUB,
    with_glab: bool = False,
    env_overrides: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run the genuine wrapper. ``pass_stub=None`` is a host with NO store at all."""
    stub_dir = tmp_path / "stub-bin"
    _write_stub(stub_dir / "docker", DOCKER_STUB)
    if pass_stub is not None:
        _write_stub(stub_dir / "pass", pass_stub)
    if with_glab:
        _write_stub(stub_dir / "glab", GLAB_STUB)

    env = {k: v for k, v in os.environ.items() if not k.startswith(("GITLAB_", "GITHUB_", "T3_", "TEATREE_"))}
    env["PATH"] = f"{stub_dir}{os.pathsep}{SYSTEM_PATH}"
    env["TEATREE_HOST_HOME"] = str(tmp_path / "home")
    env["PASS_STUB_CALLS"] = str(tmp_path / "pass-calls.log")
    # Which key the stub STORES; `PASS_KEY_OVERRIDE` is which key the wrapper ASKS for,
    # and the tests below turn them against each other.
    env["PASS_STUB_KEY"] = DEFAULT_PASS_KEY
    env["PASS_STUB_TOKEN"] = FAKE_TOKEN
    env[PASS_KEY_OVERRIDE] = DEFAULT_PASS_KEY
    env.update(env_overrides or {})

    entry = tmp_path / "teatree-deploy" / "deploy" / "t3"
    entry.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(WRAPPER, entry)
    entry.chmod(entry.stat().st_mode | stat.S_IXUSR)

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(parents=True, exist_ok=True)
    bash = shutil.which("bash", path=SYSTEM_PATH) or "bash"
    return subprocess.run(
        [bash, str(entry), *CREDENTIAL_PROBE],
        capture_output=True,
        text=True,
        check=check,
        env=env,
        cwd=elsewhere,
        timeout=120,
    )


def _pass_keys(tmp_path: Path) -> list[str]:
    log = tmp_path / "pass-calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


class TestEveryExecTargetDeclaresTheToken:
    """The declaration is the whole fix: an export cannot cross into a `docker exec`."""

    @pytest.mark.parametrize("service", EXEC_TARGET_SERVICES)
    def test_service_declares_gitlab_token(self, compose_doc: dict, service: str) -> None:
        assert "GITLAB_TOKEN" in _service_env(compose_doc, service)

    @pytest.mark.parametrize("service", EXEC_TARGET_SERVICES)
    def test_service_interpolates_and_defaults_empty(self, compose_doc: dict, service: str) -> None:
        # `:-` and not `?err`: an operator with no token on the host must still be
        # able to bring the stack up; an explicit bootstrap route may still be read.
        assert _service_env(compose_doc, service)["GITLAB_TOKEN"] == "${GITLAB_TOKEN:-}"

    def test_the_watchdog_carries_it_for_its_own_repair(self, compose_doc: dict) -> None:
        # Its repair is an inner `compose up`, which interpolates from ITS environment;
        # the host pass store is not mounted there, so it cannot re-derive the value.
        assert _service_env(compose_doc, "teatree-watchdog")["GITLAB_TOKEN"] == "${GITLAB_TOKEN:-}"

    def test_no_literal_token_is_committed(self, compose_doc: dict) -> None:
        declared = {
            service: env["GITLAB_TOKEN"]
            for service, spec in compose_doc["services"].items()
            if "GITLAB_TOKEN" in (env := spec.get("environment") or {})
        }
        assert declared, "no service declares GITLAB_TOKEN — the exec boundary is open again"
        assert all(value.startswith("${") for value in declared.values())


class TestOneCredentialNamedInOnePlace:
    """Deployment reads pass only through an explicit bootstrap route."""

    @pytest.mark.parametrize("script", [ENTRYPOINT, DEPLOY_SH, WRAPPER])
    def test_script_has_no_hidden_legacy_default(self, script: Path) -> None:
        body = script.read_text(encoding="utf-8")
        assert f"${{{PASS_KEY_OVERRIDE}:-{DEFAULT_PASS_KEY}}}" not in body
        assert f"${{{PASS_KEY_OVERRIDE}:-}}" in body


class TestTheWrapperResolvesTheTokenFromPass:
    def test_pass_token_is_forwarded(self, tmp_path: Path) -> None:
        assert forwarded(_invoke(tmp_path))["GITLAB_TOKEN"] == FAKE_TOKEN

    def test_the_explicit_bootstrap_key_is_the_one_read(self, tmp_path: Path) -> None:
        _invoke(tmp_path)
        assert _pass_keys(tmp_path) == [DEFAULT_PASS_KEY]

    def test_a_populated_legacy_entry_is_ignored_without_an_explicit_route(self, tmp_path: Path) -> None:
        proc = _invoke(tmp_path, env_overrides={PASS_KEY_OVERRIDE: ""})

        assert "GITLAB_TOKEN" not in forwarded(proc)
        assert _pass_keys(tmp_path) == []

    def test_the_key_is_overridable(self, tmp_path: Path) -> None:
        other = "gitlab/some-other-pat"
        proc = _invoke(tmp_path, env_overrides={PASS_KEY_OVERRIDE: other, "PASS_STUB_KEY": other})
        assert forwarded(proc)["GITLAB_TOKEN"] == FAKE_TOKEN
        assert _pass_keys(tmp_path) == [other]

    def test_explicit_pass_route_is_unaffected_by_an_ambient_glab_login(self, tmp_path: Path) -> None:
        assert forwarded(_invoke(tmp_path, with_glab=True))["GITLAB_TOKEN"] == FAKE_TOKEN

    def test_an_exported_token_still_wins_over_pass(self, tmp_path: Path) -> None:
        exported = "stub-operators-own-choice"
        proc = _invoke(tmp_path, env_overrides={"GITLAB_TOKEN": exported})
        assert forwarded(proc)["GITLAB_TOKEN"] == exported
        assert _pass_keys(tmp_path) == []

    def test_absent_pass_forwards_nothing(self, tmp_path: Path) -> None:
        # A host without `pass` must fall through silently, never forward an empty
        # value — an empty GITLAB_TOKEN SHADOWS the container's own environment and
        # is exactly what made the helper authenticate with a blank password.
        assert "GITLAB_TOKEN" not in forwarded(_invoke(tmp_path, pass_stub=None))

    def test_unknown_key_forwards_nothing(self, tmp_path: Path) -> None:
        assert "GITLAB_TOKEN" not in forwarded(
            _invoke(tmp_path, env_overrides={"PASS_STUB_KEY": "gitlab/not-this-one"})
        )


class TestAnAbsentStoreIsNotAWedgedOne:
    """Three states, and collapsing any two of them breaks the CLI or hides a wedge.

    NO STORE (no `pass` binary — CI, and the laptop that never set one up) and NO ENTRY
    must both continue without a host token; a store that is PRESENT and
    fails to answer must STOP the CLI by name. Collapsing "broken" into "absent" is the
    original defect — an empty credential surfacing later as `HTTP Basic: Access
    denied`. Collapsing "absent" into "broken" is what a 127 read as `> 1` did: every
    storeless venue refused. Both directions are pinned here, together, because a fix
    for one is exactly what re-creates the other.
    """

    @staticmethod
    def _assert_ran_to_dispatch(proc: subprocess.CompletedProcess[str]) -> None:
        # Anti-vacuity: "no token forwarded" is also what a wrapper that died before
        # docker reports, and that is the failure these tests exist to tell apart.
        assert proc.returncode == 0, proc.stderr
        assert argv(proc), "the wrapper never reached its docker dispatch"

    @staticmethod
    def _assert_refused_loudly(proc: subprocess.CompletedProcess[str]) -> None:
        assert proc.returncode != 0, "a wedged store let the CLI run on an empty credential"
        assert "WEDGED" in proc.stderr, proc.stderr
        assert DEFAULT_PASS_KEY in proc.stderr, proc.stderr
        assert not argv(proc), "the wrapper dispatched anyway after refusing"

    def test_no_store_at_all_lets_the_cli_run(self, tmp_path: Path) -> None:
        proc = _invoke(tmp_path, pass_stub=None, check=False)
        self._assert_ran_to_dispatch(proc)
        assert "GITLAB_TOKEN" not in forwarded(proc)

    def test_a_missing_entry_lets_the_cli_run(self, tmp_path: Path) -> None:
        proc = _invoke(tmp_path, env_overrides={"PASS_STUB_KEY": "gitlab/not-this-one"}, check=False)
        self._assert_ran_to_dispatch(proc)
        assert "GITLAB_TOKEN" not in forwarded(proc)

    def test_a_present_but_broken_store_stops_the_cli(self, tmp_path: Path) -> None:
        proc = _invoke(tmp_path, pass_stub=WEDGED_PASS_STUB, check=False)
        self._assert_refused_loudly(proc)
        assert _pass_keys(tmp_path) == [DEFAULT_PASS_KEY]

    def test_a_store_that_never_answers_stops_the_cli(self, tmp_path: Path) -> None:
        proc = _invoke(
            tmp_path,
            pass_stub=UNANSWERING_PASS_STUB,
            env_overrides={"TEATREE_SECRET_READ_DEADLINE_SECONDS": "1"},
            check=False,
        )
        self._assert_refused_loudly(proc)
        assert _pass_keys(tmp_path) == [DEFAULT_PASS_KEY]

    def test_a_wedged_store_is_never_papered_over_by_an_ambient_glab_login(self, tmp_path: Path) -> None:
        self._assert_refused_loudly(_invoke(tmp_path, pass_stub=WEDGED_PASS_STUB, with_glab=True, check=False))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
