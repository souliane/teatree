"""Every teatree-owned ``uv tool install`` names the constraints file of the checkout it installs (#4659).

The deployed image exports ``UV_CONSTRAINT`` as a build-time expansion of
``$TEATREE_CLONE_DIR``, so a layout that vendors core under ``vendor/teatree`` — and
overrides that variable at RUNTIME — inherits an ambient value naming a path nothing ever
writes. ``uv tool install`` errors outright on it (measured: ``error: File not found``,
exit 2), which takes ``t3 update``, the loop's deferred-reinstall drain, the editable-``.pth``
repair and the dep-drift repair down with it.

``deploy/entrypoint.sh`` realigns the ambient value for its own role's process tree, but a
``docker exec``-ed process starts from the container's create-time environment and never sees
that export. An explicit ``--constraints`` is the only bound that travels: it overrides the
ambient value and it follows the checkout being installed, so it is correct on both layouts
in every venue. This mirrors :mod:`teatree.utils.uv_overrides`, which exists for the same
reason on the ``--overrides`` side.
"""

import ast
import shlex
from pathlib import Path

import pytest

from teatree import self_update as self_update_mod
from teatree.cli import dep_drift_repair as dep_drift_repair_mod
from teatree.cli.dep_drift_repair import RepairPlan, resolve_repair_plan
from teatree.self_update import reinstall_running_editable
from teatree.utils import uv_constraints as uv_constraints_mod
from teatree.utils.editable_pth import EditableInstall
from teatree.utils.uv_constraints import UV_CONSTRAINTS_FILENAME, uv_constraints_args

_CORE_ROOT = Path(__file__).resolve().parents[2]
_REPAIR_HINT_SURFACES = (
    "src/teatree/cli/doctor/checks_environment.py",
    "src/teatree/cli/doctor/checks_mcp.py",
    "src/teatree/mcp/liveness.py",
    "src/teatree/backends/slack/receiver.py",
    "src/teatree/backends/markdown_conversion.py",
)


class _Proc:
    def __init__(self, returncode: int, stdout: str = "ok", stderr: str = "") -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class _RecordingRunner:
    def __init__(self, tool_dir: Path) -> None:
        self.tool_dir = tool_dir
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **_kwargs: object) -> _Proc:
        self.calls.append(cmd)
        return _Proc(0, str(self.tool_dir)) if cmd[1:3] == ["tool", "dir"] else _Proc(0)

    @property
    def install(self) -> list[str]:
        return next(cmd for cmd in self.calls if "--reinstall" in cmd)


@pytest.fixture
def vendored_checkout(tmp_path: Path) -> Path:
    """Core vendored under ``<fork>/vendor/teatree``, carrying its generated constraints file."""
    checkout = tmp_path / "fork" / "vendor" / "teatree"
    checkout.mkdir(parents=True)
    (checkout / UV_CONSTRAINTS_FILENAME).write_text("pinned==1.0\n", encoding="utf-8")
    return checkout


def _constraints_of(argv: list[str]) -> str | None:
    return argv[argv.index("--constraints") + 1] if "--constraints" in argv else None


class TestTheFlag:
    def test_it_names_the_checkouts_own_constraints_file(self, vendored_checkout: Path) -> None:
        assert uv_constraints_args(vendored_checkout) == [
            "--constraints",
            str(vendored_checkout / UV_CONSTRAINTS_FILENAME),
        ]

    def test_it_degrades_to_no_flag_when_the_generated_file_is_absent(self, tmp_path: Path) -> None:
        # The file is generated at boot and gitignored, so a dev checkout legitimately has
        # none. Naming a missing path would turn every repair into the hard resolver error
        # this module exists to prevent.
        assert uv_constraints_args(tmp_path) == []


class TestEveryTeatreeOwnedInstallCarriesIt:
    def test_the_self_update_reinstall(
        self, vendored_checkout: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(self_update_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(self_update_mod, "current_editable_source", lambda _uv: vendored_checkout)
        runner = _RecordingRunner(tmp_path / "uv-tools")

        assert reinstall_running_editable(runner=runner).ok is True
        assert _constraints_of(runner.install) == str(vendored_checkout / UV_CONSTRAINTS_FILENAME)

    def test_the_editable_receipt_repair(self, vendored_checkout: Path) -> None:
        argv = EditableInstall(checkout=vendored_checkout, host=vendored_checkout.parent.parent).install_argv("uv")

        assert _constraints_of(argv) == str(vendored_checkout / UV_CONSTRAINTS_FILENAME)

    def test_the_dep_drift_repair(self, vendored_checkout: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dep_drift_repair_mod, "editable_source_path", lambda: vendored_checkout)
        monkeypatch.setattr(dep_drift_repair_mod, "running_env_is_uv_tool", lambda: True)
        monkeypatch.setattr(dep_drift_repair_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
        plan = resolve_repair_plan(["packaging"])

        assert isinstance(plan, RepairPlan), plan
        assert _constraints_of(plan.cmd) == str(vendored_checkout / UV_CONSTRAINTS_FILENAME)
        assert "--constraints" in plan.label, "the operator's copy-paste fallback must carry it too"


class TestEveryRepairHintCarriesIt:
    def test_the_renderer_names_the_generated_file(self, vendored_checkout: Path) -> None:
        render = getattr(uv_constraints_mod, "uv_tool_install_hint", None)
        assert callable(render)

        command = render("uv tool install --editable .", vendored_checkout)

        assert command == (
            "uv tool install --editable . --constraints "
            f"{shlex.quote(str(vendored_checkout / UV_CONSTRAINTS_FILENAME))}"
        )

    def test_the_renderer_leaves_a_development_command_unconstrained(self, tmp_path: Path) -> None:
        render = getattr(uv_constraints_mod, "uv_tool_install_hint", None)
        assert callable(render)
        assert render("uv tool install --editable .", tmp_path) == "uv tool install --editable ."

    @staticmethod
    def _docstrings(tree: ast.AST) -> set[ast.Constant]:
        return {
            node.body[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }

    @staticmethod
    def _wrapped_by_renderer(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
        current = node
        while current in parents:
            current = parents[current]
            if isinstance(current, ast.Call) and isinstance(current.func, ast.Name):
                return current.func.id == "uv_tool_install_hint"
        return False

    def test_every_emitted_repair_command_uses_the_renderer(self) -> None:
        unwrapped: list[str] = []
        seen: dict[str, int] = {}
        for relative_path in _REPAIR_HINT_SURFACES:
            source_path = _CORE_ROOT / relative_path
            tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
            parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
            docstrings = self._docstrings(tree)
            commands = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "uv tool install --editable" in node.value
                and node not in docstrings
            ]
            seen[relative_path] = len(commands)
            unwrapped.extend(
                f"{relative_path}:{node.lineno}" for node in commands if not self._wrapped_by_renderer(node, parents)
            )

        assert all(seen.values()), seen
        assert unwrapped == []


class TestTheVendoredLayoutIsWhatBreaksWithoutIt:
    def test_the_flag_follows_the_checkout_never_the_fork_root(self, vendored_checkout: Path) -> None:
        # The control that pins the actual defect: the stale ambient value resolves against the
        # FORK ROOT, which has no generated constraints file. Anything anchored there is the bug.
        fork_root = vendored_checkout.parent.parent
        assert not (fork_root / UV_CONSTRAINTS_FILENAME).exists()
        assert uv_constraints_args(vendored_checkout) == [
            "--constraints",
            str(vendored_checkout / UV_CONSTRAINTS_FILENAME),
        ]
