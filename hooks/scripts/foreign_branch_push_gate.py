"""PreToolUse: refuse a ``git push`` onto a branch that is not ours.

A coder dispatched with "commit and push your branch" landed a 28-file commit
on a colleague's open draft MR, and the remediation attempt FORCE-PUSHED that
branch — a destructive rewrite of a third party's history, unasked. Prose rules
covering that existed and were not followed, so this is the deterministic
refusal.

FAIL CLOSED, which inverts every sibling gate in this directory: an un-asked
question resolves to REFUSE, naming the probe that could not answer. There is
no per-call ``[…-ok: <reason>]`` token — the incident was an agent doing exactly
what a token would have let it do — so the owner's only escape is the deliberate
config flip ``t3 <overlay> gate foreign-push disable``. The pre-push hook
:mod:`teatree.hooks.foreign_mr_cli` asks a similar question and is fail-OPEN by
construction; this gate reuses its forge seam and supplies its own tri-state.

DECISION LADDER, in order, first answer wins:

0. ``work_dir`` is inside NO repository — ``origin`` is a repository-relative
    name, so every rung below would refuse with an ``ls-remote`` argv that
    SUCCEEDS the moment the reader pastes it against their worktree. Its own
    refusal names the directory, git's cause, and the remedy (``git -C <repo>
    push …``). Refusing rather than allowing is deliberate: the hook sees only
    the payload's ``cwd``, so a shell whose real cwd IS a repository would pass;
0b. the push FORCES or DELETES and the branch IS the remote's default branch —
    REFUSE whoever owns it, checked before ownership is even asked: an owned
    open MR whose SOURCE happens to be the default branch must not let rung 2
    answer "ours" first, since no rung below it would catch the rewrite;
1. the branch is ABSENT from the remote — ours to create — ALLOW;
2. an OPEN MR/PR whose source branch is the target — its author is ours ALLOW,
    anyone else's REFUSE;
3. no MR, but the remote branch carries commits by another author and none by
    us, or none at all — REFUSE. The remote's OWN default branch is exempt from
    that arithmetic — it is compared against itself, so its range is empty for
    every push of it — a plain push of it is therefore ALLOWED here, and a
    destructive one was already refused at rung 0b;
4. anything unobtainable — REFUSE, naming what went unanswered plus, for EVERY
    git probe that ran and failed, its own exit code and stderr
    (:func:`probe_cause`): an argv is the question, git's own words the answer.

A FORCE or DELETE this gate refuses — onto a branch that is not ours, onto the
default branch, or inside material it cannot read — has no escape at all: it
bypasses the shared ``_fail_open_or_deny`` chain (self-rescue allowlist, master
fail-open switch) that every other deny here routes through. That bypass is
decided over EVERY refusing item the command yields, never the first one read,
so a force cannot hide behind a plain refusal beside it.

COVERAGE LIMIT, stated rather than implied: a push issued from a Workflow run
does not necessarily reach a ``PreToolUse`` hook and can bypass this gate
entirely. It is one enforcement point, not the whole perimeter.

Material this gate can NOT read that still runs a push — an unresolved wrapper,
a quoted script handed to a shell, a push PRINTED into a payload-less shell, a
heredoc a shell executes, wrappers nested past the recursion cutoff, unbalanced
quoting — is REFUSED naming the single-line re-issue path
(:mod:`foreign_branch_push_read` names the shapes, and the known gaps it still
allows). A non-push Bash call pays one flatten and NO forge call — this handler
runs on every Bash invocation. Cold-import safe at module top: stdlib plus the
dependency-free siblings.
"""

import json
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from types import ModuleType
from typing import Final
from urllib.parse import quote

from hooks.scripts.foreign_branch_push_argv import PushSpec
from hooks.scripts.foreign_branch_push_git import (
    GitProbe,
    git_probe,
    live_remote_range,
    push_target_policy,
    remote_branch_oid,
    repository_at,
)
from hooks.scripts.foreign_branch_push_read import ParsedPushes, push_specs
from hooks.scripts.foreign_branch_push_text import (
    UNPINNED,
    default_branch_rewrite_refusal,
    foreign_commits_body,
    foreign_mr_body,
    no_repository_body,
    refusal,
    unobtainable_body,
    unowned_branch_body,
    unread_refusal,
)
from hooks.scripts.managed_repo import teatree_src_on_path

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# registers and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("foreign_branch_push_gate", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.foreign_branch_push_gate", sys.modules[__name__])

