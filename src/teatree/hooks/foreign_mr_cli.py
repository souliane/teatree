"""Shell seam for the foreign-open-MR pre-push guard.

The guard refuses a push to a branch that backs an OPEN MR/PR authored by
someone else — pushing there silently rewrites a colleague's MR. It resolved
that MR with ``gh`` alone and exited early when ``gh`` was absent, so on a
GitLab remote it could never fire at all: the one forge it knew how to ask was
the one the branch does not live on, and a guard that cannot fire reads exactly
like a guard that found nothing.

This CLI is the thin seam onto the host-keyed routing
:mod:`teatree.hooks._forge_tool` owns, mirroring
:mod:`teatree.hooks.repo_visibility_cli`. It is deliberately Django-free — the
import chain is ``utils.run`` and the ``_repo_visibility`` slug normaliser — so
a pre-push hook pays an interpreter start, not a framework boot.

Each forge is asked in its OWN idiom — ``gh`` carries a built-in ``--jq``, while
``glab api`` has no such flag (passing one makes it exit non-zero on an unknown
flag) — and both listings come back as JSON. :func:`open_mrs_in` reads them, for
this guard and for the PreToolUse foreign-branch push gate alike: every open MR of
the remote's own project whose source is the branch counts, and one opened from a
fork does not.

Usage::

    python -m teatree.hooks.foreign_mr_cli <remote-url-or-slug> <branch>

Prints exactly one line and exits 0 whenever it could run:

``NONE``
    no open MR backs the branch, OR the guard never got as far as asking who
    owns one — an unparsable remote, an unrecognised host, an absent forge CLI,
    a failed MR query. The caller lets the push through.
``UNKNOWN <number> <author> <tool> <cause>``
    an open MR backs the branch and the pushing identity could NOT be resolved,
    so OWN-vs-FOREIGN has no ground to be decided on. The caller REFUSES: an
    unresolvable identity is not evidence the push is harmless, and answering
    NONE here made the guard's protection depend on which venue it ran in. The
    ``cause`` runs to the end of the line and is a phrase naming what each
    attempt was OBSERVED to do (:attr:`ForgeProbe.unresolved`) — a timeout and an
    unauthenticated CLI have different remedies, so the refusal reports the one
    it saw instead of asserting the credential is at fault.
``ALIAS_UNRESOLVED <alias>``
    the SSH alias did not resolve to a forge host; the caller refuses with a
    HostName remedy without asserting that any open MR exists.
``REMOTE_EMPTY``
    the push names no remote URL, so no MR could be looked up; the caller refuses
    with a set-the-remote remedy and names no MR.
``OWN <number>``
    the open MR is the configured identity's own, or one of the logins the
    operator declared for that host in ``self_forge_identities`` — a bot that
    authors our MRs so we stay eligible to approve them.
``FOREIGN <number> <author> <us>``
    a CONFIRMED foreign open MR — the only verdict the caller blocks on.

Field 3 is the MR author on the MR-backed blocking verdicts; the alias verdict
carries only its alias, and the empty-remote verdict nothing.
"""

import json
import sys
from dataclasses import dataclass
from typing import Final
from urllib.parse import quote

from teatree.config import cold_reader
from teatree.hooks._forge_tool import FORGE_TOOL, GITHUB, GITLAB, forge_and_repo_path, host_of_slug
from teatree.hooks._repo_visibility import ForgeProbe, run_forge_tool, slug_for_remote_url

NONE_VERDICT: Final[str] = "NONE"
UNKNOWN_VERDICT: Final[str] = "UNKNOWN"
ALIAS_UNRESOLVED_VERDICT: Final[str] = "ALIAS_UNRESOLVED"
#: The push names no remote URL, so no MR could be looked up — there is no number or author to report.
REMOTE_EMPTY_VERDICT: Final[str] = "REMOTE_EMPTY"

#: Host-keyed logins the operator ALSO acts as — its own bots, never a teammate.
SELF_IDENTITIES_SETTING: Final[str] = "self_forge_identities"

