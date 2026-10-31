from teatree.core.models.repo_expansion import TicketRepoExtra


class TicketCarrierExtra(TicketRepoExtra, total=False):
    carries: list[str]
    carried_by: str
