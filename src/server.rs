use std::convert::Infallible;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use bytes::Bytes;
use http_body_util::combinators::BoxBody;
use http_body_util::{BodyExt, Full, LengthLimitError, Limited, StreamBody};
use hyper::body::{Frame, Incoming};
use hyper::header::{
    ALLOW, CONNECTION, CONTENT_LENGTH, CONTENT_TYPE, RETRY_AFTER, SEC_WEBSOCKET_ACCEPT,
    SEC_WEBSOCKET_KEY, SEC_WEBSOCKET_VERSION, UPGRADE,
};
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper::{Method, Response, StatusCode};
use hyper_util::rt::{TokioIo, TokioTimer};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use tokio::net::TcpListener;
use tokio::sync::{oneshot, Notify};
use tokio_stream::wrappers::ReceiverStream;
use tokio_stream::StreamExt;

use crate::queue::Pending;
use crate::responder::{Body, Reply};
use crate::router::{RouteError, RouteTuple, Router, SpecTuple};
use crate::websocket;
use crate::worker::Worker;

struct State {
    router: Arc<Router>,
    workers: Vec<Worker>,
    next_worker: AtomicUsize,
    request_timeout: Option<Duration>,
    /// Cap on a request body. Without one, a single request can grow the
    /// process by several times the payload before the handler ever sees it.
    max_body: usize,
}

#[pyclass(name = "Server", module = "aether._core")]
pub struct Server {
    host: String,
    port: u16,
    worker_count: usize,
    max_concurrency: usize,
    max_body: usize,
    debug: bool,
    request_timeout: Option<Duration>,
    shutdown_grace: Duration,
    /// Lets something other than Ctrl-C stop the server. `serve` blocks, so a
    /// test harness needs a way in from another thread.
    stop: Arc<Notify>,
    max_connections: usize,
    quiet: bool,
    routes: Vec<Route>,
}

/// (method, path, handler, params, is_websocket, authorizer)
type Route = (String, String, Py<PyAny>, Vec<SpecTuple>, bool, Option<Py<PyAny>>);

#[pymethods]
impl Server {
    #[new]
    fn new(
        host: String,
        port: u16,
        workers: usize,
        max_concurrency: usize,
        max_body: usize,
        debug: bool,
        request_timeout_secs: f64,
        shutdown_grace_secs: f64,
        max_connections: usize,
        quiet: bool,
        routes: Vec<Route>,
    ) -> Self {
        Self {
            host,
            port,
            worker_count: workers.max(1),
            max_concurrency: max_concurrency.max(1),
            max_body,
            debug,
            // Zero disables the timeout, for a service whose handlers are
            // legitimately long-running.
            request_timeout: (request_timeout_secs > 0.0)
                .then(|| Duration::from_secs_f64(request_timeout_secs)),
            shutdown_grace: Duration::from_secs_f64(shutdown_grace_secs.max(0.0)),
            stop: Arc::new(Notify::new()),
            max_connections: max_connections.max(1),
            quiet,
            routes,
        }
    }

    /// Ask a running server to stop accepting and drain. Safe to call from
    /// another thread, which is the point: `serve` blocks the one it is on.
    fn shutdown(&self) {
        self.stop.notify_waiters();
    }

