"""The ``t3 setup`` typer command — coordination only.

The ``run`` callback wires together the composed units
(:class:`~teatree.cli.setup.tool_installer.ToolInstaller`,
:class:`~teatree.cli.setup.skill_linker.SkillLinker`,
:class:`~teatree.cli.setup.plugin_registrar.PluginRegistrar`) and the
clone-resolution helpers. Each concern lives in its own sibling module.
"""

import os
import re
from collections.abc import Callable
from pathlib import Path

import typer
from django.core.management import call_command

from teatree.agents.skill_injection import _bare_skill_name, _resolve_skill_md, harness_skills_dirs
from teatree.cli.account_switch_recover import recover_account_switch
from teatree.cli.dep_drift_repair import repair_dep_drift as _repair_dep_drift
from teatree.cli.doctor import agent_skill_dirs
from teatree.cli.doctor.checks_notion import report_notion_connections
from teatree.cli.setup.apm import strip_apm_hooks
from teatree.cli.setup.clone import find_main_clone, validate_repo
from teatree.cli.setup.codex_plugin_registrar import CodexPluginRegistrar
from teatree.cli.setup.docker_alias import retire_alias
from teatree.cli.setup.docker_launcher import DockerLauncherInstaller
from teatree.cli.setup.git_hooks_installer import GitHooksInstaller
from teatree.cli.setup.mandated_skills import MandatedSkillProvisioner
from teatree.cli.setup.mcp_registrar import McpServerRegistrar
from teatree.cli.setup.merge_driver_installer import GitMergeDriverInstaller
from teatree.cli.setup.plugin_registrar import PluginRegistrar, PyrightPluginRegistrar
from teatree.cli.setup.skill_linker import CORE_EXCLUDED_SKILLS, SkillLinker
from teatree.cli.setup.skill_pin_audit import SkillPinAuditor
from teatree.cli.setup.statusline_installer import StatuslineInstall, install_statusline
from teatree.cli.setup.tool_installer import ToolInstaller
from teatree.cli.slack.dm_provisioning import provision_all_overlay_dm_channels
from teatree.cli.slack.provision import slack_provision
from teatree.cli.slack.setup import slack_bot_setup
from teatree.cli.slack.user_token_setup import slack_user_token_setup
from teatree.core.skill_sources import demanded_skill_names, install_declared_sources
from teatree.paths import get_data_dir
from teatree.provisioning.skill_clone_install import CloneInstall
from teatree.provisioning.skill_pin import default_record_path
from teatree.provisioning.skills_cli import SkillsCli, SkillsCliError, refresh_inventory_receipt
from teatree.self_update import ensure_self_db_migrated, seed_default_loops
from teatree.utils.django_bootstrap import ensure_django

setup_app = typer.Typer(
    help="First-time setup and global skill management.",
    invoke_without_command=True,
)

_SAFE_SKILL_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")


def _assess_dispatched_skills(
    demands: tuple[str, ...],
    source_outcomes: list[CloneInstall],
    *,
    receipt: Path,
    search_dirs: list[Path] | None = None,
) -> bool:
    """Gate actual loadable skills; record clone provenance separately.

    The receipt deliberately contains names only, never clone errors or paths. A
    source can be unavailable while an already-installed skill is still usable.
    """
    directories = search_dirs if search_dirs is not None else harness_skills_dirs()
    missing: list[str] = []
    for name in sorted(set(demands)):
        bare = _bare_skill_name(name)
        body = _resolve_skill_md(name, directories) if _SAFE_SKILL_NAME.fullmatch(bare) else None
        try:
            if body is None or not body.read_text(encoding="utf-8").strip():
                missing.append(name)
        except (OSError, UnicodeError):
            missing.append(name)
    safe_missing = sorted(
        {_bare_skill_name(name) for name in missing if _SAFE_SKILL_NAME.fullmatch(_bare_skill_name(name))}
    )
    # An unsafe name cannot be reported by the boot marker, but still fails ready.
    status = "missing-skills" if missing else "ready"
    provenance = "unverified" if any(outcome.unavailable for outcome in source_outcomes) else "verified"
    receipt.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    receipt.write_text(
        f"status={status}\nprovenance={provenance}\nmissing={','.join(safe_missing)}\n", encoding="utf-8"
    )
    receipt.chmod(0o600)
    if provenance == "unverified":
        typer.echo("WARN  Declared skill-source provenance is unverified; loadable dispatch skills checked directly.")
    if missing:
        typer.echo(
            f"ERROR {len(missing)} mandatory dispatched skill(s) are not loadable: {', '.join(safe_missing)}", err=True
        )
    return not missing


