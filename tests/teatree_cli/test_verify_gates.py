"""``t3 tool verify-gates`` runs every CI-gated stage AND discloses the tree it measured.

Meta-test pinning two contracts. First the CI-parity one: a bare ``prek run
--all-files`` only fires the commit-stage hooks, so the push-stage gates CI
re-runs (comment-density, doc-update, ensure-pr, the public-repo leak gate) and
the manual-stage hooks CI runs as jobs (test-path-mirror) are structurally skipped.

Second, the venue one (#4720): the command takes no target, so run from a main
clone it grades the default branch rather than the branch under review and still
exits 0 — a reviewer handing back that exit code reports a green for a tree
nobody asked about. Every run must name the sha it measured, refuse a clean main
clone on its default branch, honour ``--expect-sha``, and name the CI jobs no
local hook covers.
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml
from django.db import connection
from django.test import TransactionTestCase
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.verify_gates import (
    CI_JOB_MANUAL_HOOKS,
    UNCOVERED_CI_JOBS,
    _declared_manual_hooks,
    _prek_config,
    _resource_refusal,
    _snapshot_control_db,
    _stage_env,
)
from teatree.core.models import PlanArtifact, Ticket, Worktree
from teatree.hooks.banned_terms_tree_scan import BannedTermsUnreadableError, BannedTermsUnsetError
from teatree.paths import ControlDb, canonical_db_in
from teatree.quality.changed_set import ChangedSet, ChangeEntry
from tests._git_repo import make_git_repo, run_git

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestPrivateGateDatabase(TransactionTestCase):
    def test_snapshot_preserves_ticket_brief_for_push_hook(self) -> None:
        with TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            source = root / "live.sqlite3"
            ticket = Ticket.objects.create()
            checkout = root / "checkout"
            checkout.mkdir()
            Worktree.objects.create(
                ticket=ticket,
                overlay="teatree",
                repo_path="souliane/teatree",
                branch="feature",
                extra={"worktree_path": str(checkout)},
            )
            PlanArtifact.objects.create(ticket=ticket, plan_text="No migration.")
            connection.ensure_connection()
            with closing(sqlite3.connect(source)) as destination:
                connection.connection.backup(destination)

            with patch.dict(os.environ, {"T3_CONFIG_DB": str(source)}):
                private_root = root / "private"
                private_root.mkdir()
                env = _stage_env(str(private_root))
                _snapshot_control_db(checkout, env)

            program = (
                "from pathlib import Path; from teatree.cli.push_gate_tools import _brief_for_worktree; "
                "import sys; print(_brief_for_worktree(Path(sys.argv[1])))"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    program,
                    str(checkout),
                ],
                cwd=REPO_ROOT,
                env={**env, "DJANGO_SETTINGS_MODULE": "teatree.settings"},
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            assert result.returncode == 0, result.stderr
            assert result.stdout.strip() == "No migration."


@pytest.fixture(autouse=True)
def _no_real_gate_receipt():
    with (
        patch("teatree.cli.verify_gates.write_gate_receipt"),
        patch("teatree.cli.verify_gates._snapshot_control_db"),
    ):
        yield


def test_resource_preflight_measures_disk_and_memory(tmp_path: Path) -> None:
    with (
        patch("teatree.cli.verify_gates.free_bytes", return_value=1024**3),
        patch("teatree.cli.verify_gates.read_ram_headroom") as memory,
    ):
        assert "disk" in _resource_refusal(tmp_path)
        memory.assert_not_called()
    with (
        patch("teatree.cli.verify_gates.free_bytes", return_value=8 * 1024**3),
        patch("teatree.cli.verify_gates.read_ram_headroom", return_value=SimpleNamespace(available_mib=900)),
    ):
        assert "memory" in _resource_refusal(tmp_path)
    with (
        patch("teatree.cli.verify_gates.free_bytes", return_value=8 * 1024**3),
        patch("teatree.cli.verify_gates.read_ram_headroom", return_value=SimpleNamespace(available_mib=8192)),
    ):
        assert _resource_refusal(tmp_path) == ""


def _calls(mock) -> list[list[str]]:
    return [list(call.args[0]) for call in mock.call_args_list]


def _changed(*paths: str) -> ChangedSet:
    return ChangedSet(tuple(ChangeEntry("M", path) for path in paths), "origin/main")


def _text(result) -> str:
    """Everything the run wrote — verify-gates reports on stderr."""
    try:
        return result.output + result.stderr
    except ValueError:
        return result.output


@pytest.fixture
def main_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clean primary clone on its default branch — the #4655 false-green venue."""
    clone = make_git_repo(tmp_path / "clone")
    monkeypatch.chdir(clone)
    return clone


