"""The Model Context Protocol endpoint.

One HTTP endpoint an agent talks to, over MCP's Streamable HTTP transport. It
lists the capabilities a service exposes, invokes them, and offers topics as
readable resources.

Nothing here is declared twice. Tools are derived from routes, which already
carry typed parameters, a body model, a response model and a docstring because
the router and OpenAPI need them. The same service answers curl, an OpenAPI
client and an agent from one set of declarations.

The transport is deliberately the simple half of the spec: a POST carrying one
JSON-RPC message, answered with JSON. Servers may also stream responses over
SSE and accept a GET to open a server-to-client channel; neither is needed to
list and call tools, so neither is implemented. A GET or DELETE gets 405 from
the router, which is what the spec asks of a server that does not support them.
"""

import json
from typing import Any

from ._capabilities import Capability, CapabilityError
from ._response import Response
from ._schema import is_model_instance

#: Protocol revisions this server has actually been exercised against, newest
#: first. A client asking for one of these gets it back; anything else gets
#: PREFERRED_VERSION, which is what the spec asks of a server that cannot
#: honour the request.
#:
#: These are verified, not aspirational. An earlier version of this list
#: claimed a revision newer than any client would accept, and the official SDK
#: refused to connect: it negotiates 2025-11-25 and rejected the newer number
#: offered back. Add a version here only after a real client has used it.
PREFERRED_VERSION = "2025-11-25"
SUPPORTED_VERSIONS = {"2025-11-25", "2025-06-18", "2025-03-26"}

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

TOPIC_SCHEME = "topic://"


def _ok(request_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _err(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _jsonable(value: Any) -> Any:
    if is_model_instance(value):
        return value.model_dump(mode="json")
    return value


class MCP:
    """Dispatch for one app's MCP endpoint."""

    __slots__ = ("app", "capabilities")

    def __init__(self, app: Any, capabilities: dict[str, Capability]) -> None:
        self.app = app
        self.capabilities = capabilities

    # ---- descriptions -----------------------------------------------------

    def tool_list(self) -> list[dict]:
        tools = []
        for capability in self.capabilities.values():
            described = capability.describe()
            model = capability.route.response_model
            if model is not None:
                # Declaring an output schema is a promise: a client may reject
                # a result that does not match it, so only routes that declare
                # a response model get one.
                described["outputSchema"] = model.model_json_schema(
                    ref_template="#/$defs/{model}"
                )
            tools.append(described)
        return tools

    def resource_list(self) -> list[dict]:
        resources = []
        for name, topic in self.app.topics.items():
            resources.append(
                {
                    "uri": f"{TOPIC_SCHEME}{name}",
                    "name": name,
                    "description": (
                        "Durable topic. Reading returns recent messages."
                        if topic.durable
                        else "In-memory topic. Live only; it keeps no history to read."
                    ),
                    "mimeType": "application/json",
                }
            )
        return resources

    async def read_resource(self, uri: str) -> list[dict]:
        if not uri.startswith(TOPIC_SCHEME):
            raise CapabilityError(f"unknown resource {uri!r}")
        name = uri[len(TOPIC_SCHEME):]
        topic = self.app.topics.get(name)
        if topic is None:
            raise CapabilityError(f"no topic named {name!r}")

        if topic.durable:
            history = await topic.history(count=50)
            body = {"topic": name, "messages": [value for _id, value in history]}
        else:
            body = {
                "topic": name,
                "messages": [],
                "note": "in-memory topic: live only, no history is retained",
                "subscribers": topic.subscribers,
            }
        return [
            {
                "uri": uri,
                "mimeType": "application/json",
                "text": json.dumps(body, default=str),
            }
        ]

    # ---- invocation -------------------------------------------------------

    async def call_tool(self, params: dict) -> dict:
        name = params.get("name")
        capability = self.capabilities.get(name)
        if capability is None:
            # A missing tool is a protocol-level error; a tool that fails while
            # running is not, and comes back as isError below.
            raise CapabilityError(f"no tool named {name!r}")

        try:
            result = await capability.invoke(params.get("arguments") or {})
        except Exception as exc:  # noqa: BLE001 - reported to the caller, not raised
            return {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True,
            }

        if isinstance(result, Response):
            body = result.encoded().decode("utf-8", "replace")
            return {"content": [{"type": "text", "text": body}], "isError": False}

        payload = _jsonable(result)
        content = {
            "content": [{"type": "text", "text": json.dumps(payload, default=str)}],
            "isError": False,
        }
        if capability.route.response_model is not None:
            content["structuredContent"] = payload
        return content

    # ---- protocol ---------------------------------------------------------

    async def dispatch(self, message: dict) -> dict | None:
        """Handle one JSON-RPC message. None means it was a notification."""
        if message.get("jsonrpc") != "2.0" or "method" not in message:
            return _err(message.get("id"), INVALID_REQUEST, "not a JSON-RPC 2.0 request")

        method = message["method"]
        request_id = message.get("id")
        params = message.get("params") or {}
        notification = request_id is None

        if method.startswith("notifications/"):
            return None

        try:
            if method == "initialize":
                asked = params.get("protocolVersion")
                result = {
                    "protocolVersion": asked if asked in SUPPORTED_VERSIONS else PREFERRED_VERSION,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"subscribe": False, "listChanged": False},
                    },
                    "serverInfo": {"name": self.app.title, "version": self.app.version},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self.tool_list()}
            elif method == "tools/call":
                result = await self.call_tool(params)
            elif method == "resources/list":
                result = {"resources": self.resource_list()}
            elif method == "resources/templates/list":
                result = {"resourceTemplates": []}
            elif method == "prompts/list":
                result = {"prompts": []}
            elif method == "resources/read":
                result = {"contents": await self.read_resource(params.get("uri", ""))}
            else:
                return None if notification else _err(
                    request_id, METHOD_NOT_FOUND, f"unknown method {method!r}"
                )
        except CapabilityError as exc:
            return None if notification else _err(request_id, INVALID_PARAMS, str(exc))
        except Exception as exc:  # noqa: BLE001 - must answer, not hang the client
            return None if notification else _err(request_id, INTERNAL_ERROR, str(exc))

        return None if notification else _ok(request_id, result)

    async def handle(self, body: bytes) -> Response:
        try:
            message = json.loads(body)
        except ValueError:
            return Response(
                json.dumps(_err(None, PARSE_ERROR, "invalid JSON")).encode(),
                status=400,
                content_type="application/json",
            )

        if isinstance(message, list):
            # Batching was removed from MCP in 2025-06-18. Say so rather than
            # half-supporting it.
            return Response(
                json.dumps(
                    _err(None, INVALID_REQUEST, "batched requests are not supported")
                ).encode(),
                status=400,
                content_type="application/json",
            )
        if not isinstance(message, dict):
            return Response(
                json.dumps(_err(None, INVALID_REQUEST, "expected a JSON object")).encode(),
                status=400,
                content_type="application/json",
            )

        reply = await self.dispatch(message)
        if reply is None:
            # A notification gets no body, only acknowledgement.
            return Response(b"", status=202, content_type="application/json")
        return Response(
            json.dumps(reply, default=str).encode(), content_type="application/json"
        )
