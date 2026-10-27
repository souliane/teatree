"""The probe program write-probes every mount it does not own, against a /proc laid out under tmp_path."""

import io
import json
import os
import shlex
import shutil
import subprocess
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.agents import codex_read_only_executor, codex_read_only_probe_program
from teatree.agents.codex_read_only_probe_program import (
    KEY_READ_BYTES,
    ProbeRequest,
    git_readable,
    observe,
    parse_mountinfo,
    write_result,
)
from teatree.agents.codex_read_only_verdict import WORKTREE_ROOT, codex_source_git_subpath, evaluate
from teatree.core.worktree.worktree_env import CACHE_DIRNAME, CACHE_FILENAME
from tests.teatree_agents._executor_fakes import EXAMPLE_ROOT, executor_fixture, probe_fixture, worker_fixture

_STATUS = "Name:\tpython3\nCapEff:\t0000000000000000\nNoNewPrivs:\t1\n"


def _fake_proc(tmp_path: Path, mounts: list[tuple[str, str]], pid1_environ: bytes = b"PATH=/usr/bin\0") -> Path:
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "1").mkdir()
    lines = [
        f"{index} 1 0:{index} / {target} rw,relatime - {fstype} src{index} rw"
        for index, (target, fstype) in enumerate(mounts)
    ]
    (proc / "self" / "mountinfo").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (proc / "self" / "status").write_text(_STATUS, encoding="utf-8")
    (proc / "1" / "environ").write_bytes(pid1_environ)
    (proc / "1" / "stat").write_text("1 (tini) S 0 1 1 0", encoding="utf-8")
    (proc / "1" / "cmdline").write_bytes(b"/usr/bin/tini\0--\0sleep\0")
    return proc


def _request(tmp_path: Path, roots: tuple[str, ...] = (), home: str = "") -> ProbeRequest:
    return ProbeRequest(
        kernel_roots=[str(tmp_path / "proc")],
        device_root=str(tmp_path / "dev"),
        tmpfs=[str(tmp_path / "scratch")],
        roots=list(roots),
        git_mounts=[],
        home=home,
        tools=["gh"],
    )


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TEATREE_CODEX_SOURCE_GIT_SUBPATH", codex_source_git_subpath())
    for name in ("root", "ws", "scratch", "dev"):
        (tmp_path / name).mkdir()
    return tmp_path


def test_tmpfs_backed_bind_at_an_allowed_target_is_write_probed_and_refused(layout: Path) -> None:
    proc = _fake_proc(layout, [(str(layout / "root"), "ext4"), (str(layout / "ws"), "tmpfs")])

    observed = observe(_request(layout), proc)
    answer = probe_fixture("pass")
    answer["writes"][WORKTREE_ROOT] = observed["writes"][str(layout / "ws")]

    assert (
        evaluate(answer, executor_fixture("pass"), worker_fixture(), (EXAMPLE_ROOT,))
        == f"{WORKTREE_ROOT} accepted a write"
    )


def test_kernel_device_and_declared_tmpfs_mounts_are_never_write_probed(layout: Path) -> None:
    mounts = [
        (str(layout / "root"), "ext4"),
        (str(layout / "proc" / "kcore"), "tmpfs"),
        (str(layout / "dev"), "tmpfs"),
        (str(layout / "scratch"), "tmpfs"),
    ]

    observed = observe(_request(layout), _fake_proc(layout, mounts))

    assert set(observed["writes"]) == {str(layout / "root")}
    assert observed["controls"] == {str(layout / "scratch"): "OK"}
    assert list((layout / "scratch").iterdir()) == []


def test_pid1_environ_is_read_from_proc_not_from_the_probes_own_env(layout: Path) -> None:
    proc = _fake_proc(layout, [(str(layout / "root"), "ext4")], b"PATH=/usr/bin\0PASSWORD_STORE_DIR=/srv/store\0")

    with patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
        observed = observe(_request(layout), proc)
    answer = probe_fixture("pass")
    answer["env"], answer["pid1_env"] = observed["env"], observed["pid1_env"]

    assert observed["pid1_env"] == ["PASSWORD_STORE_DIR", "PATH"]
    assert (
        evaluate(answer, executor_fixture("pass"), worker_fixture(), (EXAMPLE_ROOT,))
        == "its environment carries PASSWORD_STORE_DIR"
    )


