"""`t3 doctor` identity-wiring check — the deployment does not know who it is (#4241 follow-up).

A HARD FAIL, unlike its surfacing-only neighbours, because both faults it reports make the factory
unable to ship while every other check stays green: an unconfigured reviewer allowlist refuses the
owner's own CLEAR at merge time, and an unresolvable scoped forge credential opens MRs under the
owner, who is then the one person GitLab will not accept an approval from. Both were discovered by
a human hitting them, hours apart from the deploy that caused them.

The judgement lives in :mod:`teatree.core.identity_wiring`; this module is the reads.
"""

import logging
from typing import TYPE_CHECKING

import typer

from teatree.core.identity_wiring import (
    AuthoringIdentity,
    IdentityFault,
    authoring_identity_fault,
    owner_identity_fault,
    unapprovable_open_mr_fault,
)

if TYPE_CHECKING:
    from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)


def _reviewer_admission_fault() -> IdentityFault | None:
    """The configured reviewer allowlist, as the merge keystone itself resolves it."""
    from teatree.config import (  # noqa: PLC0415 — deferred: config read at call time
        effective_independent_reviewer_identities,
        get_effective_settings,
    )

    return owner_identity_fault(effective_independent_reviewer_identities(get_effective_settings()))


def _authoring_faults() -> list[IdentityFault]:
    """One fault per repo whose declared non-owner author this venue cannot act as.

    Asked of the repos ``t3 update`` already walks, deduplicated by remote: the question is about a
    credential, and two clones of one remote share the answer. A remote no overlay scopes answers
    ``OWNER`` and contributes nothing.

    The classification is repo-keyed, not read off the ambient overlay: a repo one overlay declares
    is written by a bot reads as the OWNER's from every OTHER overlay, so an ambient-only read
    reported a clean bill on precisely the repo whose identity was wrong.
    """
    from teatree.cli.update import _collect_repos  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.core.authoring_credential import (  # noqa: PLC0415 — deferred: needs the app registry
        authoring_identity_for_remote,
    )
    from teatree.core.backend_factory import get_overlay  # noqa: PLC0415 — deferred: needs the app registry
    from teatree.utils import git  # noqa: PLC0415 — deferred: keeps CLI startup light

    config = get_overlay().config
    seen: set[str] = set()
    faults: list[IdentityFault] = []
    for _name, path in _collect_repos():
        remote = git.remote_url(repo=str(path))
        if not remote or remote in seen:
            continue
        seen.add(remote)
        identity = authoring_identity_for_remote(remote, fallback=config)
        if (fault := authoring_identity_fault(remote=remote, identity=identity)) or (
            identity is AuthoringIdentity.DISTINCT and (fault := _open_mr_fault(remote, str(path)))
        ):
            faults.append(fault)
    return faults


def _open_mr_fault(remote: str, repo_path: str) -> IdentityFault | None:
    """The already-open unapprovable MRs on *remote*, or ``None`` when the read says nothing.

    Scoped to a DECLARED-author remote whose credential resolves: those are the only repos where
    an owner-authored MR is a fault rather than the norm, and there is normally one of them. An
    unreadable forge yields ``None`` — a WARN over an unreachable API would fire on every offline
    run, and the create-time refusal is the primary guard either way.
    """
    from teatree.core.authoring_credential import approver_identities  # noqa: PLC0415 — deferred: config read
    from teatree.core.backend_factory import code_host_for_repo_from_overlay  # noqa: PLC0415 — deferred: app registry
    from teatree.utils import git_remote  # noqa: PLC0415 — deferred: keeps CLI startup light

    try:
        host = code_host_for_repo_from_overlay(repo_path)
        if host is None:
            return None
        slug = git_remote.slug_from_remote(remote)
        open_prs = host.list_prs(repo=slug, state="opened")
    except Exception:  # noqa: BLE001 — an unreachable forge is missing evidence, never a fault.
        logger.warning("could not read the open merge requests on %s", remote)
        return None
    return unapprovable_open_mr_fault(
        remote=remote, authors_by_ref=_authors_by_ref(open_prs), approvers=approver_identities()
    )


def _authors_by_ref(open_prs: "list[RawAPIDict]") -> dict[str, str]:
    """``{web_url or !iid: author username}`` for every PR whose author the payload carries."""
    authors: dict[str, str] = {}
    for payload in open_prs:
        author = payload.get("author")
        username = str(author.get("username", "")) if isinstance(author, dict) else ""
        if not username:
            continue
        authors[str(payload.get("web_url") or f"!{payload.get('iid', '?')}")] = username
    return authors


def check_identity_wiring() -> bool:
    """FAIL when this deployment cannot resolve an identity it needs to act as or be reviewed by.

    Crash-proof: a probe that raises degrades to one WARN and does NOT fail the run, so a broken
    read can never masquerade as a configuration fault the operator would then chase.
    """
    try:
        faults = [fault for fault in (_reviewer_admission_fault(), *_authoring_faults()) if fault is not None]
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Identity-wiring check crashed: {exc.__class__.__name__}: {exc}")
        return True
    for fault in faults:
        for line in fault.lines():
            typer.echo(line)
    return not faults
