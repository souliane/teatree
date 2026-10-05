"""Regulated-path model eligibility — the EU data-residency / compliance allowlist (#2887).

Extracted out of :mod:`teatree.agents.model_tiering` so the tiering module owns
only tier→model resolution and this module owns the ORTHOGONAL policy question:
whether a resolved model id may run on a REGULATED lane that carries client/bank
data. The gate is governed by the DB-home ``regulated_path_model_allowlist``
setting, never inferred from the model in code. Consumed at the harness / eval boundary
(:mod:`teatree.agents.harness`, :mod:`teatree.eval.pydantic_ai_runner`).
"""

from collections.abc import Sequence
from dataclasses import dataclass

from teatree.config import UserSettings, get_effective_settings


def is_regulated_path_eligible(model_id: str, allowlist: Sequence[str]) -> bool:
    """Whether *model_id* is on the regulated-path *allowlist* (case-insensitive substring).

    The regulated path carries client/bank data, so the models eligible to run on
    it are governed by EU data-residency & regulatory compliance (GDPR, data
    residency, processor jurisdiction) and enumerated in an EXPLICIT
    operator-configured allowlist
    (:data:`~teatree.config.UserSettings.regulated_path_model_allowlist`) — a
    BYOK / residency-controlled set, never inferred from the model in code. A model
    is eligible only when its id matches an allowlist pattern. An empty allowlist
    has no eligible model; the policy skips this check when no restriction is configured.
    """
    lowered = model_id.lower()
    return any(pattern.lower() in lowered for pattern in allowlist)


def assert_model_allowed_on_regulated_path(
    model_id: str,
    *,
    allowlist: Sequence[str] | None = None,
) -> None:
    """Raise ``ValueError`` when *model_id* is not eligible for a REGULATED lane's path.

    A lane that carries regulated client/bank data (a future regulated / EU-residency lane)
    restricts inference to a compliance-vetted model set — an EU data-residency &
    regulatory-compliance requirement (GDPR, data residency, processor jurisdiction),
    not a model-origin question. A configured ``regulated_path_model_allowlist``
    identifies eligible models on a regulated lane; when empty, this installation
    has no regulated model restriction and blocks nothing.

    CLIENT-SIDE ONLY (best-effort): this rejects an ineligible id BEFORE the request,
    but a configured ``openai_compatible_model`` that is itself a SERVER-SIDE routing
    handle can still land on a model not on the allowlist. An operator needing a HARD
    regulated-path restriction must ALSO constrain the provider's own allowed-models
    policy or pin explicit model ids.

    *allowlist* is injectable for tests; the default reads resolved DB-home settings.
    """
    if allowlist is None:
        allowlist = get_effective_settings().regulated_path_model_allowlist
    if allowlist and not is_regulated_path_eligible(model_id, allowlist):
        msg = (
            f"model {model_id!r} is not eligible for the regulated path "
            "(the id is not on regulated_path_model_allowlist — "
            "the EU data-residency / regulatory-compliance allowlist for the regulated lane); "
            "add the model to regulated_path_model_allowlist for the overlay"
        )
        raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class RegulatedPathPolicy:
    """The model allowlist, carried as a value so the read happens where it is legal (#3980).

    A harness builds its model inside an ``async open``, and Django refuses a synchronous ORM
    read to a thread that owns a running event loop; the config resolver catches that refusal
    and resolves the whole DB override tier as unreadable. Read there, the allowlist
    would fall back to empty and the lane would silently stop being restricted.
    Resolving the policy where the harness is BUILT (synchronously) is what keeps
    the operator's stored values load-bearing.
    """

    allowlist: tuple[str, ...] = ()

    @classmethod
    def from_settings(cls, settings: UserSettings | None = None) -> "RegulatedPathPolicy":
        """Resolve the policy from *settings*, or from the active scope's resolved settings."""
        resolved = settings if settings is not None else get_effective_settings()
        return cls(allowlist=tuple(resolved.regulated_path_model_allowlist))

    @classmethod
    def resolve(cls, policy: "RegulatedPathPolicy | None") -> "RegulatedPathPolicy":
        """*policy* when a caller resolved one already, else a fresh read of the stored settings.

        The fallback is what keeps the gate from silently narrowing: a harness built without an
        explicit policy still enforces what the operator stored. Only the caller that resolves
        the policy SYNCHRONOUSLY buys immunity from the async-frame read.
        """
        return policy if policy is not None else cls.from_settings()

    def assert_allowed(self, model_id: str) -> None:
        """Raise ``ValueError`` when *model_id* is ineligible under THIS resolved policy."""
        assert_model_allowed_on_regulated_path(model_id, allowlist=self.allowlist)
