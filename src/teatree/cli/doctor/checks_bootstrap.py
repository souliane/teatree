"""Fresh-box bootstrap-hardening doctor checks (umbrella #3404).

Three gates that turn silent late failures on a freshly-provisioned or migrated
box into loud, up-front ones:

- :func:`_check_gh_token_permissions` (#3405) — the GitHub token authenticates but
    is missing a write permission the loop needs (``issues``/``pull_requests``/
    ``contents``). Mirrors ``deploy/entrypoint.sh``'s ``init_preflight`` probe.
- :func:`_check_provision_concurrency_from_host` (#3409/#3434) — a stale small-box
    ``provision_max_concurrency`` pin throttling a more capable host. It only
    auto-clears a pin the ENTRYPOINT seeded (never an operator's deliberate one),
    and only under ``t3 doctor --repair`` — a plain ``t3 doctor`` never mutates.
- :func:`_check_claude_settings_drift` (#3410) — the host ``~/.claude/settings.json``
    managed keys disagree with the one committed template the containers seed from.

Each is crash-proof (any inspection error degrades to a pass/WARN) so a bootstrap
diagnostic never aborts the whole doctor run.
"""

import os
from pathlib import Path

import typer

from teatree.utils.run import CommandFailedError, run_allowed_to_fail


def _teatree_repo_root() -> Path | None:
    """The installed teatree clone root (``…/src/teatree`` → repo).

    ``teatree.__file__`` is ``<repo>/src/teatree/__init__.py``; the repo root is
    its third parent — the same derivation ``_check_entrypoint_is_primary_clone``
    uses. Typed ``| None`` so callers can treat a future non-editable/packaged
    layout (no repo root) uniformly with the template-absent skip.
    """
    import teatree  # noqa: PLC0415 — deferred: keeps CLI startup light

    return Path(teatree.__file__).resolve().parents[2]


def _slug_from_repo_url(url: str) -> str | None:
    """Parse ``owner/repo`` out of an https or ssh GitHub remote URL, else ``None``."""
    text = url.strip()
    for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    else:
        return None
    owner, sep, rest = text.removesuffix(".git").partition("/")
    repo = rest.partition("/")[0]
    return f"{owner}/{repo}" if sep and owner and repo else None


def _resolve_repo_slug() -> str | None:
    """Resolve the ``owner/repo`` the deploy token operates on.

    Prefers ``TEATREE_REPO_URL`` (the deploy env), falling back to the teatree
    clone's ``origin`` remote. ``None`` when neither yields a GitHub slug — the
    caller then skips the token probe rather than guessing.
    """
    env_url = os.environ.get("TEATREE_REPO_URL", "").strip()
    if env_url:
        env_slug = _slug_from_repo_url(env_url)
        if env_slug is not None:
            return env_slug
    repo = _teatree_repo_root()
    if repo is None:
        return None
    try:
        remote = run_allowed_to_fail(["git", "-C", str(repo), "remote", "get-url", "origin"], expected_codes=None)
    except (OSError, CommandFailedError):
        return None
    return _slug_from_repo_url(remote.stdout) if remote.returncode == 0 else None


def _check_gh_token_permissions() -> bool:
    """FAIL when the GitHub token authenticates but lacks a write permission the loop needs (#3405).

    Mirrors ``deploy/entrypoint.sh``'s ``init_preflight`` in Python so the same
    late-failure — ``Resource not accessible by personal access token`` mid-run —
    is caught by ``t3 doctor`` too. Skips (passes) when the probe cannot reach a
    confident verdict: ``gh`` absent, no resolvable repo slug, or the API
    unreachable — a network fault must never redden the run or page the owner.
    A genuinely missing write permission is a hard FAIL naming each scope.
    """
    from teatree.core.gates.gh_token_preflight import probe_token_permissions  # noqa: PLC0415 — deferred import

    slug = _resolve_repo_slug()
    if slug is None:
        return True
    try:
        probe = probe_token_permissions(slug)
    except Exception as exc:  # noqa: BLE001 — a probe failure warns and passes, never blocks doctor
        typer.echo(f"WARN  Could not probe the GitHub token permissions: {exc}")
        return True
    if probe.indeterminate_reason is not None:
        return True
    if not probe.missing:
        return True
    typer.echo(
        f"FAIL  The GitHub token cannot exercise {', '.join(probe.missing)} on {slug} — "
        "the loop's `gh issue`/`gh pr`/push writes will fail mid-run with "
        "'Resource not accessible by personal access token'. Grant the missing "
        "permission(s) on TEATREE_GH_TOKEN and re-deploy, then re-run `t3 doctor check`."
    )
    return False


