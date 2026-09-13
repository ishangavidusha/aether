# Routers

A router holds routes declared in one module, to be mounted into an app in
another.

```python
# users.py
from aether import Request, Router

router = Router(prefix="/users")

@router.get("")
async def list_users(_: Request): ...

@router.get("/{user_id}")
async def get_user(_: Request, user_id: int): ...

@router.patch("/{user_id}")
async def update_user(_: Request, user_id: int): ...
```

```python
# main.py
from aether import App
import users

app = App()
app.include(users.router, prefix="/api/v1")
# GET /api/v1/users, GET /api/v1/users/{user_id}, PATCH /api/v1/users/{user_id}
```

A router has the same decorators as an app — `get`, `post`, `put`, `patch`,
`delete`, `route`, `websocket` — and `tool=True` works the same way.

## Prefixes

Prefixes and paths join literally. A prefix starts with `/` and does not end
with one.

| router prefix | decorator | served at |
|---|---|---|
| `/users` | `@router.get("")` | `/users` |
| `/users` | `@router.get("/")` | `/users/` |
| `/users` | `@router.get("/{user_id}")` | `/users/{user_id}` |

The first two are different routes. A trailing slash is significant everywhere
in Aether, and a router does not change that.

A prefix can declare path parameters. The handler accepts them like any other:

```python
tenant = Router(prefix="/tenants/{tenant}")

@tenant.get("/invoices")
async def invoices(_: Request, tenant: str): ...
```

## Nesting

Routers include routers. Prefixes accumulate.

```python
admin = Router(prefix="/admin")

@admin.get("/stats")
async def stats(_: Request): ...

users.include(admin)                     # /users/admin/stats
app.include(users, prefix="/api")        # /api/users/admin/stats
```

## Middleware

Middleware registered on a router runs only for that router's routes, and for
routers included into it. It runs inside the app's middleware.

```python
@admin.middleware
async def require_admin(request, call_next):
    if not is_admin(request):
        raise HTTPError(403)
    return await call_next(request)
```

Order, outermost first: app middleware, then each router's middleware from the
outermost router inwards, then the handler. An `HTTPError` raised by an inner
router's middleware reaches the app's middleware as a reply with its status, not
as an exception, so an access log records the status the client received.

## When mistakes are caught

A handler is checked when it is decorated, against the router's own path, and
again when the router is included, against the full path. A conflict between
two routers — the same method and the same route shape — raises at the second
`include`.

A router cannot change after it has been included. Its routes were copied into
the app at that moment, so a route added afterwards would never be served; adding
one raises `RuntimeError` instead. Include a router after everything is declared
on it.

Routers cost nothing per request: they are flattened into the app's route table
at `include`.

## Reaching the app from a router module

A module that declares a router usually cannot import the app without an import
cycle. Reach it through the request instead:

```python
@router.get("/feed")
async def feed(request: Request):
    return SSE(request.app.topic("orders").subscribe())

@router.get("/users")
async def users(request: Request):
    return await request.state.db.fetch("select ...")
```

`request.state` holds what the app's lifespans yielded. See
[Lifespan](lifespan.md).
