"""Fresh-box bootstrap-hardening doctor checks (umbrella #3404).

Five gates that turn silent late failures on a freshly-provisioned or migrated
box into loud, up-front ones:

- :func:`_check_gh_token_permissions` (#3405/#3477) — a missing REQUIRED permission
    is a hard FAIL; a missing RECOMMENDED one is a WARN with remediation, never a fail.
    Mirrors ``deploy/entrypoint.sh``'s ``init_preflight`` probe.
- :func:`_check_git_hooks_installed` — a checkout whose git hooks were never
    installed pushes with the whole local gate layer absent. A hard FAIL naming the
    missing hooks; a deliberate ``core.hooksPath`` override only WARNs.
- :func:`_check_github_remotes_are_https` (#4447) — a checkout reaching GitHub over SSH,
    or a ``url`` … ``insteadOf`` rewrite pointing github.com at one. Both defeat the token-based
    ``gh`` credential helper and live in gitconfig, where the tracked-tree conformance scan
    cannot see them. A hard FAIL naming the repoint; GitLab is deliberately out of scope.
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
import re
from pathlib import Path

import typer

from teatree.utils.git_remote import slug_from_remote
from teatree.utils.run import CommandFailedError, run_allowed_to_fail

#: An ssh-transport remote — the ``ssh://`` scheme or the scp-like ``user@host:path``. An
#: ``https://user@host/…`` URL has no ``@`` before its first ``:``, so it never matches.
_SSH_REMOTE_RE = re.compile(r"^(?:ssh://|[^/:]+@[^/:]+:)")


def _helper_host(key: str) -> str:
    """The URL a ``credential.<url>.helper`` key scopes its helper to."""
    return key.removeprefix("credential.").removesuffix(".helper")


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


def _resolve_projects_config() -> tuple[str, int]:
    """The active overlay's Projects-v2 board config, or ``("", 0)`` when unset/unresolvable (crash-proof)."""
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415 — deferred: ORM import

    try:
        overlay = get_overlay()
    except Exception:  # noqa: BLE001 — no resolvable overlay: board not configured
        return "", 0
    owner = getattr(overlay.config, "github_owner", "") or ""
    number = getattr(overlay.config, "github_project_number", 0) or 0
    return owner, number


def _check_gh_token_permissions() -> bool:
    """FAIL when the GitHub token lacks a REQUIRED permission (#3405/#3477); a RECOMMENDED gap only WARNs."""
    from teatree.core.gates.gh_token_preflight import (  # noqa: PLC0415 — deferred import
        format_remediation,
        probe_token_permissions,
    )

    slug = _resolve_repo_slug()
    if slug is None:
        return True
    owner, project_number = _resolve_projects_config()
    try:
        probe = probe_token_permissions(slug, github_owner=owner, github_project_number=project_number)
    except Exception as exc:  # noqa: BLE001 — a probe failure warns and passes, never blocks doctor
        typer.echo(f"WARN  Could not probe the GitHub token permissions: {exc}")
        return True
    if probe.indeterminate_reason is not None:
        return True
    if probe.missing_recommended:
        for line in format_remediation(probe, slug):
            typer.echo(f"WARN  {line}")
    if not probe.missing:
        return True
    typer.echo(
        f"FAIL  The GitHub token cannot exercise {', '.join(probe.missing)} on {slug} — "
        "the loop's `gh issue`/`gh pr`/push writes will fail mid-run with "
        "'Resource not accessible by personal access token'. Grant the missing "
        "permission(s) on TEATREE_GH_TOKEN and re-deploy, then re-run `t3 doctor check`."
    )
    return False


def _check_git_hooks_installed() -> bool:
    """FAIL when ANY checkout teatree commits from has no git hooks installed.

    A ``.git/hooks`` holding only ``*.sample`` files is a silent-broken install of
    the same class as #3523's PAT scopes: every push from that checkout — and from
    every worktree sharing its git dir — runs with the local gate layer absent.
    The check spans every discovered checkout precisely because the real failure is
    hooks landing in one clone while work happens in another: judging only the
    installed clone reads green while another pushes ungated. A deliberate
    ``core.hooksPath`` override is reported, never failed on.
    """
    from teatree.core.gates.git_checkouts import discover_checkouts  # noqa: PLC0415 — deferred (ORM)
    from teatree.core.gates.git_hooks_preflight import (  # noqa: PLC0415 — deferred import
        format_remediation,
        probe_checkouts,
    )

    try:
        probes = probe_checkouts(discover_checkouts())
    except Exception as exc:  # noqa: BLE001 — a probe failure warns and passes, never blocks doctor
        typer.echo(f"WARN  Could not probe the git hooks: {exc.__class__.__name__}: {exc}")
        return True

    ok = True
    for probe in probes:
        if probe.custom_hooks_path is not None:
            typer.echo(
                f"WARN  {probe.checkout}: core.hooksPath points at {probe.custom_hooks_path} — teatree leaves "
                f"that directory to you and cannot verify the commit/push gates are installed there."
            )
        remediation = format_remediation(probe)
        ok = ok and not remediation
        for line in remediation:
            typer.echo(f"FAIL  {line}")
    return ok


