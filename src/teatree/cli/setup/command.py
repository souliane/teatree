"""The ``t3 setup`` typer command — coordination only.

The ``run`` callback wires together the composed units
(:class:`~teatree.cli.setup.tool_installer.ToolInstaller`,
:class:`~teatree.cli.setup.apm.ApmInstaller`,
:class:`~teatree.cli.setup.skill_linker.SkillLinker`,
:class:`~teatree.cli.setup.plugin_registrar.PluginRegistrar`) and the
clone-resolution helpers. Each concern lives in its own sibling module.
"""

from pathlib import Path

import typer

from teatree.cli.account_switch_recover import recover_account_switch
from teatree.cli.dep_drift_repair import repair_dep_drift as _repair_dep_drift
from teatree.cli.doctor import agent_skill_dirs
from teatree.cli.setup.apm import ApmInstaller, strip_apm_hooks
from teatree.cli.setup.clone import find_main_clone, validate_repo
from teatree.cli.setup.mcp_registrar import McpServerRegistrar
from teatree.cli.setup.plugin_registrar import PluginRegistrar
from teatree.cli.setup.skill_linker import CORE_EXCLUDED_SKILLS, SkillLinker
from teatree.cli.setup.statusline_installer import StatuslineInstall, install_statusline
from teatree.cli.setup.tool_installer import ToolInstaller
from teatree.cli.slack.dm_provisioning import provision_all_overlay_dm_channels
from teatree.cli.slack.provision import slack_provision
from teatree.cli.slack.setup import slack_bot_setup
from teatree.cli.slack.user_token_setup import slack_user_token_setup
from teatree.self_update import ensure_self_db_migrated, seed_default_loops
from teatree.utils.django_bootstrap import ensure_django

setup_app = typer.Typer(
    help="First-time setup and global skill management.",
    invoke_without_command=True,
)


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


@setup_app.callback()
def run(
    ctx: typer.Context,
    *,
    skip_plugin: bool = typer.Option(False, "--skip-plugin", help="Skip Claude CLI plugin registration."),
) -> None:
    """Install and configure teatree skills globally.

    Runs APM dependency install, syncs skill symlinks, and registers the t3
    plugin in ``~/.claude/plugins/installed_plugins.json`` (``installPath``
    pointing at the main clone — no ``~/.claude/plugins/t3`` symlink).  Safe to
    run from a teatree worktree — the main clone is resolved via the worktree's
    ``.git`` file so the global install stays anchored to a stable path.
    """
    if ctx.invoked_subcommand is not None:
        return
    repo = validate_repo(find_main_clone())
    typer.echo(f"Teatree repo: {repo}")

    _repair_dep_drift(repo)
    ToolInstaller(repo).ensure_installed()

    ApmInstaller(repo).install()

    settings_json = Path.home() / ".claude" / "settings.json"
    stripped = strip_apm_hooks(settings_json)
    if stripped:
        typer.echo(f"OK    Stripped {stripped} APM-injected hook(s) from settings.json.")

    _report_statusline_install(settings_json, repo)

    from teatree.config import clone_root, load_config  # noqa: PLC0415 — deferred: keeps CLI startup light

    config = load_config()

    all_excluded = list(dict.fromkeys(CORE_EXCLUDED_SKILLS + config.user.excluded_skills))
    # The CLONE root (``~/workspace``) — skill-symlink targets are checked for
    # being under it, not under the per-overlay worktree root.
    workspace_dir = clone_root()

    # Ensure the Claude skills dir exists so overlay symlinks have a target.
    # Core skills reach Claude via the t3 plugin, not via this directory.
    claude_skills = Path.home() / ".claude" / "skills"
    claude_skills.mkdir(parents=True, exist_ok=True)

    for label, skills_dir in agent_skill_dirs():
        if not skills_dir.is_dir():
            continue
        linker = SkillLinker(skills_dir, workspace_dir)
        removed = linker.remove_excluded(all_excluded)
        if removed:
            typer.echo(f"OK    {label}: removed {removed} excluded skill(s).")

        sync_core = label != "claude"
        created, fixed = linker.sync(sync_core=sync_core)
        suffix = "" if sync_core else " (core skills via plugin)"
        typer.echo(f"OK    {label}: {created} created, {fixed} fixed{suffix}.")

        broken = linker.clean_broken()
        if broken:
            typer.echo(f"OK    {label}: removed {broken} broken symlink(s).")

    if not skip_plugin:
        PluginRegistrar(repo).install()
        # Confirm the structured-search MCP server (`t3 mcp serve`, #1023) is
        # still wired via the plugin-bundled `.mcp.json` (#2863) — read-only,
        # idempotent, warns loudly rather than silently regressing agents back
        # to shelling out to the CLI for structured reads.
        McpServerRegistrar(repo).verify()

    self_db_unmigrated = ensure_self_db_migrated(quiet=True)

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
    # #2513: also seed the default loops + prompts so a fresh (or squashed-migration)
    # install has them present. Idempotent (``get_or_create`` by name) and
    # best-effort — it never clobbers an operator-edited row and never aborts setup.
    # The cron is NOT registered here and no tick is started: the seeded rows are
    # config only until the operator opts in.
    if not self_db_unmigrated:
        ensure_django()
        provision_all_overlay_dm_channels(echo=typer.echo)
        seed_default_loops()

    # Suggest (never apply) the recommended per-user auto-mode authorizations.
    # Teatree ships no classifier whitelist of its own — see
    # ``skills/setup/references/recommended-automode-authorizations.md``.
    from teatree.cli.recommended_authorizations import report_missing_authorizations  # noqa: PLC0415 — lazy CLI import

    report_missing_authorizations(typer.echo)

    if self_db_unmigrated:
        raise typer.Exit(code=1)

    typer.echo("Done.")


setup_app.command("slack-bot")(slack_bot_setup)
setup_app.command("slack-user-token")(slack_user_token_setup)
setup_app.command("slack-provision")(slack_provision)
setup_app.command("recover-account-switch")(recover_account_switch)
