class InvalidTransitionError(ValueError):
    pass


class QualityGateError(ValueError):
    pass


class RedisSlotsExhaustedError(RuntimeError):
    """All configured Redis DB slots are in use by active tickets."""