def _check_provision_concurrency_from_host(*, repair: bool = False) -> bool:
    """Surface — and under ``--repair`` clear — a stale entrypoint-seeded concurrency pin (#3409/#3434).

    A box migrated onto more cores must not silently keep an old box's
    hard-serialized ``provision_max_concurrency`` pin. When the DB carries a pin
    STRICTLY BELOW the host-derived auto value (``nCPU/2``, cgroup-aware):

    * a pin the ENTRYPOINT seeded and the operator never touched (``seeded_by
        == entrypoint`` AND ``value == seed_value``) is a stale-migration
        artifact — cleared under ``repair=True`` so the runtime auto-derives,
        WARNed otherwise;
    * a pin with any other provenance is an operator's deliberate choice —
        WARNed, NEVER deleted, regardless of ``repair``.

    A plain ``t3 doctor`` (``repair=False``) therefore NEVER mutates the DB. A pin
    at/above the host auto, or no pin, passes silently. Reads the ORM, so it runs
    post-``ensure_django``. Crash-proof. Always returns ``True`` — surfacing-only.
    """
    from teatree.core.models.config_setting import (  # noqa: PLC0415 — deferred (ORM)
        ENTRYPOINT_SEEDER,
        GLOBAL_SCOPE,
        ConfigSetting,
    )
    from teatree.utils.ram_probe import default_provision_concurrency  # noqa: PLC0415 — deferred import

    try:
        row = ConfigSetting.objects.filter(scope=GLOBAL_SCOPE, key="provision_max_concurrency").first()
        if row is None:
            return True
        pinned = row.value
        if not isinstance(pinned, int) or isinstance(pinned, bool) or pinned <= 0:
            return True
        host_auto = default_provision_concurrency()
        if pinned >= host_auto:
            return True
        entrypoint_seeded = row.seeded_by == ENTRYPOINT_SEEDER and row.value == row.seed_value
        if not entrypoint_seeded:
            typer.echo(
                f"WARN  provision_max_concurrency={pinned} is pinned below this host's auto-derived "
                f"{host_auto} (nCPU/2), but the pin was NOT set by the deploy seed — it looks like a "
                f"deliberate operator choice, so it is left untouched. Clear it with "
                f"`t3 teatree config_setting clear provision_max_concurrency` if it is stale."
            )
            return True
        if repair:
            ConfigSetting.objects.clear("provision_max_concurrency")
            typer.echo(
                f"WARN  Cleared a stale entrypoint-seeded provision_max_concurrency={pinned} pin below this "
                f"host's auto-derived {host_auto} (nCPU/2) — it hard-serialized provisioning carried over "
                f"from a smaller box. Concurrency now auto-derives from the host; re-pin explicitly with "
                f"`t3 teatree config_setting set provision_max_concurrency <N>` if intended."
            )
        else:
            typer.echo(
                f"WARN  A stale entrypoint-seeded provision_max_concurrency={pinned} pin is below this host's "
                f"auto-derived {host_auto} (nCPU/2). Run `t3 doctor check --repair` to clear it so concurrency "
                f"auto-derives from the host."
            )
    except Exception as exc:  # noqa: BLE001 — a doctor check must never crash the run
        typer.echo(f"WARN  provision-concurrency check crashed: {exc.__class__.__name__}: {exc}")
    return True


def _check_claude_settings_drift() -> bool:
    """WARN when the host ~/.claude/settings.json managed keys drift from the committed template (#3410).

    ``deploy/claude-settings.template.json`` is the single source of truth the
    containers seed from; the host should agree on the managed keys (model,
    permission mode + allow-list, ``autoMode.allow`` grants, tool-use concurrency)
    so host and container never diverge. Surfacing-only (never gates the exit
    code): teatree edits the user's settings only on explicit
    ``t3 setup --write-automode --yes``. Skips when the template is absent (a
    non-editable/packaged install). Crash-proof.
    """
    from teatree.cli.setup.claude_settings import managed_key_drift  # noqa: PLC0415 — deferred (cycle)

    try:
        repo = _teatree_repo_root()
        if repo is None:
            return True
        template = repo / "deploy" / "claude-settings.template.json"
        if not template.is_file():
            return True
        target = Path.home() / ".claude" / "settings.json"
        drift = managed_key_drift(template, target)
        if drift:
            typer.echo(
                f"WARN  Host ~/.claude/settings.json disagrees with {template.name} on: "
                f"{', '.join(drift)}. Host and containers should share the one managed config. "
                f"Reconcile with `t3 setup --write-automode --yes`."
            )
    except Exception as exc:  # noqa: BLE001 — a doctor check must never crash the run
        typer.echo(f"WARN  Claude-settings drift check crashed: {exc.__class__.__name__}: {exc}")
    return True


def run_bootstrap_checks(*, repair: bool = False) -> bool:
    """Run every bootstrap-hardening check; return ``False`` iff a hard gate fails.

    Only the token-permission gate (#3405) affects the verdict — the concurrency
    autofix (#3409/#3434) and the settings-drift check (#3410) are surfacing-only
    and always pass. Runs post-``ensure_django`` (the concurrency autofix reads the
    ORM). ``repair`` gates the concurrency autofix's one mutation: a plain
    ``t3 doctor`` (``repair=False``) inspects and WARNs but NEVER writes.
    """
    ok = _check_gh_token_permissions()
    _check_provision_concurrency_from_host(repair=repair)
    _check_claude_settings_drift()
    return ok


__all__ = [
    "_check_claude_settings_drift",
    "_check_gh_token_permissions",
    "_check_provision_concurrency_from_host",
    "run_bootstrap_checks",
]