#: The probe ran and exited 0, but its payload named no login.
PROBE_NO_LOGIN: Final[str] = "an answer naming no login"
PROBE_NOT_JSON: Final[str] = "an answer that is not JSON"
_NOT_MRS: Final[str] = "an answer that is not a JSON array of MRs"
_NO_AUTHOR: Final[str] = "an answer listing an MR with no readable author"


@dataclass(frozen=True, slots=True)
class OpenMr:
    """The open MR/PR backing a branch, and who authored it."""

    number: str
    author: str
    url: str = ""


@dataclass(frozen=True, slots=True)
class Identity:
    """Who this venue is on a forge, or the cause the probe could not say.

    An empty ``login`` always carries a non-empty ``unresolved`` — the refusal
    downstream names what was observed, and it can only do that if the outcomes
    reach it apart.
    """

    login: str = ""
    unresolved: str = ""


def _identity_of(probe: ForgeProbe, login: str) -> Identity:
    if login:
        return Identity(login=login)
    return Identity(unresolved=probe.unresolved or PROBE_NO_LOGIN)


def _on_host(argv: list[str], host: str) -> list[str]:
    """*argv* aimed at *host*: a forge CLI otherwise answers for its own default host."""
    return [*argv, "--hostname", host] if host else argv


def _github_identity(host: str) -> Identity:
    probe = run_forge_tool(FORGE_TOOL[GITHUB], _on_host(["api", "user", "--jq", ".login"], host))
    return _identity_of(probe, (probe.stdout or "").strip())


def _gitlab_identity(host: str) -> Identity:
    probe = run_forge_tool(FORGE_TOOL[GITLAB], _on_host(["api", "user"], host))
    return _identity_of(probe, _username_of(probe.stdout))


def _username_of(stdout: str | None) -> str:
    """The ``username`` a ``glab api user`` payload names, or ``""``."""
    try:
        user = json.loads(stdout or "")
    except ValueError:
        return ""
    username = user.get("username") if isinstance(user, dict) else None
    return username.strip() if isinstance(username, str) else ""


def mr_listing_argv(forge: str, repo_path: str, branch: str, host: str) -> list[str]:
    """The forge-CLI argv listing every open MR/PR on *host* whose source branch is *branch*."""
    if forge == GITHUB:
        repo = f"{host}/{repo_path}" if host else repo_path
        fields = "number,author,url,isCrossRepository"
        return ["pr", "list", "--repo", repo, "--head", branch, "--state", "open", "--limit", "100", "--json", fields]
    query = f"projects/{quote(repo_path, safe='')}/merge_requests?source_branch={quote(branch, safe='')}&state=opened"
    return _on_host(["api", f"{query}&per_page=100"], host)


def open_mrs_in(forge: str, stdout: str) -> tuple[tuple[OpenMr, ...], str]:
    """Every readable open MR/PR of this project a listing answered with — forks skipped — and why any was not."""
    try:
        rows = json.loads(stdout.strip() or "[]")
    except ValueError:
        return (), PROBE_NOT_JSON
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return (), _NOT_MRS
    author_key, number_key, url_key = ("login", "number", "url") if forge == GITHUB else ("username", "iid", "web_url")
    mrs: list[OpenMr] = []
    unreadable = ""
    for row in rows:
        if _opened_from_a_fork(row):
            continue
        node = row.get("author")
        author = node.get(author_key) if isinstance(node, dict) else None
        if not isinstance(author, str) or not author.strip():
            unreadable = _NO_AUTHOR
            continue
        mrs.append(OpenMr(number=str(row.get(number_key, "")), author=author.strip(), url=str(row.get(url_key) or "")))
    return tuple(mrs), unreadable


def _opened_from_a_fork(row: dict) -> bool:
    """Whether *row* is an MR from another project: its source branch is not the one a push to this remote writes."""
    if row.get("isCrossRepository") is True:
        return True
    source, target = row.get("source_project_id"), row.get("target_project_id")
    return source is not None and target is not None and source != target