@pytest.fixture
def worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A linked worktree on a feature branch — where a ticket is actually graded."""
    clone = make_git_repo(tmp_path / "clone")
    checkout = tmp_path / "wt"
    run_git(clone, "worktree", "add", "-b", "feature", str(checkout))
    (checkout / "README.md").write_text("test checkout\n", encoding="utf-8")
    run_git(checkout, "add", "README.md")
    run_git(checkout, "commit", "-q", "-m", "add README")
    monkeypatch.chdir(checkout)
    return checkout


def _head_sha(repo: Path) -> str:
    return run_git(repo, "rev-parse", "HEAD")


def _make_feature_worktree(tmp_path: Path, name: str) -> Path:
    """A linked worktree on a feature branch, independent of the process cwd."""
    clone = make_git_repo(tmp_path / f"{name}-clone")
    checkout = tmp_path / name
    run_git(clone, "worktree", "add", "-b", f"{name}-feature", str(checkout))
    (checkout / "README.md").write_text("test checkout\n", encoding="utf-8")
    run_git(checkout, "add", "README.md")
    run_git(checkout, "commit", "-q", "-m", "add README")
    return checkout


def _write_prek_config(repo: Path, *hooks: dict[str, object], **top_level: object) -> None:
    config = {**top_level, "repos": [{"repo": "local", "hooks": list(hooks)}]}
    (repo / ".pre-commit-config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def _write_prek_toml(repo: Path, *hooks: dict[str, object]) -> None:
    lines = ["[[repos]]", 'repo = "local"']
    for hook in hooks:
        lines += ["[[repos.hooks]]", *(f"{key} = {json.dumps(value)}" for key, value in hook.items())]
    (repo / "prek.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _hook(hook_id: str, **fields: object) -> dict[str, object]:
    return {"id": hook_id, "name": hook_id, "entry": "true", "language": "system", **fields}


def _manual_hook(hook_id: str) -> dict[str, object]:
    return _hook(hook_id, stages=["manual"])


def _manual_selectors(run) -> list[list[str]]:
    """The CI-job hook ids each manual-stage prek call selected."""
    manual = [c for c in _calls(run) if c[-2:] == ["--hook-stage", "manual"]]
    return [[arg for arg in call if arg in CI_JOB_MANUAL_HOOKS] for call in manual]


@pytest.fixture
def _healthy_resource_preflight() -> Iterator[None]:
    with patch("teatree.cli.verify_gates._resource_refusal", return_value=""):
        yield


class TestVerifyGatesRunsBothStages:
    @pytest.fixture(autouse=True)
    def _healthy_gate_host(self, worktree: Path) -> Iterator[None]:
        with (
            patch("teatree.cli.verify_gates._timeout_available", return_value=True),
            patch("teatree.cli.verify_gates._resource_refusal", return_value=""),
        ):
            yield

    def test_refuses_low_disk_before_launching_prek(self) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates._resource_refusal", return_value="disk: 1 GiB free below 4 GiB floor"),
            patch("teatree.cli.verify_gates.run_streamed") as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 1
        assert "disk: 1 GiB free below 4 GiB floor" in result.output
        run.assert_not_called()

    def test_refuses_low_memory_before_launching_prek(self) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch(
                "teatree.cli.verify_gates._resource_refusal",
                return_value="memory: 900 MiB available below 2048 MiB floor",
            ),
            patch("teatree.cli.verify_gates.run_streamed") as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 1
        assert "memory: 900 MiB available below 2048 MiB floor" in result.output
        run.assert_not_called()

    def test_stage_children_resolve_a_private_control_database(
        self, worktree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        live = tmp_path / "live-control"
        live.mkdir()
        registry = {"leak": ["private-marker"], "prose_collider": ["private-marker"]}
        with closing(sqlite3.connect(live / "db.sqlite3")) as db:
            db.execute("CREATE TABLE teatree_config_setting (scope TEXT, key TEXT, value TEXT)")
            db.execute(
                "INSERT INTO teatree_config_setting VALUES ('', 'banned_term_registry', ?)",
                (json.dumps(registry),),
            )
            db.execute("CREATE TABLE teatree_worktree (id INTEGER)")
            db.execute("INSERT INTO teatree_worktree VALUES (5322)")
            db.commit()
        monkeypatch.setenv("T3_CONTROL_DB_DIR", str(live))
        monkeypatch.setenv("T3_CONFIG_DB", str(live / "db.sqlite3"))
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{live / 'db.sqlite3'}")
        monkeypatch.setenv("XDG_DATA_HOME", str(live))
        monkeypatch.delenv("TEATREE_TERM_REGISTRY", raising=False)

        def stage_run(_cmd: list[str], *, check: bool, cwd: Path, env: dict[str, str]) -> int:
            assert check is False
            assert cwd == worktree
            private_db = ControlDb(env, home=tmp_path).for_repo(worktree).path
            with closing(sqlite3.connect(f"file:{private_db}?mode=ro", uri=True)) as db:
                assert db.execute("SELECT value FROM teatree_config_setting").fetchone()[0] == json.dumps(registry)
            with closing(sqlite3.connect(private_db)) as db:
                assert db.execute("SELECT id FROM teatree_worktree WHERE id=5322").fetchall() == [(5322,)]
                db.execute("INSERT INTO teatree_worktree VALUES (5323)")
                db.commit()
            return 0

        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.changed_paths", return_value=_changed("README.md")),
            patch("teatree.cli.verify_gates._snapshot_control_db", side_effect=_snapshot_control_db),
            patch("teatree.cli.verify_gates.run_streamed", side_effect=stage_run) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 0, _text(result)
        assert len(run.call_args_list) >= 2
        for call in run.call_args_list:
            env = call.kwargs["env"]
            private_root = Path(env["XDG_DATA_HOME"])
            assert private_root != live
            assert Path(env["T3_CONTROL_DB_DIR"]).is_relative_to(private_root)
            assert "T3_CONFIG_DB" not in env
            assert "DATABASE_URL" not in env
            child_registry = json.loads(env["TEATREE_TERM_REGISTRY"])
            assert {key: child_registry[key] for key in registry} == registry
            assert ControlDb(env, home=tmp_path).for_repo(worktree).path.is_relative_to(private_root)
            assert not private_root.exists()
        with closing(sqlite3.connect(live / "db.sqlite3")) as db:
            assert db.execute("SELECT id FROM teatree_worktree").fetchall() == [(5322,)]

    def test_stage_children_read_a_private_snapshot_of_the_current_database(
        self, worktree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        live = tmp_path / "live-control"
        live.mkdir()
        source = live / "db.sqlite3"
        with closing(sqlite3.connect(source)) as db:
            db.execute("CREATE TABLE snapshot_marker (value TEXT)")
            db.execute("INSERT INTO snapshot_marker VALUES ('live')")
            db.commit()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("T3_CONTROL_DB_DIR", str(live))
        monkeypatch.setenv("TEATREE_TERM_REGISTRY", '{"leak": []}')
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.delenv("T3_CONFIG_DB", raising=False)
        stage_visits: list[str] = []

        def inspect_stage(_command: list[str], *, cwd: Path, env: dict[str, str], check: bool) -> int:
            assert cwd == worktree
            assert check is False
            database = canonical_db_in(ControlDb(env, home=home).primary_data_dir(), env=env, home=home)
            assert database != source
            assert database.is_file()
            with closing(sqlite3.connect(database)) as db:
                expected = "live" if not stage_visits else "stage"
                assert db.execute("SELECT value FROM snapshot_marker").fetchone() == (expected,)
                db.execute("UPDATE snapshot_marker SET value = 'stage'")
                db.commit()
            stage_visits.append(expected)
            return 0

        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.changed_paths", return_value=_changed("README.md")),
            patch("teatree.cli.verify_gates._snapshot_control_db", wraps=_snapshot_control_db),
            patch("teatree.cli.verify_gates.run_streamed", side_effect=inspect_stage),
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 0, _text(result)
        assert len(stage_visits) >= 2
        with closing(sqlite3.connect(source)) as db:
            assert db.execute("SELECT value FROM snapshot_marker").fetchone() == ("live",)

    def test_snapshot_read_failure_refuses_before_running_hooks(
        self, worktree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        live = tmp_path / "live-control"
        live.mkdir()
        sqlite3.connect(live / "db.sqlite3").close()
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("T3_CONTROL_DB_DIR", str(live))
        monkeypatch.setenv("TEATREE_TERM_REGISTRY", '{"leak": []}')
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.delenv("T3_CONFIG_DB", raising=False)
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.changed_paths", return_value=_changed("README.md")),
            patch("teatree.cli.verify_gates._snapshot_control_db", wraps=_snapshot_control_db),
            patch("teatree.cli.verify_gates._sqlite_snapshot", side_effect=OSError("read denied")),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 1
        assert "snapshot" in _text(result)
        run.assert_not_called()

    @pytest.mark.parametrize("registry_error", [BannedTermsUnreadableError, BannedTermsUnsetError])
    def test_unavailable_registry_still_isolates_stage(
        self, worktree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry_error: type[Exception]
    ) -> None:
        live_home = tmp_path / "live"
        monkeypatch.setenv("XDG_DATA_HOME", str(live_home))
        monkeypatch.delenv("TEATREE_TERM_REGISTRY", raising=False)
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.changed_paths", return_value=_changed("README.md")),
            patch("teatree.cli.verify_gates.load_registry", side_effect=registry_error),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 0, _text(result)
        assert run.call_args_list
        for call in run.call_args_list:
            env = call.kwargs["env"]
            assert Path(env["XDG_DATA_HOME"]) != live_home
            assert "TEATREE_TERM_REGISTRY" not in env

    def test_snapshot_failure_refuses_to_run_stages(
        self, worktree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        live_db = tmp_path / "live.sqlite3"
        live_db.touch()
        monkeypatch.setenv("T3_CONFIG_DB", str(live_db))
        monkeypatch.setenv("TEATREE_TERM_REGISTRY", "{}")
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.changed_paths", return_value=_changed("README.md")),
            patch("teatree.cli.verify_gates._snapshot_control_db", side_effect=_snapshot_control_db),
            patch("teatree.cli.verify_gates._sqlite_snapshot", side_effect=sqlite3.OperationalError("busy")),
            patch("teatree.cli.verify_gates.run_streamed") as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 1
        assert "private control database unavailable: busy" in _text(result)
        run.assert_not_called()

    def test_invokes_push_stage_hooks_not_just_commit(self) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates._timeout_available", return_value=True),
            patch("teatree.cli.verify_gates.changed_paths", return_value=_changed("README.md")),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 0
        calls = _calls(run)
        assert all(c[:4] == ["timeout", "--signal=TERM", "--kill-after=15s", "600s"] for c in calls)
        assert any(c[4:] == ["prek", "run", "--files", "README.md"] for c in calls)
        # One push-stage run — the gate CI re-runs that the bare run skips.
        push = [c for c in calls if "--hook-stage" in c]
        assert push, "verify-gates must invoke the push stage"
        assert push[0][-2:] == ["--hook-stage", "pre-push"]

    def test_invokes_the_manual_stage_hooks_ci_gates_on(self, worktree: Path) -> None:
        """CI's test-path-mirror job runs a ``stages: [manual]`` hook no commit/push run fires."""
        _write_prek_config(worktree, *(_manual_hook(hook) for hook in CI_JOB_MANUAL_HOOKS))
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates._timeout_available", return_value=True),
            patch("teatree.cli.verify_gates.changed_paths", return_value=_changed("README.md")),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 0
        manual = [c for c in _calls(run) if c[-2:] == ["--hook-stage", "manual"]]
        assert len(manual) == 1, "verify-gates must invoke the manual stage once"
        assert "test-path-mirror" in manual[0]

    def test_repo_declaring_no_ci_job_hook_skips_the_manual_stage(self, worktree: Path) -> None:
        """A selector naming no declared hook makes prek exit 1, so the stage must not run at all."""
        _write_prek_config(worktree, _hook("ruff"))
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 0, _text(result)
        assert len(_calls(run)) == 2
        assert not [c for c in _calls(run) if "manual" in c], "no hook declared, so no manual-stage run"
        assert "manual (CI-job hooks) skipped" in _text(result)
        summaries = [line for line in _text(result).splitlines() if "all gate stages green" in line]
        assert summaries
        assert all("manual" not in line for line in summaries), "a green summary must not claim a stage never run"

    def test_manual_stage_selects_only_the_declared_ci_job_hooks(self, worktree: Path) -> None:
        _write_prek_config(worktree, _hook("test-path-mirror", stages=["manual"]))
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 0, _text(result)
        (manual,) = [c for c in _calls(run) if c[-2:] == ["--hook-stage", "manual"]]
        assert "test-path-mirror" in manual
        assert "test-shape" not in manual

    @pytest.mark.parametrize(
        ("declared", "selected"),
        [(("test-path-mirror",), [["test-path-mirror"]]), ((), [])],
        ids=["toplevel-declares-mirror", "toplevel-declares-none"],
    )
    def test_run_from_a_subdirectory_reads_the_toplevel_config(
        self, worktree: Path, declared: tuple[str, ...], selected: list[list[str]]
    ) -> None:
        """Prek run from a subdirectory loads the toplevel config, so the selectors must come from it too."""
        _write_prek_config(worktree, _hook("ruff"), *(_manual_hook(hook) for hook in declared))
        (worktree / "docs").mkdir()
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates", "--repo", str(worktree / "docs")])
        assert result.exit_code == 0, _text(result)
        assert _manual_selectors(run) == selected

    @pytest.mark.parametrize(
        ("declared", "selected"),
        [(("test-path-mirror",), [["test-path-mirror"]]), ((), [])],
        ids=["declares-mirror", "declares-none"],
    )
    def test_prek_toml_only_repo_is_read(
        self, worktree: Path, declared: tuple[str, ...], selected: list[list[str]]
    ) -> None:
        _write_prek_toml(worktree, _hook("ruff"), *(_manual_hook(hook) for hook in declared))
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 0, _text(result)
        assert _manual_selectors(run) == selected

    @pytest.mark.parametrize(
        ("hooks", "top_level"),
        [
            ((), {}),
            ((_hook("test-shape", stages=["pre-commit"]),), {}),
            ((_hook("test-path-mirror"), _hook("test-shape")), {"default_stages": ["pre-commit"]}),
        ],
        ids=["no-hooks", "ci-job-hook-at-commit-only", "default-stages-exclude-manual"],
    )
    def test_readable_config_that_declares_no_hook_selects_none(
        self, worktree: Path, hooks: tuple[dict[str, object], ...], top_level: dict[str, object]
    ) -> None:
        _write_prek_config(worktree, *hooks, **top_level)
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 0, _text(result)
        assert _manual_selectors(run) == []
        assert "manual (CI-job hooks) skipped" in _text(result)

    @pytest.mark.parametrize(
        "raw", [None, b"repos: [\n", b"- a list\n"], ids=["missing-config", "malformed-config", "non-mapping-config"]
    )
    def test_config_that_cannot_be_read_still_selects_every_hook(self, worktree: Path, raw: bytes | None) -> None:
        """Reading "no hooks" into a config nobody could parse would skip CI's gates; prek's own error stays red."""
        if raw is not None:
            (worktree / ".pre-commit-config.yaml").write_bytes(raw)
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 0, _text(result)
        assert _manual_selectors(run) == [list(CI_JOB_MANUAL_HOOKS)]
        assert "manual (CI-job hooks) skipped" not in _text(result)

    def test_config_change_falls_back_to_all_files(self) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates._timeout_available", return_value=True),
            patch("teatree.cli.verify_gates.changed_paths", return_value=_changed("pyproject.toml")),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 0
        assert all("--all-files" in c for c in _calls(run))

    def test_timeout_is_incomplete_not_green(self) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates._timeout_available", return_value=True),
            patch("teatree.cli.verify_gates.changed_paths", return_value=_changed("README.md")),
            patch("teatree.cli.verify_gates.run_streamed", side_effect=[124, 0, 0]),
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 1
        assert "INCOMPLETE" in result.output

    def test_missing_timeout_fails_closed(self) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates._timeout_available", return_value=False),
            patch("teatree.cli.verify_gates.run_streamed") as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 1
        run.assert_not_called()

    def test_uses_canonical_pre_push_stage_value(self) -> None:
        """Prek rejects the literal ``push``; the canonical value is ``pre-push``."""
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            runner.invoke(app, ["tool", "verify-gates"])
        push = [c for c in _calls(run) if "--hook-stage" in c]
        assert push, "verify-gates must invoke the push stage"
        assert "push" not in push[0], "must pass pre-push, not the rejected 'push'"

    @pytest.mark.parametrize("failing_stage", ["commit", "pre-push", "manual"])
    def test_fails_when_any_stage_fails(self, worktree: Path, failing_stage: str) -> None:
        _write_prek_config(worktree, *(_manual_hook(hook) for hook in CI_JOB_MANUAL_HOOKS))

        def _rc(cmd: list[str], **_kwargs: object) -> int:
            stage = cmd[-1] if "--hook-stage" in cmd else "commit"
            return 1 if stage == failing_stage else 0

        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", side_effect=_rc),
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 1
        assert f"FAILED stage(s): {failing_stage}" in _text(result)

    def test_both_green_passes(self, worktree: Path) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0),
            patch("teatree.cli.verify_gates.write_gate_receipt") as receipt,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 0
        assert receipt.call_args_list[0].kwargs["state"] == "incomplete"
        assert receipt.call_args_list[-1].kwargs["state"] == "green"

    def test_missing_prek_fails_closed(self, worktree: Path) -> None:
        with patch("teatree.cli.verify_gates._prek_available", return_value=False):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 1


