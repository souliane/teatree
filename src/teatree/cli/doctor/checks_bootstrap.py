"""Fresh-box bootstrap-hardening doctor checks (umbrella #3404).

Three gates that turn silent late failures on a freshly-provisioned or migrated
box into loud, up-front ones:

- :func:`_check_gh_token_permissions` (#3405) — the GitHub token authenticates but
    is missing a write permission the loop needs (``issues``/``pull_requests``/
    ``contents``). Mirrors ``deploy/entrypoint.sh``'s ``init_preflight`` probe.
- :func:`_check_provision_concurrency_from_host` (#3409) — a stale small-box
    ``provision_max_concurrency`` pin throttling a more capable host; auto-clears
    it so the runtime auto-derives from the host.
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


def _check_provision_concurrency_from_host(*, apply: bool = True) -> bool:
    """Auto-clear a stale small-box ``provision_max_concurrency`` pin on a bigger host (#3409).

    A box migrated onto more cores must not silently keep the old box's
    hard-serialized ``provision_max_concurrency`` pin. When the DB carries an
    explicit pin STRICTLY BELOW the host-derived auto value (``nCPU/2``,
    cgroup-aware), this clears the row so the runtime auto-derives from the host,
    and WARNs (a green, surfacing-only outcome — the pin is fixed). A pin at or
    above the host auto, or no pin at all, passes silently. Reads the ORM, so it
    runs post-``ensure_django``. Crash-proof.
    """
    from teatree.core.models.config_setting import ConfigSetting  # noqa: PLC0415 — deferred (ORM)
    from teatree.utils.ram_probe import default_provision_concurrency  # noqa: PLC0415 — deferred import

    try:
        pinned = ConfigSetting.objects.get_effective("provision_max_concurrency")
        if not isinstance(pinned, int) or isinstance(pinned, bool) or pinned <= 0:
            return True
        host_auto = default_provision_concurrency()
        if pinned >= host_auto:
            return True
        if apply:
            ConfigSetting.objects.clear("provision_max_concurrency")
        typer.echo(
            f"WARN  Cleared a stale provision_max_concurrency={pinned} pin below this host's "
            f"auto-derived {host_auto} (nCPU/2) — it hard-serialized provisioning carried over "
            f"from a smaller box. Concurrency now auto-derives from the host; re-pin explicitly "
            f"with `t3 teatree config_setting set provision_max_concurrency <N>` if intended."
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


def run_bootstrap_checks(*, apply: bool = True) -> bool:
    """Run every bootstrap-hardening check; return ``False`` iff a hard gate fails.

    Only the token-permission gate (#3405) affects the verdict — the concurrency
    autofix (#3409) and the settings-drift check (#3410) are surfacing-only and
    always pass. Runs post-``ensure_django`` (the concurrency autofix reads the
    ORM). ``apply`` is threaded to the concurrency autofix for tests.
    """
    ok = _check_gh_token_permissions()
    _check_provision_concurrency_from_host(apply=apply)
    _check_claude_settings_drift()
    return ok


__all__ = [
    "_check_claude_settings_drift",
    "_check_gh_token_permissions",
    "_check_provision_concurrency_from_host",
    "run_bootstrap_checks",
]
