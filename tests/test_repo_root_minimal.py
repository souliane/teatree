r"""The repo root stays minimal — every tracked top-level entry is allowlisted.

The #1993 "structure honestly" criterion: a newcomer reads the tree and the
root carries only the conventional/required set. This pin fails when a tracked
file or directory appears at the root that is not on the allowlist, so root
clutter can never creep back silently. Adding a genuinely-needed root entry is a
one-line allowlist edit with a justification in the commit message.

Each non-obvious entry is load-bearing at the root by an external tool's
convention (verified, not assumed):

-   ``agents/`` — Claude plugin agent (phase) definitions; the skill-ref prek
    hook globs ``^agents/.*\.md``.
-   ``settings.json`` — Claude plugin settings, resolved at root by the loader.
-   ``apm.yml`` + ``.apm/`` — APM package manifest + primitives dir; APM aborts
    the install without them (see ``tests/test_apm_package_structure.py``).
-   ``dev/`` — dev/test infra; CI builds ``dev/Dockerfile.test`` and the local
    runners are ``dev/test-*.sh``.
-   ``e2e/`` — declared in ``tach.toml`` ``source_roots``.
-   ``dist/`` — build/SBOM artifact dir.
"""

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

_ALLOWED_ROOT_ENTRIES = frozenset(
    {
        # Conventional Python / project files
        "README.md",
        "LICENSE",
        "pyproject.toml",
        "uv.lock",
        "tach.toml",
        "manage.py",
        "mkdocs.yml",
        # Architecture + agent docs
        "BLUEPRINT.md",
        "CLAUDE.md",
        "AGENTS.md",
        # Source trees
        "src",
        "tests",
        "scripts",
        "docs",
        "hooks",
        "skills",
        "evals",  # eval definitions (specs + fixtures); SOT is evals/README.md
        "e2e",
        # Build / tool dirs and manifests required at root by external tooling
        "dist",
        "dev",
        "agents",
        "apm.yml",
        "settings.json",
        ".apm",
        ".claude-plugin",
        ".github",
        ".ast-grep",
        ".vscode",
        # CI / quality gates
        "requirements.audit.ignore",  # per-CVE pip-audit allowlist (CI security gate)
        # Dotfiles
        ".claudeignore",
        ".codespell-dictionary.txt",
        ".editorconfig",
        ".gitignore",
        ".gitlab-ci.yml",
        ".jscpd.json",
        ".markdownlint-cli2.yaml",
        ".mcp.json",
        ".pre-commit-config.yaml",
        ".python-version",
    }
)


def _tracked_root_entries() -> set[str]:
    out = subprocess.run(
        ["git", "ls-files"],  # noqa: S607
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {line.split("/", 1)[0] for line in out.splitlines() if line}


def test_no_unexpected_tracked_root_entries() -> None:
    unexpected = sorted(_tracked_root_entries() - _ALLOWED_ROOT_ENTRIES)
    assert not unexpected, (
        "new tracked entries at the repo root are not on the minimal allowlist: "
        f"{unexpected}. Move them under an owning directory, or add to the "
        "allowlist with a justification if they are root-conventional."
    )
