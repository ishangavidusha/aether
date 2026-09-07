# Query parameters

Any handler argument that is not a path parameter and not a pydantic model is a
query parameter, coerced in Rust alongside path parameters.

```python
@app.get("/search")
async def search(_: Request, q: str, limit: int = 10, cursor: str | None = None):
    return {"q": q, "limit": limit, "cursor": cursor}
```

- **No default means required.** A missing one is a `422`, produced before
  Python is woken.
- **A default makes it optional**, and the handler's own default applies.
- **`str | None` without a default** is optional and arrives as `None`.

## Types

The same set as path parameters: `str`, `int`, `float`, `bool`, `uuid.UUID`,
`datetime.date`, `datetime.datetime`.

Booleans accept `true`/`false`, `1`/`0`, `yes`/`no` and `on`/`off`, in any case.

```python
@app.get("/reports")
async def reports(_: Request, since: datetime.date, verbose: bool = False):
    ...
```

```
GET /reports?since=2026-01-31&verbose=yes   ->  200
GET /reports?since=2026-02-30               ->  422  no such day
GET /reports                                ->  422  missing required parameter
```

## Repeated keys

A parameter annotated `list[T]` collects every occurrence of the key. Without
the annotation only the first is taken, which is what a scalar parameter means.

```python
@app.get("/items")
async def items(_: Request, tag: list[str], id: list[int] = []):
    return {"tags": tag, "ids": id}
```

```
GET /items?tag=red&tag=blue&id=1&id=2   ->  {"tags": ["red", "blue"], "ids": [1, 2]}
GET /items?tag=solo                     ->  {"tags": ["solo"], "ids": []}
GET /items                              ->  422  tag is required
GET /items?tag=red&id=x                 ->  422  one bad element fails the request
```

The same rule as any other parameter: no default means required, so a list
without one needs at least one occurrence. Each element is coerced
individually.

## Cost

A route that declares no query parameters skips query-string parsing
altogether, so it measures the same as a route with no parameters at all.

| target | req/s |
|---|---:|
| `/` | 184,861 |
| `/users/{user_id}` | 184,697 |
| `/search?q=..&limit=..` | 181,478 |
| granian + FastAPI, query route | 19,478 |
| uvicorn + FastAPI, query route | 9,851 |
