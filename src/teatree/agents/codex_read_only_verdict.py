"""What the read-only executor must look like, judged from its live probe and docker inspect."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

EXECUTOR_HOME_ROOT: Final = "/run/t3-ro"
SCRATCH_ROOT: Final = "/run/t3-tmp"
CLONE_ROOT: Final = "/home/teatree/workspace"  # privacy-scan:allow — the box's public, documented deploy home
WORKTREE_ROOT: Final = f"{CLONE_ROOT}/t3-workspaces"
DECLARED_TMPFS: Final = frozenset({EXECUTOR_HOME_ROOT, SCRATCH_ROOT})
KERNEL_ROOTS: Final = ("/proc", "/sys")
DEVICE_ROOT: Final = "/dev"
DEVICE_MOUNTS: Final = frozenset(
    {
        (DEVICE_ROOT, "tmpfs"),
        (f"{DEVICE_ROOT}/pts", "devpts"),
        (f"{DEVICE_ROOT}/mqueue", "mqueue"),
        (f"{DEVICE_ROOT}/shm", "tmpfs"),
    }
)
DOCKER_FILE_MOUNTS: Final = frozenset({"/etc/hosts", "/etc/hostname", "/etc/resolv.conf"})
ENV_ALLOWLIST: Final = frozenset(
    {"PATH", "HOME", "HOSTNAME", "LANG", "LC_ALL", "TZ", "TERM", "TMPDIR", "CODEX_HOME", "GIT_OPTIONAL_LOCKS"}
)
FORBIDDEN_TOOLS: Final = ("t3", "gh", "docker", "pass")
CHECKOUT_ENV: Final = "TEATREE_DEPLOY_CHECKOUT"

# A non-root write to Docker's own file mounts fails its permission check before the read-only check.
_FILE_MOUNT_REFUSALS: Final = frozenset({"EROFS", "EACCES"})
_PROBE_FIELDS: Final = frozenset(
    (
        "mounts",
        "writes",
        "controls",
        "cap_eff",
        "no_new_privs",
        "env",
        "pid1_env",
        "tools",
        "roots",
        "home",
        "uid",
        "scan",
    )
)
_PRIVATE_NAMESPACES: Final = frozenset({"", "private"})
_NO_NEW_PRIVILEGES: Final = ("no-new-privileges:true",)


class MalformedAnswerError(ValueError):
    pass


def _require(field: str, *, holds: bool) -> None:
    if not holds:
        raise MalformedAnswerError(field)


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if isinstance(value, Mapping) and all(isinstance(key, str) for key in value):
        return value
    raise MalformedAnswerError(field)


def _exact(value: object, field: str, fields: frozenset[str]) -> Mapping[str, object]:
    answer = _mapping(value, field)
    _require(field, holds=set(answer) == fields)
    return answer


def _text(value: object, field: str) -> str:
    if isinstance(value, str):
        return value
    raise MalformedAnswerError(field)


def _flag(value: object, field: str) -> bool:
    if isinstance(value, bool):
        return value
    raise MalformedAnswerError(field)


def _number(value: object, field: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise MalformedAnswerError(field)


def _list(value: object, field: str) -> list[object]:
    if isinstance(value, list):
        return value
    raise MalformedAnswerError(field)


def _texts(value: object, field: str) -> tuple[str, ...]:
    return tuple(_text(item, field) for item in _list(value, field))


def _optional_texts(value: object, field: str) -> tuple[str, ...]:
    return () if value is None else _texts(value, field)


@dataclass(frozen=True, slots=True)
class _Mount:
    target: str
    options: tuple[str, ...]
    fstype: str

    @classmethod
    def parse(cls, value: object) -> "_Mount":
        mount = _exact(value, "mounts", frozenset({"target", "options", "fstype", "source"}))
        _text(mount["source"], "mounts")
        return cls(
            _text(mount["target"], "mounts"),
            tuple(_text(mount["options"], "mounts").split(",")),
            _text(mount["fstype"], "mounts"),
        )


@dataclass(frozen=True, slots=True)
class _Root:
    exists: bool
    is_dir: bool
    write: str
    git: str

    @classmethod
    def parse(cls, value: object) -> "_Root":
        root = _exact(value, "roots", frozenset({"exists", "is_dir", "write", "git"}))
        return cls(
            _flag(root["exists"], "roots"),
            _flag(root["is_dir"], "roots"),
            _text(root["write"], "roots"),
            _text(root["git"], "roots"),
        )


@dataclass(frozen=True, slots=True)
class _Scan:
    owner: int
    secrets: tuple[str, ...]

    @classmethod
    def parse(cls, value: object) -> "_Scan":
        scan = _exact(value, "scan", frozenset({"owner", "secrets"}))
        return cls(_number(scan["owner"], "scan"), _texts(scan["secrets"], "scan"))


@dataclass(frozen=True, slots=True)
class _Probe:
    mounts: tuple[_Mount, ...]
    writes: Mapping[str, str]
    controls: Mapping[str, str]
    cap_eff: str
    no_new_privs: str
    env: frozenset[str]
    tools: Mapping[str, bool]
    roots: Mapping[str, _Root]
    home: tuple[str, ...]
    uid: int
    scan: Mapping[str, _Scan]

    @classmethod
    def parse(cls, value: object, roots: Sequence[str]) -> "_Probe":
        answer = _exact(value, "probe", _PROBE_FIELDS)
        planted: list[str] = []
        if answer["home"] is not None:
            home = _exact(answer["home"], "home", frozenset({"rules", "config", "plugins"}))
            planted.extend(f"rules/{name}" for name in _texts(home["rules"], "home"))
            planted.extend(["config.toml"] if _flag(home["config"], "home") else [])
            planted.extend(["plugins/"] if _flag(home["plugins"], "home") else [])
        tools = _mapping(answer["tools"], "tools")
        _require("tools", holds=set(tools) == set(FORBIDDEN_TOOLS))
        root_answers = _mapping(answer["roots"], "roots")
        _require("roots", holds=set(root_answers) == set(roots))
        return cls(
            mounts=tuple(_Mount.parse(mount) for mount in _list(answer["mounts"], "mounts")),
            writes={key: _text(item, "writes") for key, item in _mapping(answer["writes"], "writes").items()},
            controls={key: _text(item, "controls") for key, item in _mapping(answer["controls"], "controls").items()},
            cap_eff=_text(answer["cap_eff"], "cap_eff"),
            no_new_privs=_text(answer["no_new_privs"], "no_new_privs"),
            env=frozenset(_texts(answer["env"], "env")) | frozenset(_texts(answer["pid1_env"], "pid1_env")),
            tools={name: _flag(found, "tools") for name, found in tools.items()},
            roots={root: _Root.parse(item) for root, item in root_answers.items()},
            home=tuple(planted),
            uid=_number(answer["uid"], "uid"),
            scan={target: _Scan.parse(item) for target, item in _mapping(answer["scan"], "scan").items()},
        )


@dataclass(frozen=True, slots=True)
class _DockerMount:
    kind: str
    source: str
    name: str
    read_write: bool

    @classmethod
    def by_destination(cls, value: object, field: str) -> dict[str, "_DockerMount"]:
        mounts: dict[str, _DockerMount] = {}
        for raw in _list(value, field):
            mount = _mapping(raw, field)
            mounts[_text(mount.get("Destination"), field)] = cls(
                _text(mount.get("Type"), field),
                _text(mount.get("Source"), field),
                _text(mount.get("Name", ""), field),
                _flag(mount.get("RW"), field),
            )
        return mounts


def _worker_checkout(worker: Mapping[str, object]) -> str:
    entries = _texts(worker["CheckoutEnv"], "worker deploy checkout")
    _require("worker deploy checkout", holds=len(entries) == 1 and entries[0].startswith(f"{CHECKOUT_ENV}=/"))
    return entries[0].partition("=")[2]


def _host_config_refusal(host: Mapping[str, object], project: str) -> str | None:
    tmpfs = _mapping(host.get("Tmpfs") or {}, "Tmpfs")
    network = _text(host.get("NetworkMode"), "NetworkMode")
    security = _optional_texts(host.get("SecurityOpt"), "SecurityOpt")
    namespaces = {kind: _text(host.get(f"{kind}Mode"), f"{kind}Mode") for kind in ("Pid", "Ipc", "Userns")}
    shared = sorted(f"{kind} {mode}" for kind, mode in namespaces.items() if mode not in _PRIVATE_NAMESPACES)
    checks = (
        (_flag(host.get("ReadonlyRootfs"), "ReadonlyRootfs"), "its root filesystem is writable"),
        ("ALL" in _optional_texts(host.get("CapDrop"), "CapDrop"), "it keeps Linux capabilities"),
        (not _optional_texts(host.get("CapAdd"), "CapAdd"), "it adds Linux capabilities"),
        (not _flag(host.get("Privileged"), "Privileged"), "it is privileged"),
        (network == f"{project}_ro-executor", f"it is on the {network} network, not {project}_ro-executor"),
        (not shared, f"it shares the {', '.join(shared)} namespace"),
        (security == _NO_NEW_PRIVILEGES, f"its security options are {list(security)}, not {list(_NO_NEW_PRIVILEGES)}"),
        (not host.get("Devices"), "it maps host devices"),
        (set(tmpfs) == DECLARED_TMPFS, f"its tmpfs set is {sorted(tmpfs)}"),
    )
    return next((reason for passed, reason in checks if not passed), None)


def _expected_mounts(worker: Mapping[str, _DockerMount], checkout: str) -> dict[str, _DockerMount | None]:
    """The worker's clone and worktree mounts as they are, and only the .git of its deploy checkout."""
    whole = worker.get(checkout)
    git = None if whole is None else _DockerMount(whole.kind, f"{whole.source}/.git", whole.name, read_write=False)
    return {CLONE_ROOT: worker.get(CLONE_ROOT), WORKTREE_ROOT: worker.get(WORKTREE_ROOT), f"{checkout}/.git": git}


