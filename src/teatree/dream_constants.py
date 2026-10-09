"""Dependency-free constants shared by dream processing and backlog scanning."""

from typing import Final

#: Opens every batch manifest; the extract drops any line carrying it, since text the pass rendered is never drift.
DREAM_BATCH_MANIFEST_HEADER: Final = "Dream promotion batch (#4776)"