class TestPrekConfig:
    """The config prek itself loads: the nearest one at or above the run directory, within the checkout."""

    def test_no_config_is_none(self, tmp_path: Path) -> None:
        assert _prek_config(tmp_path, tmp_path) is None

    def test_subdirectory_without_its_own_config_reads_the_toplevel_one(self, tmp_path: Path) -> None:
        _write_prek_config(tmp_path)
        (tmp_path / "docs").mkdir()
        assert _prek_config(tmp_path / "docs", tmp_path) == tmp_path / ".pre-commit-config.yaml"

    def test_nearest_config_wins_over_the_toplevel_one(self, tmp_path: Path) -> None:
        _write_prek_config(tmp_path)
        (tmp_path / "docs").mkdir()
        _write_prek_config(tmp_path / "docs")
        assert _prek_config(tmp_path / "docs", tmp_path) == tmp_path / "docs" / ".pre-commit-config.yaml"

    def test_never_reads_above_the_checkout(self, tmp_path: Path) -> None:
        _write_prek_config(tmp_path)
        (tmp_path / "checkout").mkdir()
        assert _prek_config(tmp_path / "checkout", tmp_path / "checkout") is None

    @pytest.mark.parametrize(
        ("present", "loaded"),
        [
            (("prek.toml", ".pre-commit-config.yaml", ".pre-commit-config.yml"), "prek.toml"),
            ((".pre-commit-config.yaml", ".pre-commit-config.yml"), ".pre-commit-config.yaml"),
            ((".pre-commit-config.yml",), ".pre-commit-config.yml"),
        ],
        ids=["toml-first", "yaml-before-yml", "yml-alone"],
    )
    def test_follows_preks_file_precedence(self, tmp_path: Path, present: tuple[str, ...], loaded: str) -> None:
        for name in present:
            (tmp_path / name).write_text("", encoding="utf-8")
        assert _prek_config(tmp_path, tmp_path) == tmp_path / loaded


