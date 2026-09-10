"""The skills teatree never installs, whatever declares them.

One authority, reachable from both the runtime linker and the provisioning layer.
It lives here rather than beside the linker because ``cli.setup.skill_linker``
imports ``cli.doctor``, so a doctor-side probe reading the list from there would
close an import cycle — and the list is a fact about what teatree provisions, not
about how one command syncs symlinks.
"""

#: Skills that conflict with teatree's multi-repo architecture. Always excluded —
#: not user-configurable. Users add their own via the ``excluded_skills`` setting.
CORE_EXCLUDED_SKILLS = ["using-superpowers", "using-git-worktrees"]
