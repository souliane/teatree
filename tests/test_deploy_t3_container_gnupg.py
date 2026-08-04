# test-path: cross-cutting — drives deploy/t3 (no src mirror).
"""The containerized CLI wrapper reaches the GPG home the entrypoint resolved.

``deploy/entrypoint.sh``'s ``resolve_gnupg_home`` points ``GNUPGHOME`` at a
container-local copy of the key material whenever the host's GPG mount cannot host
the ``S.*`` sockets gpg-agent and keyboxd bind. That export reaches only the
service's MAIN process: ``docker exec`` starts from the container's own
environment, which still carries the image's baked ``GNUPGHOME``, so every secret
read through the sanctioned wrapper dies on ``No Keybox daemon running`` — and an
empty ``pass`` read is indistinguishable from the credential simply being absent.

``TMPDIR`` closed the same gap with a container-level ``environment:`` pin
(docker-compose.yml). ``GNUPGHOME`` cannot use that shape: its correct value is
not the same on every host. The box's mount hosts sockets in place and MUST stay
in place, because one shared home is what gives the services one gpg-agent and
makes a cached passphrase work; a Docker-Desktop host must use the tmpfs copy. A
static pin would be wrong on one of the two, and pinning the derived path would
also make the entrypoint's own ``[ -d "$GNUPGHOME" ]`` guard skip the copy that
creates it.

So the wrapper carries a prologue the CONTAINER evaluates, which reads the
evidence the entrypoint left on disk rather than re-deriving anything: a derived
home holding key material exists only when the entrypoint decided the mount could
not host the sockets. On the box the tmpfs stays empty and ``GNUPGHOME`` is left
exactly as the image set it.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash (present in the deploy image and CI)")

WRAPPER = Path(__file__).resolve().parents[1] / "deploy" / "t3"
_SH = shutil.which("sh") or "sh"

PROLOGUE_NAME = "CONTAINER_GNUPG_PROLOGUE"


def _extract_single_quoted_assignment(name: str) -> str:
    """Return the verbatim value of the single-quoted shell assignment *name*."""
    lines = WRAPPER.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if not line.startswith(f"{name}='"):
            continue
        body = [line[len(name) + 2 :]]
        if body[0].endswith("'"):
            return body[0][:-1]
        for continuation in lines[index + 1 :]:
            if continuation.endswith("'"):
                body.append(continuation[:-1])
                return "\n".join(body)
            body.append(continuation)
    not_found = f"assignment {name!r} not found in {WRAPPER}"
    raise AssertionError(not_found)


def _run_prologue(runtime_dir: Path, baked_home: str) -> str:
    """Evaluate the wrapper's prologue the way the container does, and report GNUPGHOME."""
    proc = subprocess.run(
        [
            _SH,
            "-c",
            _extract_single_quoted_assignment(PROLOGUE_NAME),
            "t3-in-container",
            "sh",
            "-c",
            'printf "%s" "${GNUPGHOME:-<unset>}"',
        ],
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


class TestContainerGnupgPrologue:
    def test_adopts_the_derived_home_the_entrypoint_built(self, tmp_path: Path) -> None:
        # The laptop: without this the exec'd process keeps the image's baked home,
        # keyboxd cannot bind its socket there, and every `pass show` reads empty.
        derived = _derived_home_with_key_material(tmp_path / "run")
        assert _run_prologue(tmp_path / "run", "/home/teatree/.gnupg") == str(derived)

    def test_leaves_the_baked_home_alone_when_nothing_was_derived(self, tmp_path: Path) -> None:
        # The box: the mount hosts sockets in place, the tmpfs stays empty, and the
        # services must keep sharing ONE gpg-agent on the in-place home.
        (tmp_path / "run").mkdir()
        assert _run_prologue(tmp_path / "run", "/home/teatree/.gnupg") == "/home/teatree/.gnupg"

    def test_an_empty_derived_home_is_not_adopted(self, tmp_path: Path) -> None:
        # `derive_container_gnupg_home` yields an empty home when the host has no key
        # material. Adopting it would REPLACE a readable home with an empty one.
        (tmp_path / "run" / "gnupg").mkdir(parents=True)
        assert _run_prologue(tmp_path / "run", "/home/teatree/.gnupg") == "/home/teatree/.gnupg"

    def test_execs_the_command_it_was_handed(self, tmp_path: Path) -> None:
        # The prologue wraps every containerized `t3` invocation, so a prologue that
        # swallowed its arguments would break the whole CLI rather than one secret read.
        _derived_home_with_key_material(tmp_path / "run")
        proc = subprocess.run(
            [
                _SH,
                "-c",
                _extract_single_quoted_assignment(PROLOGUE_NAME),
                "t3-in-container",
                "sh",
                "-c",
                'printf "argv=[%s]" "$*"',
                "_",
                "review",
                "post-comment",
            ],
            capture_output=True,
            text=True,
            check=True,
            env={"TEATREE_GNUPG_RUNTIME_DIR": str(tmp_path / "run"), "PATH": "/usr/bin:/bin"},
        )
        assert proc.stdout.strip() == "argv=[review post-comment]"


class TestWrapperUsesThePrologue:
    def test_the_running_worker_exec_path_carries_it(self) -> None:
        wrapper = WRAPPER.read_text(encoding="utf-8")
        exec_lines = [line for line in wrapper.splitlines() if "compose -f" in line and " exec " in line]
        assert exec_lines, "the wrapper must still exec into the running worker"
        for line in exec_lines:
            assert f'"${PROLOGUE_NAME}"' in line, f"exec path bypasses the GPG-home prologue: {line.strip()}"

    def test_never_advertises_the_host_install_as_a_remedy(self) -> None:
        # The host `t3` is the forbidden path — the sanctioned wrapper pointing at it
        # is what keeps host processes holding descriptors on the control DB.
        assert "~/.local/bin/t3" not in WRAPPER.read_text(encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
