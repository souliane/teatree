"""Let one dataclass parameter stand for its fields on a command line or in a tool schema.

Typer and the MCP SDK both build their parameters from the handler's signature, so a verb with
seven flags has seven function parameters. :func:`expand_dataclass_params` rewrites that signature
only: the handler keeps taking one parameter, and receives the dataclass built from the fields.
"""

import dataclasses
import functools
import inspect
from collections.abc import Callable, MutableMapping
from typing import TYPE_CHECKING, Annotated, cast, get_args, get_origin, get_type_hints

if TYPE_CHECKING:
    from _typeshed import DataclassInstance


@dataclasses.dataclass(frozen=True, slots=True)
class Expand:
    """Annotated metadata: prefix every expanded field name with *prefix* (``body_file`` from ``file``)."""

    prefix: str = ""


def expand_dataclass_params[H: Callable[..., object]](handler: H) -> H:
    signature = inspect.signature(handler)
    hints = get_type_hints(handler, include_extras=True)
    expanded: list[inspect.Parameter] = []
    groups: dict[str, tuple[type[DataclassInstance], str]] = {}
    for param in signature.parameters.values():
        cls, prefix = _dataclass_and_prefix(hints.get(param.name, param.annotation))
        if cls is None:
            expanded.append(param)
            continue
        groups[param.name] = (cls, prefix)
        _refuse_unexpandable_fields(cls)
        field_hints = get_type_hints(cls, include_extras=True)
        expanded.extend(
            inspect.Parameter(
                prefix + field.name,
                inspect.Parameter.KEYWORD_ONLY,
                default=inspect.Parameter.empty if field.default is dataclasses.MISSING else field.default,
                annotation=field_hints[field.name],
            )
            for field in dataclasses.fields(cls)
        )

    def rebuild(kwargs: MutableMapping[str, object]) -> None:
        for name, (cls, prefix) in groups.items():
            kwargs[name] = cls(**{field.name: kwargs.pop(prefix + field.name) for field in dataclasses.fields(cls)})

    wrapper: Callable[..., object]
    if inspect.iscoroutinefunction(handler):

        async def wrapper(*args: object, **kwargs: object) -> object:
            rebuild(kwargs)
            return await handler(*args, **kwargs)

    else:

        def wrapper(*args: object, **kwargs: object) -> object:
            rebuild(kwargs)
            return handler(*args, **kwargs)

    functools.update_wrapper(wrapper, handler)
    wrapper.__dict__["__signature__"] = signature.replace(parameters=expanded)
    wrapper.__annotations__ = {param.name: param.annotation for param in expanded} | {
        "return": signature.return_annotation
    }
    return cast("H", wrapper)


def _refuse_unexpandable_fields(cls: "type[DataclassInstance]") -> None:
    for field in dataclasses.fields(cls):
        if not field.init or field.default_factory is not dataclasses.MISSING:
            msg = f"{cls.__name__}.{field.name}: an expanded field needs init=True and a plain default or none"
            raise ValueError(msg)


def _dataclass_and_prefix(hint: object) -> "tuple[type[DataclassInstance] | None, str]":
    prefix = ""
    if get_origin(hint) is Annotated:
        hint, *metadata = get_args(hint)
        prefix = next((item.prefix for item in metadata if isinstance(item, Expand)), "")
    if isinstance(hint, type) and dataclasses.is_dataclass(hint):
        return hint, prefix
    return None, ""
