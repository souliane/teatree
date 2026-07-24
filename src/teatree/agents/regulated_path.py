"""Regulated-path model eligibility — the EU data-residency / compliance allowlist (#2887).

Extracted out of :mod:`teatree.agents.model_tiering` so the tiering module owns
only tier→model resolution and this module owns the ORTHOGONAL policy question:
whether a resolved model id may run on a REGULATED lane that carries client/bank
data. The gate is governed by the DB-home ``enforce_regulated_path`` /
``regulated_path_model_allowlist`` settings, never inferred from the model in
code. Consumed at the harness / eval boundary
(:mod:`teatree.agents.harness`, :mod:`teatree.eval.pydantic_ai_runner`).
"""

from collections.abc import Sequence

from teatree.config import get_effective_settings


def is_regulated_path_eligible(model_id: str, allowlist: Sequence[str]) -> bool:
    """Whether *model_id* is on the regulated-path *allowlist* (case-insensitive substring).

    The regulated path carries client/bank data, so the models eligible to run on
    it are governed by EU data-residency & regulatory compliance (GDPR, data
    residency, processor jurisdiction) and enumerated in an EXPLICIT
    operator-configured allowlist
    (:data:`~teatree.config.UserSettings.regulated_path_model_allowlist`) — a
    BYOK / residency-controlled set, never inferred from the model in code. A model
    is eligible only when its id matches an allowlist pattern; an empty allowlist
    makes nothing eligible (fail-closed for a regulated lane).
    """
    lowered = model_id.lower()
    return any(pattern.lower() in lowered for pattern in allowlist)


def assert_model_allowed_on_regulated_path(
    model_id: str,
    *,
    enforce_regulated_path: bool | None = None,
    allowlist: Sequence[str] | None = None,
) -> None:
    """Raise ``ValueError`` when *model_id* is not eligible for a REGULATED lane's path.

    A lane that carries regulated client/bank data (a future regulated / EU-residency lane)
    restricts inference to a compliance-vetted model set — an EU data-residency &
    regulatory-compliance requirement (GDPR, data residency, processor jurisdiction),
    not a model-origin question. The gate is the DB-home ``enforce_regulated_path``
    (default ``False`` — the teatree factory lane carries no regulated data and runs
    unrestricted, incl. cheap open-source models); when ``True``, only a model whose
    id is on the EXPLICIT ``regulated_path_model_allowlist`` (a per-overlay,
    BYOK / residency-controlled allowlist) may run — everything else is refused as a
    config-policy violation.

    CLIENT-SIDE ONLY (best-effort): this rejects an ineligible id BEFORE the request,
    but a configured ``openai_compatible_model`` that is itself a SERVER-SIDE routing
    handle can still land on a model not on the allowlist. An operator needing a HARD
    regulated-path restriction must ALSO constrain the provider's own allowed-models
    policy or pin explicit model ids.

    *enforce_regulated_path* / *allowlist* are injectable for tests; the defaults
    read the resolved DB-home settings.
    """
    if enforce_regulated_path is None or allowlist is None:
        settings = get_effective_settings()
        if enforce_regulated_path is None:
            enforce_regulated_path = settings.enforce_regulated_path
        if allowlist is None:
            allowlist = settings.regulated_path_model_allowlist
    if enforce_regulated_path and not is_regulated_path_eligible(model_id, allowlist):
        msg = (
            f"model {model_id!r} is not eligible for the regulated path "
            "(enforce_regulated_path is True and the id is not on regulated_path_model_allowlist — "
            "the EU data-residency / regulatory-compliance allowlist for the regulated lane); "
            "add the model to regulated_path_model_allowlist for the overlay, or "
            "`t3 <overlay> config_setting set enforce_regulated_path false --overlay <name>`"
        )
        raise ValueError(msg)
