"""Self-pin that the repo root holds only the conventional minimum.

The root is a high-traffic surface: every contributor and every agent reads it
first. A relocatable artifact left at root (a docs map, an audit note, a build
SBOM) is clutter that erodes that signal. This fitness function freezes the
tracked-root set to the ecosystem / plugin / APM / build / Django contract
allowlist below; adding any other tracked entry to root turns it red, so
re-clutter is structurally impossible rather than caught by review vigilance.

The allowlist mirrors the KEEP-at-root classification: Python packaging, the
conventional repo docs / agent contracts, the Django entry point, the tach /
docs-site configs, CI / hooks / tool configs, the Claude Code plugin contract
(``plugins/t3`` is a self-symlink back to root, so ``settings.json`` / ``hooks``
/ ``skills`` / ``agents`` / ``.claude-plugin`` are load-bearing AT root), the
APM package contract (``apm.yml`` + ``.apm/``, pinned by
``test_apm_package_structure.py``), and the source / tests / eval-definitions
(``evals/``) / docs / tooling trees.

The relocated trio is asserted explicitly absent so a half-move that left the
old root copy behind (the silent-sbom-divergence trap) is caught here.
"""

import shutil
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GIT = shutil.which("git") or "git"

ALLOWED_ROOT = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        "README.md",
        "LICENSE",
        "BLUEPRINT.md",
        "CLAUDE.md",
        "AGENTS.md",
        "manage.py",
        "tach.toml",
        "mkdocs.yml",
        ".pre-commit-config.yaml",
        ".gitignore",
        ".gitlab-ci.yml",
        "requirements.audit.ignore",  # per-CVE pip-audit allowlist (CI security gate)
        ".github",
        ".editorconfig",
        ".jscpd.json",
        ".markdownlint-cli2.yaml",
        ".codespell-dictionary.txt",
        ".ast-grep",
        ".vscode",
        ".claudeignore",
        ".claude-plugin",
        "settings.json",
        "hooks",
        "skills",
        "agents",
        "apm.yml",
        ".apm",
        "src",
        "tests",
        "evals",  # eval definitions (specs + fixtures); the SOT is evals/README.md
        "e2e",
        "docs",
        "scripts",
        "dev",
        "dist",
    }
)

RELOCATED = frozenset({"MAP.md", "audits", "sbom.json"})


def _tracked_root_entries() -> set[str]:
    out = subprocess.run(
        [_GIT, "ls-tree", "--name-only", "HEAD"],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {line for line in out.splitlines() if line}


class TestRepoRootMinimal:
    def test_no_unexpected_tracked_entries_at_root(self) -> None:
        extra = sorted(_tracked_root_entries() - ALLOWED_ROOT)
        assert not extra, f"unexpected tracked entries at repo root: {extra}"

    def test_relocated_artifacts_absent_from_root(self) -> None:
        present = sorted(_tracked_root_entries() & RELOCATED)
        assert not present, f"relocated artifacts still tracked at root: {present}"
