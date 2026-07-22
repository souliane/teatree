"""Register the ``generated`` git merge driver during ``t3 setup`` (#3582).

Sibling of :class:`~teatree.cli.setup.git_hooks_installer.GitHooksInstaller`:
the merge driver, like the git hooks, is a per-checkout ``.git/config`` property
that has to be set in *every* checkout teatree commits from — not just the clone
setup was invoked from. This walks the same checkout set and registers the
driver in each, echoing one line per checkout.
"""

from collections.abc import Callable, Iterable
from pathlib import Path

from teatree.core.gates.git_checkouts import discover_checkouts
from teatree.core.git_merge_driver import install_merge_driver


class GitMergeDriverInstaller:
    """Compose unit: register the generated-docs merge driver in every checkout."""

    def __init__(self, repo: Path, checkouts: Iterable[Path] | None = None) -> None:
        self._repo = repo
        self._checkouts = list(checkouts) if checkouts is not None else None

    def _targets(self) -> list[Path]:
        if self._checkouts is not None:
            return self._checkouts
        discovered = discover_checkouts()
        return discovered if self._repo.resolve() in discovered else [self._repo, *discovered]

    def install(self, *, echo: Callable[[str], None]) -> None:
        for checkout in self._targets():
            echo(install_merge_driver(checkout))