    /// Start workers, bind, and serve until Ctrl-C or `shutdown`. Blocks the
    /// calling thread but detaches from the interpreter for the duration.
    fn serve(&self, py: Python<'_>) -> PyResult<()> {
        let handlers: Arc<Vec<Py<PyAny>>> = Arc::new(
            self.routes
                .iter()
                .map(|(_, _, handler, _, _, _)| handler.clone_ref(py))
                .collect(),
        );

        // Parallel to `handlers`: the optional pre-accept check for a socket.
        let gates: Arc<Vec<Option<Py<PyAny>>>> = Arc::new(
            self.routes
                .iter()
                .map(|(_, _, _, _, _, gate)| gate.as_ref().map(|g| g.clone_ref(py)))
                .collect(),
        );

        let specs: Vec<RouteTuple> = self
            .routes
            .iter()
            .map(|(method, path, _, params, websocket, gate)| {
                (
                    method.clone(),
                    path.clone(),
                    params.clone(),
                    *websocket,
                    gate.is_some(),
                )
            })
            .collect();
        let router = Arc::new(Router::build(&specs).map_err(PyValueError::new_err)?);

        // Built before the workers, because each Responder needs a handle to
        // spawn its disconnect watcher on.
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .map_err(|e| PyRuntimeError::new_err(format!("tokio runtime: {e}")))?;

        let mut workers = Vec::with_capacity(self.worker_count);
        for i in 0..self.worker_count {
            workers.push(Worker::spawn(
                py,
                i,
                handlers.clone(),
                gates.clone(),
                router.clone(),
                self.max_concurrency,
                self.debug,
                runtime.handle().clone(),
            )?);
        }

        let state = Arc::new(State {
            router,
            workers,
            next_worker: AtomicUsize::new(0),
            request_timeout: self.request_timeout,
            max_body: self.max_body,
        });

        let addr: SocketAddr = format!("{}:{}", self.host, self.port)
            .parse()
            .map_err(|e| PyRuntimeError::new_err(format!("bad address: {e}")))?;

        let shutdown = state.clone();
        let stop = self.stop.clone();
        let quiet = self.quiet;
        let slots = self.max_connections;
        let result: Result<(), String> =
            py.detach(|| runtime.block_on(serve_loop(addr, state, stop, slots, quiet)));

        // Draining. The listener has stopped, but connection tasks are still
        // on the runtime and handlers are still on the worker loops, so wait
        // for them rather than dropping their clients mid-request.
        let grace = self.shutdown_grace;
        let drained = py.detach(|| {
            let deadline = Instant::now() + grace;
            loop {
                let busy: usize = shutdown.workers.iter().map(|w| w.queue.load()).sum();
                if busy == 0 {
                    return true;
                }
                if Instant::now() >= deadline {
                    return false;
                }
                std::thread::sleep(Duration::from_millis(25));
            }
        });
        if !drained {
            let busy: usize = shutdown.workers.iter().map(|w| w.queue.load()).sum();
            eprintln!("aether: shutdown grace expired with {busy} request(s) still in flight");
        }

        for w in &shutdown.workers {
            w.stop(py);
        }
        drop(runtime);

        result.map_err(PyRuntimeError::new_err)
    }
}

async fn serve_loop(
    addr: SocketAddr,
    state: Arc<State>,
    stop: Arc<Notify>,
    max_connections: usize,
    quiet: bool,
) -> Result<(), String> {
    let listener = TcpListener::bind(addr)
        .await
        .map_err(|e| format!("bind {addr}: {e}"))?;
    if !quiet {
        println!("Aether listening on http://{addr}");
    }

    // `max_concurrency` bounds requests handed to a worker, which is not the
    // same as sockets held open. An idle keep-alive connection costs a file
    // descriptor and buffers without ever reaching a worker, so it needs its
    // own limit.
    let connections = Arc::new(tokio::sync::Semaphore::new(max_connections));

    loop {
        // Taken before accepting, so at the limit the listener simply stops
        // accepting and the OS backlog absorbs the wait. That is the shape of
        // backpressure a client understands.
        let permit = tokio::select! {
            slot = connections.clone().acquire_owned() => match slot {
                Ok(permit) => permit,
                Err(_) => break,
            },
            _ = stop.notified() => break,
        };

        tokio::select! {
            accepted = listener.accept() => {
                let Ok((stream, _)) = accepted else { continue };
                let _ = stream.set_nodelay(true);
                let state = state.clone();
                tokio::spawn(async move {
                    // Released when the connection task ends.
                    let _permit = permit;
                    let io = TokioIo::new(stream);
                    let svc = service_fn(move |req| handle(req, state.clone()));
                    // `with_upgrades` is required for 101 responses to hand
                    // the connection over instead of closing it.
                    //
                    // The header timeout is what stops a client from opening a
                    // connection and dribbling request headers forever.
                    let _ = http1::Builder::new()
                        // hyper panics on a timeout with no timer wired in,
                        // so this line is load-bearing, not decorative.
                        .timer(TokioTimer::new())
                        .header_read_timeout(Some(Duration::from_secs(15)))
                        .serve_connection(io, svc)
                        .with_upgrades()
                        .await;
                });
            }
            _ = tokio::signal::ctrl_c() => break,
            _ = stop.notified() => break,
        }
    }
    Ok(())
}

