"""Aether: a fast Python web framework with a Rust core.

Async handlers, a radix-tree router with typed path and query parameters
coerced in Rust, pydantic request and response bodies, bounded concurrency,
and OpenAPI 3.1 generated from the same route metadata.
"""

from ._app import App
from ._core import Request
from ._response import Response

__all__ = ["App", "Request", "Response"]