class TestDeclaredManualHooks:
    """Which CI-job hooks a prek config runs at the manual stage."""

    @pytest.mark.parametrize(
        "raw",
        [
            b"repos: [\n",
            b"\xff\xfe\x00",
            b"",
            b"- a list\n",
            b"repos: 3\n",
            b"repos: [3]\n",
            b"repos: [{hooks: [{}]}]\n",
        ],
        ids=["yaml-error", "not-utf8", "empty", "top-level-list", "repos-not-list", "repo-not-mapping", "hook-no-id"],
    )
    def test_unparsable_yaml_config_selects_every_hook(self, tmp_path: Path, raw: bytes) -> None:
        config = tmp_path / ".pre-commit-config.yaml"
        config.write_bytes(raw)
        assert _declared_manual_hooks(config) == CI_JOB_MANUAL_HOOKS

    def test_unparsable_toml_config_selects_every_hook(self, tmp_path: Path) -> None:
        config = tmp_path / "prek.toml"
        config.write_text("[[repos]\n", encoding="utf-8")
        assert _declared_manual_hooks(config) == CI_JOB_MANUAL_HOOKS

    def test_unreadable_config_selects_every_hook(self, tmp_path: Path) -> None:
        assert _declared_manual_hooks(tmp_path / ".pre-commit-config.yaml") == CI_JOB_MANUAL_HOOKS

    def test_neither_hook_declared_selects_none(self, tmp_path: Path) -> None:
        _write_prek_config(tmp_path, _hook("ruff"))
        assert _declared_manual_hooks(tmp_path / ".pre-commit-config.yaml") == ()

    def test_config_with_no_repos_entries_selects_none(self, tmp_path: Path) -> None:
        config = tmp_path / ".pre-commit-config.yaml"
        config.write_text("repos: []\n", encoding="utf-8")
        assert _declared_manual_hooks(config) == ()

    def test_hook_stages_decide(self, tmp_path: Path) -> None:
        _write_prek_config(
            tmp_path,
            _hook("test-path-mirror", stages=["manual"]),
            _hook("test-shape", stages=["pre-commit"]),
            default_stages=["manual"],
        )
        assert _declared_manual_hooks(tmp_path / ".pre-commit-config.yaml") == ("test-path-mirror",)

    def test_hook_without_stages_takes_the_default_stages(self, tmp_path: Path) -> None:
        _write_prek_config(tmp_path, _hook("test-path-mirror"), _hook("test-shape"), default_stages=["pre-commit"])
        assert _declared_manual_hooks(tmp_path / ".pre-commit-config.yaml") == ()

    def test_hook_without_any_stages_runs_at_every_stage(self, tmp_path: Path) -> None:
        _write_prek_config(tmp_path, _hook("test-shape"))
        assert _declared_manual_hooks(tmp_path / ".pre-commit-config.yaml") == ("test-shape",)

    def test_prek_toml_stages_decide(self, tmp_path: Path) -> None:
        _write_prek_toml(tmp_path, _manual_hook("test-shape"), _hook("test-path-mirror", stages=["pre-commit"]))
        assert _declared_manual_hooks(tmp_path / "prek.toml") == ("test-shape",)

    def test_this_repo_declares_both(self) -> None:
        config = _prek_config(REPO_ROOT, REPO_ROOT)
        assert config is not None
        assert _declared_manual_hooks(config) == CI_JOB_MANUAL_HOOKS