GATE_SETTING: Final[str] = "foreign_branch_push_gate_enabled"
GATE_ID: Final[str] = "foreign_branch_push"

_SUBSTITUTION_RE: Final[re.Pattern[str]] = re.compile(r"[$`]")
_HEADS_PREFIX: Final[str] = "refs/heads/"
_TAGS_PREFIX: Final[str] = "refs/tags/"
_HEAD_UNPINNABLE: Final[str] = "the pushed branch (HEAD is detached or unreadable)"


@dataclass(frozen=True, slots=True)
class _ForgeCtx:
    """Where a push's remote lives on a forge we recognise."""

    forge: ModuleType
    kind: str
    repo_path: str
    slug: str


def handle_block_foreign_branch_push(data: dict) -> bool:
    """Refuse a push onto a branch some other author owns; allow everything else.

    Returns ``True`` when a deny was emitted (the caller stops the handler
    chain). Only a ``Bash`` command that really pushes is evaluated, and only a
    push to a branch that already EXISTS on the remote reaches the forge.
    """
    from hooks.scripts.hook_router import (  # noqa: PLC0415 deferred back-import
        _fail_open_or_deny,
        _teatree_bool_setting,
        emit_pretooluse_deny,
    )

    if data.get("tool_name") != "Bash":
        return False
    tool_input = data.get("tool_input", {})
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if not isinstance(command, str) or not command:
        return False
    parsed = push_specs(command, str(data.get("cwd", "") or ""))
    if not parsed.specs and not parsed.unread:
        return False
    if not _teatree_bool_setting(GATE_SETTING, default=True):
        return False
    verdict = _refusal_verdict(parsed)
    if verdict is None:
        return False
    reason, destructive = verdict
    # A force rewrites a colleague's history and a delete removes it outright: both
    # are refused past the shared allowlist + master fail-open switch.
    return emit_pretooluse_deny(reason, gate_id=GATE_ID) if destructive else _fail_open_or_deny(data, reason)


def _refusal_verdict(parsed: ParsedPushes) -> tuple[str, bool] | None:
    """The refusal to emit and whether it bypasses the shared fail-open chain.

    Two SHORT-CIRCUITING passes, destructive items first: the bypass is a property
    of the WHOLE command, so once one destructive refusal is found no remaining
    item is worth its forge round trip — probing them all is how an established
    denial is lost to the hook's own timeout.
    """
    for found in _refusals(parsed, destructive=True):
        return found
    for found in _refusals(parsed, destructive=False):
        return found
    return None


def _refusals(parsed: ParsedPushes, *, destructive: bool) -> Iterator[tuple[str, bool]]:
    for item in parsed.unread:
        if item.force == destructive:
            yield unread_refusal(item.kind, item.leader), destructive
    for spec in parsed.specs:
        if spec.destructive == destructive and (reason := push_refusal(spec)):
            yield reason, destructive


def push_refusal(spec: PushSpec) -> str | None:
    """The refusal text for *spec*, or ``None`` when the push is ours to make."""
    if spec.unpinnable:
        return _refuse(spec, UNPINNED, unobtainable_body(spec.unpinnable))
    repo = repository_at(spec.work_dir)
    if not repo.ok:
        target = spec.refspecs[0] if spec.refspecs else "the pushed branch"
        return _refuse(spec, UNPINNED, no_repository_body(spec.work_dir, spec.remote, target, repo))
    branches, unpinnable, failed = _target_branches(spec)
    if unpinnable:
        return _refuse(spec, UNPINNED, unobtainable_body(unpinnable, failed))
    for branch in branches:
        if reason := _branch_refusal(spec, branch):
            return reason
    return None