def test_a_tool_on_the_exec_path_is_reported(layout: Path) -> None:
    tool = layout / "bin" / "gh"
    tool.parent.mkdir()
    tool.write_text("#!/bin/sh\n", encoding="utf-8")

    with patch.dict(os.environ, {"PATH": str(tool.parent)}, clear=True):
        observed = observe(_request(layout), _fake_proc(layout, [(str(layout / "root"), "ext4")]))

    assert observed["tools"] == {"gh": True}


_PLACEHOLDER = "EXAMPLE_TOKEN=placeholder\n"
_SECRETS = (
    ".netrc",
    "app/.env",
    "app/.env-local",
    "app/.env.local",
    "app/.envrc",
    "app/.git-credentials",
    "app/.npmrc",
    "app/.pgpass",
    "app/.pypirc",
    "app/deploy/teatree.env",
    "app/linked.env",
    "keys/id_ed25519",
    "vendor/node_modules/pkg/.env",
)
_PRIVATE_KEY = "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nplaceholder\n"
_CERTIFICATE = "-----BEGIN " + "CERTIFICATE-----\nplaceholder\n"


def _plant(root: Path, *relatives: str, text: str = _PLACEHOLDER) -> None:
    for relative in relatives:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_every_readable_env_or_credential_file_under_a_mount_is_reported(layout: Path) -> None:
    root = layout / "root"
    _plant(root, *(relative for relative in _SECRETS if relative != "app/linked.env"))
    _plant(root, "app/.env.example", "app/.t3-env.cache", "app/notes.env.md", "keys/id_ed25519.pub")
    _plant(root, "app/empty.env", text="")
    _plant(root, "app/locked.env")
    (root / "app" / "locked.env").chmod(0)
    (root / "app" / "linked.env").symlink_to(root / "app" / ".env")

    observed = observe(_request(layout), _fake_proc(layout, [(str(root), "ext4")]))

    assert observed["uid"] == os.getuid()
    assert observed["scan"] == {
        str(root): {"owner": os.getuid(), "secrets": sorted(str(root / relative) for relative in _SECRETS)}
    }


def test_git_config_with_embedded_http_credential_is_a_secret(layout: Path) -> None:
    git = layout / "root" / ".git"
    git.mkdir()
    config = git / "config"
    config.write_text('[remote "origin"]\n    url = https://token@example.test/org/repo.git\n', encoding="utf-8")
    proc = _fake_proc(layout, [(str(layout / "root"), "ext4")])

    observed = observe(_request(layout), proc)

    assert str(config) in observed["scan"][str(layout / "root")]["secrets"]

    config.write_text('[remote "origin"]\n    url = https://example.test/org/repo.git\n', encoding="utf-8")
    assert str(config) not in observe(_request(layout), proc)["scan"][str(layout / "root")]["secrets"]

    config.write_text("x" * (KEY_READ_BYTES + 1), encoding="utf-8")
    assert str(config) in observe(_request(layout), proc)["scan"][str(layout / "root")]["secrets"]

    config.write_text("[http]\n    extraheader = Authorization: Bearer placeholder\n", encoding="utf-8")
    assert str(config) in observe(_request(layout), proc)["scan"][str(layout / "root")]["secrets"]

    config.write_text(
        '[remote "origin"]\n    url = https://token\\\n      @example.test/org/repo.git\n', encoding="utf-8"
    )
    assert str(config) in observe(_request(layout), proc)["scan"][str(layout / "root")]["secrets"]