/// Responses are either a complete buffer or a stream of chunks, so every
/// helper hands back the same boxed body type.
type Out = BoxBody<Bytes, Infallible>;

fn full(bytes: Bytes) -> Out {
    Full::new(bytes).boxed()
}

fn plain(status: StatusCode, msg: &'static str) -> Response<Out> {
    Response::builder()
        .status(status)
        .header(CONTENT_TYPE, "text/plain")
        .body(full(Bytes::from_static(msg.as_bytes())))
        .unwrap()
}

fn json(status: StatusCode, body: Vec<u8>) -> Response<Out> {
    Response::builder()
        .status(status)
        .header(CONTENT_TYPE, "application/json")
        .body(full(Bytes::from(body)))
        .unwrap()
}

fn overloaded() -> Response<Out> {
    Response::builder()
        .status(StatusCode::SERVICE_UNAVAILABLE)
        .header(CONTENT_TYPE, "text/plain")
        .header(RETRY_AFTER, "1")
        .body(full(Bytes::from_static(b"server overloaded")))
        .unwrap()
}

/// Path parameters are consumed by whichever Pending takes them, so the gate
/// needs its own copy before the handler's.
fn clone_param(value: &crate::router::ParamValue) -> crate::router::ParamValue {
    use crate::router::ParamValue as V;
    match value {
        V::Str(v) => V::Str(v.clone()),
        V::Int(v) => V::Int(*v),
        V::Float(v) => V::Float(*v),
        V::Bool(v) => V::Bool(*v),
        V::Uuid(v) => V::Uuid(v.clone()),
        V::Date(v) => V::Date(v.clone()),
        V::DateTime(v) => V::DateTime(v.clone()),
        V::List(items) => V::List(items.iter().map(clone_param).collect()),
        V::Null => V::Null,
        V::Omit => V::Omit,
    }
}

fn too_large() -> Response<Out> {
    Response::builder()
        .status(StatusCode::PAYLOAD_TOO_LARGE)
        .header(CONTENT_TYPE, "text/plain")
        .body(full(Bytes::from_static(b"request body too large")))
        .unwrap()
}

fn method_not_allowed(allow: String) -> Response<Out> {
    Response::builder()
        .status(StatusCode::METHOD_NOT_ALLOWED)
        .header(CONTENT_TYPE, "text/plain")
        .header(ALLOW, allow)
        .body(full(Bytes::from_static(b"method not allowed")))
        .unwrap()
}

/// Find a worker with room and hand it the request. False means every worker
/// is at its limit.
fn enqueue(state: &State, pending: Pending) -> bool {
    let mut pending = pending;
    let start = state.next_worker.fetch_add(1, Ordering::Relaxed);
    let count = state.workers.len();
    for offset in 0..count {
        let idx = (start + offset) % count;
        match state.workers[idx].queue.try_push(pending) {
            Ok(()) => return true,
            Err(returned) => pending = returned,
        }
    }
    false
}

