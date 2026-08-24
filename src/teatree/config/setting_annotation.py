"""What a setting ACCEPTS, as one line of text — its stored type and its admissible values.

:func:`~teatree.config.schema.setting_choices` already answers the values half, raw, and is
what the dashboard's selects are built from. This module adds the type half and writes the
pair out as prose, so the TOML comment beside a key and any other surface that explains one
cannot phrase the same constraint two ways.

Both halves live on THIS side of the schema rather than inside it, for the reason
``setting_choices`` states about its own values: how a stored form READS is a surface
decision. ``dict`` is named ``table`` here because that is what it renders as in the file —
schema vocabulary would have no opinion. The unwrapping mirrors ``schema._enumerated``
(``Literal`` members share one type, an optional is named beside its ``None``, a ``StrEnum``
is stored as the ``str`` it subclasses), and
``tests/teatree_config/test_setting_annotation.py`` pins the naming TOTAL over the schema so
a shape neither handles cannot ship silently.

``bool`` names its type and no list: its two values ARE the type, and spelling them out
beside 104 keys is noise the type already carries. Every other constrained type names its
alternatives, because nothing else in the line says what they are.
"""

from enum import StrEnum
from types import NoneType, UnionType
from typing import Literal, Union, get_args, get_origin

from teatree.config.schema import TeatreeSettingsSchema, setting_choices

#: The stored TYPE each annotation maps to, in the vocabulary the FILE writes it in. Keyed on
#: the EXACT type rather than by ``issubclass``, so ``bool`` never resolves through ``int``.
_STORED_TYPE_NAMES: dict[object, str] = {
    bool: "bool",
    int: "int",
    float: "float",
    str: "str",
    list: "list",
    dict: "table",
}


def _type_name(annotation: object) -> str:
    """The stored type *annotation* accepts, or ``""`` when no name above fits its shape."""
    if get_origin(annotation) is Literal:
        return _type_name(type(get_args(annotation)[0]))
    if get_origin(annotation) in {Union, UnionType}:
        named = (_type_name(arg) for arg in get_args(annotation) if arg is not NoneType)
        return next((name for name in named if name), "")
    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        return "str"
    return _STORED_TYPE_NAMES.get(get_origin(annotation) or annotation, "")


def setting_type_name(key: str) -> str:
    """*key*'s stored type — the other half of "what may I put here", beside its choices.

    Derived off the same field annotation :func:`~teatree.config.schema.setting_choices`
    reads, so a surface explaining a setting takes both halves from the schema rather than
    inferring the type from whichever value happens to be stored. ``""`` for a key the
    schema does not declare, so an unknown key degrades to no annotation rather than raising.
    """
    field = TeatreeSettingsSchema.model_fields.get(key)
    return "" if field is None else _type_name(field.annotation)


def choice_token(value: object) -> str:
    """One admissible *value* as it reads in a list — the two invisible ones made visible.

    A bare rendering would print the empty string and ``None`` as nothing at all, so a
    reader could not tell an offered value from a formatting slip in the list beside it.
    Both are real states here (auto-detect / unset), never placeholders.
    """
    if value is None:
        return "none"
    return '""' if isinstance(value, str) and not value else str(value)


def setting_annotation(key: str) -> str:
    """*key*'s type, and the values it admits when the schema constrains them to a listable set.

    ``""`` for a key the schema does not declare — the caller then annotates nothing rather
    than asserting a type it cannot derive.
    """
    type_name = setting_type_name(key)
    choices = setting_choices(key)
    if not type_name or not choices or type_name == "bool":
        return type_name
    return f"{type_name}, one of: {' | '.join(choice_token(value) for value in choices)}"


__all__ = ["choice_token", "setting_annotation", "setting_type_name"]