def _refresh_skill_inventory(path: Path, *, cli: SkillsCli, echo: Callable[[str], None]) -> bool:
    try:
        refresh_inventory_receipt(path, cli=cli)
    except SkillsCliError as error:
        echo(f"WARN  Harness skill inventory could not be refreshed: {error}")
        return False
    echo(f"OK    Harness skill inventory refreshed at {path}.")
    return True


def _reset_strict_skills_marker(path: Path, *, strict: bool) -> None:
    if strict:
        path.unlink(missing_ok=True)


def _complete_strict_skills_setup(path: Path, *, strict: bool, ready: bool) -> None:
    if not strict:
        return
    if not ready:
        typer.echo("ERROR Required agent skills/plugins are incomplete; refusing strict headless setup.", err=True)
        raise typer.Exit(code=1)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text("v1\n", encoding="utf-8")
    path.chmod(0o600)


def _provision_agent_skills(
    repo: Path,
    *,
    harness_exclusions: list[str],
    skip_plugin: bool,
) -> bool:
    """Install declared skills and plugins, returning strict readiness."""
    skills_cli = SkillsCli()
    ready = MandatedSkillProvisioner(repo, cli=skills_cli).provision(typer.echo)

    if ready:
        source_outcomes = install_declared_sources(
            cache_root=get_data_dir("skill-sources"),
            demand_names=set(demanded_skill_names()),
            harness_exclusions=harness_exclusions,
            cli=skills_cli,
        )
        for outcome in source_outcomes:
            typer.echo(outcome.render())
        dispatch_ready = _assess_dispatched_skills(
            demanded_skill_names(), source_outcomes, receipt=get_data_dir("skills") / "setup-outcome"
        )
        inventory_ready = _refresh_skill_inventory(
            get_data_dir("skills") / "inventory.json",
            cli=skills_cli,
            echo=typer.echo,
        )
        ready = inventory_ready and dispatch_ready
    else:
        typer.echo("WARN  Harness skill source installation and inventory refresh skipped after preflight failure.")

    # Setup is the remote-reading lane for the suggestion-only pin audit. Doctor
    # consumes its recorded result without requiring network access.
    SkillPinAuditor(repo, default_record_path()).audit(typer.echo)

    if skip_plugin:
        return ready

    claude_plugin_ready = PluginRegistrar(repo).install()
    codex_plugin_ready = CodexPluginRegistrar(repo).install()
    ready = claude_plugin_ready and codex_plugin_ready and ready
    PyrightPluginRegistrar().install()
    PyrightPluginRegistrar.ensure_langserver()
    McpServerRegistrar(repo).verify()
    return ready


def provision_declared_notion_routing() -> None:
    """Run the ORM-backed route provisioner after setup migrated the self DB."""
    call_command("provision_declared_notion_routing")


def _write_automode_consented(*, yes: bool) -> bool:
    """True when the operator explicitly consented to writing managed settings.

    Consent is an explicit ``--yes`` or a truthy ``TEATREE_WRITE_AUTOMODE`` env
    (the non-interactive/provisioning consent channel). Teatree edits the user's
    ``~/.claude/settings.json`` only under this explicit grant — the classifier
    whitelist stays the operator's final say (BLUEPRINT §11.4).
    """
    return yes or os.environ.get("TEATREE_WRITE_AUTOMODE", "").strip().lower() in {"1", "true", "yes"}


def _maybe_write_managed_settings(repo: Path, settings_json: Path, *, write_automode: bool, yes: bool) -> None:
    """Deep-merge the committed Claude-settings template into ``settings_json`` on consent (#3408/#3410).

    ``deploy/claude-settings.template.json`` is the single source of truth for the
    managed keys (model, permission mode + allow-list, ``autoMode.allow`` recommended
    grants, tool-use concurrency). With ``--write-automode`` and explicit consent this
    applies the SAME deep merge the container seed uses, so host and container never
    drift. Without consent it only points the operator at the flag — it never writes.
    """
    if not write_automode:
        return
    if not _write_automode_consented(yes=yes):
        typer.echo(
            "WARN  --write-automode needs explicit consent — re-run with `--yes` "
            "(or set TEATREE_WRITE_AUTOMODE=1) to deep-merge the managed Claude settings.",
        )
        return
    from teatree.cli.setup.claude_settings import write_host_claude_settings  # noqa: PLC0415 — lazy CLI import

    template = repo / "deploy" / "claude-settings.template.json"
    try:
        write_host_claude_settings(template, settings_json)
    except FileNotFoundError:
        typer.echo(f"WARN  No Claude-settings template at {template} — skipped --write-automode.")
        return
    typer.echo(f"OK    Merged managed Claude settings from {template.name} into {settings_json}.")


