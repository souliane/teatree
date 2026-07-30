"""Compatibility shim — the hook body now lives in the packaged portable set.

Canonical source: :mod:`teatree.hooks.portable.check_test_path_mirror` (#3312),
runnable in any repo via ``t3 hook run check_test_path_mirror``. Kept so
teatree's own prek entry (``scripts/hooks/check_test_path_mirror.py``) resolves
to that one module.
"""

import sys

from teatree.hooks.portable import check_test_path_mirror as _hook

sys.modules[__name__] = _hook

if __name__ == "__main__":
    raise SystemExit(_hook.main())
