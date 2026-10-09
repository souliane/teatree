"""Compatibility shim — the hook body now lives in the packaged portable set.

Canonical source: :mod:`teatree.hooks.portable.check_no_silent_skip` (#3312),
runnable in any repo via ``t3 hook run check_no_silent_skip``. Kept so teatree's
own prek entry (``scripts/hooks/check_no_silent_skip.py``) and ``import
scripts.hooks.check_no_silent_skip`` resolve to that one module.
"""

import sys

from teatree.hooks.portable import check_no_silent_skip as _hook

sys.modules[__name__] = _hook

if __name__ == "__main__":
    raise SystemExit(_hook.main())
