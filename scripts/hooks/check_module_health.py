"""Compatibility shim — the hook body now lives in the packaged portable set.

The canonical source of truth is
:mod:`teatree.hooks.portable.check_module_health` (#3312), so a non-editable
teatree install can run the same gate via ``t3 hook run check_module_health``.
This file is kept only so teatree's own prek + CI entry
(``scripts/hooks/check_module_health.py``) and ``import
scripts.hooks.check_module_health`` (existing tests) resolve to that one module.
Replacing this module object in ``sys.modules`` makes both paths observe the
packaged module directly — monkeypatching internals still reaches the real code.
"""

import sys

from teatree.hooks.portable import check_module_health as _hook

sys.modules[__name__] = _hook

if __name__ == "__main__":
    raise SystemExit(_hook.main())
