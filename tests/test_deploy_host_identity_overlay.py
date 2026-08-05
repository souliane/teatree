"""The host-identity compose overlay is WIRED, and only where it belongs (#4193).

``deploy/docker-compose.host-identity.yml`` mounts the worktree tree at its own HOST
path so the container has one coordinate both venues agree on. The container hands
worktree paths it resolved as CONTAINER paths to the host docker daemon (``t3 <overlay>
worktree start`` runs ``docker compose up`` through the mounted socket), and off-box
those paths do not exist on the host — dockerd answers "mounts denied: the path ... is
not shared from the host" and every worktree stack fails to start.

The file shipped with a header claiming ``deploy/t3`` and ``deploy/deploy.sh`` each add
its ``-f``. Neither did: ``deploy/t3`` assigned ``HOST_IDENTITY_FILE`` and never read it,
and ``deploy.sh`` never mentioned the file at all. Dead code whose own documentation
said it was live is worse than absent — a reader checking whether off-box worktrees are
handled finds an answer that is not true.

Both halves are pinned here: the overlay is added when the homes differ, and NOT added
when they match, because on a matching host the two mount targets collide.
"""

import re
from pathlib import Path

import pytest
import yaml

DEPLOY_DIR = Path(__file__).resolve().parents[1] / "deploy"
OVERLAY = DEPLOY_DIR / "docker-compose.host-identity.yml"
WRAPPER = DEPLOY_DIR / "t3"
DEPLOY_SH = DEPLOY_DIR / "deploy.sh"

BASE_COMPOSE = DEPLOY_DIR / "docker-compose.yml"


def _container_home() -> str:
    """HOME inside the container, DERIVED from the base compose file rather than restated.

    Every base-file mount TARGET is anchored here, so this is the value the host home
    is compared against. Read from the canonical source so the two can never drift, and
    so this file carries no second copy of a real path.
    """
    default = yaml.safe_load(BASE_COMPOSE.read_text(encoding="utf-8"))
    clones = next(
        mount
        for service in default["services"].values()
        for mount in service.get("volumes", [])
        if isinstance(mount, str) and mount.endswith("/workspace")
    )
    return clones.rsplit(":", 1)[1].removesuffix("/workspace")


CONTAINER_HOME = _container_home()

SCRIPTS = pytest.mark.parametrize("script", [WRAPPER, DEPLOY_SH], ids=["deploy/t3", "deploy/deploy.sh"])


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestTheOverlayIsReferencedAtAll:
    @SCRIPTS
    def test_each_script_names_the_overlay_file(self, script: Path) -> None:
        assert OVERLAY.name in _text(script), (
            f"{script.name} never mentions {OVERLAY.name} — the overlay's own header claims it does"
        )

    @SCRIPTS
    def test_the_overlay_path_is_read_not_merely_assigned(self, script: Path) -> None:
        """An assignment alone is what made this dead: the variable was set and never used."""
        body = _text(script)
        assert re.search(r'HOST_IDENTITY_FILE="[^"]+"', body), f"{script.name} does not resolve the overlay path"
        assert body.count("HOST_IDENTITY_FILE") > 1, (
            f"{script.name} assigns HOST_IDENTITY_FILE and never reads it — the #4193 dead assignment"
        )

    @SCRIPTS
    def test_the_overlay_is_passed_as_a_second_compose_file(self, script: Path) -> None:
        assert '-f "$HOST_IDENTITY_FILE"' in _text(script)


class TestTheOverlayIsConditionalOnTheHostHome:
    @SCRIPTS
    def test_the_condition_compares_the_host_home_to_the_container_home(self, script: Path) -> None:
        """Both scripts must DERIVE the container home, never restate it, then compare against it.

        Asserting the derivation rather than a literal keeps the one statement of that
        path in the compose file, where the mount targets that define it already live.
        """
        body = _text(script)
        assert "CONTAINER_HOME=" in body
        assert f"CONTAINER_HOME={CONTAINER_HOME}" not in body, (
            "the container home is restated here; derive it from the compose declaration instead"
        )
        assert "TEATREE_HOST_HOME" in body

    @SCRIPTS
    def test_no_compose_invocation_still_hardcodes_the_base_file_alone(self, script: Path) -> None:
        """Every call must route through the composed file list, or it silently skips the overlay."""
        stray = [
            line.strip()
            for line in _text(script).splitlines()
            if 'docker compose -f "$COMPOSE_FILE"' in line and "HOST_IDENTITY_FILE" not in line
        ]
        # The only permitted occurrences are inside the branch that deliberately omits
        # the overlay (the homes match), which each script keeps beside its `else`.
        assert len(stray) <= 1, f"{script.name} has compose calls that bypass the overlay: {stray}"


class TestTheOverlayItselfStaysMinimal:
    def test_it_only_adds_worktree_tree_mounts_at_path_identity(self) -> None:
        overlay = yaml.safe_load(_text(OVERLAY))
        assert set(overlay) == {"services"}
        for name, service in overlay["services"].items():
            assert set(service) == {"volumes"}, f"{name} overrides more than volumes"
            for mount in service["volumes"]:
                assert mount["source"] == mount["target"], (
                    f"{name} mount is not at path identity, which is the overlay's whole purpose: {mount}"
                )

    def test_it_never_replaces_the_canonical_container_target(self) -> None:
        """The base file's ``<container home>/workspace/...`` target is what every Worktree row records."""
        overlay = yaml.safe_load(_text(OVERLAY))
        targets = {mount["target"] for service in overlay["services"].values() for mount in service["volumes"]}
        assert not any(target.startswith(f"{CONTAINER_HOME}/") for target in targets), (
            "the overlay ADDS a host-identical view; it must never restate the canonical target"
        )
