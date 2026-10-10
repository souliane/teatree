"""A scratch checkout that holds the real pre-push leak hook and the fingerprint it verifies the registry against.

The hook derives its checkout from its own location and holds the term registry it loads to the
fingerprint committed at that checkout's ``HEAD``. A test that drives the hook from this repository
would meet the real owner's digests, so it places the hook (a symlink, never a copy that could drift
from the file under test) in a scratch git checkout whose fingerprint is computed from the registry
the test hands the hook.
"""

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from teatree.hooks.term_fingerprint import gate_fingerprints

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
HOOK: Final = REPO_ROOT / "scripts" / "hooks" / "refuse-public-push-with-leak.sh"
REAL_SCANNER: Final = REPO_ROOT / "scripts" / "privacy_scan.py"
FINGERPRINT_PATH: Final = Path(".github") / "term-source-fingerprint.json"


def registry_table(registry: str | Mapping[str, object]) -> dict[str, list[str]]:
    """The ``{class: terms}`` table of a registry given as JSON text or as a table; ``{}`` when it is neither."""
    parsed = registry
    if isinstance(registry, str):
        try:
            parsed = json.loads(registry)
        except ValueError:
            return {}
    if not isinstance(parsed, Mapping):
        return {}
    return {str(key): [str(term) for term in value] for key, value in parsed.items() if isinstance(value, list)}


def fingerprint_text(registry: str | Mapping[str, object]) -> str:
    """The committed fingerprint file's text for *registry*."""
    return json.dumps({"version": 1, "gates": gate_fingerprints(registry_table(registry))}, indent=2) + "\n"


def _git(root: Path, *args: str) -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    subprocess.run([str(shutil.which("git")), *args], cwd=root, check=True, capture_output=True, env=env)


def _commit_everything(root: Path) -> None:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=scratch", "-c", "user.email=scratch@example.invalid", "commit", "-q", "-m", "x")


@dataclass(frozen=True)
class CommittedFingerprint:
    """What the scratch checkout commits: *registry*'s digests (the hook's own registry when ``None``), in *state*.

    ``committed``: the file is at HEAD and clean. ``missing``: HEAD has no such file. ``untracked``: the file is
    on disk and was never committed. ``edited``: HEAD holds the committed digests and the disk holds the hook's
    registry's. ``garbled``: the committed file is not JSON.
    """

    state: str = "committed"
    registry: str | Mapping[str, object] | None = None


def place_hook(
    root: Path,
    *,
    registry: str | Mapping[str, object],
    fingerprint: CommittedFingerprint | None = None,
    scanner: Path | str | None = REAL_SCANNER,
    src: Path | None = REPO_ROOT / "src",
) -> Path:
    """Place the real hook at ``<root>/scripts/hooks/`` in a git checkout and return its path.

    *registry* is what the hook will load. *scanner* is the real scanner (a symlink), the text of a stand-in
    the test authors, or ``None`` for none.
    """
    fingerprint = fingerprint or CommittedFingerprint()
    hooks_dir = root / "scripts" / "hooks"
    hooks_dir.mkdir(parents=True)
    placed = hooks_dir / HOOK.name
    placed.symlink_to(HOOK)
    if src is not None:
        (root / "src").symlink_to(src)
    if isinstance(scanner, Path):
        (root / "scripts" / "privacy_scan.py").symlink_to(scanner)
    elif scanner is not None:
        (root / "scripts" / "privacy_scan.py").write_text(scanner, encoding="utf-8")
    (root / "README.md").write_text("scratch checkout\n", encoding="utf-8")
    target = root / FINGERPRINT_PATH
    target.parent.mkdir(parents=True)
    committed_text = fingerprint_text(registry if fingerprint.registry is None else fingerprint.registry)
    if fingerprint.state in {"committed", "edited"}:
        target.write_text(committed_text, encoding="utf-8")
    if fingerprint.state == "garbled":
        target.write_text("{not json\n", encoding="utf-8")
    _commit_everything(root)
    if fingerprint.state == "untracked":
        target.write_text(committed_text, encoding="utf-8")
    if fingerprint.state == "edited":
        target.write_text(fingerprint_text(registry), encoding="utf-8")
    return placed


def default_hook(parent: Path, env: Mapping[str, str]) -> Path:
    """The hook placed beside *parent* for the registry *env* carries, reused when it is already there."""
    registry = env.get("TEATREE_TERM_REGISTRY", "")
    root = parent / f".leak-hook-{hashlib.sha256(registry.encode()).hexdigest()[:10]}"
    placed = root / "scripts" / "hooks" / HOOK.name
    return placed if placed.exists() else place_hook(root, registry=registry)
