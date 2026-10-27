from teatree.core.models.repo_expansion import RepoExpansion, TicketRepoExtra
from teatree.core.models.types import TicketExtra


def test_ticket_extra_keeps_repo_metadata_optional() -> None:
    assert RepoExpansion.__required_keys__ == {"repos", "reason"}
    assert {"branches", "repo_expansions"} <= TicketRepoExtra.__optional_keys__
    assert {"branches", "repo_expansions"} <= TicketExtra.__optional_keys__