def _docker_mount_refusal(
    executor: Mapping[str, object], worker_mounts: Mapping[str, _DockerMount], checkout: str
) -> str | None:
    expected = _expected_mounts(worker_mounts, checkout)
    for destination, mount in _DockerMount.by_destination(executor["Mounts"], "Mounts").items():
        if destination not in expected:
            return f"it mounts {destination}, which is not a declared read-only mount"
        if mount.read_write:
            return f"its mount {destination} is read-write"
        worker = expected[destination]
        if worker is None or (worker.kind, worker.source, worker.name) != (mount.kind, mount.source, mount.name):
            return f"its mount {destination} is not the worker's {destination}"
    return None


def _under(target: str, root: str) -> bool:
    return target == root or target.startswith(root.rstrip("/") + "/")


def _mount_set_refusal(mounts: Mapping[str, _Mount], read_only: set[str]) -> str | None:
    if missing := sorted((read_only | DECLARED_TMPFS) - set(mounts)):
        return f"{missing[0]} is not mounted in the executor"
    for target, mount in sorted(mounts.items()):
        runtime = any(_under(target, root) for root in KERNEL_ROOTS) or (target, mount.fstype) in DEVICE_MOUNTS
        if not runtime and target not in read_only | DECLARED_TMPFS | DOCKER_FILE_MOUNTS:
            return f"unexpected mount {target} ({mount.fstype})"
    return None


