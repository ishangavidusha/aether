"""Explicit responses, for when returning a value is not enough.

Returning a dict or a model covers most handlers. This covers the rest: a
chosen status code, a content type that is not JSON, or bytes that are already
encoded and should not be touched.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Response:
    """A ready-to-send response.

    `body` may be bytes or str; str is encoded as UTF-8. `headers` are sent in
    addition to the content type.
    """

    body: bytes | str = b""
    status: int = 200
    content_type: str = "application/json"
    headers: dict[str, str] = field(default_factory=dict)

    def encoded(self) -> bytes:
        return self.body.encode() if isinstance(self.body, str) else bytes(self.body)

    def header_list(self) -> list[tuple[str, str]] | None:
        return list(self.headers.items()) if self.headers else None
