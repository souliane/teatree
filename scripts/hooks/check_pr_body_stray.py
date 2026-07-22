"""Compatibility shim — the hook body now lives in the packaged portable set.

Canonical source: :mod:`teatree.hooks.portable.check_pr_body_stray` (#3581),
runnable in any repo via ``t3 hook run check_pr_body_stray``. Kept so teatree's
own prek entry (``scripts/hooks/check_pr_body_stray.py``) resolves to that one
module.
"""

import sys

from teatree.hooks.portable import check_pr_body_stray as _hook

sys.modules[__name__] = _hook

if __name__ == "__main__":
    raise SystemExit(_hook.main())