def declared_self_identities_raw(host: str) -> tuple[str, ...]:
    """The logins the operator declared as its own on *host*, AS WRITTEN.

    The one read of ``self_forge_identities``, so the three consumers of that
    setting — this push gate, the review-candidate self-author skip, and the #162
    issue-hygiene authority guard — cannot drift on what it means. Case is
    preserved because they do not agree on folding: this gate lowercases, the
    review-candidate comparison is case-sensitive, and folding here would silently
    narrow it. Each caller folds (or does not) as its own comparison requires.

    Lives in the hooks layer because the cold PreToolUse subprocess reads it too,
    and that subprocess may import only stdlib plus :mod:`teatree.config`.
    """
    if not host:
        return ()
    declared = cold_reader.mapping_setting(SELF_IDENTITIES_SETTING).get(host)
    if not isinstance(declared, list):
        return ()
    return tuple(e.strip() for e in declared if isinstance(e, str) and e.strip())


def declared_self_identities(host: str) -> frozenset[str]:
    """Logins the operator declared as its own on *host*, lowercased.

    A forge CLI answers with ONE login, so an MR authored by our own bot reads
    exactly like a teammate's. Declaring nothing keeps the verdict unchanged.
    """
    return frozenset(entry.lower() for entry in declared_self_identities_raw(host))


def _unresolved_remote_verdict(remote: str) -> str:
    if ":" in remote and "://" not in remote:
        alias = remote.partition(":")[0].rsplit("@", 1)[-1]
        return f"{ALIAS_UNRESOLVED_VERDICT} {alias}"
    return (
        f"{UNKNOWN_VERDICT} 0 unknown ssh remote could not be resolved; add a HostName for its alias in ~/.ssh/config"
    )


def foreign_mr_verdict(remote: str, branch: str) -> str:
    """Return the one-line verdict for *branch* on *remote* (see the module docstring).

    Fail-open up to the point an open MR is found: an unparsable remote, an
    unrouted host, an absent branch and a failed MR query all collapse to
    :data:`NONE_VERDICT`, so a venue that cannot reach the forge at all still
    pushes. A listing with rows it cannot read is judged on its readable rows
    first, and collapses to NONE only when none of them decides. Once an MR IS
    found the question is live, and an identity neither the declaration nor the
    probe can settle yields :data:`UNKNOWN_VERDICT` rather than silently allowing it.
    """
    if not remote.strip():
        return REMOTE_EMPTY_VERDICT
    slug = slug_for_remote_url(remote.strip())
    forge, repo_path = forge_and_repo_path(slug)
    if not slug:
        return _unresolved_remote_verdict(remote)
    if not forge or not branch.strip():
        return NONE_VERDICT
    host = host_of_slug(slug)
    listing = run_forge_tool(FORGE_TOOL[forge], mr_listing_argv(forge, repo_path, branch, host)).stdout
    mrs, unreadable = open_mrs_in(forge, listing) if listing is not None else ((), "")
    # A cold config read, so it answers in the very venues the probe cannot;
    # asking it only AFTER the probe refused pushes to MRs already declared ours.
    declared = declared_self_identities(host) if mrs else frozenset()
    identity: Identity | None = None
    for mr in (mr for mr in mrs if mr.author.lower() not in declared):
        identity = identity or (_github_identity(host) if forge == GITHUB else _gitlab_identity(host))
        if not identity.login:
            return f"{UNKNOWN_VERDICT} {mr.number} {mr.author} {FORGE_TOOL[forge]} {identity.unresolved}"
        if mr.author.lower() != identity.login.lower():
            return f"FOREIGN {mr.number} {mr.author} {identity.login}"
    return f"OWN {mrs[0].number}" if mrs and not unreadable else NONE_VERDICT


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    expected_args = 2
    if len(args) != expected_args or not args[0].strip():
        sys.stdout.write(f"{NONE_VERDICT}\n")
        return 0
    sys.stdout.write(f"{foreign_mr_verdict(args[0], args[1])}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
