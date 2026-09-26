"""``t3 eval``'s module tree loads when it is reached, not when the CLI starts.

Registering a Typer sub-app requires the app OBJECT, so ``add_typer`` used to import
``teatree.cli.eval`` at CLI startup — and that chain reaches ``claude_agent_sdk`` and
then ``mcp``. Every invocation paid for it, including commands with no relation to
evals, and an overlay command paid TWICE because ``teatree.cli.overlay.managepy_core``
spawns a second interpreter.

Laziness is only safe if it is invisible, and "invisible" here has a specific meaning:
the command SURFACE must be identical. A placeholder that answered "no subcommands" or
"no options" would still work for every command anyone happened to type while quietly
shortening the CLI reference and the catalogue the ``skill-command-validity`` lane
trusts. So the load-bearing assertions below compare the lazy group against the REAL
one rather than against a hardcoded list, and the deferral itself is asserted on
``sys.modules`` rather than inferred from a timing.
"""

import subprocess
import sys
from pathlib import Path

import click
import pytest
from typer.main import get_command, get_group

# Imported at module scope even though this file is ABOUT not importing it. Every
# assertion that the SDK stays unloaded runs in a fresh subprocess below, so what this
# interpreter has already imported cannot reach them — while the surface comparisons
# need the real app in hand. Keeping these deferred would only make the file look
# careful without making any assertion stronger.
import teatree.cli as tc
from teatree.cli.eval import eval_app, skill_command_lane

REPO_ROOT = Path(__file__).resolve().parents[2]


def _root_group() -> click.Command:
    return get_command(tc.app)


@pytest.fixture
def eval_group() -> click.Command:
    root = _root_group()
    command = root.get_command(click.Context(root), "eval")
    assert command is not None, "the `eval` group vanished from the root app"
    return command


# ── the deferral itself ───────────────────────────────────────────────


def test_importing_the_cli_does_not_import_the_agent_sdk_or_mcp() -> None:
    """The whole point, asserted in a CLEAN interpreter.

    Run as a subprocess because this test session has almost certainly imported the
    SDK already through some other module — asserting on THIS process's ``sys.modules``
    would pass or fail for reasons unrelated to the import graph.
    """
    probe = "import sys; import teatree.cli; print(int('claude_agent_sdk' in sys.modules), int('mcp' in sys.modules))"
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=600,
        check=True,
        cwd=REPO_ROOT,
    )
    assert result.stdout.split() == ["0", "0"], f"SDK/mcp still imported at CLI startup: {result.stdout!r}"


def test_the_probe_would_notice_an_eager_import() -> None:
    """Anti-vacuity guard for the test above.

    The assertion is that two names are ABSENT, which is exactly the shape that passes
    when the probe itself is broken — a typo'd module, a non-zero exit swallowed, an
    interpreter that imported nothing. Importing the SDK explicitly must flip it.
    """
    probe = (
        "import sys; import teatree.cli; import claude_agent_sdk; "
        "print(int('claude_agent_sdk' in sys.modules), int('mcp' in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=600,
        check=True,
        cwd=REPO_ROOT,
    )
    assert result.stdout.split() == ["1", "1"], f"the probe cannot detect an eager import: {result.stdout!r}"


def test_listing_the_root_commands_still_does_not_load_eval() -> None:
    """`t3 --help` must stay cheap: it reads each group's help text, not its children."""
    probe = (
        "import sys, click; from typer.main import get_command; import teatree.cli as tc; "
        "root = get_command(tc.app); ctx = click.Context(root); "
        "names = root.list_commands(ctx); "
        "sub = root.get_command(ctx, 'eval'); "
        "sub.get_short_help_str(limit=200); "
        "print(int('eval' in names), int('teatree.cli.eval' in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=600,
        check=True,
        cwd=REPO_ROOT,
    )
    assert result.stdout.split() == ["1", "0"], f"root listing changed or loaded eval: {result.stdout!r}"


# ── the surface must be identical ─────────────────────────────────────


def test_the_lazy_group_exposes_exactly_the_real_subcommands(eval_group: click.Command) -> None:
    real = get_group(eval_app)
    lazy_names = sorted(eval_group.list_commands(click.Context(eval_group)))
    real_names = sorted(real.list_commands(click.Context(real)))
    assert lazy_names == real_names
    assert lazy_names, "no subcommands at all — the comparison would hold vacuously"


def test_the_lazy_group_exposes_the_real_group_options(eval_group: click.Command) -> None:
    """`t3 eval`'s bare form takes options, and the CLI reference reads them off here.

    A placeholder reporting none would publish a reference for a command documented as
    accepting nothing.
    """
    real = get_group(eval_app)
    assert sorted(p.name or "" for p in eval_group.params) == sorted(p.name or "" for p in real.params)
    assert real.params, "the real group has no options — this comparison proves nothing"


def test_the_help_text_matches_the_real_app(eval_group: click.Command) -> None:
    """The placeholder duplicates the help string, so drift has to be caught here."""
    assert eval_group.get_short_help_str(limit=200) == get_group(eval_app).get_short_help_str(limit=200)


def test_the_registry_seam_the_eval_package_owns_is_filled_on_load(eval_group: click.Command) -> None:
    """Moving the provider registration into the loader must not leave it unset.

    ``build_command_registry`` reads it, and the only route there is an ``eval``
    subcommand — which cannot be reached without the loader having run.
    """
    eval_group.list_commands(click.Context(eval_group))  # force the load
    assert skill_command_lane._registry_provider is not None
    assert skill_command_lane.build_command_registry()[0]
