"""Runs inside the read-only executor as ``python3 -I -S -``: stdlib only, one JSON object on stdout."""

import errno
import json
import os
import re
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
DOTENV_POINTER = re.compile(rb"dotenv[ \t]+[A-Za-z0-9_.][A-Za-z0-9_./-]*")
KEY_READ_BYTES = 64 * 1024


class ProbeRequest(TypedDict):
    kernel_roots: list[str]
    device_root: str
    tmpfs: list[str]
    roots: list[str]
    git_mounts: list[str]
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


def _dotenv_pointer(path: str) -> bool:
    """Every non-blank line is ``dotenv <relative path>``, so the file names another file and holds no value."""
    content = _head(path, KEY_READ_BYTES + 1)
    if content is None or len(content) > KEY_READ_BYTES:
        return False
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    return bool(lines) and all(DOTENV_POINTER.fullmatch(line) for line in lines)


def _named_secret(name: str, path: str) -> bool:
    return _readable(path) and not (name == ".envrc" and _dotenv_pointer(path))


def _secret_file(name: str, path: str, git_mounts: set[str]) -> bool:
    if secret_name(name):
        return _named_secret(name, path)
    if name in {"config", "config.worktree"} and (
        "/.git/" in path or any(_under(path, target) for target in git_mounts)
    ):
        content = _head(path, KEY_READ_BYTES + 1)
        normalized = re.sub(rb"\\\r?\n\s*", b"", content) if content is not None else None
        if content is not None and (
            len(content) > KEY_READ_BYTES
            or re.search(
                rb"https?://[^\s/@]+@|^\s*(?:extraheader|password)\s*=", normalized or b"", re.IGNORECASE | re.MULTILINE
            )
        ):
            return True
    head = _head(path, KEY_READ_BYTES) if key_name(name) else None
    return head is not None and any(marker in head for marker in PRIVATE_KEY_MARKERS)


def _secrets(top: str, mounted: set[str], git_mounts: set[str]) -> list[str]:
    """Every readable, non-empty secret file under *top*, never crossing into another mount."""
    found: list[str] = []
    pending = [top]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except PermissionError:
            # Listing denied, yet a known name inside may still open.
            found.extend(path for name in SECRET_NAMES if _named_secret(name, path := str(Path(directory, name))))
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
            elif _secret_file(entry.name, entry.path, git_mounts):
                found.append(entry.path)
    return sorted(found)


def resolved_git_dirs(root: str) -> list[Path]:
    pointer = Path(root, ".git")
    gitdir = pointer
    if not pointer.is_dir():
        named = Path(pointer.read_text(encoding="utf-8").removeprefix("gitdir:").strip())
        gitdir = named if named.is_absolute() else pointer.parent / named
    gitdir = gitdir.resolve()
    commondir = gitdir / "commondir"
    common = (gitdir / commondir.read_text(encoding="utf-8").strip()).resolve() if commondir.is_file() else gitdir
    return list(dict.fromkeys([gitdir, common]))


def _internal_git_link(path: Path, tops: list[Path]) -> bool:
    try:
        target = path.resolve(strict=True)
    except OSError:
        return False
    return any(target.is_relative_to(top) for top in tops)


def _git_path_readable(path: Path, tops: list[Path], *, directory: bool) -> bool:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if stat.S_ISLNK(mode):
        return _internal_git_link(path, tops)
    needed = stat.S_IROTH | stat.S_IXOTH if directory else stat.S_IROTH
    access = os.R_OK | os.X_OK if directory else os.R_OK
    return mode & needed == needed and os.access(path, access)


def _unreadable_git_paths(top: Path, tops: list[Path]) -> set[str]:
    unreadable: set[str] = set()
    for directory, names, filenames in os.walk(top, onerror=lambda exc: unreadable.add(str(exc.filename))):
        current = Path(directory)
        if not _git_path_readable(current, tops, directory=True):
            unreadable.add(str(current))
        unreadable.update(
            str(path) for name in filenames if not _git_path_readable(path := current / name, tops, directory=False)
        )
        for name in list(names):
            path = current / name
            if path.is_symlink():
                if not _git_path_readable(path, tops, directory=True):
                    unreadable.add(str(path))
                names.remove(name)
    return unreadable


def git_readable(root: str) -> str:
    """``OK`` when the executor can read every reachable Git metadata path."""
    try:
        tops = resolved_git_dirs(root)
    except OSError as exc:
        return f"{exc.filename}: {_error_name(exc)}"
    unreadable: set[str] = set()
    for top in tops:
        unreadable.update(_unreadable_git_paths(top, tops))
    if not unreadable:
        return "OK"
    return "Git metadata unreadable: " + "; ".join(sorted(unreadable)) + "; fix: chmod -R a+rX <gitdir> in the worker"


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
    git_mounts = set(request["git_mounts"]) & set(targets)
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
            target: ScanAnswer(owner=Path(target).stat().st_uid, secrets=_secrets(target, set(targets), git_mounts))
            for target in probed
            if Path(target).is_dir()
        },
    )


def main() -> None:
    sys.stdout.write(json.dumps(observe(json.loads(sys.argv[1])), sort_keys=True))


if __name__ == "__main__":
    main()
