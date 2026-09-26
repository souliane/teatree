# test-path: cross-cutting — drives deploy/entrypoint.sh (no src mirror).
"""The deploy entrypoint SEEDS the one container-local GPG home every venue agrees on.

``GNUPGHOME`` is baked in the image at a FIXED container-local path, so the entrypoint
never has to redirect it — it only decides what that path is made OF:

* a socket-capable host home (the deployment box's ext4 bind mount) is ADOPTED in place
    through a symlink, so the services keep sharing ONE gpg-agent and the
    cached-passphrase setup ``deploy/README.md`` documents keeps working;
* a host home on a file-sharing transport (Docker Desktop for Mac reports ``fakeowner``,
    which cannot host the ``S.*`` sockets gpg-agent and keyboxd bind) is COPIED into the
    tmpfs, where a socket binds normally.

Either way the host's GPG home is strictly READ-ONLY: it is the SOURCE, never
``GNUPGHOME``, and the switch is decided from the mount table so not even the detection
touches the directory. ``tests/test_deploy_gnupg_lock_isolation.py`` pins the invariant
this shape exists for — that no container process ever opens the host keybox.

Runs the REAL shell functions (extracted verbatim from the entrypoint) in a bash
subprocess against real files under ``tmp_path``, mirroring the sibling
entrypoint tests (``test_deploy_entrypoint_disk_tmpdir.py``).
"""

import os
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

_FUNCTIONS = (
    "path_fstype",
    "fstype_hosts_unix_sockets",
    "same_directory",
    "clear_container_gnupg_home",
    "derive_container_gnupg_home",
    "seed_container_gnupg_home",
)

#: The entrypoint sets this at TOP level, outside any function, so the harness has to
#: carry it too — extracted verbatim rather than restated, since a test that spells the
#: path itself would go on passing after the entrypoint moved it.
_TOP_LEVEL_ASSIGNMENTS = ("CONTAINER_GNUPG_HOME",)


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


def _extract_assignment(name: str) -> str:
    """Return the verbatim top-level ``name=...`` line from the entrypoint."""
    for line in ENTRYPOINT.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}="):
            return line
    not_found = f"assignment {name!r} not found in {ENTRYPOINT}"
    raise AssertionError(not_found)


def _run(tmp_path: Path, script: str, env: dict[str, str] | None = None) -> str:
    """Run *script* with the entrypoint's real GNUPGHOME functions in scope.

    *env* is layered onto the process environment rather than exported inside the
    script, because ``CONTAINER_GNUPG_HOME`` is a top-level assignment the entrypoint
    evaluates ONCE at source time — the container supplies its inputs from outside, and
    an in-script export would land after the assignment had already resolved.
    """
    harness = tmp_path / "harness.sh"
    preamble = [_extract_assignment(name) for name in _TOP_LEVEL_ASSIGNMENTS]
    preamble += [_extract_shell_function(name) for name in _FUNCTIONS]
    harness.write_text("set -euo pipefail\n" + "\n".join(preamble) + f"\n{script}\n", encoding="utf-8")
    proc = subprocess.run(
        [_BASH, str(harness)], capture_output=True, text=True, check=True, env={**os.environ, **(env or {})}
    )
    return proc.stdout.strip()


def _mount_table(tmp_path: Path, entries: list[tuple[str, str]]) -> Path:
    """A fixture /proc/mounts carrying ``(mount point, fstype)`` rows."""
    table = tmp_path / "mounts"
    table.parent.mkdir(parents=True, exist_ok=True)
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


