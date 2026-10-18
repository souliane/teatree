from teatree.core.models import Task
from teatree.core.models.checkout_exit import CheckoutExit, checkout_exit_scope, prepared_checkout_exit


def test_a_repeat_settle_is_reused_but_a_non_attesting_one_settles_afresh() -> None:
    attests: list[bool] = []

    def settle(*, attest: bool) -> CheckoutExit:
        attests.append(attest)
        return CheckoutExit(release=True, attested_state="digest" if attest else None)

    task = Task(pk=7)
    with checkout_exit_scope(lambda _task, attest: settle(attest=attest)):
        prepared_checkout_exit(task)
        prepared_checkout_exit(task)
        settled = prepared_checkout_exit(task, attest=False)

    assert attests == [True, False]
    assert settled is not None
    assert settled.attested_state is None