def _write_refusal(probe: _Probe, mounts: Mapping[str, _Mount], read_only: set[str]) -> str | None:
    for target in sorted(read_only | (DOCKER_FILE_MOUNTS & set(mounts))):
        result = probe.writes.get(target)
        if result is None:
            return f"the write probe skipped {target}"
        if result == "OK":
            return f"{target} accepted a write"
        file_mount = target in DOCKER_FILE_MOUNTS and "ro" in mounts[target].options
        if result not in (_FILE_MOUNT_REFUSALS if file_mount else {"EROFS"}):
            return f"{target} is not a read-only mount ({result})"
    for target in sorted(DECLARED_TMPFS):
        if mounts[target].fstype != "tmpfs" or probe.controls.get(target) != "OK":
            return f"{target} is not a writable tmpfs ({mounts[target].fstype}, {probe.controls.get(target)})"
    return None


def _exposure_refusal(probe: _Probe, read_only: set[str]) -> str | None:
    """Nothing it reads may be its own or a readable secret, so a mount regression cannot leak a credential."""
    if probe.uid == 0:
        return "it runs as root"
    for target in sorted(read_only):
        scan = probe.scan.get(target)
        if scan is None:
            return f"the secret scan skipped {target}"
        if scan.owner == probe.uid:
            return f"it runs as uid {probe.uid}, which owns {target}"
        if scan.secrets:
            return f"it can read {scan.secrets[0]}, a secret file under {target}"
    return None