def _report_statusline_install(settings_json: Path, repo: Path) -> None:
    """Install the Claude Code statusLine block and echo the outcome (PR-17)."""
    result = install_statusline(settings_json, repo)
    if result is StatuslineInstall.INSTALLED:
        typer.echo("OK    Installed statusLine block into settings.json.")
    elif result is StatuslineInstall.ALREADY_PRESENT:
        typer.echo("OK    statusLine already configured — left untouched.")
    elif result is StatuslineInstall.UNWRITABLE:
        typer.echo("WARN  Could not write the statusline to settings.json (not writable) — skipping; setup continues.")
    else:
        typer.echo("WARN  settings.json unparsable — skipped statusLine install.")


def _sync_runtime_skill_links(workspace_dir: Path, excluded: list[str]) -> None:
    """Sync overlay skill symlinks into every plugin-backed runtime."""
    for label, skills_dir in agent_skill_dirs():
        if not skills_dir.is_dir():
            continue
        linker = SkillLinker(skills_dir, workspace_dir)
        removed = linker.remove_excluded(excluded)
        if removed:
            typer.echo(f"OK    {label}: removed {removed} excluded skill(s).")

        created, fixed = linker.sync(sync_core=False)
        typer.echo(f"OK    {label}: {created} created, {fixed} fixed (core skills via plugin).")

        broken = linker.clean_broken()
        if broken:
            typer.echo(f"OK    {label}: removed {broken} broken symlink(s).")


def _install_checkout_git_config(repo: Path) -> None:
    """Install the per-checkout git config every checkout teatree commits from needs.

    A checkout whose hooks were never installed pushes with the whole local gate
    layer absent (leak gate, banned-terms, dev/push-gate.sh) and nothing errors;
    a checkout without the ``generated`` merge driver merges a generated doc with
    no warning that it is now stale against the merged command tree
    (souliane/teatree#3582, souliane/teatree#4259). Both are per-``.git/config``
    properties, so both walk the same checkout set.

    Ordering: ``prek_hook.install`` routes through ``run_step``'s optional
    time-box, which imports ``provision_timebox`` -> ``notify`` ->
    ``teatree.core.models`` at call time — Django must already be configured
    (the caller runs ``ensure_django()`` first).
    """
    GitHooksInstaller(repo).install(echo=typer.echo)
    GitMergeDriverInstaller(repo).install(echo=typer.echo)


