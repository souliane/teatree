"""The repositories a forge-reading scanner may read and act inside."""

import logging
from dataclasses import dataclass, field

from teatree.loop.url_specificity import best_url_match_specificity


@dataclass(slots=True)
class RepositoryScope:
    """An overlay's URL prefixes, where an empty set means the scope could not be resolved.

    An unresolved scope admits nothing: reading everything the credential can list
    would reach repositories no overlay claimed, and a scanner acts on what it reads.
    """

    prefixes: tuple[str, ...]
    scanner: str
    log: logging.Logger
    _refusal_reported: bool = field(default=False, init=False)

    def refuses(self) -> bool:
        if self.prefixes:
            return False
        if not self._refusal_reported:
            self.log.warning("%s: no repository scope resolved; scanning nothing", self.scanner)
            self._refusal_reported = True
        return True

    def admits(self, url: str) -> bool:
        return bool(url) and best_url_match_specificity(url, self.prefixes) > 0
