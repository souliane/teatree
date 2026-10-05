"""The shell seam resolves visibility on the forge the remote actually lives on.

The pre-push leak gate (:file:`scripts/hooks/refuse-public-push-with-leak.sh`)
used to shell ``gh repo view`` for EVERY remote. That hard-codes one forge: a
``gitlab.com`` remote made ``gh`` error out on the namespace, so visibility came
back undetermined on every single push and the gate fell into its fail-closed
branch -- scanning a PRIVATE repo, forever, on every push.

These tests drive the real CLI as a subprocess with real ``gh``/``glab`` shims on
``PATH`` (nothing about the routing is mocked) and pin:

- a GitLab remote routes to ``glab`` and resolves -- the regression that made the
    gate unable to classify its own remote,
- a GitHub remote still routes to ``gh`` -- the no-regression control,
- an unresolvable remote yields ``UNKNOWN``, the fail-safe the gate must read as
    "keep scanning" rather than "skip",
- the verdict is cached per slug, so a repeat push pays no probe at all.

The ``UNKNOWN`` row is the anti-vacuity guard: it proves a resolved verdict comes
from the probe actually answering, not from the CLI defaulting to something
permissive.
"""

import json
import os
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from teatree.hooks._repo_visibility import slug_for_remote_url

REPO_ROOT = Path(__file__).resolve().parents[2]

_GITLAB_REMOTE = "git@gitlab.com:acme-eng/inner/widget.git"
_GITHUB_REMOTE = "https://github.com/acme/widget.git"