class TestSeedContainerGnupgHome:
    """The seed decides what the FIXED container home is made of — never where it is."""

    def _seed(self, tmp_path: Path, table: Path, host_home: str, runtime: Path) -> str:
        return _run(
            tmp_path,
            'seed_container_gnupg_home >/dev/null\nprintf "%s" "${GNUPGHOME:-<unset>}"',
            env={
                "TEATREE_PROC_MOUNTS": str(table),
                "TEATREE_GNUPG_RUNTIME_DIR": str(runtime),
                "TEATREE_HOST_GNUPG_DIR": host_home,
                "GNUPGHOME": "/home/teatree/.gnupg",
            },
        )

    def test_gnupghome_is_the_container_path_on_a_socket_capable_host(self, tmp_path: Path) -> None:
        # The box. GNUPGHOME is the container path even here — that is the whole point:
        # one value every venue agrees on, so a `docker exec` cannot resolve a different
        # one and open the host keybox behind the role's back.
        host, runtime = tmp_path / "host", tmp_path / "run"
        _build_host_gnupg(host)
        assert self._seed(tmp_path, _mount_table(tmp_path, [("/", "ext4")]), str(host), runtime) == str(
            runtime / "gnupg"
        )

    def test_a_socket_capable_host_home_is_adopted_in_place_by_symlink(self, tmp_path: Path) -> None:
        # Adopted, not copied: one shared home is what gives the services ONE gpg-agent,
        # which is what makes the cached-passphrase setup work at all.
        host, runtime = tmp_path / "host", tmp_path / "run"
        _build_host_gnupg(host)
        self._seed(tmp_path, _mount_table(tmp_path, [("/", "ext4")]), str(host), runtime)
        derived = runtime / "gnupg"
        assert derived.is_symlink()
        assert derived.resolve() == host.resolve()

    def test_a_sharing_transport_gets_a_real_container_local_copy(self, tmp_path: Path) -> None:
        # The laptop. A COPY, so the container's keyboxd binds its sockets on the tmpfs
        # and never takes a dotlock on the keybox the host's own keyboxd holds.
        host, runtime = tmp_path / "host", tmp_path / "run"
        _build_host_gnupg(host)
        table = _mount_table(tmp_path, [("/", "ext4"), (str(host), "fakeowner")])
        assert self._seed(tmp_path, table, str(host), runtime) == str(runtime / "gnupg")
        derived = runtime / "gnupg"
        assert not derived.is_symlink()
        assert (derived / "private-keys-v1.d" / "ABC123.key").read_bytes() == b"secret-key"

    def test_the_host_home_is_never_written_to_on_either_path(self, tmp_path: Path) -> None:
        for fstype in ("ext4", "fakeowner"):
            host, runtime = tmp_path / f"host-{fstype}", tmp_path / f"run-{fstype}"
            _build_host_gnupg(host)
            before = {q.relative_to(host): q.stat().st_mtime_ns for q in host.rglob("*")}
            table = _mount_table(tmp_path / fstype, [("/", "ext4"), (str(host), fstype)])
            self._seed(tmp_path, table, str(host), runtime)
            after = {q.relative_to(host): q.stat().st_mtime_ns for q in host.rglob("*")}
            assert after == before, f"the host GPG home must stay read-only on the {fstype} path"

    def test_an_absent_host_home_still_lands_gnupghome_on_the_container_path(self, tmp_path: Path) -> None:
        # A box on the CLAUDE_CODE_OAUTH_TOKEN env path never provisions one. Absence
        # stays a no-op — but it must NOT fall back to the host path, or the one venue
        # with no keys becomes the one venue that opens the shared keybox.
        runtime = tmp_path / "run"
        absent = str(tmp_path / "absent")
        table = _mount_table(tmp_path, [("/", "ext4")])
        assert self._seed(tmp_path, table, absent, runtime) == str(runtime / "gnupg")
        assert not (runtime / "gnupg").exists()

    def test_switching_between_the_two_shapes_is_idempotent(self, tmp_path: Path) -> None:
        # A restart after the host mount changed character must not leave a symlink
        # standing where a copy belongs, or a stale copy shadowing an adopted home.
        host, runtime = tmp_path / "host", tmp_path / "run"
        _build_host_gnupg(host)
        shared = _mount_table(tmp_path / "a", [("/", "ext4"), (str(host), "fakeowner")])
        local = _mount_table(tmp_path / "b", [("/", "ext4")])
        derived = runtime / "gnupg"

        self._seed(tmp_path, shared, str(host), runtime)
        assert not derived.is_symlink()
        self._seed(tmp_path, local, str(host), runtime)
        assert derived.is_symlink()
        self._seed(tmp_path, shared, str(host), runtime)
        assert not derived.is_symlink()
        assert (derived / "private-keys-v1.d" / "ABC123.key").read_bytes() == b"secret-key"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
