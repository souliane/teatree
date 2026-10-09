"""Whether THIS worker's image generation may claim — read on every check, healed only by the claim path."""

from django.apps import apps

from teatree.generation import current_generation, short_sha


def generation_admission_refusal() -> str:
    """Refuse a worker whose image generation is draining, retired or failed."""
    sha = current_generation()
    if not sha:
        return ""
    state = apps.get_model("core", "WorkerGeneration").objects.claim_refusing_state(sha)
    return f"this worker's generation {short_sha(sha)} is {state}" if state else ""


def reopen_own_stranded_drain() -> None:
    """The claim path, never the read path, is where a drain stranded past its deadline heals."""
    if sha := current_generation():
        apps.get_model("core", "WorkerGeneration").objects.reopen_stranded_drain(sha)