def _refuse(spec: PushSpec, branch: str, body: str) -> str:
    return refusal(spec.remote, branch, body, force=spec.force)


def _branch_refusal(spec: PushSpec, branch: str) -> str | None:
    """The refusal for one target *branch*, or ``None`` to allow it."""
    remote_ref = remote_branch_oid(spec.work_dir, spec.remote, branch)
    if not remote_ref.ok:
        probe = f"`git ls-remote --heads {spec.remote} refs/heads/{branch}` (run in `{spec.work_dir}`)"
        return _refuse(spec, branch, unobtainable_body(probe, remote_ref))
    if not remote_ref.out:
        return None
    # Before ownership: an owned open MR sourced from the default branch must not
    # answer "ours" for a rewrite no later rung would catch.
    if spec.destructive and (refusal_text := _default_branch_destructive_refusal(spec, branch)):
        return refusal_text
    return _ownership_refusal(spec, branch, remote_ref.out)


def _ownership_refusal(spec: PushSpec, branch: str, branch_oid: str) -> str | None:
    """Rungs 2-4 of the ladder: MR ownership, then live-commit authorship."""
    verdict, detail, failed = _open_mr_owner(spec, branch)
    if verdict == "ours":
        return None
    if verdict == "foreign":
        return _refuse(spec, branch, detail)
    if verdict == "unknown":
        return _refuse(spec, branch, unobtainable_body(detail, failed))
    return _foreign_commit_refusal(spec, branch, branch_oid)


def _default_branch_destructive_refusal(spec: PushSpec, branch: str) -> str | None:
    """Refuse a force or delete onto the remote's default branch; ``None`` when *branch* is not it."""
    probe = _git(spec, "symbolic-ref", "--short", f"refs/remotes/{spec.remote}/HEAD")
    remote_prefix = f"{spec.remote}/"
    if not probe.ok or not probe.out.startswith(remote_prefix):
        return _refuse(spec, branch, unobtainable_body(f"`{spec.remote}/HEAD` (the remote's default branch)", probe))
    if probe.out.removeprefix(remote_prefix) != branch:
        return None
    return default_branch_rewrite_refusal(spec.remote, branch)


# ── Target resolution ──────────────────────────────────────────────


def _target_branches(spec: PushSpec) -> tuple[tuple[str, ...], str, GitProbe | None]:
    """The remote branch names *spec* writes, the reason none could be pinned, and the probe that failed.

    The probe travels with the reason. Pinning the target runs a real git probe
    whose rc and stderr are exactly what tells an unreadable HEAD from a detached
    one, and dropping it left the refusal naming a question with no answer beside
    it — the failure the ladder's own docstring says it does not have.
    """
    if spec.refspecs:
        return _branches_of(spec.refspecs, spec)
    policy = push_target_policy(spec.work_dir, spec.remote, spec.config_overrides)
    if policy.unpinnable:
        return (), policy.unpinnable, policy.probe
    if policy.refspecs:
        return _branches_of(policy.refspecs, spec)
    current, failed = _current_branch(spec, prefer_upstream=policy.prefer_upstream)
    return ((current,), "", None) if current else ((), _HEAD_UNPINNABLE, failed)


def _branches_of(refspecs: tuple[str, ...], spec: PushSpec) -> tuple[tuple[str, ...], str, GitProbe | None]:
    """Every remote branch *refspecs* writes, or the first one this gate could not pin."""
    branches: list[str] = []
    for refspec in refspecs:
        if _SUBSTITUTION_RE.search(refspec):
            return (), f"the refspec `{refspec}` (a shell substitution this gate cannot expand)", None
        branch, failed = _refspec_branch(refspec, spec)
        if branch is None:
            return (), f"the refspec `{refspec}`", failed
        if branch:
            branches.append(branch)
    return tuple(branches), "", None


