# Dependencies

A handler argument defaulted to `Depends(...)` is resolved before the handler
runs.

```python
from aether import Depends

async def get_db(request):
    db = await pool.acquire()
    try:
        yield db
    finally:
        await pool.release(db)          # runs after the handler, even on error

@app.get("/users")
async def list_users(_: Request, db = Depends(get_db)):
    return await db.fetch("select ...")
```

A dependency is any callable. It may take the request or take nothing, be sync
or async, be a plain function or a generator.

## Why this exists

Resolving a value could just as well be a function call at the top of the
handler. Releasing a resource could not. An async generator dependency gets its
teardown run after the handler returns, including when the handler raised, and
that guarantee is what a helper function called inside the handler cannot
provide.

Teardown runs in reverse order of setup.

## Caching

Results are cached per request, so a dependency shared by three others runs
once.

```python
async def current_user(session = Depends(get_session)):
    return await lookup(session["user_id"])

async def permissions(user = Depends(current_user)):
    return user.permissions

@app.get("/admin")
async def admin(_: Request, user = Depends(current_user), perms = Depends(permissions)):
    # current_user ran once, not twice
    ...
```

## Sub-dependencies

A dependency can declare dependencies of its own, to any depth, and they
resolve the same way.

## What dependencies are not

Dependencies are not parameters. An argument defaulted to `Depends` is left out
of the OpenAPI document and out of an MCP tool's argument schema, because it is
not something a client supplies.
