"""Name a matched registry term by ``class#index`` so a gate reports it without printing it."""

from collections.abc import Mapping

from teatree.hooks.leak_policy import ALLOW

UNLISTED_REF = "unlisted#0"


def _key(term: str) -> str:
    return term.strip().lower()


class TermRefs:
    """``class#index`` of each registry term; the earliest class wins a term listed twice.

    Terms the registry does not carry (a caller-supplied scan list) are named by their
    position in *scan_terms*, so a finding never falls back to printing the value.
    """

    def __init__(self, registry: Mapping[str, tuple[str, ...]] | None, scan_terms: tuple[str, ...] = ()) -> None:
        self._refs = {_key(term): f"scan#{index}" for index, term in enumerate(scan_terms)}
        for term_class, terms in reversed(list((registry or {}).items())):
            if term_class == ALLOW:
                continue
            self._refs.update({_key(term): f"{term_class}#{index}" for index, term in enumerate(terms)})

    def ref(self, term: str) -> str:
        return self._refs.get(_key(term), UNLISTED_REF)
