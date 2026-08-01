"""PreToolUse: refuse a ``glab mr create`` that ``glab`` would silently no-op.

``glab`` reserves the remote name ``glab-base`` and PERSISTS a ``-R <slug>``
override into the cwd repository as a remote of that name. The next
``glab mr create -R <different-slug>`` from the same working directory then has a
STALE ``glab-base`` pointing at the previous, unrelated project — and glab's
base/head resolution bails: it exits **0**, prints **nothing**, and creates
nothing. Reads (``glab mr list``) are unaffected, so the repo looks healthy.

That outcome is the worst shape a tool can produce. A command that must fail
(a non-existent source branch) and a command that should succeed become
indistinguishable, both rendering as an empty result — which reads exactly like a
hook that swallowed the call, and sends the debugging at the gates instead of at
the command. This gate converts that silence into a named, actionable block.

It fires narrowly: only a real ``glab mr create`` (verb detected on the
quote/heredoc-stripped skeleton, so the phrase inside a commit message or doc
string never false-fires) that names an explicit ``-R``/``--repo`` target, run in
a repository whose ``glab-base`` remote points somewhere ELSE. A matching
``glab-base``, an absent one, a non-git cwd, an unreadable remote, or any other
command all ALLOW.

Never-lockout: a per-call ``[glab-base-ok: <reason>]`` token and the
``glab_stale_base_remote_gate_enabled`` kill-switch
(``t3 <overlay> gate glab-base-remote disable``) both ALLOW, the deny routes
through ``_fail_open_or_deny`` so the self-rescue allowlist + master fail-open
switch + circuit breaker apply, and every internal error fails OPEN.

Cold-import safe: stdlib-only module top plus the already-extracted
``mr_cli_fields`` / ``managed_repo`` siblings — no Django, no ``teatree`` at
import. Helpers that emit the deny and read config live in ``hook_router`` and are
imported lazily at call time (the router imports this module at top level, so a
top-level back-import would cycle).
"""

import re
import sys
from pathlib import Path

from hooks.scripts.managed_repo import git_text
from hooks.scripts.mr_cli_fields import extract_mr_target_repo, strip_quoted_and_heredoc

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# registers and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("glab_stale_base_remote_guard", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.glab_stale_base_remote_guard", sys.modules[__name__])

# The remote name glab reserves for the base-repo override. ``glab-head`` is its
# head-repo counterpart; a stale one of either steers the same resolution.
BASE_REMOTE = "glab-base"

# Only ``create`` — the surface measured to no-op silently. ``update`` addresses
# an existing MR by iid and is not part of the observed failure, so it is left
# alone rather than blocked on a guess.
_GLAB_MR_CREATE_RE = re.compile(r"\bglab\s+mr\s+create\b")
# A leading ``cd <dir> &&`` / ``cd <dir>;`` — the sanctioned prefix on an
# otherwise-bare forge call, and the directory glab would actually run in.
_LEADING_CD_RE = re.compile(r"^\s*cd\s+(?P<dir>'[^']*'|\"[^\"]*\"|[^\s;&|]+)\s*(?:&&|;)")
# Per-call escape, mirroring the other gates' ``[…-ok: <reason>]`` tokens.
_GLAB_BASE_OK_RE = re.compile(r"\[glab-base-ok:\s*(\S[^\]]*?)\s*\]")


def _gate_enabled() -> bool:
    """Whether the gate is enabled (default True).

    Fails OPEN to enabled on a missing/broken config; an explicit ``false``
    (``[teatree] glab_stale_base_remote_gate_enabled = false``) is the one-line
    kill-switch.
    """
    from hooks.scripts.hook_router import _teatree_bool_setting  # noqa: PLC0415 deferred back-import

    return _teatree_bool_setting("glab_stale_base_remote_gate_enabled", default=True)


def _glab_base_ok_token(command: str) -> str | None:
    """Return the reason from a ``[glab-base-ok: <reason>]`` token, else None.

    Scanned within the first 512 chars (mirroring the other per-call escapes);
    an empty reason returns None so a bare ``[glab-base-ok: ]`` never allows.
    """
    match = _GLAB_BASE_OK_RE.search(command[:512])
    if not match:
        return None
    return match.group(1).strip() or None


