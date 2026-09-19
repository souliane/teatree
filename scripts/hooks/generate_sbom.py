"""Pre-commit hook: regenerate the lock-derived SBOM and stage it in the same commit.

``dist/sbom.json`` is generated FROM ``uv.lock`` and committed, so a commit that moves a
dependency without regenerating it lands a stale SBOM and CI's ``sbom`` job (regenerate +
``git diff --exit-code``) goes red by construction — most visibly on the weekly lock
upgrade, whose whole purpose is to move versions.

This wrapper closes that gap without forking the definition of the SBOM's bytes:
``scripts/hooks/generate_sbom.sh`` stays the single generator, called unchanged by CI's
``sbom`` job and by ``.github/workflows/uv-lock-upgrade.yml``. It also keeps the staging
rule in one place — ``generated_doc_staging``, which anchors ``git add`` on the file
itself so the recorded path is the real work-tree-relative one whatever cwd a hook was
handed. That is the pattern ``generate-cli-reference`` established (souliane/teatree#2599).

Staging matters as much as regenerating: without it the file on disk differs from the
index, prek reports "files were modified by this hook", and the commit ships the stale
bytes anyway. ``SBOM_NO_STAGE`` is CI's arm — regenerate, leave it unstaged, so a plain
``git diff`` (no ``--cached``) still catches a stale committed SBOM.

A generator failure propagates its exit code: never a silently kept stale file.

See: souliane/teatree#2663
"""

import os
import subprocess
import sys
from pathlib import Path

import generated_doc_staging

#: The single generator, repo-relative. CI's ``sbom`` job and the weekly lock-upgrade
#: workflow invoke this exact script, so it stays the one definition of the SBOM's bytes.
GENERATOR_REL = Path("scripts/hooks/generate_sbom.sh")

#: The committed, lock-derived artifact this hook keeps fresh.
SBOM_REL = Path("dist/sbom.json")


def checkout_root() -> Path:
    """The tree this hook ships inside — the anchor its relative paths are resolved from.

    Derived from ``__file__`` rather than the cwd: prek hands a nested project's hooks its
    own directory, and a cwd-anchored path would then write a stray ``dist/sbom.json``
    there instead of the one this commit records.
    """
    return Path(__file__).resolve().parents[2]


def main(*, repo_root: Path | None = None) -> int:
    root = (repo_root or checkout_root()).resolve()
    sbom = root / SBOM_REL

    old = sbom.read_bytes() if sbom.is_file() else b""

    result = subprocess.run([str(root / GENERATOR_REL)], cwd=root, check=False)
    if result.returncode != 0:
        sys.stderr.write(
            f"generate_sbom: {GENERATOR_REL} exited {result.returncode}; {SBOM_REL} may be "
            f"stale. Refusing to stage it.\n",
        )
        return result.returncode

    if sbom.read_bytes() == old or os.environ.get("SBOM_NO_STAGE"):
        return 0

    generated_doc_staging.stage(sbom)
    print(f"Updated {SBOM_REL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