def test_git_config_on_a_neutral_named_declared_mount_is_a_secret(layout: Path) -> None:
    git = layout / "root" / "source-metadata"
    git.mkdir()
    config = git / "config"
    config.write_text("[http]\n    extraheader = Authorization: Bearer placeholder\n", encoding="utf-8")
    request = _request(layout)
    request["git_mounts"] = [str(git)]
    proc = _fake_proc(layout, [(str(layout / "root"), "ext4"), (str(git), "ext4")])

    assert str(config) in observe(request, proc)["scan"][str(git)]["secrets"]

    config.write_text("[core]\n    repositoryformatversion = 0\n", encoding="utf-8")
    assert str(config) not in observe(request, proc)["scan"][str(git)]["secrets"]


def test_a_readable_private_key_is_found_by_its_content_under_any_key_name(layout: Path) -> None:
    root = layout / "root"
    _plant(root, "deploy/server.pem", "deploy/tls.key", "keys/id_deploy", text=_PRIVATE_KEY)
    _plant(root, "deploy/chain.pem", text=_CERTIFICATE)
    _plant(root, "deploy/notes.key", text="placeholder notes\n")
    _plant(root, "keys/id_deploy.pub", text="ssh-ed25519 placeholder\n")
    _plant(root, "src/leak_patterns.py", text=f"PATTERN = {_PRIVATE_KEY!r}\n")
    _plant(root, "deploy/locked.pem", text=_PRIVATE_KEY)
    (root / "deploy" / "locked.pem").chmod(0)

    observed = observe(_request(layout), _fake_proc(layout, [(str(root), "ext4")]))

    assert observed["scan"][str(root)]["secrets"] == sorted(
        str(root / relative) for relative in ("deploy/server.pem", "deploy/tls.key", "keys/id_deploy")
    )


def test_a_nested_mount_is_scanned_once_under_its_own_target(layout: Path) -> None:
    root, nested = layout / "root", layout / "root" / "ws"
    _plant(nested, ".env")
    (root / "alias").symlink_to(nested)

    observed = observe(_request(layout), _fake_proc(layout, [(str(root), "ext4"), (str(nested), "ext4")]))

    assert observed["scan"][str(root)]["secrets"] == []
    assert observed["scan"][str(nested)]["secrets"] == [str(nested / ".env")]


def test_a_secret_in_a_directory_that_cannot_be_listed_is_still_found(layout: Path) -> None:
    hidden = layout / "root" / "hidden"
    _plant(hidden, ".env")
    hidden.chmod(0o111)
    try:
        observed = observe(_request(layout), _fake_proc(layout, [(str(layout / "root"), "ext4")]))
    finally:
        hidden.chmod(0o755)

    assert observed["scan"][str(layout / "root")]["secrets"] == [str(hidden / ".env")]


def test_a_planted_placeholder_env_file_refuses_the_executor_and_names_the_fix(layout: Path) -> None:
    worktrees = layout / "ws"
    _plant(worktrees, "example/teatree/.env")
    observed = observe(_request(layout), _fake_proc(layout, [(str(worktrees), "ext4")]))
    answer = probe_fixture("pass")
    answer["scan"][WORKTREE_ROOT] = observed["scan"][str(worktrees)]
    secret = f"{worktrees}/example/teatree/.env"

    assert evaluate(answer, executor_fixture("pass"), worker_fixture(), (EXAMPLE_ROOT,)) == (
        f"it can read {secret}, a secret file under {WORKTREE_ROOT}; "
        f"fix: chmod o-rwx {shlex.quote(secret)} in the worker, or move it out of {WORKTREE_ROOT}"
    )


def test_the_fix_quotes_a_path_with_a_space_and_never_says_to_delete(layout: Path) -> None:
    worktrees = layout / "ws"
    _plant(worktrees, "my project/.env")
    observed = observe(_request(layout), _fake_proc(layout, [(str(worktrees), "ext4")]))
    answer = probe_fixture("pass")
    answer["scan"][WORKTREE_ROOT] = observed["scan"][str(worktrees)]

    refusal = evaluate(answer, executor_fixture("pass"), worker_fixture(), (EXAMPLE_ROOT,))

    assert refusal is not None
    assert f"chmod o-rwx '{worktrees}/my project/.env' in the worker" in refusal
    assert "delete" not in refusal


