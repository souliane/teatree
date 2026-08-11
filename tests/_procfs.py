"""Shared synthetic ``/proc`` builder for the scratch sweep's open-file guard (#4165).

Any test that reaches ``sweep_scratch`` / ``ScratchSweep`` without pinning a process
table reads the MACHINE's live ``/proc``, so its result is a property of whatever
else happens to be running: one same-uid pid presenting a listable-but-unresolvable
``fd``/``map_files`` blinds the probe table-wide, the sweep correctly removes
nothing, and a "the stale file is gone" assertion fails. That is how the serial
``test-shuffle`` seeds red while every sharded job on the same image is green.

Build the table here instead of hand-rolling one per test file — the invariant
"a live procfs always carries at least one answering pid" is load-bearing for the
probe's fail-closed contract, and two private copies of it drift.
"""

from pathlib import Path


def net_unix(namespace_dir: Path, *bind_paths: str) -> None:
    """Write a synthetic ``net/unix`` table under *namespace_dir*, one socket per bind path.

    *namespace_dir* is a NUMERIC pid dir for the tables the sweep actually reads;
    passing the bare proc root instead builds the ambiguous ``/proc/net`` decoy the
    pinning tests prove is never consulted.
    """
    (namespace_dir / "net").mkdir(parents=True, exist_ok=True)
    rows = "".join(
        f"0000000000000000: 00000002 00000000 00000000 0001 01 {12345 + index} {path}\n"
        for index, path in enumerate(bind_paths)
    )
    (namespace_dir / "net" / "unix").write_text(
        "Num       RefCount Protocol Flags    Type St Inode Path\n" + rows, encoding="utf-8"
    )


def listening_socket(pid_dir: Path, *bind_paths: str) -> None:
    """A pid holding a bound socket, shaped the way a real ``/proc`` presents one.

    The socket's own fd reads back as ``socket:[inode]`` — never the bind path —
    which is the whole reason the bind path has to come from ``net/unix``.
    """
    (pid_dir / "fd").mkdir(parents=True, exist_ok=True)
    (pid_dir / "fd" / "4").symlink_to("socket:[12345]")
    net_unix(pid_dir, *bind_paths)


def answering_pid(proc_root: Path, target: Path) -> Path:
    """A pid that RESOLVES one fd — the witness every live procfs carries.

    Seeded into the default table because a pid-less ``proc_root`` is not a quiet
    box, it is a mount that is not a procfs, and the probe fails closed on it.
    """
    fd_dir = proc_root / "1" / "fd"
    fd_dir.mkdir(parents=True)
    (fd_dir / "0").symlink_to(target)
    return proc_root / "1"
