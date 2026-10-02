"""Re-export of :mod:`teatree.url_title_fetcher` for the ``lib.*`` hook import path.

The implementation is canonical in ``src/teatree/url_title_fetcher.py``. This is
a real module (never a symlink): a symlink puts one physical file under both the
``scripts/**`` and ``src/**`` ruff per-file-ignore scopes, which breaks
``ruff check --fix`` idempotency on a pristine checkout.
"""

from teatree.url_title_fetcher import enrich_prompt, fetch_titles

__all__ = ["enrich_prompt", "fetch_titles"]