@pytest.mark.usefixtures("_healthy_resource_preflight")
class TestVerifyGatesDisclosesTheTree:
    def test_new_head_committed_during_gates_cannot_get_green_receipt(self, worktree: Path) -> None:
        starting_head = _head_sha(worktree)

        def _run(cmd: list[str], **_kwargs: object) -> int:
            if "--hook-stage" not in cmd:
                run_git(worktree, "commit", "-q", "--allow-empty", "-m", "concurrent commit")
            return 0

        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", side_effect=_run),
            patch("teatree.cli.verify_gates.write_gate_receipt") as receipt,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])

        assert _head_sha(worktree) != starting_head
        assert result.exit_code == 2
        assert "checkout changed" in _text(result)
        assert receipt.call_args_list[-1].kwargs["state"] == "incomplete"

    def test_green_summary_names_the_measured_head_sha(self, worktree: Path) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0),
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 0
        assert _head_sha(worktree) in _text(result)

    def test_failed_summary_names_the_measured_head_sha(self, worktree: Path) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=1),
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 1
        assert _head_sha(worktree) in _text(result)

    def test_banner_names_the_branch_and_venue(self, worktree: Path) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0),
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        text = _text(result)
        assert "feature" in text
        assert "worktree" in text

    def test_green_run_names_the_ci_jobs_it_does_not_cover(self, worktree: Path) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0),
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        text = _text(result)
        for job in UNCOVERED_CI_JOBS:
            assert job in text, f"the uncovered-surfaces line must name {job}"

    def test_uncovered_jobs_are_real_ci_jobs(self) -> None:
        """A stale job name would advertise coverage nobody can check."""
        ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        jobs = set(re.findall(r"^  ([a-z0-9][a-z0-9-]*):$", ci, flags=re.MULTILINE))
        assert jobs, "could not parse any job key out of ci.yml"
        assert set(UNCOVERED_CI_JOBS) <= jobs, f"not CI jobs: {set(UNCOVERED_CI_JOBS) - jobs}"

    def test_every_manual_only_ci_job_is_run_or_disclosed(self) -> None:
        """A manual-stage hook CI runs as a job must be run here or named as uncovered."""
        ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        jobs = set(re.findall(r"^  ([a-z0-9][a-z0-9-]*):$", ci, flags=re.MULTILINE))
        config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
        manual = {hook["id"] for repo in config["repos"] for hook in repo["hooks"] if hook.get("stages") == ["manual"]}
        manual_jobs = manual & jobs
        assert set(CI_JOB_MANUAL_HOOKS) <= manual_jobs, (
            f"not manual CI-job hooks: {set(CI_JOB_MANUAL_HOOKS) - manual_jobs}"
        )
        unaccounted = manual_jobs - set(CI_JOB_MANUAL_HOOKS) - set(UNCOVERED_CI_JOBS)
        assert not unaccounted, f"manual-stage CI jobs neither run nor disclosed: {unaccounted}"


