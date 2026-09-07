# Agents

The same handler that serves HTTP can be a capability an agent calls, over the
[Model Context Protocol](https://modelcontextprotocol.io).

```python
@app.get("/notes/{note_id}", tool=True)
async def read_note(_: Request, note_id: int) -> Note:
    """Read one note by its id."""
    return Note(**NOTES[note_id])
```

Point an MCP client at `/mcp` and it sees a tool named `read_note`, with a
typed `note_id` argument, that docstring as its description, `Note` as its
output schema, and a read-only hint inferred from the fact that it is a `GET`.

**Nothing is declared twice.** The name, description, argument schema and
output schema all come from the handler that already exists. This is the same
`RouteInfo` the router and the OpenAPI generator use, which is why the three
cannot drift apart.

## Opt-in, deliberately

A route without `tool=True` is still a perfectly good endpoint. It simply is
not offered to agents.

!!! danger "Do not make this a default"

    Every route being agent-callable by default would mean an administrative
    delete endpoint is agent-callable by default. Opting a route in is a
    decision someone makes about that route.

## Arguments

Body fields are flattened into the argument list, so an agent calls
`write_note(title=..., body=...)` rather than nesting an object whose shape it
has to infer.

```python
@app.post("/notes", tool=True)
async def write_note(_: Request, body: NoteIn) -> Note:
    """Create a note."""
```

A body field that collides with a path or query parameter is an error at import
time, not a silent overwrite at call time.

## Topics as resources

Topics show up as readable resources at `topic://<name>`. A durable one returns
recent messages.

## Inspecting without serving

```python
capabilities = app.capabilities()
```

Built from the routes marked `tool=True` without starting a server, so it can
be asserted in a test — a useful thing to pin, since the set of capabilities is
the surface an agent is allowed to reach.

## Transport

The simple half of the specification: a `POST` carrying one JSON-RPC message,
answered with JSON.

Not implemented: streaming responses, the server-to-client `GET` channel, and
resource subscriptions. The router returns `405` for those, which is what the
spec asks for.

Turn the endpoint off entirely with `App(mcp_url=None)`.

## Verified against a real client

`tests/capabilities.py` drives the official MCP SDK client against a running
server.

A constant exported by an SDK is not the same thing as a version a client will
negotiate, and nothing short of a real handshake distinguishes the two. The same
test asserts the wire format separately: the SDK exposes `snake_case` names
while the protocol on the wire is `camelCase`, so testing through the SDK alone
would not detect a serialization error.

`examples/agent_service.py` serves one set of declarations to curl, to an
OpenAPI client and to an agent.