_POINTER = f"dotenv ../{CACHE_DIRNAME}/app/{CACHE_FILENAME}\n"


def _scanned_secrets(layout: Path) -> list[str]:
    observed = observe(_request(layout), _fake_proc(layout, [(str(layout / "root"), "ext4")]))
    return observed["scan"][str(layout / "root")]["secrets"]


def _assert_refused_naming(layout: Path, *files: Path) -> None:
    observed = observe(_request(layout), _fake_proc(layout, [(str(layout / "root"), "ext4")]))
    answer = probe_fixture("pass")
    answer["scan"][WORKTREE_ROOT] = observed["scan"][str(layout / "root")]

    refusal = evaluate(answer, executor_fixture("pass"), worker_fixture(), (EXAMPLE_ROOT,))

    assert observed["scan"][str(layout / "root")]["secrets"] == [str(file) for file in files]
    assert refusal is not None
    assert str(files[0]) in refusal


@pytest.mark.parametrize(
    "content",
    [
        _POINTER,
        f"\n{_POINTER}\n\n",
        _POINTER.replace("\n", "\r\n"),
        f"{_POINTER}dotenv ../{CACHE_DIRNAME}/other/{CACHE_FILENAME}\n",
        f"  \t{_POINTER}",
    ],
    ids=["bare", "blank-lines", "crlf", "two-pointers", "indented"],
)
def test_a_bare_dotenv_pointer_envrc_does_not_refuse_the_executor(layout: Path, content: str) -> None:
    worktrees = layout / "ws"
    _plant(worktrees, "example/teatree/.envrc", text=content)
    observed = observe(_request(layout), _fake_proc(layout, [(str(worktrees), "ext4")]))
    answer = probe_fixture("pass")
    answer["scan"][WORKTREE_ROOT] = observed["scan"][str(worktrees)]

    assert observed["scan"][str(worktrees)]["secrets"] == []
    assert evaluate(answer, executor_fixture("pass"), worker_fixture(), (EXAMPLE_ROOT,)) is None


def test_a_bare_dotenv_pointer_envrc_in_a_directory_that_cannot_be_listed_is_not_a_secret(layout: Path) -> None:
    pointer_dir, secret_dir = layout / "root" / "pointer", layout / "root" / "secret"
    _plant(pointer_dir, ".envrc", text=_POINTER)
    _plant(secret_dir, ".envrc")
    pointer_dir.chmod(0o111)
    secret_dir.chmod(0o111)
    try:
        secrets = _scanned_secrets(layout)
    finally:
        pointer_dir.chmod(0o755)
        secret_dir.chmod(0o755)

    assert secrets == [str(secret_dir / ".envrc")]