def _seed_config_db(path: Path, rows: dict[str, object]) -> Path:
    """Write a ``teatree_config_setting`` store the Django-free cold reader can read."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS teatree_config_setting "
        "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
    )
    for key, value in rows.items():
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', ?, ?)", (key, json.dumps(value))
        )
    conn.commit()
    conn.close()
    return path


def _write_shim(bin_dir: Path, name: str, body: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / name
    shim.write_text(body, encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def _forge_shims(bin_dir: Path, *, gh_visibility: str = "PUBLIC", glab_visibility: str = "private") -> Path:
    """Real ``gh``/``glab`` shims that also RECORD which one was invoked."""
    log = bin_dir / "invocations.log"
    _write_shim(
        bin_dir,
        "gh",
        "#!/usr/bin/env bash\n"
        f'echo "gh $*" >> "{log}"\n'
        'if [[ "$*" == *"repo view"* && "$*" == *"visibility"* ]]; then\n'
        f'  echo "{gh_visibility}"\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
    )
    _write_shim(
        bin_dir,
        "glab",
        "#!/usr/bin/env bash\n"
        f'echo "glab $*" >> "{log}"\n'
        'if [[ "$*" == *"api"* && "$*" == *"projects/"* ]]; then\n'
        f'  echo \'{{"visibility":"{glab_visibility}"}}\'\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
    )
    return log


def _run_cli(
    remote: str,
    tmp_path: Path,
    bin_dir: Path,
    *,
    data_dir: Path | None = None,
    config_db: Path | None = None,
) -> str:
    result = _cli_process(remote, tmp_path, bin_dir, data_dir=data_dir, config_db=config_db)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def _cli_process(
    remote: str,
    tmp_path: Path,
    bin_dir: Path,
    *,
    data_dir: Path | None = None,
    config_db: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        # Isolate the day cache so a verdict never leaks between tests or in
        # from the developer's own machine.
        "T3_DATA_DIR": str(data_dir if data_dir is not None else tmp_path / "state"),
        # An EMPTY allowlist by default, so the developer's own `private_repos`
        # rows can never hand a probe row its verdict for free.
        "T3_CONFIG_DB": str(config_db if config_db is not None else _seed_config_db(tmp_path / "empty.db", {})),
        "PYTHONPATH": str(REPO_ROOT / "src"),
        # No host projection either, so an unreadable store cannot borrow the
        # developer's own published settings as its fallback.
        "XDG_DATA_HOME": str(tmp_path / "xdg"),
    }
    return subprocess.run(
        [sys.executable, "-m", "teatree.hooks.repo_visibility_cli", remote],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


class TestVisibilityRoutesToTheRemotesOwnForge:
    def test_gitlab_remote_resolves_via_glab(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        log = _forge_shims(bin_dir, glab_visibility="private")

        assert _run_cli(_GITLAB_REMOTE, tmp_path, bin_dir) == "PRIVATE"
        assert "glab" in log.read_text(encoding="utf-8")

    def test_gitlab_remote_never_asks_github(self, tmp_path: Path) -> None:
        """The exact defect: a GitLab remote sent to ``gh`` can never resolve."""
        bin_dir = tmp_path / "bin"
        log = _forge_shims(bin_dir, glab_visibility="private")

        _run_cli(_GITLAB_REMOTE, tmp_path, bin_dir)

        assert not any(line.startswith("gh ") for line in log.read_text(encoding="utf-8").splitlines())

    def test_github_remote_still_resolves_via_gh(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        log = _forge_shims(bin_dir, gh_visibility="PUBLIC")

        assert _run_cli(_GITHUB_REMOTE, tmp_path, bin_dir) == "PUBLIC"
        assert any(line.startswith("gh ") for line in log.read_text(encoding="utf-8").splitlines())

    @pytest.mark.parametrize(
        "remote",
        [
            "not-a-remote",
            "",
            "https://example.invalid/",
        ],
    )
    def test_unresolvable_remote_is_unknown(self, remote: str, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        _forge_shims(bin_dir)

        assert _run_cli(remote, tmp_path, bin_dir) == "UNKNOWN"

    def test_absent_forge_cli_is_unknown_not_public(self, tmp_path: Path) -> None:
        """No forge tool must yield the fail-safe UNKNOWN, never a permissive verdict."""
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()

        assert _run_cli(_GITLAB_REMOTE, tmp_path, empty_bin) == "UNKNOWN"


class TestVisibilityVerdictIsCachedPerRemote:
    def test_second_resolution_makes_no_probe(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        log = _forge_shims(bin_dir, glab_visibility="private")

        assert _run_cli(_GITLAB_REMOTE, tmp_path, bin_dir) == "PRIVATE"
        probes_after_first = len(log.read_text(encoding="utf-8").splitlines())
        assert _run_cli(_GITLAB_REMOTE, tmp_path, bin_dir) == "PRIVATE"

        assert len(log.read_text(encoding="utf-8").splitlines()) == probes_after_first

    def test_cache_is_keyed_by_host_qualified_slug(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        _forge_shims(bin_dir, glab_visibility="private")
        _run_cli(_GITLAB_REMOTE, tmp_path, bin_dir)

        cache = json.loads((tmp_path / "state" / "repo-visibility-cache.json").read_text(encoding="utf-8"))

        assert "gitlab.com/acme-eng/inner/widget" in cache


def _failing_forge_shims(bin_dir: Path) -> Path:
    """``gh``/``glab`` that are PRESENT but never answer — a timed-out probe's shape."""
    log = bin_dir / "invocations.log"
    for tool in ("gh", "glab"):
        _write_shim(bin_dir, tool, f'#!/usr/bin/env bash\necho "{tool} $*" >> "{log}"\nexit 1\n')
    return log


_ALLOWLIST = {"private_repos": ["gitlab.com/acme-eng"]}