def command_working_dir(command: str, harness_cwd: str) -> Path | None:
    """The directory glab will actually run in — a leading ``cd``, else the harness cwd.

    A bare forge call is the documented shape, but a leading ``cd <dir> &&`` is
    explicitly sanctioned, and it is precisely what decides which repository's
    remotes glab reads. ``None`` when neither resolves to an existing directory,
    which fails the gate open.
    """
    match = _LEADING_CD_RE.match(command)
    candidate = match.group("dir").strip("'\"") if match else harness_cwd
    if not candidate:
        return None
    path = Path(candidate).expanduser()
    return path if path.is_dir() else None


def project_path(slug_or_url: str) -> str:
    """The comparable ``namespace/project`` path of a repo slug or remote URL.

    A ``-R`` target is a bare slug (``group/project``); a git remote is an SSH or
    HTTPS URL. Both reduce to the same lowercase path with any host, ``.git``
    suffix and leading slash removed, so the two are comparable without a network
    call. ``""`` when nothing path-shaped is left.
    """
    text = slug_or_url.strip().removesuffix(".git")
    if "://" in text:
        text = text.split("://", 1)[1].split("/", 1)[-1]
    elif ":" in text and "@" in text.split(":", 1)[0]:
        text = text.split(":", 1)[1]
    return text.strip("/").lower()


def stale_base_remote(work_dir: Path, target_slug: str) -> str | None:
    """The ``glab-base`` remote URL when it points somewhere OTHER than *target_slug*.

    ``None`` — and therefore an allow — whenever glab would resolve normally: no
    ``glab-base`` remote, one that already matches the target, an unreadable
    remote, or a cwd that is not a git repository.
    """
    remote_url = git_text(work_dir, "remote", "get-url", BASE_REMOTE)
    if not remote_url:
        return None
    target = project_path(target_slug)
    if not target or project_path(remote_url) == target:
        return None
    return remote_url


def _reason(remote_url: str, target_slug: str, work_dir: Path) -> str:
    return (
        f"BLOCKED: `glab mr create -R {target_slug}` would SILENTLY do nothing here. "
        f"`{work_dir}` carries a `{BASE_REMOTE}` remote pointing at `{remote_url}` — glab "
        f"reserves that remote name for its `-R` override and persists one there, so a "
        f"LATER create aimed at a different project bails out with exit 0, no output and "
        f"no MR. A must-fail and a should-succeed call become indistinguishable. "
        f"Run the create from the target repository's own clone instead (`cd` into the "
        f"clone of `{target_slug}`, then re-issue the same command), or rename/remove "
        f"that remote (`git -C {work_dir} remote rename {BASE_REMOTE} <name>`). "
        f"If you have verified this specific call really does reach GitLab, append "
        f"`[glab-base-ok: <reason>]` to the command."
    )


def handle_block_glab_stale_base_remote(data: dict) -> bool:
    """Block a ``glab mr create`` whose cwd has a mismatched ``glab-base`` remote.

    Fires only when the gate is enabled, no per-call ``[glab-base-ok: <reason>]``
    token is present, the command really invokes ``glab mr create`` with an
    explicit ``-R``/``--repo`` target, and the directory it would run in has a
    ``glab-base`` remote for a DIFFERENT project. Everything else ALLOWS, and any
    internal error fails OPEN — the gate exists to replace a silent no-op with a
    readable one, so it must never become a new way to get stuck.
    """
    from hooks.scripts.hook_router import _fail_open_or_deny  # noqa: PLC0415 deferred back-import

    if data.get("tool_name") != "Bash" or not _gate_enabled():
        return False
    command = data.get("tool_input", {}).get("command", "")
    if not command or not _GLAB_MR_CREATE_RE.search(strip_quoted_and_heredoc(command)):
        return False
    if reason := _glab_base_ok_token(command):
        sys.stderr.write(f"NOTE: stale `{BASE_REMOTE}` remote gate skipped via [glab-base-ok: {reason}].\n")
        return False

    target_slug = extract_mr_target_repo(command)
    work_dir = command_working_dir(command, data.get("cwd", ""))
    if not target_slug or work_dir is None:
        return False
    remote_url = stale_base_remote(work_dir, target_slug)
    if remote_url is None:
        return False
    return _fail_open_or_deny(data, _reason(remote_url, target_slug, work_dir))
