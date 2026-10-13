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
        self._refs: dict[str, str] = {}
        self._terms: dict[str, str] = {}
        for index, term in enumerate(scan_terms):
            self._register(term, f"scan#{index}")
        for term_class, terms in reversed(list((registry or {}).items())):
            if term_class != ALLOW:
                for index, term in enumerate(terms):
                    self._register(term, f"{term_class}#{index}")

    @classmethod
    def of_classes(cls, registry: Mapping[str, tuple[str, ...]], classes: tuple[str, ...]) -> "TermRefs":
        """Name every value of *classes*, ``allow`` included, for a scan whose findings are those values."""
        refs = cls(None)
        for term_class in reversed(classes):
            for index, term in enumerate(registry[term_class]):
                refs._register(term, f"{term_class}#{index}")
        return refs

    def _register(self, term: str, ref: str) -> None:
        self._refs[_key(term)] = ref
        self._terms[ref] = term.strip()

    def ref(self, term: str) -> str:
        return self._refs.get(_key(term), UNLISTED_REF)

    def term(self, ref: str) -> str | None:
        """The registry term a ``class#index`` names, for the in-process caller that must act on it."""
        return self._terms.get(ref)
