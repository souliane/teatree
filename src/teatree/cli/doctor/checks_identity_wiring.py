"""`t3 doctor` identity-wiring check — the deployment does not know who it is (#4241 follow-up).

A HARD FAIL, unlike its surfacing-only neighbours, because both faults it reports make the factory
unable to ship while every other check stays green: an unconfigured reviewer allowlist refuses the
owner's own CLEAR at merge time, and an unresolvable scoped forge credential opens MRs under the
owner, who is then the one person GitLab will not accept an approval from. Both were discovered by
a human hitting them, hours apart from the deploy that caused them.

The judgement lives in :mod:`teatree.core.identity_wiring`; this module is the reads.
"""

import typer

from teatree.core.identity_wiring import IdentityFault, authoring_identity_fault, owner_identity_fault


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
    credential, and two clones of one remote share the answer. An overlay that scopes no credential
    answers ``OWNER`` everywhere and contributes nothing.
    """
    from teatree.cli.update import _collect_repos  # noqa: PLC0415 — deferred: keeps CLI startup light
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
        if fault := authoring_identity_fault(remote=remote, identity=config.authoring_identity_on(remote)):
            faults.append(fault)
    return faults


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