@setup_app.callback()
def run(
    ctx: typer.Context,
    *,
    skip_plugin: bool = typer.Option(False, "--skip-plugin", help="Skip Claude and Codex plugin registration."),
    strict_agent_skills: bool = typer.Option(
        False,
        "--strict-agent-skills",
        help="Exit non-zero unless required skills, inventory, and both TeaTree plugins are ready.",
    ),
    write_automode: bool = typer.Option(
        False,
        "--write-automode",
        help="Deep-merge the committed Claude-settings template (recommended autoMode grants + managed keys) "
        "into ~/.claude/settings.json. Requires --yes (or TEATREE_WRITE_AUTOMODE=1).",
    ),
    yes: bool = typer.Option(
        False, "--yes", help="Consent to teatree editing ~/.claude/settings.json (see --write-automode)."
    ),
) -> None:
    """Install and configure teatree skills globally.

    Installs declared skill dependencies, syncs skill symlinks, and registers the t3
    plugin for Claude Code (the checkout) and Codex (a slim copy). Safe to run from a
    teatree worktree — the main clone is resolved via the worktree's ``.git``
    file so the global install stays anchored to a stable path.
    """
    if ctx.invoked_subcommand is not None:
        return
    strict_agent_skills = strict_agent_skills is True
    skip_plugin = skip_plugin is True
    if strict_agent_skills and skip_plugin:
        message = "--strict-agent-skills cannot be combined with --skip-plugin"
        raise typer.BadParameter(message)
    repo = validate_repo(find_main_clone())
    typer.echo(f"Teatree repo: {repo}")

    _repair_dep_drift(repo)
    ToolInstaller(repo).ensure_installed()

    # ensure_django() is idempotent; the later call before DM provisioning is a
    # no-op repeat. It must precede _install_checkout_git_config — see that helper.
    ensure_django()
    _install_checkout_git_config(repo)

    settings_json = Path.home() / ".claude" / "settings.json"
    stripped = strip_apm_hooks(settings_json)
    if stripped:
        typer.echo(f"OK    Stripped {stripped} APM-injected hook(s) from settings.json.")

    _report_statusline_install(settings_json, repo)

    # #3232: wire the containerized `t3` workflow. The launcher is the ONLY
    # mechanism — an executable `t3` on PATH that execs `deploy/t3`, so scripts,
    # git hooks, cron and sub-agents resolve the same containerized CLI an
    # interactive shell does. A container writes it through the host bin mount; a
    # host writes it directly. The alias a previous version installed is retired,
    # never refreshed. Both are best-effort — a refusal or an unwritable path
    # WARNs, never aborts.
    DockerLauncherInstaller(repo).install(echo=typer.echo)
    retire_alias(echo=typer.echo)

    # Ahead of every in-process settings read below: `ConfigSetting` is the DB override
    # tier, and a fresh install has no table for it until this runs — a read before it
    # resolves from defaults AND logs the miss as a real read fault, on the one command
    # every new user runs.
    self_db_unmigrated = ensure_self_db_migrated(quiet=True)

    from teatree.config import clone_root, get_effective_settings  # noqa: PLC0415 — deferred: keeps CLI startup light

    effective_settings = get_effective_settings()
    skills_ready_marker = get_data_dir("skills") / "ready"
    _reset_strict_skills_marker(skills_ready_marker, strict=strict_agent_skills)
    all_excluded = list(dict.fromkeys(CORE_EXCLUDED_SKILLS + effective_settings.excluded_skills))
    # The CLONE root (``~/workspace``) — skill-symlink targets are checked for
    # being under it, not under the per-overlay worktree root.
    workspace_dir = clone_root()

    for _label, skills_dir in agent_skill_dirs():
        skills_dir.mkdir(parents=True, exist_ok=True)

    _sync_runtime_skill_links(workspace_dir, all_excluded)

    agent_skills_ready = _provision_agent_skills(
        repo,
        harness_exclusions=effective_settings.harness_skill_exclusions,
        skip_plugin=skip_plugin,
    )

    _complete_strict_skills_setup(
        skills_ready_marker,
        strict=strict_agent_skills,
        ready=agent_skills_ready,
    )

    # Per-overlay Slack-bot IM provisioning (#1342) — open ``conversations.open``
    # once for every Slack-bot overlay in the DB ``overlays`` registry that has no
    # ``slack_dm_channel_id`` cached yet, then persist the resulting channel id back
    # into that registry row. Without this step a freshly-registered per-overlay bot
    # has no IM with the user, ``messaging_from_overlay`` returns a backend that hits
    # ``channel_not_found`` on first DM, and the post silently falls back through
    # whichever bot already had an IM open — conflating per-overlay attribution. Runs
    # after the self-DB migrate so the ``ConfigSetting`` table exists, and behind
    # ``ensure_django()`` — since #3074 the registry read is an in-process
    # ``ConfigSetting`` ORM read, while the migrate/seed steps are subprocesses that
    # never configure Django in this interpreter.
    # A declared Notion pass-key is a bootstrap route, not an operator override:
    # persist it only when neither DB scope already names one. This makes the DB
    # the effective source after first setup while preserving every explicit pin.
    # #2513: also seed the default loops + prompts so a fresh (or squashed-migration)
    # install has them present. Idempotent (``get_or_create`` by name) and
    # best-effort — it never clobbers an operator-edited row and never aborts setup.
    # The cron is NOT registered here and no tick is started: the seeded rows are
    # config only until the operator opts in.
    if not self_db_unmigrated:
        ensure_django()
        provision_declared_notion_routing()
        provision_all_overlay_dm_channels(echo=typer.echo)
        seed_default_loops()
        # Headless on purpose: this venue's own store and grants, reported; `t3 doctor` is the gate.
        report_notion_connections(typer.echo)

    # Suggest (never apply) the recommended per-user auto-mode authorizations.
    # Teatree ships no classifier whitelist of its own — see
    # ``skills/setup/references/recommended-automode-authorizations.md``.
    from teatree.cli.recommended_authorizations import report_missing_authorizations  # noqa: PLC0415 — lazy CLI import

    report_missing_authorizations(typer.echo)

    # #3408/#3410: on explicit consent, WRITE the managed settings (recommended
    # autoMode grants + model/permissions/env) from the one committed template so
    # a fresh box is classifier-unblocked and host+container stay in lockstep.
    _maybe_write_managed_settings(repo, settings_json, write_automode=write_automode, yes=yes)

    if self_db_unmigrated:
        raise typer.Exit(code=1)

    typer.echo("Done.")


setup_app.command("slack-bot")(slack_bot_setup)
setup_app.command("slack-user-token")(slack_user_token_setup)
setup_app.command("slack-provision")(slack_provision)
setup_app.command("recover-account-switch")(recover_account_switch)
