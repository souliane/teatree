"""Stage a generated artifact at the path git will actually record for it.

Every ``generate_*`` hook in this directory rewrites a file and then ``git add``s it.
A bare ``git add <path>`` is correct only while the hook's cwd IS the top of the work
tree, and two independent rules conspire to make that false. Git exports ``GIT_DIR``
to a hook fired from a linked worktree, and its documented rule for a ``GIT_DIR`` given
without ``GIT_WORK_TREE``/``core.worktree`` is that the CURRENT DIRECTORY is the top of
the work tree. A prek workspace runs a NESTED project's hooks — any directory carrying
its own ``.pre-commit-config.yaml`` — with the cwd set to that project's directory.

Composed, they record the file one level off: a doc written correctly to
``<nested>/docs/generated/x.md`` is staged as ``docs/generated/x.md``, an index entry
for a file that exists nowhere. The committer unstages it by hand every cycle, or
misses it and ships the phantom path into the tree.

Anchoring the call on the file's own directory and dropping the two work-tree
overrides makes git rediscover the repository from the file itself, so the recorded
path is the real work-tree-relative one whatever cwd the hook was handed.
``GIT_INDEX_FILE`` is deliberately kept: it names the index the in-progress commit
will read, and a partial commit (``git commit --only``) points it at a temporary one.
"""

import os
import subprocess
from pathlib import Path

#: The two variables that decide where git thinks the work tree starts. Everything
#: else a hook inherits is either irrelevant to ``add`` or load-bearing for it.
_WORK_TREE_OVERRIDES = frozenset({"GIT_DIR", "GIT_WORK_TREE"})


def stage(path: Path) -> None:
    target = path.resolve()
    env = {key: value for key, value in os.environ.items() if key not in _WORK_TREE_OVERRIDES}
    command = ["git", "-C", str(target.parent), "add", "--", str(target)]
    subprocess.run(command, check=False, env=env)