@pytest.mark.parametrize(
    "content",
    [
        "EXAMPLE_TOKEN=placeholder\n",
        "export EXAMPLE_TOKEN=placeholder\n",
        f"{_POINTER}export EXAMPLE_TOKEN=placeholder\n",
        f"export EXAMPLE_TOKEN=placeholder\n{_POINTER}",
        "dotenv /srv/app/.t3-env.cache\n",
        "dotenv ~/.t3-env.cache\n",
        "dotenv $HOME/.t3-env.cache\n",
        "dotenv ../app/.t3-env.cache; export EXAMPLE_TOKEN=placeholder\n",
        "dotenv ../app/`id`\n",
        "dotenv ../app/a ../app/b\n",
        "dotenv ../app/a \\\n  ../app/b\n",
        "dotenv_if_exists ../app/.t3-env.cache\n",
        "# dotenv ../app/.t3-env.cache\n",
        "dotenv\n",
        "  \n",
        _POINTER + "\n" * (KEY_READ_BYTES + 1) + "export EXAMPLE_TOKEN=placeholder\n",
        "dotenv ..\n",
        "dotenv ../../../../etc/x\n",
        "dotenv prod.vars\n",
        f"dotenv ../{CACHE_DIRNAME}/../x/{CACHE_FILENAME}\n",
        f"dotenv ../{CACHE_DIRNAME}/../{CACHE_FILENAME}\n",
        f"dotenv ../{CACHE_DIRNAME}/./{CACHE_FILENAME}\n",
        f"dotenv ../{CACHE_DIRNAME}/app/sub/{CACHE_FILENAME}\n",
        f"dotenv ./{CACHE_DIRNAME}/app/{CACHE_FILENAME}\n",
        f"dotenv ../{CACHE_DIRNAME}/app/{CACHE_FILENAME}.bak\n",
        f"dotenv ../{CACHE_DIRNAME}/app/other.cache\n",
    ],
    ids=[
        "assignment",
        "export",
        "pointer-then-export",
        "export-then-pointer",
        "absolute-path",
        "home-path",
        "expanded-path",
        "chained-command",
        "command-substitution",
        "two-arguments",
        "continuation",
        "other-directive",
        "comment",
        "no-path",
        "whitespace-only",
        "pointer-then-padding-then-export",
        "parent-directory",
        "etc-traversal",
        "bare-file",
        "cache-dir-traversal-segment",
        "cache-dir-traversal-name",
        "cache-dir-dot-segment",
        "two-segments",
        "not-the-cache-dir",
        "cache-name-suffix",
        "other-cache-name",
    ],
)
def test_an_envrc_with_any_other_content_still_refuses_the_executor(layout: Path, content: str) -> None:
    _plant(layout / "root", "app/.envrc", text=content)

    _assert_refused_naming(layout, layout / "root" / "app" / ".envrc")


def test_a_pointer_at_a_readable_file_that_holds_values_still_refuses_the_executor(layout: Path) -> None:
    _plant(layout / "root", "app/.envrc", text="dotenv prod.vars\n")
    _plant(layout / "root", "app/prod.vars", text="EXAMPLE_TOKEN=placeholder\n")

    _assert_refused_naming(layout, layout / "root" / "app" / ".envrc")


def test_a_pointer_in_any_other_secret_file_is_still_a_secret(layout: Path) -> None:
    _plant(layout / "root", "app/.env", "app/.netrc", text=_POINTER)

    _assert_refused_naming(layout, layout / "root" / "app" / ".env", layout / "root" / "app" / ".netrc")


def test_requested_roots_and_the_live_home_are_reported(layout: Path) -> None:
    home = layout / "home"
    (home / "rules").mkdir(parents=True)
    (home / "rules" / "planted.rules").write_text("", encoding="utf-8")
    (home / "config.toml").write_text("", encoding="utf-8")

    observed = observe(
        _request(layout, roots=(str(layout / "ws"), str(layout / "absent")), home=str(home)),
        _fake_proc(layout, [(str(layout / "root"), "ext4")]),
    )

    assert observed["roots"] == {
        str(layout / "ws"): {"exists": True, "is_dir": True, "write": "OK", "git": f"{layout / 'ws' / '.git'}: ENOENT"},
        str(layout / "absent"): {
            "exists": False,
            "is_dir": False,
            "write": "ENOENT",
            "git": f"{layout / 'absent' / '.git'}: ENOENT",
        },
    }
    assert observed["home"] == {"rules": ["planted.rules"], "config": True, "plugins": False}


def _git(*args: str) -> None:
    git = shutil.which("git")
    assert git is not None
    subprocess.run([git, "-c", "user.name=Example", "-c", "user.email=example@example.invalid", *args], check=True)


def _clone_with_worktree(layout: Path) -> tuple[Path, Path]:
    clone, worktree = layout / "root" / "clone", layout / "ws" / "ticket"
    _git("init", "-q", "-b", "main", str(clone))
    _plant(clone, "README")
    _git("-C", str(clone), "add", "README")
    _git("-C", str(clone), "commit", "-qm", "init")
    _git("-C", str(clone), "pack-refs", "--all")
    _git("-C", str(clone), "worktree", "add", "-q", str(worktree))
    return clone, worktree


