"""One interpreter root, at one absolute path, visible to both venues (#4642).

A ``pyvenv.cfg`` records the ABSOLUTE path of the interpreter its environment was
built against. The worktree tree is bind-mounted path-identically, so host and
container read the SAME ``.venv`` — and while the two venues installed their
interpreters into different roots, each judged the other's environment invalid
and DELETED it: ``Removed virtual environment at: .venv``, ~1 GB per venue flip,
indefinitely. 37 such rebuilds took the box to 100% full.

The fix is a single shared root for the PROJECT plane: uv's own default under the
HOST home, bound at path identity — exactly the rule the worktree tree it must
stay consistent with already follows. It is set at RUNTIME by compose, not by the
image.

The address has to be the HOST's, not the container's. A first cut rebased only
the bind SOURCE and left the container's coordinate hardcoded at ``/home/teatree``,
which is an identity mount ONLY on a box whose deploy home happens to be that. On
any other host the container then ran ``uv python install`` against a directory it
called ``/home/teatree/...`` while the host called it something else, and uv — which
writes its ``cpython-<minor>`` aliases as ABSOLUTE symlinks, with no relative mode —
rewrote the HOST's alias set to point inside the container. Every alias dangled, and
every venv and hook env resolving through one broke. Worse on a Linux host, where
the platform tag matches and the container overwrites the host's real interpreter.
:class:`TestRenderedRootIsIdenticalInBothVenues` renders a non-matching host home
and pins source, target and env to one string.

Where it is set is load-bearing, not a detail. The image's own ``uv python
install`` runs after the image ``ENV``, so an image-level project root would make
the baked ``teatree`` and ``prek`` tool venvs record a host-controlled directory
that the bind then shadows — coupling the container's own toolchain to whatever
the host keeps there. So the TOOL plane keeps its own root on ``teatree_uv``,
owned by the image ``ENV`` and pinned explicitly on the entrypoint's two SHELL
tool installs. :class:`TestToolPlaneStaysOnTheNamedVolume` asserts exactly those
two things and nothing wider — the PYTHON-side installs inherit the ambient root
and are not covered here. It is a second half of the fix, not a bystander guard.
"""

import re
from pathlib import Path

import pytest
import yaml
from test_deploy_bindmount_compose import _render, _rendered_mounts

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = REPO_ROOT / "deploy"
DOCKERFILE = DEPLOY_DIR / "Dockerfile"
COMPOSE_FILE = DEPLOY_DIR / "docker-compose.yml"
ENTRYPOINT = DEPLOY_DIR / "entrypoint.sh"

# HOME inside the container, and the host-side root every bind SOURCE rebases on.
CONTAINER_HOME = "/home/teatree"  # privacy-scan:allow — the box's public, documented deploy home
HOST_HOME_PLACEHOLDER = "${TEATREE_HOST_HOME:-" + CONTAINER_HOME + "}"

# The ONE interpreter root, spelled the ONE way both the bind and the env use it.
# It is the HOST's address, named by the same variable on both sides of the mount,
# so every ABSOLUTE path uv writes inside it resolves in BOTH venues. Rebasing only
# the SOURCE — the container keeping `/home/teatree` as its own coordinate — is the
# defect this module now pins: uv rewrites the alias set with container-absolute
# targets, and off-box every one of them dangles for the host.
SHARED_PYTHON_ROOT = f"{HOST_HOME_PLACEHOLDER}/.local/share/uv/python"

# What that root renders to on the deployed box, where the variable takes its
# default. Byte-identical to the pre-fix hardcoding, which is why the box is unchanged.
BOX_PYTHON_ROOT = f"{CONTAINER_HOME}/.local/share/uv/python"

# The tool plane, which stays on its named volume.
TOOL_PLANE_VOLUME = "teatree_uv"
TOOL_PLANE_TARGET = "/opt/teatree/uv"
TOOL_PYTHON_ROOT = f"{TOOL_PLANE_TARGET}/python"


def _fold_continuations(text: str) -> str:
    return re.sub(r"\\\n\s*", " ", text)


