# OpenAPI

The schema is generated from the same route metadata the router uses, so it
cannot describe an endpoint the server would not accept.

```python
app = App(title="Notes", version="1.0.0", description="A notes service.")
```

Two routes are added for you:

- `GET /openapi.json` — the document
- `GET /docs` — a documentation page rendered from it

Pass `openapi_url=None` or `docs_url=None` to turn either off.

## Without a server

```python
document = app.openapi()
```

`app.openapi()` returns the document without starting anything, which makes it
usable for client generation in CI, or for a test that asserts the API did not
change accidentally.

## What ends up in it

- Path and query parameters, with their types, and whether they are required
- Request bodies from pydantic models, with nested models hoisted into
  `components/schemas`
- Response models, taken from the handler's return annotation
- The first line of the handler's docstring as the summary, the rest as the
  description
- The `422` shape, so a client knows what a validation failure looks like

Left out: WebSocket routes, because OpenAPI 3.1 has no vocabulary for them, and
dependency arguments, because a client does not supply those. The `/mcp`
endpoint is registered after the document is built, so the agent transport does
not describe itself as a REST endpoint.

## It is checked, not assumed

The test suite runs the generated document through
[`openapi-spec-validator`](https://pypi.org/project/openapi-spec-validator/), so
"valid OpenAPI 3.1" is a checked result rather than a reading of the
specification. The same distinction applies to the [agent
interface](agents.md), which is verified against the official MCP client.
