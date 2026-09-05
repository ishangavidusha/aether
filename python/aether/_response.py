"""Explicit responses, for when returning a value is not enough.

Returning a dict or a model covers most handlers. This covers the rest: a
chosen status code, a content type that is not JSON, or bytes that are already
encoded and should not be touched.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Response:
    """A ready-to-send response.

    `body` may be bytes or str; str is encoded as UTF-8.
    """

    body: bytes | str = b""
    status: int = 200
    content_type: str = "application/json"

    def encoded(self) -> bytes:
        return self.body.encode() if isinstance(self.body, str) else bytes(self.body)