def _refspec_branch(refspec: str, spec: PushSpec) -> tuple[str | None, GitProbe | None]:
    """The remote BRANCH *refspec* writes; ``""`` for a tag, ``None`` when unpinnable."""
    source, separator, destination = refspec.removeprefix("+").partition(":")
    target = destination if separator else source
    if target.startswith(_TAGS_PREFIX):
        return "", None
    target = target.removeprefix(_HEADS_PREFIX)
    if target in {"HEAD", "@"}:
        # Measured on git 2.50.1: `git push origin HEAD` writes the LOCAL branch name.
        current, failed = _current_branch(spec, prefer_upstream=False)
        return (current or None), failed
    return (target or None), None


def _current_branch(spec: PushSpec, *, prefer_upstream: bool = True) -> tuple[str, GitProbe | None]:
    """The branch this push writes by default, else ``""`` and the probe that would not say.

    Under `push.default=current` git writes the LOCAL name, so preferring `@{u}` there
    judges a branch git leaves alone and lets the one it writes through unexamined.
    """
    if prefer_upstream:
        upstream = _git(spec, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        if upstream.ok and "/" in upstream.out:
            return upstream.out.split("/", 1)[1], None
    head = _git(spec, "symbolic-ref", "--short", "HEAD")
    return (head.out, None) if head.ok and head.out else ("", head)


# ── Probes ─────────────────────────────────────────────────────────


def _git(spec: PushSpec, *args: str) -> GitProbe:
    return git_probe(spec.work_dir, *args)


def _open_mr_owner(spec: PushSpec, branch: str) -> tuple[str, str, GitProbe | None]:
    """``(verdict, detail, failed probe)`` for the open MR/PR on *branch*: ours / foreign / none / unknown."""
    forge_ctx, unobtainable, failed = _forge_context(spec)
    if forge_ctx is None:
        return ("unknown", unobtainable, failed) if unobtainable else ("none", "", None)
    author, mr_ref, unanswered = _open_mr(forge_ctx.forge, forge_ctx.kind, forge_ctx.repo_path, branch)
    if unanswered:
        return "unknown", unanswered, None
    if not author:
        return "none", "", None
    ours = _our_logins(forge_ctx.forge, forge_ctx.kind, forge_ctx.forge.host_of_slug(forge_ctx.slug))
    if ours is None:
        return "unknown", f"`{forge_ctx.forge.FORGE_TOOL[forge_ctx.kind]} api user` (who we are on this forge)", None
    if author.lower() in ours:
        return "ours", "", None
    return "foreign", foreign_mr_body(author, mr_ref), None


def _forge_context(spec: PushSpec) -> tuple["_ForgeCtx | None", str, GitProbe | None]:
    """*spec*'s forge coordinates, or ``None`` plus the probe that went unanswered.

    ``(None, "", None)`` is the one non-failure ``None``: the remote is not a forge
    we recognise, so there is no MR to own and authorship falls to the commits.

    ``git remote get-url`` is a git probe like any other, and its rc and stderr say
    which failure it was — a remote that does not exist, a repository git could not
    read, a `.git/config` it could not open. Returning only the argv named the
    question and dropped the answer, which is the shape the ladder's docstring
    claims this gate does not have.
    """
    forge = _forge_seam()
    if forge is None:
        return None, "the teatree forge seam (`teatree.hooks.foreign_mr_cli`) could not be imported", None
    remote_url = _git(spec, "remote", "get-url", spec.remote)
    if not remote_url.ok or not remote_url.out:
        return None, f"`git remote get-url {spec.remote}`", remote_url
    slug = forge.slug_for_remote_url(remote_url.out)
    kind, repo_path = forge.forge_and_repo_path(slug)
    if not kind:
        return None, "", None
    return _ForgeCtx(forge=forge, kind=kind, repo_path=repo_path, slug=slug), "", None


def _open_mr(forge: ModuleType, kind: str, repo_path: str, branch: str) -> tuple[str, str, str]:
    """``(author, mr_ref, failed_probe)`` for the open MR/PR whose source is *branch*."""
    if kind == forge.GITHUB:
        argv = ["pr", "list", "--repo", repo_path, "--head", branch, "--state", "open"]
        rows = _forge_rows(forge, kind, [*argv, "--json", "number,author,url", "--limit", "5"])
        author_key, ref_key = "login", "url"
    else:
        project = quote(repo_path, safe="")
        query = f"projects/{project}/merge_requests?source_branch={quote(branch, safe='')}&state=opened"
        rows = _forge_rows(forge, kind, ["api", query])
        author_key, ref_key = "username", "web_url"
    if rows is None:
        return "", "", f"`{forge.FORGE_TOOL[kind]}` listing the open MRs for `{branch}`"
    for row in rows:
        node = row.get("author")
        author = node.get(author_key, "") if isinstance(node, dict) else ""
        if isinstance(author, str) and author.strip():
            return author.strip(), str(row.get(ref_key) or row.get("number") or "(no url)"), ""
    return "", "", ""


def _forge_rows(forge: ModuleType, kind: str, argv: list[str]) -> list[dict] | None:
    """The JSON array a forge CLI returned, or ``None`` when the question went unasked."""
    stdout = forge.run_forge_tool(forge.FORGE_TOOL[kind], argv).stdout
    if stdout is None:
        return None
    if not stdout.strip():
        return []
    try:
        payload = json.loads(stdout)
    except ValueError:
        return None
    return payload if isinstance(payload, list) else None


def _our_logins(forge: ModuleType, kind: str, host: str) -> frozenset[str] | None:
    """Every login that counts as us on this forge, or ``None`` when we cannot tell."""
    stdout = forge.run_forge_tool(forge.FORGE_TOOL[kind], ["api", "user"]).stdout
    if stdout is None:
        return None
    try:
        user = json.loads(stdout)
    except ValueError:
        return None
    key = "login" if kind == forge.GITHUB else "username"
    login = user.get(key, "") if isinstance(user, dict) else ""
    if not isinstance(login, str) or not login.strip():
        return None
    return frozenset({login.strip().lower(), *forge.declared_self_identities(host)})


def _foreign_commit_refusal(spec: PushSpec, branch: str, branch_oid: str) -> str | None:
    """Refuse when the live un-MR'd branch carries only another author's commits."""
    live = live_remote_range(spec.work_dir, spec.remote, branch, branch_oid)
    if live.failed:
        return _refuse(spec, branch, unobtainable_body(live.failed, live.probe))
    if branch == live.base_branch:
        # The default branch against itself: an empty range here is arithmetic, not
        # an unowned branch. Rung 0b already refused the destructive case.
        return default_branch_rewrite_refusal(spec.remote, branch) if spec.destructive else None
    log = _git(spec, "log", "--format=%ae%n%an", f"{live.base_oid}..{branch_oid}")
    if not log.ok:
        return _refuse(
            spec,
            branch,
            unobtainable_body(
                f"`git log <live-{live.base_branch}>..<live-{branch}>` (the remote authorship range)", log
            ),
        )
    authors = {line.strip() for line in log.out.splitlines() if line.strip()}
    if authors & _our_git_identities(spec):
        return None
    if not authors:
        return _refuse(spec, branch, unowned_branch_body(spec.remote, branch, live.base_branch))
    return _refuse(spec, branch, foreign_commits_body(branch, tuple(sorted(authors))))


def _our_git_identities(spec: PushSpec) -> set[str]:
    return {
        probe.out
        for probe in (_git(spec, "config", "user.email"), _git(spec, "config", "user.name"))
        if probe.ok and probe.out
    }


def _forge_seam() -> ModuleType | None:
    """The teatree foreign-MR seam module, or ``None`` when it cannot be imported.

    That module already re-exports the whole forge surface this gate needs
    (``slug_for_remote_url``, ``forge_and_repo_path``, ``host_of_slug``,
    ``FORGE_TOOL``, ``GITHUB``, ``run_forge_tool``, ``declared_self_identities``),
    so one import carries all of it — and it is the module that already owns the
    "whose MR is this branch's?" question.
    """
    try:
        with teatree_src_on_path():
            from teatree.hooks import foreign_mr_cli  # noqa: PLC0415 — deferred: cold-hook import
    except Exception:  # noqa: BLE001 — an unimportable seam is an UNANSWERED probe, which REFUSES.
        return None
    return foreign_mr_cli
