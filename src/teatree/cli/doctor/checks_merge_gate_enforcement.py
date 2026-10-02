"""`t3 doctor` check — a workflow-declared blocking GATE branch protection never requires.

The judgement lives in :mod:`teatree.core.merge.gate_enforcement_drift`; this module is the
live read. Self-scoping: it only probes when the running clone's OWN
``.github/workflows/ci.yml`` defines all three expected gate jobs, so a fork under a
different slug — or a future rename of any of them — degrades to "nothing to check" rather
than a false WARN about a workflow that no longer matches.
"""

from pathlib import Path

import typer


def _declares_expected_gate_jobs(repo_root: "object") -> bool:
    """True iff *repo_root*'s own CI workflow still defines every expected gate job key.

    A plain job-key search (``"  <name>:"`` at column 2, the workflow's own indent), not a
    YAML parse — the workflow is large and this only needs to know the names are still
    there, not to understand the file.
    """
    from teatree.core.merge.gate_enforcement_drift import (  # noqa: PLC0415 — deferred: pulls in Django models via teatree.core.merge's __init__, must not run before apps are ready
        EXPECTED_MERGE_GATE_CONTEXTS,
    )

    workflow = Path(str(repo_root)) / ".github" / "workflows" / "ci.yml"
    try:
        text = workflow.read_text()
    except OSError:
        return False
    return all(f"\n  {name}:\n" in text for name in EXPECTED_MERGE_GATE_CONTEXTS)


def _live_required_contexts(repo_root: "object") -> frozenset[str] | None:
    """The live branch-protection required-context set for *repo_root*'s own default branch.

    Unions the same two GitHub sources the merge keystone does — the rules endpoint (rulesets
    + classic protection) and the legacy protection endpoint — via the identical parsers, so
    this check and the keystone can never disagree about what "required" means. ``None`` when
    neither source could be read (probe outage, not evidence of anything).
    """
    from teatree.backends.forge_merge_rpc import (  # noqa: PLC0415 — deferred: keeps CLI startup light
        _github_protection_required_contexts,
        _github_rules_required_contexts,
    )
    from teatree.utils.git_branch import default_branch  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.utils.git_remote_ops import remote_slug  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.utils.run import (  # noqa: PLC0415 — deferred: keeps CLI startup light
        CommandFailedError,
        run_allowed_to_fail,
    )

    repo = str(repo_root)
    slug = remote_slug(repo=repo)
    if not slug:
        return None
    try:
        branch = default_branch(repo=repo)
    except RuntimeError:
        return None

    def _gh_api(path: str) -> tuple[int, str, str]:
        try:
            result = run_allowed_to_fail(["gh", "api", path], expected_codes=None, timeout=15)
        except (CommandFailedError, OSError):
            return (1, "", "")
        return (result.returncode, result.stdout, result.stderr)

    rules_rc, rules_out, rules_err = _gh_api(f"repos/{slug}/rules/branches/{branch}")
    rules_contexts = _github_rules_required_contexts(rules_rc, rules_out, rules_err)
    prot_rc, prot_out, prot_err = _gh_api(f"repos/{slug}/branches/{branch}/protection/required_status_checks")
    protection_contexts = _github_protection_required_contexts(prot_rc, prot_out, prot_err)
    determinate = [contexts for contexts in (rules_contexts, protection_contexts) if contexts is not None]
    if not determinate:
        return None
    union: set[str] = set()
    for contexts in determinate:
        union |= contexts
    return frozenset(union)


def _check_merge_gates_enforced() -> None:
    """WARN for every workflow-declared GATE context branch protection does not require.

    Surfacing-only — it never gates the exit code, like its `checks_gate_inertness` sibling:
    whether to widen branch protection (changing what blocks every future PR) is an owner
    decision, not one this probe makes. Crash-proof: any error degrades to one WARN line.
    """
    from teatree import paths  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.core.merge.gate_enforcement_drift import (  # noqa: PLC0415 — deferred: pulls in Django models via teatree.core.merge's __init__, must not run before apps are ready
        unenforced_gate_contexts,
    )

    try:
        repo_root = paths.CODE_REPO_ROOT
        if not _declares_expected_gate_jobs(repo_root):
            return
        required = _live_required_contexts(repo_root)
        missing = unenforced_gate_contexts(required)
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Merge-gate-enforcement check crashed: {exc.__class__.__name__}: {exc}")
        return
    if not missing:
        return
    for context in sorted(missing):
        typer.echo(
            f'WARN  CI job {context!r} is commented GATE ("PR cannot merge") in '
            "ci.yml but branch protection does not require it — a red run never blocks a merge."
        )
    typer.echo(
        "WARN  Add the job(s) above to the repo's required status checks to make the workflow's "
        "own promise hold, or drop the GATE wording if it is meant to stay advisory."
    )