/// Complete a WebSocket handshake and hand the socket to a handler.
///
/// The 101 goes out from here rather than from the handler, because hyper only
/// yields the upgraded connection after the response has been written.
async fn upgrade_websocket(
    mut req: hyper::Request<Incoming>,
    matched: crate::router::Matched,
    state: Arc<State>,
) -> Response<Out> {
    let headers = req.headers();
    let upgrading = headers
        .get(UPGRADE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.eq_ignore_ascii_case("websocket"))
        && headers
            .get(CONNECTION)
            .and_then(|v| v.to_str().ok())
            .is_some_and(|v| v.to_ascii_lowercase().contains("upgrade"));

    let version_ok = headers
        .get(SEC_WEBSOCKET_VERSION)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.trim() == "13");

    let key = headers.get(SEC_WEBSOCKET_KEY).cloned();

    let (Some(key), true, true) = (key, upgrading, version_ok) else {
        // A plain GET to a socket route is a client mistake worth naming.
        return Response::builder()
            .status(StatusCode::UPGRADE_REQUIRED)
            .header(CONTENT_TYPE, "text/plain")
            .header(SEC_WEBSOCKET_VERSION, "13")
            .body(full(Bytes::from_static(
                b"this endpoint speaks websocket; send an Upgrade request",
            )))
            .unwrap();
    };

    // Ask the application before switching protocols. Once the 101 is sent it
    // is too late to refuse, which is why this cannot be left to the handler.
    if state.router.spec(matched.route).gated {
        let (verdict_tx, verdict_rx) = oneshot::channel::<Reply>();
        let queued = enqueue(
            &state,
            Pending {
                route: matched.route,
                params: matched.params.iter().map(clone_param).collect(),
                method: req.method().as_str().to_owned(),
                path: req.uri().path().to_owned(),
                query: req.uri().query().map(str::to_owned),
                body: Vec::new(),
                headers: req.headers().clone(),
                reply: verdict_tx,
                websocket: None,
                gate: true,
            },
        );
        if !queued {
            return overloaded();
        }

        let verdict = match state.request_timeout {
            Some(limit) => match tokio::time::timeout(limit, verdict_rx).await {
                Ok(result) => result,
                Err(_) => return plain(StatusCode::GATEWAY_TIMEOUT, "authorizer timed out"),
            },
            None => verdict_rx.await,
        };

        match verdict {
            // 101 from the authorizer means "go ahead"; the real handshake
            // response is built below.
            Ok(reply) if reply.status == 101 => {}
            Ok(reply) => {
                let body = match reply.body {
                    Body::Full(bytes) => Bytes::from(bytes),
                    Body::Stream(..) => Bytes::new(),
                };
                let mut builder = Response::builder()
                    .status(reply.status)
                    .header(CONTENT_TYPE, reply.content_type);
                for (name, value) in &reply.headers {
                    builder = builder.header(name.as_str(), value.as_str());
                }
                return builder
                    .body(full(body))
                    .unwrap_or_else(|_| plain(StatusCode::FORBIDDEN, "refused"));
            }
            Err(_) => {
                return plain(StatusCode::INTERNAL_SERVER_ERROR, "authorizer did not answer")
            }
        }
    }

    let accept = tokio_tungstenite::tungstenite::handshake::derive_accept_key(key.as_bytes());
    let (shared, outgoing) = websocket::Shared::new();

    // The reply channel goes nowhere: the response is built here, and the
    // Responder exists only so the in-flight count is released on drop.
    let (reply_tx, reply_rx) = oneshot::channel();
    drop(reply_rx);

    let queued = enqueue(
        &state,
        Pending {
            route: matched.route,
            params: matched.params,
            method: req.method().as_str().to_owned(),
            path: req.uri().path().to_owned(),
            query: req.uri().query().map(str::to_owned),
            body: Vec::new(),
            headers: req.headers().clone(),
            reply: reply_tx,
            websocket: Some(shared.clone()),
            gate: false,
        },
    );
    if !queued {
        return overloaded();
    }

    let upgrade = hyper::upgrade::on(&mut req);
    tokio::spawn(async move {
        match upgrade.await {
            Ok(upgraded) => {
                websocket::serve(TokioIo::new(upgraded), shared, outgoing).await;
            }
            // The handler is already running; tell it the socket never opened.
            Err(_) => shared.mark_closed(),
        }
    });

    Response::builder()
        .status(StatusCode::SWITCHING_PROTOCOLS)
        .header(UPGRADE, "websocket")
        .header(CONNECTION, "Upgrade")
        .header(SEC_WEBSOCKET_ACCEPT, accept)
        .body(full(Bytes::new()))
        .unwrap()
}

