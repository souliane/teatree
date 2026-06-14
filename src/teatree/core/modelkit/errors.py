class RedisSlotsExhaustedError(RuntimeError):
    """All configured Redis DB slots are in use by active tickets."""
