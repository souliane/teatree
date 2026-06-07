"""``t3 review`` on-behalf-gated subcommands bootstrap Django before touching the ORM (#1003).

Every `t3 review` subcommand that publishes under the user's identity goes through
:func:`teatree.cli.review.on_behalf.check_on_behalf` to enforce the recorded-approval
on-behalf pre-gate (#960). That helper imports
:mod:`teatree.core.on_behalf_gate_recorded` lazily so the CLI package can be
imported by typer (for help rendering, completion, or a privacy-scan subprocess)
*before* ``django.setup()`` has run.

Two invariants must hold for this to work end-to-end:

* :mod:`teatree.core.on_behalf_gate_recorded` must itself be Django-free at import
    time — it cannot eagerly import ORM models, only the pure setting resolver in
    :mod:`teatree.on_behalf_gate`. The ORM imports go inside
    :func:`require_on_behalf_approval` (called only after the typer command body
    has bootstrapped Django).
* Every gated typer command in :mod:`teatree.cli.review` must bootstrap Django
    before invoking the gate. ``t3 review --help`` works without that bootstrap
    because typer does not import the model layer, but the gated bodies do —
    ``check_on_behalf`` triggers the lazy import of the gate-recorded module
    which then accesses the ORM.

Without both, ``t3 review post-draft-note`` (and every sibling gated subcommand)
crashes with ``django.core.exceptions.ImproperlyConfigured: Requested setting
INSTALLED_APPS, but settings are not configured``. The bug was originally
masked by typer's rich-traceback handler exiting 0 — see souliane/teatree#932
for the management-command equivalent.

These tests pin both invariants via subprocesses (a clean child interpreter
state) so future code additions cannot silently re-break either one.
"""

import os
import subprocess
import sys
from pathlib import Path


def _clean_env() -> dict[str, str]:
    """Return an env without ``DJANGO_SETTINGS_MODULE`` — the pre-bootstrap state.

    Mirrors how the user invokes ``t3 review`` from a normal shell: no Django
    settings module is pre-exported, the CLI is responsible for setting it.
    """
    env = os.environ.copy()
    env.pop("DJANGO_SETTINGS_MODULE", None)
    return env


class TestOnBehalfGateRecordedIsImportSafePreBootstrap:
    """The lazy-import contract documented in ``cli/review/on_behalf.py``.

    A child interpreter imports :mod:`teatree.core.on_behalf_gate_recorded`
    with ``DJANGO_SETTINGS_MODULE`` unset and asserts the ORM modules were
    not pulled into ``sys.modules`` as a side effect — proof the module-top
    import chain is genuinely Django-free.
    """

    def test_module_import_does_not_eager_load_orm_models(self) -> None:
        probe = (
            "import sys\n"
            "import teatree.core.on_behalf_gate_recorded  # noqa: F401\n"
            "assert 'teatree.core.models.on_behalf_approval' not in sys.modules, (\n"
            "    'on_behalf_gate_recorded must not eagerly import the ORM models — '\n"
            "    'the import must be lazy inside require_on_behalf_approval so the '\n"
            "    'CLI can be loaded before django.setup() (souliane/teatree#1003)'\n"
            ")\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            check=False,
            capture_output=True,
            text=True,
            env=_clean_env(),
        )
        assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"


class TestReviewPostDraftNoteBootstrapsDjango:
    """`t3 review post-draft-note` bootstraps Django before the gate runs.

    The subcommand is invoked in a child interpreter without
    ``DJANGO_SETTINGS_MODULE`` pre-exported, the way a normal shell invocation
    would be. It must NOT crash with ``ImproperlyConfigured`` — the typer
    command body is responsible for calling ``django.setup()`` before the gate
    chain executes. We patch ``ReviewService.get_gitlab_token`` to return a
    sentinel and the underlying GitLab API call to a no-op so we never hit the
    network; the only behaviour under test is the bootstrap path.
    """

    def test_post_draft_note_does_not_raise_improperly_configured(self, tmp_path: Path) -> None:
        # An empty teatree.toml gate-off config so we exercise the gate
        # chokepoint without needing a recorded approval row.
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text('[teatree]\non_behalf_post_mode = "immediate"\n', encoding="utf-8")

        probe = (
            "import os\n"
            f"os.environ['HOME'] = {str(tmp_path)!r}\n"
            "from unittest.mock import patch\n"
            "from typer.testing import CliRunner\n"
            "import teatree.config as cfg_mod\n"
            f"cfg_mod.CONFIG_PATH = {str(cfg)!r}\n"
            "from pathlib import Path as _P\n"
            f"cfg_mod.CONFIG_PATH = _P({str(cfg)!r})\n"
            "from teatree.cli import app\n"
            "from teatree.cli.review import ReviewService\n"
            "\n"
            "runner = CliRunner()\n"
            "with patch.object(ReviewService, 'get_gitlab_token', return_value='t'), \\\n"
            "     patch.object(ReviewService, '_post_draft_note_impl',\n"
            "                  return_value=('OK draft_note_id=99', 0)):\n"
            "    result = runner.invoke(app, ['review', 'post-draft-note',\n"
            "                                 'org/repo', '1', 'hello', '--general'])\n"
            "if 'ImproperlyConfigured' in (result.output or '') or result.exception is not None:\n"
            "    import traceback\n"
            "    if result.exception:\n"
            "        traceback.print_exception(\n"
            "            type(result.exception), result.exception,\n"
            "            result.exception.__traceback__,\n"
            "        )\n"
            "    print('OUTPUT:', result.output)\n"
            "    raise SystemExit(2)\n"
            "assert result.exit_code == 0, result.output\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            check=False,
            capture_output=True,
            text=True,
            env=_clean_env(),
        )
        assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        assert "ImproperlyConfigured" not in result.stdout
        assert "ImproperlyConfigured" not in result.stderr