class TestDeclaredPrivateNeedsNoNetwork:
    """A declared-private repo resolves PRIVATE offline, so no probe flake can move it.

    Every sibling consumer of this module — ``publish_surface``,
    ``publish_destination``, ``public_visibility``, ``author_trust`` — asks the
    offline ``private_repos`` allowlist BEFORE the network probe. This CLI did
    not: it went straight to cache-then-probe, so the verdict for a repo the
    operator had already declared private was decided by a 5s-budget forge call.
    On a loaded box that call measures well past its budget, and the pre-push
    leak gate read the resulting UNKNOWN as "assume public".
    """

    def test_allowlisted_remote_resolves_private_without_probing_at_all(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        log = _forge_shims(bin_dir, glab_visibility="private")
        config_db = _seed_config_db(tmp_path / "allow.db", _ALLOWLIST)

        verdict = _run_cli(_GITLAB_REMOTE, tmp_path, bin_dir, config_db=config_db)

        assert verdict == "PRIVATE"
        assert log.exists(), "a reachable PUBLIC answer must be able to override the declaration"

    def test_allowlisted_remote_resolves_private_when_the_probe_never_answers(self, tmp_path: Path) -> None:
        """The incident: the forge call exceeds its budget, and the repo is still private."""
        bin_dir = tmp_path / "bin"
        _failing_forge_shims(bin_dir)
        config_db = _seed_config_db(tmp_path / "allow.db", _ALLOWLIST)

        assert _run_cli(_GITLAB_REMOTE, tmp_path, bin_dir, config_db=config_db) == "PRIVATE"

    def test_a_repo_outside_the_allowlist_still_needs_the_probe(self, tmp_path: Path) -> None:
        """Anti-vacuity: the allowlist answers for its OWN namespace, nothing wider."""
        bin_dir = tmp_path / "bin"
        _failing_forge_shims(bin_dir)
        config_db = _seed_config_db(tmp_path / "allow.db", _ALLOWLIST)

        assert _run_cli(_GITHUB_REMOTE, tmp_path, bin_dir, config_db=config_db) == "UNKNOWN"


class TestVerdictDoesNotDependOnWhichCheckoutAsks:
    """One remote URL, one verdict — whichever worktree resolves it.

    The visibility cache lands under the data dir, and the data dir is
    AUTO-ISOLATED PER WORKTREE (``paths.resolve_data_dir``). So each worktree
    kept its own cache, each cache froze whatever the flaky probe happened to
    answer there, and the same URL resolved PRIVATE in one worktree and UNKNOWN
    in the next — the shape the operator hit. The two data dirs below are what
    two worktrees of one repo get.
    """

    def test_same_remote_resolves_the_same_when_the_probe_flakes_between_checkouts(self, tmp_path: Path) -> None:
        config_db = _seed_config_db(tmp_path / "allow.db", _ALLOWLIST)
        answering = tmp_path / "answering-bin"
        _forge_shims(answering, glab_visibility="private")
        silent = tmp_path / "silent-bin"
        _failing_forge_shims(silent)

        idle_box = _run_cli(_GITLAB_REMOTE, tmp_path, answering, data_dir=tmp_path / "wt-a", config_db=config_db)
        loaded_box = _run_cli(_GITLAB_REMOTE, tmp_path, silent, data_dir=tmp_path / "wt-b", config_db=config_db)

        assert idle_box == loaded_box == "PRIVATE"


def _corrupt_store(path: Path) -> Path:
    path.write_bytes(b"this is not an sqlite database" * 64)
    return path


def _empty_store(path: Path) -> Path:
    path.touch()
    return path


def _store_without_the_table(path: Path) -> Path:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE unrelated (x)")
    conn.commit()
    conn.close()
    return path


def _store_with_a_scalar_allowlist(path: Path) -> Path:
    return _seed_config_db(path, {"private_repos": "acme-eng"})


class TestAnUnreadableAllowlistIsNamedNotEmpty:
    """A ``private_repos`` store that could not be READ is reported as such, never as "none declared".

    The cold read fails open to an empty list, so a locked, corrupt or missing store
    made a declared-private remote resolve exactly like an undeclared one: UNKNOWN,
    and a refusal telling the operator to declare a repo that already was. The
    verdict stays fail-closed (``ALLOWLIST_UNREADABLE`` is enforced like UNKNOWN);
    it only stops lying about why.
    """

    @pytest.mark.parametrize(
        "store",
        [_corrupt_store, _empty_store, _store_without_the_table, _store_with_a_scalar_allowlist],
        ids=["corrupt", "zero-byte", "no-table", "scalar-value"],
    )
    def test_unreadable_store_with_no_probe_answer_is_named(
        self, store: Callable[[Path], Path], tmp_path: Path
    ) -> None:
        bin_dir = tmp_path / "bin"
        _failing_forge_shims(bin_dir)
        db = store(tmp_path / "config.sqlite3")

        result = _cli_process(_GITLAB_REMOTE, tmp_path, bin_dir, config_db=db)

        assert result.stdout.strip() == "ALLOWLIST_UNREADABLE", result.stdout + result.stderr
        assert "could not read private_repos" in result.stderr
        assert str(db) in result.stderr

    def test_a_store_locked_by_a_writer_is_named(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        _failing_forge_shims(bin_dir)
        db = _seed_config_db(tmp_path / "config.sqlite3", _ALLOWLIST)
        writer = sqlite3.connect(str(db), isolation_level=None)
        writer.execute("PRAGMA locking_mode=EXCLUSIVE")
        writer.execute("BEGIN EXCLUSIVE")
        try:
            result = _cli_process(_GITLAB_REMOTE, tmp_path, bin_dir, config_db=db)
        finally:
            writer.execute("ROLLBACK")
            writer.close()

        assert result.stdout.strip() == "ALLOWLIST_UNREADABLE", result.stdout + result.stderr
        assert "could not read private_repos" in result.stderr

    def test_no_store_at_all_is_named(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        _failing_forge_shims(bin_dir)
        missing = tmp_path / "nowhere" / "db.sqlite3"

        result = _cli_process(_GITLAB_REMOTE, tmp_path, bin_dir, config_db=missing)

        assert result.stdout.strip() == "ALLOWLIST_UNREADABLE", result.stdout + result.stderr
        assert f"no config DB at {missing}" in result.stderr

    def test_a_probe_that_answers_still_decides(self, tmp_path: Path) -> None:
        """The unreadable store only names an undecided verdict; a forge answer is still evidence."""
        bin_dir = tmp_path / "bin"
        _forge_shims(bin_dir, glab_visibility="private")

        result = _cli_process(_GITLAB_REMOTE, tmp_path, bin_dir, config_db=_corrupt_store(tmp_path / "c.sqlite3"))

        assert result.stdout.strip() == "PRIVATE", result.stdout + result.stderr

    def test_a_readable_store_without_the_repo_stays_unknown(self, tmp_path: Path) -> None:
        """Anti-vacuity: a store that was READ and simply does not list the repo is UNKNOWN."""
        bin_dir = tmp_path / "bin"
        _failing_forge_shims(bin_dir)
        db = _seed_config_db(tmp_path / "config.sqlite3", {"private_repos": ["gitlab.com/other-org"]})

        result = _cli_process(_GITLAB_REMOTE, tmp_path, bin_dir, config_db=db)

        assert result.stdout.strip() == "UNKNOWN", result.stdout + result.stderr
        assert "could not read" not in result.stderr


class TestUrlRemotesRouteOnTheirHostAlone:
    """A ``scheme://`` remote's userinfo and port are not part of the repo's identity.

    ``ssh://git@gitlab.com/...`` normalised to ``git@gitlab.com/...``, a host
    segment no forge route recognises, so a private GitLab remote spelled that way
    (or ``ssh://...:2222/...`` on a self-hosted forge, or a credentialed
    ``https://`` CI remote) never reached ``glab`` and stayed UNKNOWN, while the
    SCP spelling of the very same remote resolved PRIVATE.
    """

    @pytest.mark.parametrize(
        "remote",
        [
            "ssh://git@gitlab.com/acme-eng/inner/widget.git",
            "ssh://git@gitlab.example.org:2222/acme-eng/inner/widget.git",
            "https://oauth2:tok3n-placeholder@gitlab.com/acme-eng/inner/widget.git",  # privacy-scan:allow fixture
        ],
        ids=["ssh-url", "ssh-url-port", "https-userinfo"],
    )
    def test_private_gitlab_url_remote_resolves_via_glab(self, remote: str, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        log = _forge_shims(bin_dir, glab_visibility="private")

        assert _run_cli(remote, tmp_path, bin_dir) == "PRIVATE"
        assert "glab api projects/acme-eng%2Finner%2Fwidget" in log.read_text(encoding="utf-8")

    def test_a_credential_in_the_url_never_reaches_the_cache(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        _forge_shims(bin_dir, glab_visibility="private")

        _run_cli(
            "https://oauth2:tok3n-placeholder@gitlab.com/acme-eng/inner/widget.git", tmp_path, bin_dir
        )  # privacy-scan:allow fixture

        cache = (tmp_path / "state" / "repo-visibility-cache.json").read_text(encoding="utf-8")
        assert "tok3n-placeholder" not in cache
        assert "gitlab.com/acme-eng/inner/widget" in json.loads(cache)

    @pytest.mark.parametrize(
        "remote", ["file:///srv/git/widget.git", "ssh://[::1/widget.git"], ids=["no-host", "malformed"]
    )
    def test_a_url_without_a_parsable_host_keeps_its_verbatim_slug(self, remote: str) -> None:
        """Anti-vacuity: only a real network host is rewritten; anything else is left exactly as it was."""
        assert slug_for_remote_url(remote) == remote.split("://", 1)[1].removesuffix(".git")
