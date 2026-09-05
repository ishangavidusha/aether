from aether import App, Request

app = App()


@app.get("/")
async def hello(_: Request):
    return {"hello": "world"}


@app.post("/echo")
async def echo(req: Request):
    return {"path": req.path, "query": req.query, "body": req.body.decode()}


if __name__ == "__main__":
    app.run(port=8000)
