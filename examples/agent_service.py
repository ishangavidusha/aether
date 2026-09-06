"""One service, three audiences: curl, an OpenAPI client, and an agent.

Nothing here is declared twice. The type annotations and docstrings that make
the router work are the same ones that produce the OpenAPI document and the
MCP tool definitions.

    python examples/agent_service.py

    curl 127.0.0.1:8000/notes/1                      # plain HTTP
    curl 127.0.0.1:8000/openapi.json                 # OpenAPI 3.1
    curl -X POST 127.0.0.1:8000/mcp \\
      -H 'content-type: application/json' \\
      -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'   # agents

Point an MCP client at http://127.0.0.1:8000/mcp and it sees three tools with
typed arguments, descriptions taken from these docstrings, and hints about
which ones are safe to call.
"""

from pydantic import BaseModel, Field

from aether import App, Request

app = App(
    title="Notes",
    version="1.0.0",
    description="A tiny note service that agents can also use.",
)

NOTES: dict[int, dict] = {1: {"id": 1, "title": "first", "body": "hello"}}


class NoteIn(BaseModel):
    title: str = Field(min_length=1, max_length=80, description="Short label.")
    body: str = Field(min_length=1, description="The note's contents.")


class Note(BaseModel):
    id: int
    title: str
    body: str


# `tool=True` is what exposes a route to agents. Without it a route is still a
# perfectly good HTTP endpoint, it is simply not offered to an agent, which is
# why the delete-everything endpoint below is left off.
@app.get("/notes/{note_id}", tool=True)
async def read_note(_: Request, note_id: int) -> Note:
    """Read one note by its id.

    Raises if the note does not exist, which an agent sees as a tool error
    rather than a broken connection.
    """
    return Note(**NOTES[note_id])


@app.get("/notes", tool=True)
async def search_notes(_: Request, contains: str = "", limit: int = 10):
    """Search notes by substring.

    `contains` matches the title or the body. `limit` caps the result count.
    """
    hits = [
        n for n in NOTES.values()
        if contains.lower() in n["title"].lower() or contains.lower() in n["body"].lower()
    ]
    return {"count": len(hits), "notes": hits[:limit]}


@app.post("/notes", tool=True)
async def write_note(_: Request, body: NoteIn) -> Note:
    """Create a note.

    The fields of NoteIn appear as flat arguments to an agent, so it calls
    write_note(title=..., body=...) rather than nesting an object.
    """
    new_id = max(NOTES) + 1 if NOTES else 1
    NOTES[new_id] = {"id": new_id, **body.model_dump()}
    return Note(**NOTES[new_id])


@app.delete("/admin/everything")
async def wipe(_: Request):
    """Deliberately NOT a tool. Reachable by HTTP, invisible to agents."""
    NOTES.clear()
    return {"wiped": True}


if __name__ == "__main__":
    app.run(port=8000)
