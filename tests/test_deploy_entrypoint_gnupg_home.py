# test-path: cross-cutting — drives deploy/entrypoint.sh (no src mirror).
"""The deploy entrypoint gives gpg a home it can actually USE.

gpg-agent and keyboxd bind their ``S.*`` sockets INSIDE ``GNUPGHOME``. On the
deployment box that home is a bind mount of a real local filesystem and binding
works, so ``resolve_gnupg_home`` leaves it alone. On an operator laptop the same
mount is served by a file-sharing transport that cannot host a unix socket at all
(Docker Desktop for Mac reports ``fakeowner``), keyboxd dies with ``exit status
2``, gpg finds zero keys, and every ``pass show`` fails even though
``private-keys-v1.d`` is intact — so the entrypoint copies the key material into
a container-local home on a tmpfs and points ``GNUPGHOME`` there.

The host's GPG home is strictly READ-ONLY throughout: the switch is decided from
the mount table, so not even the detection touches it.

Runs the REAL shell functions (extracted verbatim from the entrypoint) in a bash
subprocess against real files under ``tmp_path``, mirroring the sibling
entrypoint tests (``test_deploy_entrypoint_disk_tmpdir.py``).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash (present in the deploy image and CI)")

ENTRYPOINT = Path(__file__).resolve().parents[1] / "deploy" / "entrypoint.sh"
_BASH = shutil.which("bash") or "bash"

# The filesystems Docker Desktop / Colima / Lima serve a host bind mount over.
# None of them can host a unix socket, and the list is open-ended — which is why
# the entrypoint allowlists the good ones instead of denylisting these.
SHARING_TRANSPORTS = ["fakeowner", "virtiofs", "9p", "fuse.grpcfuse", "osxfs", "nfs", "cifs", "vboxsf", "sshfs"]
LOCAL_FILESYSTEMS = ["ext4", "xfs", "btrfs", "zfs", "overlay", "tmpfs"]

_FUNCTIONS = ("path_fstype", "fstype_hosts_unix_sockets", "derive_container_gnupg_home", "resolve_gnupg_home")


def _extract_shell_function(name: str) -> str:
    """Return the verbatim source of shell function *name* from the entrypoint."""
    body: list[str] = []
    capturing = False
    for line in ENTRYPOINT.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}() {{"):
            capturing = True
        if capturing:
            body.append(line)
            if line == "}":
                return "\n".join(body)
    not_found = f"function {name!r} not found in {ENTRYPOINT}"
    raise AssertionError(not_found)


def _run(tmp_path: Path, script: str) -> str:
    """Run *script* with the entrypoint's real GNUPGHOME functions in scope."""
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "set -euo pipefail\n" + "\n".join(_extract_shell_function(name) for name in _FUNCTIONS) + f"\n{script}\n",
        encoding="utf-8",
    )
    proc = subprocess.run([_BASH, str(harness)], capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _mount_table(tmp_path: Path, entries: list[tuple[str, str]]) -> Path:
    """A fixture /proc/mounts carrying ``(mount point, fstype)`` rows."""
    table = tmp_path / "mounts"
    table.write_text(
        "".join(f"/dev/src{point} {point} {fstype} rw,relatime 0 0\n" for point, fstype in entries), encoding="utf-8"
    )
    return table


def _build_host_gnupg(home: Path) -> None:
    """A host GPG home shaped like a real keyboxd one, sockets and locks included."""
    home.mkdir(parents=True)
    (home / "common.conf").write_text("use-keyboxd\n", encoding="utf-8")
    (home / "gpg.conf").write_text("default-key DEADBEEF\n", encoding="utf-8")
    (home / "trustdb.gpg").write_bytes(b"trust")
    (home / "random_seed").write_bytes(b"entropy")
    (home / "gpg-agent.conf").write_text("pinentry-program /opt/homebrew/bin/pinentry-mac\n", encoding="utf-8")
    (home / "private-keys-v1.d").mkdir()
    (home / "private-keys-v1.d" / "ABC123.key").write_bytes(b"secret-key")
    (home / "public-keys.d").mkdir()
    (home / "public-keys.d" / "pubring.db").write_bytes(b"keyboxd-db")
    (home / "public-keys.d" / "pubring.db.lock").write_text("22\n", encoding="utf-8")
    (home / "public-keys.d" / ".#lk0x1.host.22").write_text("22\n", encoding="utf-8")
    (home / "openpgp-revocs.d").mkdir()
    (home / "openpgp-revocs.d" / "ABC123.rev").write_bytes(b"revocation")


class TestFstypeHostsUnixSockets:
    @pytest.mark.parametrize("fstype", LOCAL_FILESYSTEMS)
    def test_real_local_filesystems_are_used_in_place(self, tmp_path: Path, fstype: str) -> None:
        # The box: the bind mount works, so nothing is copied and the services keep
        # sharing ONE gpg-agent (what makes a cached passphrase work at all).
        assert _run(tmp_path, f"fstype_hosts_unix_sockets {fstype} && echo yes || echo no") == "yes"

    @pytest.mark.parametrize("fstype", SHARING_TRANSPORTS)
    def test_file_sharing_transports_are_rejected(self, tmp_path: Path, fstype: str) -> None:
        assert _run(tmp_path, f"fstype_hosts_unix_sockets {fstype} && echo yes || echo no") == "no"

    def test_unknown_filesystem_takes_the_derive_path(self, tmp_path: Path) -> None:
        # Allowlist, not denylist: the failing set is open-ended and renamed often,
        # so an unrecognised name must fall to the path that works everywhere.
        assert _run(tmp_path, "fstype_hosts_unix_sockets some-future-vm-share && echo yes || echo no") == "no"


class TestPathFstype:
    def test_resolves_the_longest_matching_mount_point(self, tmp_path: Path) -> None:
        table = _mount_table(
            tmp_path, [("/", "ext4"), ("/home/teatree", "ext4"), ("/home/teatree/.gnupg", "fakeowner")]
        )
        script = f"export TEATREE_PROC_MOUNTS={table}\npath_fstype /home/teatree/.gnupg"
        assert _run(tmp_path, script) == "fakeowner"

    def test_falls_back_to_the_enclosing_mount(self, tmp_path: Path) -> None:
        table = _mount_table(tmp_path, [("/", "ext4"), ("/home", "xfs")])
        script = f"export TEATREE_PROC_MOUNTS={table}\npath_fstype /home/teatree/.gnupg"
        assert _run(tmp_path, script) == "xfs"

    def test_root_only_mount_table_resolves_to_root(self, tmp_path: Path) -> None:
        table = _mount_table(tmp_path, [("/", "btrfs")])
        script = f"export TEATREE_PROC_MOUNTS={table}\npath_fstype /home/teatree/.gnupg"
        assert _run(tmp_path, script) == "btrfs"

    def test_a_sibling_prefix_never_matches(self, tmp_path: Path) -> None:
        # `/home/teatree/.gnupg-run` must not be served by `/home/teatree/.gnupg`.
        table = _mount_table(tmp_path, [("/", "ext4"), ("/home/teatree/.gnupg", "fakeowner")])
        script = f"export TEATREE_PROC_MOUNTS={table}\npath_fstype /home/teatree/.gnupg-run"
        assert _run(tmp_path, script) == "ext4"

    def test_missing_mount_table_yields_empty_which_derives(self, tmp_path: Path) -> None:
        script = (
            f'export TEATREE_PROC_MOUNTS={tmp_path / "absent"}\nprintf "[%s]" "$(path_fstype /home/teatree/.gnupg)"'
        )
        assert _run(tmp_path, script) == "[]"
        assert _run(tmp_path, 'fstype_hosts_unix_sockets "" && echo yes || echo no') == "no"


class TestDeriveContainerGnupgHome:
    def test_copies_the_key_material_gpg_needs_to_decrypt(self, tmp_path: Path) -> None:
        host, derived = tmp_path / "host", tmp_path / "run" / "gnupg"
        _build_host_gnupg(host)
        _run(tmp_path, f"derive_container_gnupg_home {host} {derived}")
        assert (derived / "private-keys-v1.d" / "ABC123.key").read_bytes() == b"secret-key"
        # `use-keyboxd` is COPIED deliberately: on a keyboxd host the public keys
        # live ONLY in pubring.db, so dropping it would find zero keys.
        assert (derived / "common.conf").read_text(encoding="utf-8") == "use-keyboxd\n"
        assert (derived / "public-keys.d" / "pubring.db").read_bytes() == b"keyboxd-db"
        assert (derived / "trustdb.gpg").read_bytes() == b"trust"
        assert (derived / "gpg.conf").exists()

    def test_leaves_behind_the_locks_and_host_only_daemon_config(self, tmp_path: Path) -> None:
        host, derived = tmp_path / "host", tmp_path / "run" / "gnupg"
        _build_host_gnupg(host)
        _run(tmp_path, f"derive_container_gnupg_home {host} {derived}")
        # Dotlocks leaked by a process that died holding them must not travel.
        assert not (derived / "public-keys.d" / "pubring.db.lock").exists()
        assert not (derived / "public-keys.d" / ".#lk0x1.host.22").exists()
        # A host agent config names host-only binaries (`pinentry-mac`) absent here.
        assert not (derived / "gpg-agent.conf").exists()
        assert not (derived / "random_seed").exists()
        assert not (derived / "openpgp-revocs.d").exists()

    def test_the_derived_home_is_private(self, tmp_path: Path) -> None:
        # gpg refuses a group/other-readable home.
        host, derived = tmp_path / "host", tmp_path / "run" / "gnupg"
        _build_host_gnupg(host)
        _run(tmp_path, f"derive_container_gnupg_home {host} {derived}")
        assert oct(derived.stat().st_mode)[-3:] == "700"

    def test_never_writes_to_the_host_home(self, tmp_path: Path) -> None:
        host, derived = tmp_path / "host", tmp_path / "run" / "gnupg"
        _build_host_gnupg(host)
        before = {p.relative_to(host): p.stat().st_mtime_ns for p in host.rglob("*")}
        _run(tmp_path, f"derive_container_gnupg_home {host} {derived}")
        after = {p.relative_to(host): p.stat().st_mtime_ns for p in host.rglob("*")}
        assert after == before, "the host GPG home must be treated as strictly read-only"

    def test_is_idempotent_across_restarts(self, tmp_path: Path) -> None:
        host, derived = tmp_path / "host", tmp_path / "run" / "gnupg"
        _build_host_gnupg(host)
        _run(tmp_path, f"derive_container_gnupg_home {host} {derived}")
        (derived / "stale-from-a-previous-boot").write_text("x", encoding="utf-8")
        _run(tmp_path, f"derive_container_gnupg_home {host} {derived}")
        assert not (derived / "stale-from-a-previous-boot").exists()
        assert (derived / "private-keys-v1.d" / "ABC123.key").exists()

    def test_an_empty_host_home_yields_an_empty_derived_home(self, tmp_path: Path) -> None:
        # Absence stays a no-op rather than becoming a NEW failure: gpg finds no
        # keys exactly as it did before, and init_preflight reports as it always did.
        host, derived = tmp_path / "host", tmp_path / "run" / "gnupg"
        host.mkdir()
        _run(tmp_path, f"derive_container_gnupg_home {host} {derived}")
        assert derived.is_dir()
        assert list(derived.iterdir()) == []


class TestResolveGnupgHome:
    def _resolve(self, tmp_path: Path, table: Path, gnupghome: str, runtime: Path) -> str:
        script = (
            f"export TEATREE_PROC_MOUNTS={table} TEATREE_GNUPG_RUNTIME_DIR={runtime} GNUPGHOME={gnupghome}\n"
            "resolve_gnupg_home >/dev/null\n"
            'printf "%s" "${GNUPGHOME:-<unset>}"'
        )
        return _run(tmp_path, script)

    def test_a_socket_capable_home_is_left_exactly_as_it_was(self, tmp_path: Path) -> None:
        # The box's behaviour must not change at all — same home, one shared agent.
        host = tmp_path / "host"
        _build_host_gnupg(host)
        table = _mount_table(tmp_path, [("/", "ext4")])
        assert self._resolve(tmp_path, table, str(host), tmp_path / "run") == str(host)
        assert not (tmp_path / "run").exists(), "nothing may be derived when the mount works in place"

    def test_a_sharing_transport_switches_to_the_container_local_copy(self, tmp_path: Path) -> None:
        host, runtime = tmp_path / "host", tmp_path / "run"
        _build_host_gnupg(host)
        table = _mount_table(tmp_path, [("/", "ext4"), (str(host), "fakeowner")])
        assert self._resolve(tmp_path, table, str(host), runtime) == str(runtime / "gnupg")
        assert (runtime / "gnupg" / "private-keys-v1.d" / "ABC123.key").read_bytes() == b"secret-key"

    def test_an_unset_gnupghome_is_a_no_op(self, tmp_path: Path) -> None:
        table = _mount_table(tmp_path, [("/", "ext4")])
        script = (
            f"export TEATREE_PROC_MOUNTS={table}\nunset GNUPGHOME\n"
            'resolve_gnupg_home\nprintf "%s" "${GNUPGHOME:-<unset>}"'
        )
        assert _run(tmp_path, script) == "<unset>"

    def test_a_missing_gnupghome_is_a_no_op(self, tmp_path: Path) -> None:
        # A box on the CLAUDE_CODE_OAUTH_TOKEN env path never provisions one.
        table = _mount_table(tmp_path, [("/", "ext4")])
        absent = str(tmp_path / "absent")
        assert self._resolve(tmp_path, table, absent, tmp_path / "run") == absent


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