@pytest.mark.usefixtures("_healthy_resource_preflight")
class TestVerifyGatesRefusesTheWrongTree:
    def test_clean_main_clone_on_default_branch_is_refused(self, main_clone: Path) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
            patch("teatree.cli.verify_gates.write_gate_receipt") as receipt,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 2
        assert not _calls(run), "a refused tree must not be graded"
        receipt.assert_not_called()
        assert "--allow-main-clone" in _text(result)

    def test_allow_main_clone_grades_it_anyway(self, main_clone: Path) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates", "--allow-main-clone"])
        assert result.exit_code == 0
        assert _calls(run), "the explicit override must still run the gates"

    def test_dirty_main_clone_runs_because_it_carries_a_change(self, main_clone: Path) -> None:
        (main_clone / "scratch.txt").write_text("work in progress\n", encoding="utf-8")
        run_git(main_clone, "add", "scratch.txt")
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 0
        assert _calls(run)

    def test_detached_head_is_never_the_default_branch(self, main_clone: Path) -> None:
        run_git(main_clone, "checkout", "--detach")
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 0
        assert _calls(run), "a cold-review detached checkout is a real target"

    def test_untracked_scratch_does_not_excuse_a_clean_main_clone(self, main_clone: Path) -> None:
        """``--all-files`` grades TRACKED files, so an unadded scratch file measured nothing extra."""
        (main_clone / "scratch.txt").write_text("notes\n", encoding="utf-8")
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 2
        assert not _calls(run)

    def test_configured_target_branch_is_refused_like_the_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fork's ``teatree.targetBranch`` checkout is the same structural non-target."""
        clone = make_git_repo(tmp_path / "fork", default_branch="development")
        run_git(clone, "config", "teatree.targetBranch", "development")
        monkeypatch.chdir(clone)
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 2
        assert not _calls(run)

    def test_non_git_directory_is_refused_before_any_hook(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        monkeypatch.chdir(plain)
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])
        assert result.exit_code == 2
        assert not _calls(run)