def _process_refusal(probe: _Probe) -> str | None:
    try:
        capabilities = int(probe.cap_eff, 16)
    except ValueError:
        return f"its capability set is unreadable ({probe.cap_eff!r})"
    if capabilities or probe.no_new_privs != "1":
        return f"it runs with capabilities {probe.cap_eff} and no_new_privs {probe.no_new_privs}"
    if extra := sorted(probe.env - ENV_ALLOWLIST):
        return f"its environment carries {', '.join(extra)}"
    if tools := sorted(name for name, found in probe.tools.items() if found):
        return f"{', '.join(tools)} resolves on its PATH"
    return None


def _root_refusal(probe: _Probe) -> str | None:
    for root, answer in sorted(probe.roots.items()):
        if not (answer.exists and answer.is_dir) or answer.write != "EROFS":
            return f"the checkout {root} is not a directory on a read-only mount there ({answer.write})"
        if answer.git != "OK":
            return f"git cannot read the checkout {root} there ({answer.git})"
    return None


def evaluate(observation: object, executor: object, worker: object, roots: Sequence[str]) -> str | None:
    try:
        worker_answer = _exact(worker, "worker", frozenset({"Id", "Project", "Mounts", "CheckoutEnv"}))
        executor_answer = _exact(executor, "executor", frozenset({"Id", "Mounts", "HostConfig"}))
        checkout = _worker_checkout(worker_answer)
        probe = _Probe.parse(observation, roots)
        mounts = {mount.target: mount for mount in probe.mounts}
        read_only = {"/", CLONE_ROOT, WORKTREE_ROOT, f"{checkout}/.git"}
        project = _text(worker_answer["Project"], "worker")
        return (
            _host_config_refusal(_mapping(executor_answer["HostConfig"], "HostConfig"), project)
            or _docker_mount_refusal(
                executor_answer, _DockerMount.by_destination(worker_answer["Mounts"], "worker"), checkout
            )
            or _mount_set_refusal(mounts, read_only)
            or _write_refusal(probe, mounts, read_only)
            or _exposure_refusal(probe, read_only)
            or _process_refusal(probe)
            or _root_refusal(probe)
        )
    except MalformedAnswerError as exc:
        return f"its answer is malformed ({exc})"


def planted_home(observation: object) -> str | None:
    """Rules, config or plugins in a live Codex home the core never writes; ``evaluate`` already validated it."""
    try:
        roots = _mapping(_mapping(observation, "probe")["roots"], "roots")
        planted = _Probe.parse(observation, tuple(roots)).home
    except MalformedAnswerError as exc:
        return f"its answer is malformed ({exc})"
    return f"its Codex home holds {', '.join(planted)}, which the core never writes" if planted else None