def test_a_root_with_private_git_data_names_the_path(layout: Path) -> None:
    clone, worktree = _clone_with_worktree(layout)
    teatree_file = clone / ".git" / "worktrees" / "ticket" / "t3" / "verify-gates.json"
    _plant(teatree_file.parent, teatree_file.name)
    teatree_file.chmod(0)

    observed = observe(_request(layout, roots=(str(worktree),)), _fake_proc(layout, [(str(layout / "root"), "ext4")]))

    assert str(teatree_file) in observed["roots"][str(worktree)]["git"]


def test_owner_only_git_data_of_a_worktrees_clone_is_named(layout: Path) -> None:
    clone, worktree = _clone_with_worktree(layout)
    packed_refs = clone / ".git" / "packed-refs"
    packed_refs.chmod(0)

    observed = observe(_request(layout, roots=(str(worktree),)), _fake_proc(layout, [(str(layout / "root"), "ext4")]))

    assert str(packed_refs) in observed["roots"][str(worktree)]["git"]


def test_git_readability_reports_every_restrictive_path_and_repair(layout: Path) -> None:
    clone, worktree = _clone_with_worktree(layout)
    packed_refs = clone / ".git" / "packed-refs"
    packed_refs.chmod(0o600)
    graph = clone / ".git" / "objects" / "info" / "commit-graph"
    graph.write_bytes(b"graph")
    graph.chmod(0o600)
    refs = clone / ".git" / "refs" / "remotes" / "origin"
    refs.mkdir(parents=True)
    refs.chmod(0o700)

    answer = git_readable(str(worktree))

    assert all(str(path) in answer for path in (packed_refs, graph, refs))
    assert "chmod" in answer


