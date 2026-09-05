"""Raw ASGI app, no framework. Best case for uvicorn/granian."""
import json

BODY = json.dumps({"hello": "world"}).encode()
HEADERS = [(b"content-type", b"application/json")]


async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    await send({"type": "http.response.start", "status": 200, "headers": HEADERS})
    await send({"type": "http.response.body", "body": BODY})
