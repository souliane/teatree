"""The probe program write-probes every mount it does not own, against a /proc laid out under tmp_path."""

import io
import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.agents import codex_read_only_probe_program
from teatree.agents.codex_read_only_probe_program import ProbeRequest, observe, parse_mountinfo, write_result
from teatree.agents.codex_read_only_verdict import WORKTREE_ROOT, evaluate
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
        home=home,
        tools=["gh"],
    )


@pytest.fixture
def layout(tmp_path: Path) -> Path:
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
    tool.chmod(0o755)

    with patch.dict(os.environ, {"PATH": str(tool.parent)}, clear=True):
        observed = observe(_request(layout), _fake_proc(layout, [(str(layout / "root"), "ext4")]))

    assert observed["tools"] == {"gh": True}


_PLACEHOLDER = "EXAMPLE_TOKEN=placeholder\n"
_SECRETS = (
    ".netrc",
    "app/.env",
    "app/.env.local",
    "app/.git-credentials",
    "app/.pgpass",
    "app/deploy/teatree.env",
    "app/linked.env",
    "vendor/node_modules/pkg/.env",
)


def _plant(root: Path, *relatives: str, text: str = _PLACEHOLDER) -> None:
    for relative in relatives:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_every_readable_env_or_credential_file_under_a_mount_is_reported(layout: Path) -> None:
    root = layout / "root"
    _plant(root, *(relative for relative in _SECRETS if relative != "app/linked.env"))
    _plant(root, "app/.env.example", "app/.envrc", "app/.t3-env.cache", "app/notes.env.md")
    _plant(root, "app/empty.env", text="")
    _plant(root, "app/locked.env")
    (root / "app" / "locked.env").chmod(0)
    (root / "app" / "linked.env").symlink_to(root / "app" / ".env")

    observed = observe(_request(layout), _fake_proc(layout, [(str(root), "ext4")]))

    assert observed["uid"] == os.getuid()
    assert observed["scan"] == {
        str(root): {"owner": os.getuid(), "secrets": sorted(str(root / relative) for relative in _SECRETS)}
    }


def test_a_nested_mount_is_scanned_once_under_its_own_target(layout: Path) -> None:
    root, nested = layout / "root", layout / "root" / "ws"
    _plant(nested, ".env")

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


def test_a_planted_placeholder_env_file_refuses_the_executor(layout: Path) -> None:
    worktrees = layout / "ws"
    _plant(worktrees, "example/teatree/.env")
    observed = observe(_request(layout), _fake_proc(layout, [(str(worktrees), "ext4")]))
    answer = probe_fixture("pass")
    answer["scan"][WORKTREE_ROOT] = observed["scan"][str(worktrees)]

    assert evaluate(answer, executor_fixture("pass"), worker_fixture(), (EXAMPLE_ROOT,)) == (
        f"it can read {worktrees}/example/teatree/.env, a secret file under {WORKTREE_ROOT}"
    )


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
    _git("init", "-q", str(clone))
    _plant(clone, "README")
    _git("-C", str(clone), "add", "README")
    _git("-C", str(clone), "commit", "-qm", "init")
    _git("-C", str(clone), "pack-refs", "--all")
    _git("-C", str(clone), "worktree", "add", "-q", str(worktree))
    return clone, worktree


def test_a_root_whose_git_data_is_readable_answers_ok(layout: Path) -> None:
    clone, worktree = _clone_with_worktree(layout)
    teatree_file = clone / ".git" / "worktrees" / "ticket" / "t3" / "verify-gates.json"
    _plant(teatree_file.parent, teatree_file.name)
    teatree_file.chmod(0)

    observed = observe(_request(layout, roots=(str(worktree),)), _fake_proc(layout, [(str(layout / "root"), "ext4")]))

    assert observed["roots"][str(worktree)]["git"] == "OK"


def test_owner_only_git_data_of_a_worktrees_clone_is_named(layout: Path) -> None:
    clone, worktree = _clone_with_worktree(layout)
    packed_refs = clone / ".git" / "packed-refs"
    packed_refs.chmod(0)

    observed = observe(_request(layout, roots=(str(worktree),)), _fake_proc(layout, [(str(layout / "root"), "ext4")]))

    assert observed["roots"][str(worktree)]["git"] == f"{packed_refs} is unreadable"


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