def test_worker_repairs_recreated_git_metadata_before_the_next_probe(layout: Path) -> None:
    clone, worktree = _clone_with_worktree(layout)
    _git("-C", str(clone), "pack-refs", "--all")
    assert not (clone / ".git" / "refs" / "heads" / "ticket").exists()
    gitdir = clone / ".git"
    packed_refs = gitdir / "packed-refs"
    graph = gitdir / "objects" / "info" / "commit-graph"
    refs = gitdir / "refs" / "remotes" / "origin"
    graph.write_bytes(b"graph")
    refs.mkdir(parents=True)

    for _ in range(2):
        packed_refs.chmod(0o600)
        graph.chmod(0o600)
        refs.chmod(0o700)
        assert git_readable(str(worktree)) != "OK"

        codex_read_only_executor.repair_git_metadata(worktree, allowed_roots=(clone.parent, worktree.parent))

        assert git_readable(str(worktree)) == "OK"
        git = shutil.which("git")
        assert git is not None
        assert subprocess.run([git, "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True, check=True).stdout


def test_packed_only_worktree_branch_resolves_as_uid_65534_after_repair(layout: Path) -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker is unavailable")
    image = "teatree-ro-executor:local"
    available = subprocess.run([docker, "image", "inspect", image], capture_output=True, check=False)
    if available.returncode != 0:
        pytest.skip("read-only executor image is unavailable")

    clone, worktree = _clone_with_worktree(layout)
    _git("-C", str(clone), "pack-refs", "--all")
    assert not (clone / ".git" / "refs" / "heads" / "ticket").exists()
    packed_refs = clone / ".git" / "packed-refs"
    packed_refs.chmod(0o600)
    layout.chmod(0o755)
    probe_file = layout / "probe_program.py"
    probe_file.write_text(codex_read_only_executor._PROBE_PROGRAM, encoding="utf-8")
    probe_code = (
        "import sys; ns={'__name__': 'git_probe'}; "
        "exec(compile(open(sys.argv[1]).read(), 'git_probe', 'exec'), ns); "
        "print(ns['git_readable'](sys.argv[2]))"
    )

    def in_executor(command: list[str]) -> subprocess.CompletedProcess[bytes]:
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as bundle:
            bundle.add(layout, arcname=str(layout).lstrip("/"))
        return subprocess.run(
            [
                docker,
                "run",
                "--rm",
                "--pull=never",
                "--interactive",
                "--user",
                "0:0",
                "--entrypoint",
                "sh",
                image,
                "-c",
                'tar -xf - -C / && exec setpriv --reuid=65534 --regid=65534 --clear-groups "$@"',
                "sh",
                *command,
            ],
            input=archive.getvalue(),
            capture_output=True,
            check=False,
            timeout=30,
        )

    def probe() -> subprocess.CompletedProcess[bytes]:
        return in_executor(["python3", "-c", probe_code, str(probe_file), str(worktree)])

    def head() -> subprocess.CompletedProcess[bytes]:
        return in_executor(["git", "-c", "safe.directory=*", "-C", str(worktree), "rev-parse", "HEAD"])

    before = probe()
    assert before.returncode == 0, before.stderr
    assert str(packed_refs).encode() in before.stdout
    assert head().returncode != 0

    codex_read_only_executor.repair_git_metadata(worktree, allowed_roots=(clone.parent, worktree.parent))

    after = probe()
    assert after.returncode == 0, after.stderr
    assert after.stdout.strip() == b"OK"
    git = shutil.which("git")
    assert git is not None
    expected = subprocess.run(
        [git, "-C", str(worktree), "rev-parse", "HEAD"], text=True, capture_output=True, check=True
    )
    resolved = head()
    assert resolved.returncode == 0, resolved.stderr
    assert resolved.stdout.strip() == expected.stdout.strip().encode()


def test_repair_skips_chmod_on_already_readable_directories(layout: Path) -> None:
    clone, worktree = _clone_with_worktree(layout)
    original_chmod = Path.chmod

    def chmod(path: Path, mode: int, *args: object, **kwargs: object) -> None:
        if path.is_dir() and path.is_relative_to(clone / ".git"):
            msg = "readable directory is owned by another user"
            raise PermissionError(msg)
        original_chmod(path, mode, *args, **kwargs)

    with patch.object(Path, "chmod", chmod):
        codex_read_only_executor.repair_git_metadata(worktree, allowed_roots=(clone.parent, worktree.parent))

    assert git_readable(str(worktree)) == "OK"


def test_repair_accepts_a_permission_race_when_the_file_became_readable(layout: Path) -> None:
    clone, worktree = _clone_with_worktree(layout)
    packed_refs = clone / ".git" / "packed-refs"
    packed_refs.chmod(0o600)
    original_chmod = Path.chmod

    def chmod(path: Path, mode: int, *args: object, **kwargs: object) -> None:
        original_chmod(path, mode, *args, **kwargs)
        if path == packed_refs:
            msg = "another writer already made it readable"
            raise PermissionError(msg)

    with patch.object(Path, "chmod", chmod):
        codex_read_only_executor.repair_git_metadata(worktree, allowed_roots=(clone.parent, worktree.parent))

    assert git_readable(str(worktree)) == "OK"


def test_repair_ignores_a_file_pruned_after_directory_listing(layout: Path) -> None:
    clone, worktree = _clone_with_worktree(layout)
    vanished = clone / ".git" / "packed-refs"
    original_lstat = Path.lstat

    def lstat(path: Path, *args: object, **kwargs: object) -> os.stat_result:
        if path == vanished:
            raise FileNotFoundError(str(path))
        return original_lstat(path, *args, **kwargs)

    with patch.object(Path, "lstat", lstat):
        codex_read_only_executor.repair_git_metadata(worktree, allowed_roots=(clone.parent, worktree.parent))

    assert git_readable(str(worktree)) == "OK"


def test_probe_ignores_a_file_pruned_after_directory_listing(layout: Path) -> None:
    clone, worktree = _clone_with_worktree(layout)
    vanished = clone / ".git" / "packed-refs"
    original_lstat = Path.lstat

    def lstat(path: Path, *args: object, **kwargs: object) -> os.stat_result:
        if path == vanished:
            raise FileNotFoundError(str(path))
        return original_lstat(path, *args, **kwargs)

    with patch.object(Path, "lstat", lstat):
        assert git_readable(str(worktree)) == "OK"


def test_worker_leaves_an_already_readable_hardlink_unchanged(layout: Path) -> None:
    clone, worktree = _clone_with_worktree(layout)
    outside = layout / "outside-object"
    outside.write_bytes(b"shared object")
    outside.chmod(0o644)
    (clone / ".git" / "objects" / "shared").hardlink_to(outside)

    codex_read_only_executor.repair_git_metadata(worktree, allowed_roots=(clone.parent, worktree.parent))

    assert outside.stat().st_mode & 0o777 == 0o644
    assert git_readable(str(worktree)) == "OK"


def test_worker_refuses_to_change_an_unreadable_hardlink(layout: Path) -> None:
    clone, worktree = _clone_with_worktree(layout)
    outside = layout / "outside-object"
    outside.write_bytes(b"shared object")
    outside.chmod(0o600)
    (clone / ".git" / "objects" / "shared").hardlink_to(outside)

    with pytest.raises(codex_read_only_executor.ExecutorRefusalError, match="links outside its checkout"):
        codex_read_only_executor.repair_git_metadata(worktree, allowed_roots=(clone.parent, worktree.parent))

    assert outside.stat().st_mode & 0o777 == 0o600


def test_worker_refuses_git_metadata_outside_the_declared_mounts(layout: Path) -> None:
    clone, worktree = _clone_with_worktree(layout)
    outside = layout / "outside-git"
    outside.mkdir()
    private = outside / "packed-refs"
    private.write_text("", encoding="utf-8")
    private.chmod(0o600)
    (worktree / ".git").write_text(f"gitdir: {outside}\n", encoding="utf-8")

    with pytest.raises(codex_read_only_executor.ExecutorRefusalError, match="outside the executor"):
        codex_read_only_executor.repair_git_metadata(worktree, allowed_roots=(clone.parent, worktree.parent))

    assert private.stat().st_mode & 0o777 == 0o600


def test_readable_internal_git_symlink_does_not_refuse_the_probe(layout: Path) -> None:
    clone, worktree = _clone_with_worktree(layout)
    target = clone / ".git" / "info" / "exclude"
    (clone / ".git" / "info" / "exclude-alias").symlink_to(target)

    assert git_readable(str(worktree)) == "OK"


@pytest.mark.parametrize("tree", ["objects", "refs"])
def test_an_unreadable_object_or_loose_ref_of_the_clone_is_named(layout: Path, tree: str) -> None:
    clone, worktree = _clone_with_worktree(layout)
    _git("-C", str(clone), "branch", "loose")
    unreadable = next(path for path in sorted((clone / ".git" / tree).rglob("*")) if path.is_file())
    unreadable.chmod(0)

    observed = observe(_request(layout, roots=(str(worktree),)), _fake_proc(layout, [(str(layout / "root"), "ext4")]))

    assert str(unreadable) in observed["roots"][str(worktree)]["git"]


def test_a_file_target_is_opened_for_append_and_never_written(tmp_path: Path) -> None:
    target = tmp_path / "hosts"
    target.write_text("127.0.0.1 localhost\n", encoding="utf-8")

    assert write_result(str(target)) == "OK"
    assert target.read_text(encoding="utf-8") == "127.0.0.1 localhost\n"


def test_a_refused_write_names_its_errno(tmp_path: Path) -> None:
    assert write_result(str(tmp_path / "missing" / "file")) == "ENOENT"


def test_mountinfo_octal_escapes_are_decoded() -> None:
    (mount,) = parse_mountinfo("36 35 98:0 / /mnt/with\\040space ro,relatime shared:1 - ext4 /dev/disk0 rw\n")

    assert mount == {"target": "/mnt/with space", "options": "ro,relatime", "fstype": "ext4", "source": "/dev/disk0"}


def test_main_prints_one_json_object() -> None:
    request = json.dumps({"roots": []})
    with (
        patch.object(codex_read_only_probe_program, "observe", return_value={"roots": {}}) as observed,
        patch("sys.argv", ["-", request]),
        patch("sys.stdout", new_callable=io.StringIO) as stdout,
    ):
        codex_read_only_probe_program.main()

    observed.assert_called_once_with({"roots": []})
    assert json.loads(stdout.getvalue()) == {"roots": {}}