def _dockerfile_env() -> dict[str, str]:
    """Every ``KEY=VALUE`` the Dockerfile's ``ENV`` instructions declare."""
    env: dict[str, str] = {}
    for line in _fold_continuations(DOCKERFILE.read_text(encoding="utf-8")).splitlines():
        if not line.startswith("ENV "):
            continue
        for token in line.removeprefix("ENV ").split():
            key, sep, value = token.partition("=")
            if sep:
                env[key] = value.strip('"')
    return env


def _dockerfile_path_entries() -> list[str]:
    return _dockerfile_env()["PATH"].split(":")


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


def _service_environments() -> dict[str, dict[str, str]]:
    """Each compose service's ``environment`` mapping, YAML anchors resolved."""
    return {name: (service.get("environment") or {}) for name, service in _compose()["services"].items()}


def _shell_assignments(text: str) -> dict[str, str]:
    return dict(re.findall(r"^\s*([A-Z_][A-Z0-9_]*)=(\S+)$", text, re.MULTILINE))


def _expand(value: str, variables: dict[str, str]) -> str:
    for name, resolved in variables.items():
        value = value.replace(f"${{{name}}}", resolved).replace(f"${name}", resolved)
    return value


def _common_volumes() -> list:
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    return compose["x-teatree-common"]["volumes"]


def _bind_mounts() -> dict[str, dict]:
    return {
        entry["target"]: entry for entry in _common_volumes() if isinstance(entry, dict) and entry.get("type") == "bind"
    }


def _declared_python_version() -> str:
    return (REPO_ROOT / ".python-version").read_text(encoding="utf-8").strip()


def _uv_python_install_versions(path: Path) -> list[str]:
    """The version argument of every `uv python install` INVOCATION in *path*.

    Anchored to a command position so the same three words quoted inside an
    error message — the entrypoint names the remedy it cannot perform offline —
    are prose, not a second install site that drifted from ``.python-version``.
    """
    text = _fold_continuations(path.read_text(encoding="utf-8"))
    return re.findall(r"(?:^|&&|;|\|)\s*uv python install\s+(\S+)", text, re.MULTILINE)


def _uv_python_install_dir_assignments(path: Path) -> list[str]:
    """Every value *path* assigns to ``UV_PYTHON_INSTALL_DIR``, in any syntax.

    Shell variables are resolved against *path*'s own assignments, so a value
    named once and referenced twice is compared as the path it resolves to
    rather than as the literal ``$NAME`` — which no assertion could judge.
    """
    text = _fold_continuations(path.read_text(encoding="utf-8"))
    variables = _shell_assignments(path.read_text(encoding="utf-8"))
    raw = re.findall(r"UV_PYTHON_INSTALL_DIR=(\S+)", text)
    return [_expand(value.strip('"').strip("'"), variables) for value in raw]


