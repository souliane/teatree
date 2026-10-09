# test-path: cross-cutting — drives deploy/entrypoint.sh + docker-compose.yml (no src mirror).
"""The clone worktrees are cut from must resolve at ONE path in every venue.

``git worktree add`` bakes an ABSOLUTE ``gitdir:`` pointer into its source clone,
so the clone's path — not the worktree's — decides who can use the result. The
container cut worktrees from the ``teatree_src`` volume at ``$TEATREE_CLONE_DIR``,
a path that exists in no other venue, so every worktree the containerised
``workspace ticket`` handed back answered ``fatal: not a git repository`` from the
host where an agent's file tools run (#4120). The worktree tree itself was already
bind-mounted at path identity, which is why the files looked fine and only git failed.

The deploy checkout is ALREADY bind-mounted at path identity for the watchdog
(source == target, one variable read by both sides). These tests pin the fix: that
mount joins the shared set read-write, and the entrypoint points the org-prefixed
discovery link at it, so the pointer git records names a directory both venues resolve.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash (present in the deploy image and CI)")

_DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
_COMPOSE = _DEPLOY / "docker-compose.yml"
_ENTRYPOINT = _DEPLOY / "entrypoint.sh"

_BASH = shutil.which("bash") or "/bin/bash"
_GIT = shutil.which("git") or "/usr/bin/git"

#: Both mount sides and the forwarded env read this ONE placeholder, so path
#: identity holds by construction rather than only at the box's default path.
_CHECKOUT_PLACEHOLDER = (
    "${TEATREE_DEPLOY_CHECKOUT:-/home/teatree/teatree-deploy}"  # privacy-scan:allow — public deploy home
)

#: The roles that provision worktrees (worker) or prepare the shared state it reads
#: (init). Both need the checkout's path in their environment for the retarget.
_RETARGETING_SERVICES = ("teatree-init", "teatree-worker")

_DISCOVERY_LINK = (
    "/home/teatree/workspace/souliane/teatree"  # privacy-scan:allow — the box's public, documented deploy home
)


def _compose() -> dict:
    # SafeLoader resolves `&teatree-common` and the `<<` merge keys, so each
    # service's list is what that role actually runs with.
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))


def _volumes(service: str) -> list:
    return _compose()["services"][service]["volumes"]


def _extract_shell_function(name: str) -> str:
    """Return the verbatim source of shell function *name* from the entrypoint."""
    body: list[str] = []
    capturing = False
    for line in _ENTRYPOINT.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}() {{"):
            capturing = True
        if capturing:
            body.append(line)
            if line == "}":
                return "\n".join(body)
    not_found = f"function {name!r} not found in {_ENTRYPOINT}"
    raise AssertionError(not_found)


def _run_retarget(tmp_path: Path, link: Path, checkout: str) -> subprocess.CompletedProcess[str]:
    """Run the REAL ``retarget_clone_discovery`` against *link* in a bash subprocess."""
    harness = tmp_path / "harness.sh"
    harness.write_text(
        f'set -euo pipefail\n{_extract_shell_function("retarget_clone_discovery")}\nretarget_clone_discovery "$1"\n',
        encoding="utf-8",
    )
    env = {**os.environ, "TEATREE_DEPLOY_CHECKOUT": checkout}
    return subprocess.run([_BASH, str(harness), str(link)], capture_output=True, text=True, check=True, env=env)


def _make_clone(path: Path) -> Path:
    path.mkdir(parents=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    subprocess.run([_GIT, "init", "-b", "main", str(path)], check=True, capture_output=True, env=env)
    (path / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run([_GIT, "-C", str(path), "add", "-A"], check=True, capture_output=True, env=env)
    subprocess.run([_GIT, "-C", str(path), "commit", "-m", "init"], check=True, capture_output=True, env=env)
    return path


class TestSharedCheckoutMount:
    """The deploy checkout is mounted at path identity for the roles that provision."""

    def test_app_services_mount_the_checkout_at_path_identity(self) -> None:
        for service in _RETARGETING_SERVICES:
            binds = [v for v in _volumes(service) if isinstance(v, dict) and v.get("type") == "bind"]
            identity = [v for v in binds if v["source"] == _CHECKOUT_PLACEHOLDER]
            assert identity, f"{service} must bind the deploy checkout so the recorded gitdir resolves off-box"
            assert identity[0]["target"] == _CHECKOUT_PLACEHOLDER, (
                f"{service}'s checkout mount must be at PATH IDENTITY (source == target) — "
                "a different target is exactly the #4120 split"
            )

    def test_the_provisioning_mount_is_writable(self) -> None:
        # `git worktree add` writes `.git/worktrees/<n>` into the clone, so the
        # watchdog's read-only spelling of this same mount cannot serve provisioning.
        binds = [v for v in _volumes("teatree-worker") if isinstance(v, dict) and v.get("type") == "bind"]
        checkout = next(v for v in binds if v["source"] == _CHECKOUT_PLACEHOLDER)
        assert not checkout.get("read_only", False)

    def test_the_watchdogs_own_checkout_mount_stays_read_only(self) -> None:
        # Behaviour preservation: the supervisor only READS deploy/watchdog.sh.
        binds = [v for v in _volumes("teatree-watchdog") if isinstance(v, dict) and v.get("type") == "bind"]
        checkout = next(v for v in binds if v["source"] == _CHECKOUT_PLACEHOLDER)
        assert checkout.get("read_only") is True

    def test_retargeting_services_receive_the_checkout_path(self) -> None:
        services = _compose()["services"]
        for service in _RETARGETING_SERVICES:
            assert services[service]["environment"]["TEATREE_DEPLOY_CHECKOUT"] == _CHECKOUT_PLACEHOLDER


class TestRetargetCloneDiscovery:
    def test_points_the_discovery_link_at_the_mounted_checkout(self, tmp_path: Path) -> None:
        clone = _make_clone(tmp_path / "teatree-deploy")
        link = tmp_path / "workspace" / "souliane" / "teatree"

        _run_retarget(tmp_path, link, str(clone))

        assert link.is_symlink()
        assert link.resolve() == clone.resolve()

    def test_replaces_the_images_container_only_link(self, tmp_path: Path) -> None:
        # The image bakes the link at the `teatree_src` volume. That
        # link is the #4120 defect, so the retarget must overwrite it, not skip it.
        clone = _make_clone(tmp_path / "teatree-deploy")
        container_only = _make_clone(tmp_path / "container-src")
        link = tmp_path / "workspace" / "souliane" / "teatree"
        link.parent.mkdir(parents=True)
        link.symlink_to(container_only)

        _run_retarget(tmp_path, link, str(clone))

        assert link.resolve() == clone.resolve()

    def test_leaves_the_link_alone_when_no_checkout_is_mounted(self, tmp_path: Path) -> None:
        # A bare `docker compose up` with nothing exported: dockerd creates an empty
        # host dir at the default path. Degrading to the image's link keeps the
        # container working exactly as it did before, rather than breaking discovery.
        empty = tmp_path / "not-a-checkout"
        empty.mkdir()
        container_only = _make_clone(tmp_path / "container-src")
        link = tmp_path / "workspace" / "souliane" / "teatree"
        link.parent.mkdir(parents=True)
        link.symlink_to(container_only)

        result = _run_retarget(tmp_path, link, str(empty))

        assert link.resolve() == container_only.resolve()
        assert "4120" in result.stderr

    def test_unset_checkout_leaves_the_link_alone(self, tmp_path: Path) -> None:
        container_only = _make_clone(tmp_path / "container-src")
        link = tmp_path / "workspace" / "souliane" / "teatree"
        link.parent.mkdir(parents=True)
        link.symlink_to(container_only)

        _run_retarget(tmp_path, link, "")

        assert link.resolve() == container_only.resolve()

    def test_never_replaces_a_real_clone_at_the_link_path(self, tmp_path: Path) -> None:
        # An operator (or `ensure_clone`) may have put a REAL clone there. Replacing
        # a directory with a symlink would strand whatever it holds.
        clone = _make_clone(tmp_path / "teatree-deploy")
        real = _make_clone(tmp_path / "workspace" / "souliane" / "teatree")

        result = _run_retarget(tmp_path, real, str(clone))

        assert not real.is_symlink()
        assert (real / ".git").is_dir()
        assert "4120" in result.stderr

    def test_is_idempotent(self, tmp_path: Path) -> None:
        clone = _make_clone(tmp_path / "teatree-deploy")
        link = tmp_path / "workspace" / "souliane" / "teatree"

        _run_retarget(tmp_path, link, str(clone))
        _run_retarget(tmp_path, link, str(clone))

        assert link.resolve() == clone.resolve()

    def test_the_entrypoint_actually_calls_it(self) -> None:
        # A defined-but-uncalled function is the failure this whole file exists to
        # prevent, and every assertion above passes without the call site.
        text = _ENTRYPOINT.read_text(encoding="utf-8")
        assert "\nretarget_clone_discovery\n" in text

    def test_the_production_default_is_the_org_prefixed_discovery_link(self) -> None:
        # `find_clone_path(clone_root, "souliane/teatree")` matches the slug's
        # LITERAL path; a bare `workspace/teatree` link is invisible to it.
        assert _DISCOVERY_LINK in _extract_shell_function("retarget_clone_discovery")


class TestRecordedGitdirNamesTheCloneItResolvesTo:
    """The mechanism the fix rests on, asserted rather than assumed."""

    def test_worktree_records_the_symlink_target_not_the_link(self, tmp_path: Path) -> None:
        # git resolves the clone path before recording it, so the SYMLINK TARGET is
        # what lands in the worktree's `.git` — which is why the target, not the
        # link, is the path that has to exist in both venues.
        clone = _make_clone(tmp_path / "real-clone")
        link = tmp_path / "souliane" / "teatree"
        link.parent.mkdir(parents=True)
        link.symlink_to(clone)
        worktree = tmp_path / "wt"

        subprocess.run(
            [_GIT, "-C", str(link), "worktree", "add", "-b", "topic", str(worktree)],
            check=True,
            capture_output=True,
        )

        recorded = (worktree / ".git").read_text(encoding="utf-8").strip()
        assert recorded.startswith("gitdir: ")
        assert recorded.removeprefix("gitdir: ").startswith(str(clone.resolve()))
        assert not recorded.removeprefix("gitdir: ").startswith(str(link))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
