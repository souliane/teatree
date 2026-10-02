"""Whether an artifact's documented rebuild can run in its checkout."""

from pathlib import Path

from teatree.core.cleanup.artifact_lock import artifact_name_pattern

#: What each name's documented rebuild command needs to FIND in the checkout before
#: the artifact can honestly be called rebuildable. "Rebuildable" is a claim about the
#: checkout, not a property of the name: ``npm ci`` REFUSES outright without a lockfile
#: and ``uv sync`` needs a manifest, so in a scratch repo carrying neither, removing the
#: artifact is not the free reclaim this pass advertises — it is a loss the documented
#: recovery cannot undo. ``.nx`` and ``.angular`` are absent DELIBERATELY: they are build
#: caches the next build regenerates unconditionally, with no manifest to consult, so
#: withholding them on this ground would cost reclaim for no safety.
#: A name may carry a ``*``, matched with :meth:`Path.glob` — ``requirements-dev.txt`` and
#: ``requirements/base.txt`` rebuild a venv exactly as ``requirements.txt`` does, and a
#: literal-only set called a project carrying one unrebuildable.
_REBUILD_INPUTS: dict[str, tuple[str, ...]] = {
    ".venv": (
        "uv.lock",
        "pyproject.toml",
        "requirements*.txt",
        "requirements/*.txt",
        "setup.py",
        "setup.cfg",
        "Pipfile",
    ),
    # A hook environment is a uv PROJECT environment (`UV_PROJECT_ENVIRONMENT` in
    # scripts/hooks/lib/resolve-uv.sh), so only a uv manifest rebuilds it. Keyed by the
    # ARTIFACT_NAMES pattern, since the name on disk carries the platform.
    ".venv-hook*": ("uv.lock", "pyproject.toml"),
    "node_modules": (
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lock",
        "bun.lockb",
    ),
}


def unrebuildable_reason(artifact: Path, *, checkout: Path) -> str:
    """Nothing in this checkout can rebuild it, so removing it is not a free reclaim.

    The whole module rests on "rebuildable", and until now that was ASSERTED by the name
    set rather than checked: ``_ARTIFACT_NAMES`` applies to every ``.git``-carrying
    directory under the scan roots, including scratch repos that never had a lockfile.
    Checked here against :data:`_REBUILD_INPUTS`; a name with no entry is a build cache
    the next build regenerates unconditionally and is never withheld on this ground.
    """
    inputs = rebuild_inputs_for(artifact)
    if not inputs or any(_rebuild_input_present(checkout, name) for name in inputs):
        return ""
    return f"nothing in the checkout rebuilds it — no {' / '.join(inputs)}, so the documented rebuild would refuse"


def rebuild_inputs_for(artifact: Path) -> tuple[str, ...] | None:
    """What the documented rebuild of *artifact* looks for, keyed by the PATTERN it matched.

    Keyed by the raw name, a platform-scoped hook environment matches no entry and reads
    as a build cache no manifest gates — evicting it from a checkout uv cannot rebuild in.
    """
    pattern = artifact_name_pattern(artifact.name)
    return _REBUILD_INPUTS.get(pattern) if pattern is not None else None


def _rebuild_input_present(checkout: Path, name: str) -> bool:
    """Whether *name* — a literal or a glob — names something in *checkout*; unreadable is absent."""
    try:
        return any(checkout.glob(name)) if "*" in name else (checkout / name).exists()
    except OSError:
        return False