class TestInterpreterRootIsOneSharedPath:
    def test_compose_points_every_bind_mounting_service_at_that_root(self) -> None:
        # Mounting the shared root and NOT resolving interpreters from it is the
        # bug, so the two are asserted as one biconditional rather than against a
        # hardcoded service list: a service added later must decide both together.
        environments = _service_environments()
        mounts_shared_root = {
            name
            for name, service in _compose()["services"].items()
            if any(
                isinstance(volume, dict) and volume.get("target") == SHARED_PYTHON_ROOT
                for volume in (service.get("volumes") or [])
            )
        }
        resolves_shared_root = {
            name for name, env in environments.items() if env.get("UV_PYTHON_INSTALL_DIR") == SHARED_PYTHON_ROOT
        }
        assert mounts_shared_root, "no service mounts the shared interpreter root"
        assert mounts_shared_root == resolves_shared_root
        # And the value each mounting service resolves is that mount's own TARGET,
        # not a second spelling of it. An env var naming a path the bind does not
        # land on is how `uv python install` writes into a directory nobody shares.
        target = _bind_mounts()[SHARED_PYTHON_ROOT]["target"]
        for name in mounts_shared_root:
            assert environments[name]["UV_PYTHON_INSTALL_DIR"] == target

    def test_that_root_is_uvs_own_default_under_the_host_home(self) -> None:
        # Naming uv's DEFAULT relative path is what lets the HOST — which sets the
        # variable nowhere — arrive at the same directory without configuring
        # anything. The PREFIX is the host home, so the address the container uses
        # is the host's own; the variable's default keeps the box rendering to the
        # container home it always did.
        assert f"{HOST_HOME_PLACEHOLDER}/.local/share/uv/python" == SHARED_PYTHON_ROOT
        assert SHARED_PYTHON_ROOT.replace(HOST_HOME_PLACEHOLDER, CONTAINER_HOME, 1) == BOX_PYTHON_ROOT

    def test_that_root_is_bound_from_the_host_at_path_identity(self) -> None:
        entry = _bind_mounts().get(SHARED_PYTHON_ROOT)
        assert entry is not None, f"{SHARED_PYTHON_ROOT} must be a host bind mount"
        # source == target OUTRIGHT, as one literal string. The earlier form
        # substituted HOST_HOME_PLACEHOLDER away before comparing, which asserted
        # the identity only for a host whose home IS the container home — so it
        # stayed green while the container's coordinate remained `/home/teatree`
        # on every other host. THIS is the assertion that would have caught it.
        assert entry["source"] == entry["target"] == SHARED_PYTHON_ROOT

    def test_the_shared_root_is_writable_by_the_container(self) -> None:
        # `uv python install` provisions the shared root at runtime; a read-only
        # bind would turn the offline-gap remedy into an unfixable failure.
        assert not _bind_mounts()[SHARED_PYTHON_ROOT].get("read_only", False)

    def test_every_uv_python_install_tracks_the_declared_version(self) -> None:
        # The mechanism, not a one-off copy: whatever version the deploy path
        # installs is the project's declared one, so the 3.14 move (#4404) needs
        # no second step here.
        for path in (DOCKERFILE, ENTRYPOINT):
            versions = _uv_python_install_versions(path)
            assert versions, f"{path.name} must still install an interpreter"
            assert set(versions) == {_declared_python_version()}, (
                f"{path.name}: `uv python install` must track .python-version"
            )

    def test_the_shared_root_is_never_hardcoded_into_the_image(self) -> None:
        # The separation this fix rests on. The image's own `uv python install`
        # runs AFTER its ENV, so an image-level project root would make the baked
        # tool venvs record it — and the host bind then shadows that directory at
        # runtime, breaking the container's own `t3` and `prek`. The project root
        # therefore arrives only from compose, never from the image.
        for path in (DOCKERFILE, ENTRYPOINT):
            for assigned in _uv_python_install_dir_assignments(path):
                assert assigned == TOOL_PYTHON_ROOT, (
                    f"{path.name}: UV_PYTHON_INSTALL_DIR={assigned} — the image and entrypoint "
                    f"own the TOOL plane only; the project root is compose's to set"
                )