@pytest.mark.usefixtures("_healthy_resource_preflight")
class TestVerifyGatesExpectSha:
    def test_mismatched_target_is_refused_before_any_hook(self, worktree: Path) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates", "--expect-sha", "0" * 40])
        assert result.exit_code == 2
        assert not _calls(run), "the wrong tree must not be graded"
        assert _head_sha(worktree) in _text(result)

    def test_matching_full_sha_runs_every_stage(self, worktree: Path) -> None:
        _write_prek_config(worktree, *(_manual_hook(hook) for hook in CI_JOB_MANUAL_HOOKS))
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates", "--expect-sha", _head_sha(worktree)])
        assert result.exit_code == 0
        assert len(_calls(run)) == 3

    def test_matching_abbreviated_sha_runs(self, worktree: Path) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0),
        ):
            result = runner.invoke(app, ["tool", "verify-gates", "--expect-sha", _head_sha(worktree)[:12]])
        assert result.exit_code == 0

    def test_uppercase_sha_from_a_forge_ui_still_matches(self, worktree: Path) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0),
        ):
            result = runner.invoke(app, ["tool", "verify-gates", "--expect-sha", _head_sha(worktree).upper()])
        assert result.exit_code == 0

    def test_explicit_target_overrides_the_main_clone_refusal(self, main_clone: Path) -> None:
        """Naming the sha IS saying which tree you meant — the refusal has nothing to add."""
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates", "--expect-sha", _head_sha(main_clone)])
        assert result.exit_code == 0
        assert _calls(run)

    def test_env_var_supplies_the_target(self, worktree: Path) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(
                app,
                ["tool", "verify-gates"],
                env={"T3_VERIFY_GATES_EXPECT_SHA": "0" * 40},
            )
        assert result.exit_code == 2
        assert not _calls(run)

    def test_empty_env_var_is_not_a_target(self, worktree: Path) -> None:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"], env={"T3_VERIFY_GATES_EXPECT_SHA": ""})
        assert result.exit_code == 0
        assert _calls(run)


