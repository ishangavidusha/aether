"""Optional pydantic integration.

Pydantic is the declaration language for request and response bodies. Its core
is Rust, `model_validate_json` parses bytes without building an intermediate
Python dict, and `model_json_schema()` gives OpenAPI and MCP tool schemas for
free later.

The trade-off, taken deliberately: unlike path parameters, body validation runs
on the worker thread rather than the tokio thread, so a bad body does wake a
Python worker before it is rejected.

Aether still imports and runs without pydantic. Only body models need it.
"""

from typing import Any

try:
    from pydantic import BaseModel, ValidationError

    HAVE_PYDANTIC = True
    _MODEL_TYPES: tuple[type, ...] = (BaseModel,)
except ModuleNotFoundError:  # pragma: no cover - depends on the environment
    BaseModel = None  # type: ignore[assignment]
    ValidationError = None  # type: ignore[assignment]
    HAVE_PYDANTIC = False
    _MODEL_TYPES = ()


class RequestValidationError(Exception):
    """A request body failed validation. Carries a ready-to-send JSON body."""

    __slots__ = ("body",)

    def __init__(self, body: bytes) -> None:
        super().__init__("request body failed validation")
        self.body = body


def is_model(annotation: Any) -> bool:
    return HAVE_PYDANTIC and isinstance(annotation, type) and issubclass(annotation, BaseModel)


def is_model_instance(value: Any) -> bool:
    # Empty tuple makes this a cheap constant False when pydantic is absent.
    return isinstance(value, _MODEL_TYPES)


def to_json(instance: Any) -> bytes:
    """Serialize a model straight to bytes, skipping the str round trip."""
    return instance.__pydantic_serializer__.to_json(instance)


def validation_body(exc: Any) -> bytes:
    """FastAPI-shaped error payload, so existing clients and tooling can read it."""
    return b'{"detail":' + exc.json().encode() + b"}"