class TestToolPlaneStaysOnTheNamedVolume:
    """The second half of the fix: the tool plane keeps its own root.

    The baked ``prek`` and ``teatree`` tool venvs record the interpreter that
    ``UV_PYTHON_INSTALL_DIR`` resolved when they were built. Two positive things
    are asserted below: the image ``ENV`` names the tool plane, and the
    entrypoint's two SHELL ``uv tool install`` calls — which run under compose's
    project-root env and would otherwise rebuild the tool venvs against a
    host-controlled directory — pin it explicitly.

    That is the whole coverage, and it is narrower than the runtime. The
    PYTHON-side installs (``self_update``, ``dep_drift_repair``, and
    ``editable_pth.install_argv``, reached from ``t3 setup`` and
    ``doctor --repair``) shell out through ``run_allowed_to_fail``/``run_captured``
    with ``env=None``, so they inherit the ambient project root and are NOT
    covered by anything here. The commit body records that gap.
    """

    def test_the_image_env_owns_the_tool_plane_python_root(self) -> None:
        assert _dockerfile_env()["UV_PYTHON_INSTALL_DIR"] == TOOL_PYTHON_ROOT

    def test_every_runtime_tool_install_pins_the_tool_plane(self) -> None:
        # RED if the pin is dropped: the ambient value at runtime is compose's
        # project root, so an unpinned `uv tool install --reinstall` rebuilds the
        # container's own toolchain against the host's directory.
        text = _fold_continuations(ENTRYPOINT.read_text(encoding="utf-8"))
        variables = _shell_assignments(ENTRYPOINT.read_text(encoding="utf-8"))
        # Anchored to a COMMAND position: the same three words appear in the
        # comments explaining each call, and a prose match has no prefix to judge.
        installs = re.findall(
            r"(?:^|&&|;|\||\$\()[ \t]*(?:(?:if|then|else|elif|while|until|do|!)[ \t]+)*"
            r"(\S+=\S+)?[ \t]*uv tool install\b",
            text,
            re.MULTILINE,
        )
        assert len(installs) == 2, f"expected the editable + prek installs, found {len(installs)}"
        for prefix in installs:
            resolved = _expand(prefix or "", variables).replace('"', "").replace("'", "")
            assert resolved == f"UV_PYTHON_INSTALL_DIR={TOOL_PYTHON_ROOT}", (
                f"`uv tool install` prefixed with {prefix!r} does not pin the tool plane"
            )

    def test_the_tool_volume_is_still_mounted(self) -> None:
        assert f"{TOOL_PLANE_VOLUME}:{TOOL_PLANE_TARGET}" in _common_volumes()

    def test_the_tool_volume_is_still_declared(self) -> None:
        compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
        assert TOOL_PLANE_VOLUME in (compose.get("volumes") or {})

    def test_the_tool_env_still_resolves_into_that_volume(self) -> None:
        env = _dockerfile_env()
        assert env["UV_TOOL_DIR"] == f"{TOOL_PLANE_TARGET}/tools"
        assert env["UV_TOOL_BIN_DIR"] == f"{TOOL_PLANE_TARGET}/bin"

    def test_path_still_leads_with_the_tool_bin_dir(self) -> None:
        assert _dockerfile_path_entries()[0] == f"{TOOL_PLANE_TARGET}/bin"


class TestRenderedRootIsIdenticalInBothVenues:
    """`docker compose config` end to end — on the box, and on a host that is not it.

    The static assertions above read one compose file; these render it. The
    non-matching host home is the case the box can never exercise and the one the
    defect lived in, so it is asserted as a TRIPLE — bind source, bind target, and
    the env every mounting service resolves — all one string.
    """

    def test_the_box_renders_the_root_it_always_did(self, tmp_path: Path) -> None:
        # The no-change half of the acceptance: where the variable takes its
        # default, every rendered byte is what it was before the fix.
        entry = _rendered_mounts(_render(tmp_path, {}))[BOX_PYTHON_ROOT]
        assert entry["source"] == entry["target"] == BOX_PYTHON_ROOT

    def test_a_non_matching_host_home_renders_one_identical_triple(self, tmp_path: Path) -> None:
        host_home = "/home/operator"
        expected = f"{host_home}/.local/share/uv/python"
        rendered = _render(tmp_path, {"TEATREE_HOST_HOME": host_home})
        mounts = _rendered_mounts(rendered)

        assert expected in mounts, f"{expected} missing from the rendered config"
        assert mounts[expected]["source"] == mounts[expected]["target"] == expected

        mounting = [
            name
            for name, service in rendered["services"].items()
            if any(volume.get("target") == expected for volume in service.get("volumes", []))
        ]
        assert mounting, "no rendered service mounts the interpreter root"
        for name in mounting:
            assert rendered["services"][name]["environment"]["UV_PYTHON_INSTALL_DIR"] == expected

    def test_a_non_matching_host_home_leaves_no_container_coordinate_behind(self, tmp_path: Path) -> None:
        # The defect stated as its own absence. Pre-fix this rendered a mount
        # targeting the CONTAINER's `/home/teatree/...` while its source was the
        # operator's — the split address uv then wrote container-absolute alias
        # targets into the host's root through.
        rendered = _render(tmp_path, {"TEATREE_HOST_HOME": "/home/operator"})
        assert BOX_PYTHON_ROOT not in _rendered_mounts(rendered)
        resolved = {
            service.get("environment", {}).get("UV_PYTHON_INSTALL_DIR") for service in rendered["services"].values()
        }
        assert BOX_PYTHON_ROOT not in resolved


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
