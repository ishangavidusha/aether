"""Aether: a fast Python web framework with a Rust core.

Milestone-1 spike: async handlers, exact-match routing, JSON encoded in Rust.
"""

from ._app import App
from ._core import Request

__all__ = ["App", "Request"]
