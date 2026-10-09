# test-path: cross-cutting — pins deploy/Dockerfile's stage layout; no teatree module owns it.
"""``deploy/Dockerfile`` bakes a generation as a reusable base plus a thin per-commit layer; the default stays."""

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from teatree.cli.setup.plugin_registrar import PLUGIN_NAME
from teatree.paths import GENERATION_MARKER

DOCKERFILE = Path(__file__).resolve().parents[1] / "deploy" / "Dockerfile"


def _stages() -> dict[str, str]:
    text = DOCKERFILE.read_text(encoding="utf-8")
    parts = re.split(r"^FROM\s+\S+(?:\s+AS\s+(\S+))?\s*$", text, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def _last_stage_name() -> str:
    return re.findall(r"^FROM\s+\S+(?:\s+AS\s+(\S+))?\s*$", DOCKERFILE.read_text(encoding="utf-8"), re.MULTILINE)[-1]


def test_the_generation_is_a_thin_layer_on_a_base_built_from_the_shared_system_stage() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert "ARG TEATREE_BASE_IMAGE=generation-base\nFROM ubuntu:" in text
    assert re.search(r"^FROM system AS generation-base$", text, re.MULTILINE)
    assert re.search(r"^FROM \$\{TEATREE_BASE_IMAGE\} AS generation$", text, re.MULTILINE)


def test_no_code_file_reaches_the_shared_system_layers_or_the_base() -> None:
    stages = _stages()

    assert "COPY" not in stages["system"]
    assert "entrypoint.sh" not in stages["generation-base"]
    assert "--mount=type=bind,target=/tmp/generation-context" not in stages["generation-base"]


def test_the_default_target_is_still_the_upstream_source_bake() -> None:
    default = _stages()[_last_stage_name()]

    assert 'git clone "$TEATREE_REPO_URL" "$TEATREE_CLONE_DIR"' in default
    assert "TEATREE_GENERATION" not in default


def test_a_generation_refuses_to_build_without_a_full_commit_sha() -> None:
    stage = _stages()["generation"]

    assert "ARG TEATREE_GENERATION" in stage
    assert "[0-9a-f]{40}" in stage


def test_the_base_bakes_the_locked_runtime_and_the_commit_only_its_own_packages() -> None:
    base, generation = _stages()["generation-base"], _stages()["generation"]

    assert "sync --frozen --no-dev --extra slack --compile-bytecode --no-install-workspace" in base
    assert 'locked-version.sh "$deps/uv.lock" prek' in base
    assert "sync --frozen --no-dev --extra slack --inexact" in generation
    assert "UV_PROJECT_ENVIRONMENT=/opt/teatree/venv" in generation
    assert re.search(r'PATH="/opt/teatree/venv/bin:\$\{PATH\}"', generation)


def test_the_baked_source_is_root_owned_and_read_only() -> None:
    stage = _stages()["generation"]

    assert re.search(r'chown -R root:root "\$root"', stage)
    assert re.search(r'chmod -R a-w "\$root"', stage)
    assert stage.rstrip().endswith("USER teatree")


def test_the_generation_names_itself_by_env_and_revision_label() -> None:
    stage = _stages()["generation"]

    assert "TEATREE_GENERATION=${TEATREE_GENERATION}" in stage
    assert "LABEL org.opencontainers.image.revision=${TEATREE_GENERATION}" in stage


def test_the_generation_never_fetches_source_from_a_remote() -> None:
    assert "git clone" not in _stages()["generation"]


_STUB_UV = r"""#!/bin/bash
printf '%s\n' "$*" >>"$FAKE_DIR/uv.log"
case "$1" in
export)
    while [ $# -gt 0 ]; do [ "$1" = -o ] && { printf 'prek==1.0\n' >"$2"; break; }; shift; done ;;
sync)
    mkdir -p "$FAKE_VENV/bin" ;;
pip)
    if [ -n "${UV_CONSTRAINT:-}" ] && [ ! -f "$UV_CONSTRAINT" ]; then
        echo "error: File not found: \`$UV_CONSTRAINT\`" >&2
        exit 2
    fi
    printf '%s\n' "${UV_CONSTRAINT:-}" >"$FAKE_DIR/pip-constraint" ;;
esac
"""


def _run_body(stage_name: str, context_mount: str) -> str:
    pattern = rf"^RUN --mount=type=bind,target={context_mount}.*?(?<!\\)\n"
    run = re.search(pattern, _stages()[stage_name], re.MULTILINE | re.DOTALL)
    assert run is not None, f"{stage_name} no longer bakes from {context_mount}"
    lines = run.group(0).split("\\\n")
    return " ".join(line for line in lines[1:] if not line.strip().startswith("--mount"))


def _stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _bake(tmp_path: Path, *, core_subdir: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    context = tmp_path / "context"
    core = context / core_subdir if core_subdir else context
    (core / "src" / "teatree").mkdir(parents=True)
    (core / "src" / "teatree" / "__init__.py").touch()
    root, venv, bin_dir, opt = tmp_path / "baked", tmp_path / "venv", tmp_path / "bin", tmp_path / "opt"
    opt.mkdir()
    _stub(bin_dir / "uv", _STUB_UV)
    for tool in ("locked-version.sh",):
        _stub(bin_dir / tool, "#!/bin/bash\necho 1.0\n")
    for tool in ("chown", "chmod"):
        _stub(bin_dir / tool, "#!/bin/bash\nexit 0\n")
    _stub(venv / "bin" / "python", "#!/bin/bash\nexit 0\n")
    _stub(venv / "bin" / "t3", '#!/bin/bash\nprintf "%s" "$HOME" >"$FAKE_DIR/t3-home"\n')
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DIR": str(tmp_path),
        "FAKE_VENV": str(venv),
        "TEATREE_CORE_SUBDIR": core_subdir,
        "TEATREE_GENERATION": SHA,
        "UV_PYTHON_INSTALL_DIR": str(tmp_path / "python"),
        "UV_OVERRIDE": str(root / "uv-overrides.txt"),
        "HOME": IMAGE_HOME,
    }
    for stage_name, mount in (("generation-base", "/tmp/base-context"), ("generation", "/tmp/generation-context")):
        script = (
            _run_body(stage_name, mount)
            .replace(mount, str(context))
            .replace("/tmp/deps", str(tmp_path / "deps"))
            .replace("/home/teatree/.local/bin/uv", str(bin_dir / "uv"))
            .replace("/home/teatree/teatree", str(root))
            .replace("/opt/teatree/uv-constraints.txt", str(opt / "uv-constraints.txt"))
            .replace("/opt/teatree/venv", str(venv))
        )
        result = subprocess.run([BASH, "-ec", script], capture_output=True, text=True, env=env, check=False)
        if result.returncode != 0:
            break
    return result, root


SHA = "c" * 40
BASH = shutil.which("bash") or ""
IMAGE_HOME = "/home/teatree"


@pytest.mark.parametrize(("core_subdir", "layout"), [("", "core-only"), ("vendor/teatree", "fork")])
class TestTheGenerationBakeRuns:
    def test_prek_installs_against_the_constraints_the_bake_exported(
        self, tmp_path: Path, core_subdir: str, layout: str
    ) -> None:
        result, root = _bake(tmp_path, core_subdir=core_subdir)

        assert result.returncode == 0, f"{layout}: {result.stderr}"
        assert (tmp_path / "pip-constraint").read_text().strip() == str(tmp_path / "opt" / "uv-constraints.txt")
        core = root / core_subdir if core_subdir else root
        assert (core / "uv-constraints.txt").read_text() == "prek==1.0\n"

    def test_the_commit_layer_keeps_what_the_base_installed(
        self, tmp_path: Path, core_subdir: str, layout: str
    ) -> None:
        result, _root = _bake(tmp_path, core_subdir=core_subdir)

        assert result.returncode == 0, f"{layout}: {result.stderr}"
        base_sync, commit_sync = [
            line for line in (tmp_path / "uv.log").read_text().splitlines() if line.startswith("sync")
        ]
        assert "--no-install-workspace" in base_sync
        assert "--inexact" in commit_sync

    def test_the_baked_tree_is_stamped_with_its_generation(self, tmp_path: Path, core_subdir: str, layout: str) -> None:
        result, root = _bake(tmp_path, core_subdir=core_subdir)

        assert result.returncode == 0, f"{layout}: {result.stderr}"
        assert (root / GENERATION_MARKER).read_text(encoding="utf-8").strip() == SHA

    def test_the_smoke_run_leaves_nothing_in_the_images_home(
        self, tmp_path: Path, core_subdir: str, layout: str
    ) -> None:
        result, _root = _bake(tmp_path, core_subdir=core_subdir)

        assert result.returncode == 0, f"{layout}: {result.stderr}"
        smoke_home = (tmp_path / "t3-home").read_text(encoding="utf-8")
        assert smoke_home != IMAGE_HOME
        assert not Path(smoke_home).exists()

    def test_the_marketplace_link_setup_needs_is_baked_in(self, tmp_path: Path, core_subdir: str, layout: str) -> None:
        result, root = _bake(tmp_path, core_subdir=core_subdir)

        assert result.returncode == 0, f"{layout}: {result.stderr}"
        assert (root / "plugins" / PLUGIN_NAME).readlink() == Path("..")
