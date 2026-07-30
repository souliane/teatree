"""Django-free value coercion shared by the hot (Django) and cold (stdlib) config readers.

The single home for the strict type-coercion rules a stored ``ConfigSetting``
value is checked against — extracted so the pre-Django ``cold_reader`` and the
Django-side ``setting_parsers`` no longer duplicate the logic (config §3d #5). The
ONE intentional divergence between the two readers — the hot path coerces a JSON
string ``"5"`` to ``5`` while the cold path rejects it — is now an EXPLICIT
``accept_numeric_str`` argument, not a comment keeping two copies aligned.

Every coercer is strict about the ``bool`` / ``int`` subclass trap (#258): a
``bool`` is never a valid ``int`` / ``float`` value (``int(True) == 1`` would
silently accept a JSON ``true`` for an int setting), so it raises rather than
coercing. Callers that want fail-SAFE degradation (the cold reader returning a
default, the pane-budget parsers returning their bound) wrap the raise.

Stdlib only — importable on the pre-Django cold path with no Django / no
``teatree.paths`` side effects.
"""


def strict_bool(raw: object) -> bool:
    """Return *raw* only when it is a real ``bool``; raise ``ValueError`` otherwise.

    TOML ``true``/``false`` and JSON ``true``/``false`` both decode to a real
    Python ``bool``. A quoted ``"false"`` (a ``str``), a number, or a list is
    rejected rather than truthy-coerced via ``bool(...)`` — ``bool("false") ==
    True`` silently ENABLED an opt-in safety setting (#258).
    """
    if isinstance(raw, bool):
        return raw
    msg = f"Invalid bool value {raw!r}; expected a JSON/TOML boolean (true/false), not a quoted string or number"
    raise ValueError(msg)


def strict_int(raw: object, *, accept_numeric_str: bool) -> int:
    """Coerce *raw* to ``int``, rejecting ``bool`` and (unless opted in) numeric strings.

    A ``bool`` always raises ``TypeError`` — it subclasses ``int``, so a bare
    ``int`` coercion made ``int(True) == 1`` silently accept a JSON ``true`` for
    an int-typed setting (#258). A ``float`` raises rather than truncating.

    ``accept_numeric_str`` is the one hot-vs-cold divergence made explicit: the
    Django-side read tier may store ``"5"`` and coerces it (``accept_numeric_str=
    True``); the pre-Django ``cold_reader`` rejects a numeric string
    (``accept_numeric_str=False``) as defense-in-depth, because its only writer is
    the validated ``config_setting`` path that stores canonical JSON ints.
    """
    if isinstance(raw, bool):
        msg = f"Invalid int value {raw!r}; a boolean is not an integer setting value"
        raise TypeError(msg)
    if isinstance(raw, int):
        return raw
    if accept_numeric_str and isinstance(raw, str):
        return int(raw.strip())
    msg = f"Invalid int value {raw!r}; expected a JSON/TOML integer"
    raise TypeError(msg)


def strict_float(raw: object, *, accept_numeric_str: bool = True) -> float:
    """Coerce *raw* to ``float``, rejecting ``bool`` (``float(True) == 1.0``, #258).

    Accepts a real ``float``, an ``int`` (a TOML ``25`` for a float setting is
    legitimate), and — when ``accept_numeric_str`` — a numeric string.
    """
    if isinstance(raw, bool):
        msg = f"Invalid float value {raw!r}; a boolean is not a float setting value"
        raise TypeError(msg)
    if isinstance(raw, int | float):
        return float(raw)
    if accept_numeric_str and isinstance(raw, str):
        return float(raw.strip())
    msg = f"Invalid float value {raw!r}; expected a JSON/TOML number"
    raise TypeError(msg)


def strict_str(raw: object) -> str:
    """Return *raw* only when it is a real ``str``; raise ``TypeError`` otherwise.

    Rejects a ``bool``/``int``/``float``/``list`` rather than stringifying via
    ``str(...)`` (``str(True) == "True"``, #258).
    """
    if not isinstance(raw, str):
        msg = f"Invalid str value {raw!r}; expected a JSON/TOML string"
        raise TypeError(msg)
    return raw


def strict_str_list(raw: object) -> list[str]:
    """Coerce a real list to ``list[str]``; raise ``TypeError`` on a non-list scalar.

    A bool, an int, or a bare string RAISES rather than degrading to ``[]`` (#258):
    ``config_setting set excluded_skills true`` would otherwise persist the raw
    ``True`` masked as an empty list with no signal.
    """
    if not isinstance(raw, list):
        msg = f"Invalid list value {raw!r}; expected a JSON/TOML array, not a scalar"
        raise TypeError(msg)
    return [str(s) for s in raw]