async fn handle(
    mut req: hyper::Request<Incoming>,
    state: Arc<State>,
) -> Result<Response<Out>, Infallible> {
    // HTTP requires HEAD wherever GET is allowed, so a miss on HEAD retries as
    // GET and the body is dropped from the reply below.
    let head = req.method() == Method::HEAD;
    let found = state
        .router
        .find(req.method().as_str(), req.uri().path(), req.uri().query());
    let found = match found {
        Err(RouteError::NotFound) | Err(RouteError::MethodNotAllowed(_)) if head => state
            .router
            .find("GET", req.uri().path(), req.uri().query())
            // A HEAD cannot open a socket, so leave upgrade routes to fail.
            .and_then(|m| {
                if state.router.spec(m.route).websocket {
                    Err(RouteError::MethodNotAllowed("GET".to_owned()))
                } else {
                    Ok(m)
                }
            })
            .or(found),
        other => other,
    };

    let matched = match found {
        Ok(matched) => matched,
        Err(RouteError::NotFound) => return Ok(plain(StatusCode::NOT_FOUND, "not found")),
        Err(RouteError::MethodNotAllowed(allow)) => return Ok(method_not_allowed(allow)),
        // Coercion runs here, so a bad path parameter never wakes a worker.
        Err(RouteError::BadParam(err)) => {
            return Ok(json(StatusCode::UNPROCESSABLE_ENTITY, err.to_json()))
        }
    };

    if state.router.spec(matched.route).websocket {
        return Ok(upgrade_websocket(req, matched, state).await);
    }

    let method = req.method().as_str().to_owned();
    let path = req.uri().path().to_owned();
    let query = req.uri().query().map(str::to_owned);
    // Moved, not copied: handing the whole map over costs nothing, and a
    // handler that never reads a header never pays to convert one.
    let headers = std::mem::take(req.headers_mut());
    let collected = match Limited::new(req.into_body(), state.max_body).collect().await {
        Ok(collected) => collected,
        Err(err) if err.downcast_ref::<LengthLimitError>().is_some() => {
            return Ok(too_large())
        }
        Err(_) => return Ok(plain(StatusCode::BAD_REQUEST, "bad body")),
    };

    let (reply_tx, reply_rx) = oneshot::channel::<Reply>();
    let pending = Pending {
        route: matched.route,
        params: matched.params,
        method,
        path,
        query,
        body: collected.to_bytes().to_vec(),
        headers,
        reply: reply_tx,
        websocket: None,
        gate: false,
    };

    // No Python involvement on this thread: plain Rust data plus one byte
    // written to the worker's wake socket.
    if !enqueue(&state, pending) {
        // Every worker is at its limit. Shed the request now rather than let it
        // wait behind work the server has already failed to keep up with.
        return Ok(overloaded());
    }

    // Waiting only for the *first* reply, so a long-lived SSE stream is not
    // affected: its headers go out as soon as the handler starts streaming.
    let replied = match state.request_timeout {
        Some(limit) => match tokio::time::timeout(limit, reply_rx).await {
            Ok(result) => result,
            Err(_) => {
                return Ok(plain(
                    StatusCode::GATEWAY_TIMEOUT,
                    "handler did not respond in time",
                ))
            }
        },
        None => reply_rx.await,
    };

    match replied {
        Ok(reply) => {
            // A HEAD reply carries the headers a GET would, including the
            // length it would have had, but no body.
            if head {
                let length = match &reply.body {
                    Body::Full(bytes) => bytes.len(),
                    Body::Stream(..) => 0,
                };
                let mut builder = Response::builder()
                    .status(reply.status)
                    .header(CONTENT_TYPE, reply.content_type)
                    .header(CONTENT_LENGTH, length);
                for (name, value) in &reply.headers {
                    builder = builder.header(name.as_str(), value.as_str());
                }
                return Ok(builder.body(full(Bytes::new())).unwrap_or_else(|_| {
                    plain(StatusCode::INTERNAL_SERVER_ERROR, "bad response header")
                }));
            }

            let body = match reply.body {
                Body::Full(bytes) => full(Bytes::from(bytes)),
                // Headers go out now; chunks follow as the handler produces
                // them, which is what makes SSE possible. `guard` is moved into
                // the closure so it lives exactly as long as the body, and its
                // drop is what tells the handler the client has gone.
                Body::Stream(rx, guard) => StreamBody::new(ReceiverStream::new(rx).map(
                    move |chunk| {
                        let _keep_alive = &guard;
                        Ok::<_, Infallible>(Frame::data(chunk))
                    },
                ))
                .boxed(),
            };
            let mut builder = Response::builder()
                .status(reply.status)
                .header(CONTENT_TYPE, reply.content_type);
            for (name, value) in &reply.headers {
                builder = builder.header(name.as_str(), value.as_str());
            }
            // A handler-supplied header could be malformed; fall back rather
            // than kill the connection.
            Ok(builder
                .body(body)
                .unwrap_or_else(|_| plain(StatusCode::INTERNAL_SERVER_ERROR, "bad response header")))
        }
        Err(_) => Ok(plain(
            StatusCode::INTERNAL_SERVER_ERROR,
            "handler finished without responding",
        )),
    }
}