def _git_config_pairs(checkout: Path, pattern: str) -> list[tuple[str, str]]:
    """``git config --get-regexp <pattern>`` as ``(key, value)``, spanning local+global+system.

    ``--get-regexp`` exits 1 for "nothing matched", which is an ordinary answer here rather
    than a failure — hence the empty list rather than a raise.
    """
    result = run_allowed_to_fail(["git", "-C", str(checkout), "config", "--get-regexp", pattern], expected_codes=None)
    if result.returncode != 0:
        return []
    return [(key, value.strip()) for key, _, value in (line.partition(" ") for line in result.stdout.splitlines())]


def _check_github_remotes_are_https() -> bool:
    """FAIL when a checkout reaches GitHub over SSH instead of https + ``gh`` (#4447).

    teatree authenticates to GitHub with a TOKEN through ``gh auth git-credential`` (the helper
    ``deploy/entrypoint.sh`` installs), so there is no key to distribute, mount or rotate. An
    ssh remote — or a ``url`` … ``insteadOf`` rewrite pointing github.com at one — is a second,
    undocumented credential path that the ``gh`` helper cannot serve and that works on one box
    and not another. Both live in gitconfig rather than the tracked tree, which is exactly why
    the sibling conformance scan cannot see them.

    Narrow on purpose: GitLab remotes and rewrites are a separate legitimate credential path,
    and a bare ``~/.ssh`` is not load-bearing until a remote or a rewrite references it — so
    neither is judged here. Crash-proof like every bootstrap check: an unreadable config WARNs
    and passes rather than aborting the doctor run.
    """
    from teatree.core.gates.git_checkouts import discover_checkouts  # noqa: PLC0415 — deferred (ORM)
    from teatree.core.public_identity import is_github_host  # noqa: PLC0415 — deferred import

    try:
        checkouts = discover_checkouts()
    except Exception as exc:  # noqa: BLE001 — a discovery failure warns and passes, never blocks doctor
        typer.echo(f"WARN  Could not discover the checkouts to probe for ssh GitHub remotes: {exc!r}")
        return True

    ok = True
    helperless: list[Path] = []
    for checkout in checkouts:
        try:
            remotes = _git_config_pairs(checkout, r"^remote\..*\.(url|pushurl)$")
            rewrites = _git_config_pairs(checkout, r"^url\..*\.insteadof$")
            helpers = _git_config_pairs(checkout, r"^credential\..*helper$")
        except OSError as exc:
            typer.echo(f"WARN  {checkout}: could not read the git config: {exc!r}")
            continue

        for key, url in remotes:
            if not (_SSH_REMOTE_RE.match(url) and is_github_host(url)):
                continue
            name = key.removeprefix("remote.").rsplit(".", 1)[0]
            push = " --push" if key.endswith(".pushurl") else ""
            typer.echo(
                f"FAIL  {checkout}: {key} reaches GitHub over SSH ({url}). teatree authenticates to GitHub "
                f"with a token through `gh auth git-credential`, which cannot serve an ssh remote — repoint "
                f"it: git remote set-url{push} {name} https://github.com/{slug_from_remote(url)}.git"
            )
            ok = False

        for key, value in rewrites:
            if not (is_github_host(key.removeprefix("url.").removesuffix(".insteadof")) or is_github_host(value)):
                continue
            typer.echo(
                f"FAIL  {checkout}: `{key} = {value}` rewrites GitHub URLs behind git's back, defeating the "
                f"`gh` credential helper — remove it: git config --unset {key}"
            )
            ok = False

        has_helper = any(key == "credential.helper" or is_github_host(_helper_host(key)) for key, _ in helpers)
        serves_github_over_https = any(not _SSH_REMOTE_RE.match(url) and is_github_host(url) for _, url in remotes)
        if serves_github_over_https and not has_helper:
            helperless.append(checkout)

    if helperless:
        typer.echo(
            f"WARN  {len(helperless)} checkout(s) carry https GitHub remotes with no credential helper "
            f"(first: {helperless[0]}) — git prompts for a password instead of using the token. "
            f"Wire it up: gh auth setup-git"
        )
    return ok


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

    Only the token-permission gate (#3405), the git-hooks gate and the GitHub-transport
    gate (#4447) affect the verdict — the concurrency autofix (#3409/#3434) and the
    settings-drift check (#3410) are surfacing-only and always pass. Every check runs
    before the verdict is returned, so one failure never masks another's output. Runs
    post-``ensure_django`` (the concurrency autofix reads the
    ORM). ``repair`` gates the concurrency autofix's one mutation: a plain
    ``t3 doctor`` (``repair=False``) inspects and WARNs but NEVER writes.
    """
    ok = _check_gh_token_permissions()
    hooks_ok = _check_git_hooks_installed()
    transport_ok = _check_github_remotes_are_https()
    _check_provision_concurrency_from_host(repair=repair)
    _check_claude_settings_drift()
    return ok and hooks_ok and transport_ok


__all__ = [
    "_check_claude_settings_drift",
    "_check_gh_token_permissions",
    "_check_git_hooks_installed",
    "_check_github_remotes_are_https",
    "_check_provision_concurrency_from_host",
    "run_bootstrap_checks",
]
