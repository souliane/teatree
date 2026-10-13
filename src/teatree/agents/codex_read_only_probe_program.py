"""Runs inside the read-only executor as ``python3 -I -S -``: stdlib only, one JSON object on stdout."""

import errno
import json
import os
import stat
import sys
from pathlib import Path
from typing import TypedDict

SECRET_NAMES = frozenset(
    {".env", ".envrc", ".netrc", ".npmrc", ".pypirc", ".git-credentials", ".pgpass"}
    | {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
)
TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist")
KEY_SUFFIXES = (".pem", ".key", ".p8", ".ppk")
PRIVATE_KEY_MARKERS = (b"PRIVATE KEY-----", b"PuTTY-User-Key-File")
KEY_READ_BYTES = 64 * 1024
GIT_FILES = ("HEAD", "commondir", "config", "index", "packed-refs")
GIT_TREES = ("objects", "refs")


class ProbeRequest(TypedDict):
    kernel_roots: list[str]
    device_root: str
    tmpfs: list[str]
    roots: list[str]
    home: str
    tools: list[str]


class Mount(TypedDict):
    target: str
    options: str
    fstype: str
    source: str


class RootAnswer(TypedDict):
    exists: bool
    is_dir: bool
    write: str
    git: str


class HomeAnswer(TypedDict):
    rules: list[str]
    config: bool
    plugins: bool


class ScanAnswer(TypedDict):
    owner: int
    secrets: list[str]


class Observation(TypedDict):
    mounts: list[Mount]
    writes: dict[str, str]
    controls: dict[str, str]
    cap_eff: str
    no_new_privs: str
    env: list[str]
    pid1_env: list[str]
    tools: dict[str, bool]
    roots: dict[str, RootAnswer]
    home: HomeAnswer | None
    uid: int
    scan: dict[str, ScanAnswer]


def _decode(field: str) -> str:
    out, index = [], 0
    while index < len(field):
        if field[index] == "\\" and field[index + 1 : index + 4].isdigit():
            out.append(chr(int(field[index + 1 : index + 4], 8)))
            index += 4
        else:
            out.append(field[index])
            index += 1
    return "".join(out)


def parse_mountinfo(text: str) -> list[Mount]:
    mounts: list[Mount] = []
    for line in text.splitlines():
        fields = line.split()
        separator = fields.index("-")
        mounts.append(
            Mount(
                target=_decode(fields[4]),
                options=fields[5],
                fstype=fields[separator + 1],
                source=_decode(fields[separator + 2]),
            )
        )
    return mounts


def _error_name(exc: OSError) -> str:
    return errno.errorcode.get(exc.errno or 0, str(exc.errno))


def write_result(path: str) -> str:
    target = Path(path)
    if target.is_dir():
        sentinel = target / f".t3-ro-probe-{os.urandom(8).hex()}"
        try:
            fd = os.open(sentinel, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as exc:
            return _error_name(exc)
        os.close(fd)
        sentinel.unlink()
        return "OK"
    try:
        fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_NONBLOCK)
    except OSError as exc:
        return _error_name(exc)
    os.close(fd)
    return "OK"


def _under(target: str, root: str) -> bool:
    return target == root or target.startswith(root.rstrip("/") + "/")


def _status_field(status: str, name: str) -> str:
    for line in status.splitlines():
        key, _, value = line.partition(":")
        if key == name:
            return value.strip()
    return ""


def _environ_names(raw: bytes) -> list[str]:
    return sorted({entry.split(b"=", 1)[0].decode() for entry in raw.split(b"\0") if entry})


def _resolves(name: str) -> bool:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / name
        if directory and candidate.is_file():
            return True
    return False


def secret_name(name: str) -> bool:
    if name in SECRET_NAMES or name.endswith(".env"):
        return True
    return name.startswith((".env.", ".env-", ".env_")) and not name.endswith(TEMPLATE_SUFFIXES)


def key_name(name: str) -> bool:
    """A name a private key goes by; only its content decides, since certificates and public keys share them."""
    return name.endswith(KEY_SUFFIXES) or (name.startswith("id_") and not name.endswith(".pub"))


def _head(path: str, size: int) -> bytes | None:
    """The first *size* bytes of a non-empty regular file this process can open, else ``None``."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
            return None
        return os.read(fd, size) if size else b""
    except OSError:
        return None
    finally:
        os.close(fd)


def _readable(path: str) -> bool:
    return _head(path, 0) is not None


def _secret_file(name: str, path: str) -> bool:
    if secret_name(name):
        return _readable(path)
    head = _head(path, KEY_READ_BYTES) if key_name(name) else None
    return head is not None and any(marker in head for marker in PRIVATE_KEY_MARKERS)


def _secrets(top: str, mounted: set[str]) -> list[str]:
    """Every readable, non-empty secret file under *top*, never crossing into another mount."""
    found: list[str] = []
    pending = [top]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except PermissionError:
            # Listing denied, yet a known name inside may still open.
            found.extend(path for name in SECRET_NAMES if _readable(path := str(Path(directory, name))))
            continue
        except OSError:
            continue
        for entry in entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if is_dir:
                if entry.path not in mounted:
                    pending.append(entry.path)
            elif _secret_file(entry.name, entry.path):
                found.append(entry.path)
    return sorted(found)


def _git_dirs(root: str) -> list[Path]:
    pointer = Path(root, ".git")
    gitdir = pointer
    if not pointer.is_dir():
        gitdir = Path(pointer.read_text(encoding="utf-8").removeprefix("gitdir:").strip())
    commondir = gitdir / "commondir"
    common = (gitdir / commondir.read_text(encoding="utf-8").strip()).resolve() if commondir.is_file() else gitdir
    return list(dict.fromkeys([gitdir, common]))


def git_readable(root: str) -> str:
    """``OK`` when this process can read every file git reads for *root*, else the first one it cannot."""
    try:
        tops = _git_dirs(root)
    except OSError as exc:
        return f"{exc.filename}: {_error_name(exc)}"
    files = [top / name for top in tops for name in GIT_FILES if (top / name).exists()]
    unreadable = [str(path) for path in files if not os.access(path, os.R_OK)]
    for tree in (top / name for top in tops for name in GIT_TREES if (top / name).exists()):
        for directory, _, filenames in os.walk(tree, onerror=lambda exc: unreadable.append(str(exc.filename))):
            paths = (str(Path(directory, name)) for name in filenames)
            unreadable.extend(path for path in paths if not os.access(path, os.R_OK))
    return f"{unreadable[0]} is unreadable" if unreadable else "OK"


def _home(home: str) -> HomeAnswer | None:
    if not home:
        return None
    root = Path(home)
    rules = root / "rules"
    return HomeAnswer(
        rules=sorted(entry.name for entry in rules.glob("*.rules")) if rules.is_dir() else [],
        config=(root / "config.toml").exists(),
        plugins=(root / "plugins").exists(),
    )


def observe(request: ProbeRequest, proc: Path = Path("/proc")) -> Observation:
    mounts = parse_mountinfo((proc / "self" / "mountinfo").read_text(encoding="utf-8"))
    status = (proc / "self" / "status").read_text(encoding="utf-8")
    unprobed = [*request["kernel_roots"], request["device_root"]]
    tmpfs = set(request["tmpfs"])
    targets = sorted({mount["target"] for mount in mounts})
    probed = [
        target for target in targets if target not in tmpfs and not any(_under(target, root) for root in unprobed)
    ]
    return Observation(
        mounts=mounts,
        writes={target: write_result(target) for target in probed},
        controls={target: write_result(target) for target in sorted(tmpfs)},
        cap_eff=_status_field(status, "CapEff"),
        no_new_privs=_status_field(status, "NoNewPrivs"),
        env=sorted(os.environ),
        pid1_env=_environ_names((proc / "1" / "environ").read_bytes()),
        tools={name: _resolves(name) for name in request["tools"]},
        roots={
            root: RootAnswer(
                exists=Path(root).exists(), is_dir=Path(root).is_dir(), write=write_result(root), git=git_readable(root)
            )
            for root in request["roots"]
        },
        home=_home(request["home"]),
        uid=os.getuid(),
        scan={
            target: ScanAnswer(owner=Path(target).stat().st_uid, secrets=_secrets(target, set(targets)))
            for target in probed
            if Path(target).is_dir()
        },
    )


def main() -> None:
    sys.stdout.write(json.dumps(observe(json.loads(sys.argv[1])), sort_keys=True))


if __name__ == "__main__":
    main()
