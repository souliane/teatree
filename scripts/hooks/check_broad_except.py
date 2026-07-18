"""Compatibility shim — the hook body now lives in the packaged portable set.

Canonical source: :mod:`teatree.hooks.portable.check_broad_except` (#3312),
runnable in any repo via ``t3 hook run check_broad_except``. Kept so teatree's
own prek entry (``scripts/hooks/check_broad_except.py``) and ``import
scripts.hooks.check_broad_except`` resolve to that one module.
"""

import sys

from teatree.hooks.portable import check_broad_except as _hook

sys.modules[__name__] = _hook

if __name__ == "__main__":
    raise SystemExit(_hook.main())
