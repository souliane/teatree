# test-path: cross-cutting — drives deploy/profile-gnupg-home.sh (no src mirror).
"""A raw ``docker exec`` login shell reaches the GPG home the entrypoint resolved.

``deploy/entrypoint.sh``'s ``resolve_gnupg_home`` points ``GNUPGHOME`` at a
container-local copy of the key material whenever the host's GPG mount cannot host
the ``S.*`` sockets gpg-agent and keyboxd bind. That export reaches only the
service's MAIN process. ``deploy/t3`` closed the gap for the sanctioned wrapper
with an inline prologue, but a hand-issued ``docker exec <container> sh -lc 'pass
show …'`` still starts from the image's baked ``GNUPGHOME`` and reads EMPTY with
rc=0 — a credential failure shaped exactly like the feature being switched off.

A compose ``environment:`` pin cannot carry the value: it differs per host, and
pinning the derived path would make the entrypoint's own ``[ -d "$GNUPGHOME" ]``
guard skip the copy that creates it. So the image ships the wrapper's predicate as
a login-shell profile snippet, reading the EVIDENCE the entrypoint left on disk
rather than re-deriving anything. On the box the tmpfs stays empty and
``GNUPGHOME`` is left exactly as the image set it.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("sh") is None, reason="needs a POSIX sh (present in the deploy image and CI)"
)

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
PROFILE_SCRIPT = DEPLOY / "profile-gnupg-home.sh"
DOCKERFILE = DEPLOY / "Dockerfile"
_SH = shutil.which("sh") or "sh"

BAKED_HOME = "/home/teatree/.gnupg"


def _sourced_gnupg_home(runtime_dir: Path, baked_home: str) -> str:
    """Source the profile snippet the way a login shell does, and report GNUPGHOME."""
    proc = subprocess.run(
        [_SH, "-c", '. "$1"; printf "%s" "${GNUPGHOME:-<unset>}"', "sh", str(PROFILE_SCRIPT)],
        capture_output=True,
        text=True,
        check=True,
        env={"GNUPGHOME": baked_home, "TEATREE_GNUPG_RUNTIME_DIR": str(runtime_dir), "PATH": "/usr/bin:/bin"},
    )
    return proc.stdout.strip()


def _derived_home_with_key_material(runtime_dir: Path) -> Path:
    """The shape ``derive_container_gnupg_home`` leaves behind on a sharing-transport host."""
    derived = runtime_dir / "gnupg"
    (derived / "private-keys-v1.d").mkdir(parents=True)
    (derived / "private-keys-v1.d" / "ABC123.key").write_bytes(b"secret-key")
    (derived / "common.conf").write_text("use-keyboxd\n", encoding="utf-8")
    return derived


class TestLoginShellGnupgHome:
    def test_adopts_the_derived_home_the_entrypoint_built(self, tmp_path: Path) -> None:
        # The laptop: without this a hand-issued `docker exec … sh -lc 'pass show …'`
        # keeps the baked home, keyboxd cannot bind its socket there, and the read is
        # empty with rc=0.
        derived = _derived_home_with_key_material(tmp_path / "run")
        assert _sourced_gnupg_home(tmp_path / "run", BAKED_HOME) == str(derived)

    def test_leaves_the_baked_home_alone_when_nothing_was_derived(self, tmp_path: Path) -> None:
        # The box: the mount hosts sockets in place, the tmpfs stays empty, and the
        # services must keep sharing ONE gpg-agent on the in-place home.
        (tmp_path / "run").mkdir()
        assert _sourced_gnupg_home(tmp_path / "run", BAKED_HOME) == BAKED_HOME

    def test_an_empty_derived_home_is_not_adopted(self, tmp_path: Path) -> None:
        # `derive_container_gnupg_home` yields an empty home when the host has no key
        # material. Adopting it would REPLACE a readable home with an empty one.
        (tmp_path / "run" / "gnupg").mkdir(parents=True)
        assert _sourced_gnupg_home(tmp_path / "run", BAKED_HOME) == BAKED_HOME

    def test_leaves_no_scratch_variable_behind(self, tmp_path: Path) -> None:
        # It runs in the operator's own login shell, so a leaked loop variable is a
        # name collision in every interactive session the image serves.
        _derived_home_with_key_material(tmp_path / "run")
        proc = subprocess.run(
            [_SH, "-c", '. "$1"; set | grep -c "^_teatree" || true', "sh", str(PROFILE_SCRIPT)],
            capture_output=True,
            text=True,
            check=True,
            env={"GNUPGHOME": BAKED_HOME, "TEATREE_GNUPG_RUNTIME_DIR": str(tmp_path / "run"), "PATH": "/usr/bin:/bin"},
        )
        assert proc.stdout.strip() == "0"


class TestTheImageSourcesIt:
    def test_installed_where_a_login_shell_reads_it(self) -> None:
        # /etc/profile globs `/etc/profile.d/*.sh`, so a target outside that directory
        # or without the suffix ships the file and changes nothing.
        copies = [
            line
            for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()
            if line.startswith("COPY") and PROFILE_SCRIPT.name in line
        ]
        assert copies, f"{PROFILE_SCRIPT.name} is never baked into the image"
        target = copies[0].split()[-1]
        assert target.startswith("/etc/profile.d/"), f"not sourced by a login shell: {target}"
        assert target.endswith(".sh"), f"/etc/profile only globs *.sh: {target}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
