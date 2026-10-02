"""Pure model-support leaves — phase vocabulary, DB-lock retry, Fibonacci backoff.

The lowest stratum of ``teatree.core``: pure helpers with no edge back into the
rest of ``core``. ``managers`` and ``models`` import DOWN into this leaf, so it
carries ``depends_on = []`` in ``tach.toml``. Imported by submodule path; no
eager re-export.
"""