class TestVerifyGatesFollowsTheInvocationCwd:
    """#4859: the containerized process cwd is the container WORKDIR, not the operator's.

    Measuring/grading ``Path.cwd()`` graded whatever checkout happened to be mounted at
    WORKDIR (or refused a non-git WORKDIR) instead of the ticket worktree declared via
    ``TEATREE_INVOCATION_CWD``.
    """

    @pytest.fixture(autouse=True)
    def _healthy_gate_host(self) -> Iterator[None]:
        with (
            patch("teatree.cli.verify_gates._prek_available", return_value=True),
            patch("teatree.cli.verify_gates._timeout_available", return_value=True),
            patch("teatree.cli.verify_gates._resource_refusal", return_value=""),
        ):
            yield

    def test_declared_invocation_cwd_is_measured_not_the_process_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        declared = _make_feature_worktree(tmp_path, "declared")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.setenv("TEATREE_INVOCATION_CWD", str(declared))
        monkeypatch.chdir(elsewhere)

        with (
            patch("teatree.cli.verify_gates.run_streamed", return_value=0),
            patch("teatree.cli.verify_gates.write_gate_receipt") as receipt,
        ):
            result = runner.invoke(app, ["tool", "verify-gates"])

        assert result.exit_code == 0, _text(result)
        assert _head_sha(declared) in _text(result)
        assert receipt.call_args_list[-1].args[0] == declared

    def test_prek_subprocess_runs_with_cwd_the_declared_worktree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        declared = _make_feature_worktree(tmp_path, "declared")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.setenv("TEATREE_INVOCATION_CWD", str(declared))
        monkeypatch.chdir(elsewhere)

        with patch("teatree.cli.verify_gates.run_streamed", return_value=0) as run:
            result = runner.invoke(app, ["tool", "verify-gates"])

        assert result.exit_code == 0, _text(result)
        assert run.call_args_list, "prek must have been invoked"
        assert all(call.kwargs.get("cwd") == declared for call in run.call_args_list)

    def test_an_explicit_repo_still_wins_over_the_declared_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: the declaration is a DEFAULT, never an override of what the caller asked."""
        declared = _make_feature_worktree(tmp_path, "declared")
        explicit = _make_feature_worktree(tmp_path, "explicit")
        monkeypatch.setenv("TEATREE_INVOCATION_CWD", str(declared))

        with patch("teatree.cli.verify_gates.run_streamed", return_value=0):
            result = runner.invoke(app, ["tool", "verify-gates", "--repo", str(explicit)])

        assert result.exit_code == 0, _text(result)
        assert _head_sha(explicit) in _text(result)

    def test_no_declaration_falls_back_to_the_process_cwd(
        self, worktree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: a host-native run is byte-identical to the pre-#4859 behaviour."""
        monkeypatch.delenv("TEATREE_INVOCATION_CWD", raising=False)

        with patch("teatree.cli.verify_gates.run_streamed", return_value=0):
            result = runner.invoke(app, ["tool", "verify-gates"])

        assert result.exit_code == 0, _text(result)
        assert _head_sha(worktree) in _text(result)
