use std::convert::Infallible;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use bytes::Bytes;
use http_body_util::combinators::BoxBody;
use http_body_util::{BodyExt, Full, StreamBody};
use hyper::body::{Frame, Incoming};
use hyper::header::{ALLOW, CONTENT_TYPE, RETRY_AFTER};
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper::{Response, StatusCode};
use hyper_util::rt::TokioIo;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use tokio::net::TcpListener;
use tokio::sync::oneshot;
use tokio_stream::wrappers::ReceiverStream;
use tokio_stream::StreamExt;

use crate::queue::Pending;
use crate::responder::{Body, Reply};
use crate::router::{RouteError, Router, SpecTuple};
use crate::worker::Worker;

struct State {
    router: Arc<Router>,
    workers: Vec<Worker>,
    next_worker: AtomicUsize,
}

#[pyclass(name = "Server", module = "aether._core")]
pub struct Server {
    host: String,
    port: u16,
    worker_count: usize,
    max_concurrency: usize,
    routes: Vec<Route>,
}

/// (method, path, handler, [(name, type, source, presence)])
type Route = (String, String, Py<PyAny>, Vec<SpecTuple>);

#[pymethods]
impl Server {
    #[new]
    fn new(
        host: String,
        port: u16,
        workers: usize,
        max_concurrency: usize,
        routes: Vec<Route>,
    ) -> Self {
        Self {
            host,
            port,
            worker_count: workers.max(1),
            max_concurrency: max_concurrency.max(1),
            routes,
        }
    }

    /// Start workers, bind, and serve until Ctrl-C. Blocks the calling thread
    /// but detaches from the interpreter for the duration.
    fn serve(&self, py: Python<'_>) -> PyResult<()> {
        let handlers: Arc<Vec<Py<PyAny>>> =
            Arc::new(self.routes.iter().map(|(_, _, h, _)| h.clone_ref(py)).collect());

        let specs: Vec<(String, String, Vec<SpecTuple>)> = self
            .routes
            .iter()
            .map(|(method, path, _, params)| (method.clone(), path.clone(), params.clone()))
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
                router.clone(),
                self.max_concurrency,
                runtime.handle().clone(),
            )?);
        }

        let state = Arc::new(State {
            router,
            workers,
            next_worker: AtomicUsize::new(0),
        });

        let addr: SocketAddr = format!("{}:{}", self.host, self.port)
            .parse()
            .map_err(|e| PyRuntimeError::new_err(format!("bad address: {e}")))?;

        let shutdown = state.clone();
        let result: Result<(), String> = py.detach(|| runtime.block_on(serve_loop(addr, state)));

        for w in &shutdown.workers {
            w.stop(py);
        }
        drop(runtime);

        result.map_err(PyRuntimeError::new_err)
    }
}

async fn serve_loop(addr: SocketAddr, state: Arc<State>) -> Result<(), String> {
    let listener = TcpListener::bind(addr)
        .await
        .map_err(|e| format!("bind {addr}: {e}"))?;
    println!("Aether listening on http://{addr}");

    loop {
        tokio::select! {
            accepted = listener.accept() => {
                let Ok((stream, _)) = accepted else { continue };
                let _ = stream.set_nodelay(true);
                let state = state.clone();
                tokio::spawn(async move {
                    let io = TokioIo::new(stream);
                    let svc = service_fn(move |req| handle(req, state.clone()));
                    let _ = http1::Builder::new().serve_connection(io, svc).await;
                });
            }
            _ = tokio::signal::ctrl_c() => break,
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

fn method_not_allowed(allow: String) -> Response<Out> {
    Response::builder()
        .status(StatusCode::METHOD_NOT_ALLOWED)
        .header(CONTENT_TYPE, "text/plain")
        .header(ALLOW, allow)
        .body(full(Bytes::from_static(b"method not allowed")))
        .unwrap()
}

async fn handle(
    req: hyper::Request<Incoming>,
    state: Arc<State>,
) -> Result<Response<Out>, Infallible> {
    let matched = match state
        .router
        .find(req.method().as_str(), req.uri().path(), req.uri().query())
    {
        Ok(matched) => matched,
        Err(RouteError::NotFound) => return Ok(plain(StatusCode::NOT_FOUND, "not found")),
        Err(RouteError::MethodNotAllowed(allow)) => return Ok(method_not_allowed(allow)),
        // Coercion runs here, so a bad path parameter never wakes a worker.
        Err(RouteError::BadParam(err)) => {
            return Ok(json(StatusCode::UNPROCESSABLE_ENTITY, err.to_json()))
        }
    };

    let method = req.method().as_str().to_owned();
    let path = req.uri().path().to_owned();
    let query = req.uri().query().map(str::to_owned);
    let Ok(collected) = req.into_body().collect().await else {
        return Ok(plain(StatusCode::BAD_REQUEST, "bad body"));
    };

    let (reply_tx, reply_rx) = oneshot::channel::<Reply>();
    let mut pending = Pending {
        route: matched.route,
        params: matched.params,
        method,
        path,
        query,
        body: collected.to_bytes().to_vec(),
        reply: reply_tx,
    };

    // No Python involvement on this thread: plain Rust data plus one byte
    // written to the worker's wake socket. Start round-robin, but fall through
    // to any worker with room, so one slow handler cannot stall its share of
    // traffic while other loops sit idle.
    let start = state.next_worker.fetch_add(1, Ordering::Relaxed);
    let count = state.workers.len();
    let mut queued = false;
    for offset in 0..count {
        let idx = (start + offset) % count;
        match state.workers[idx].queue.try_push(pending) {
            Ok(()) => {
                queued = true;
                break;
            }
            Err(returned) => pending = returned,
        }
    }
    if !queued {
        // Every queue is full. Shed the request now rather than let it wait
        // behind work the server has already failed to keep up with.
        return Ok(overloaded());
    }

    match reply_rx.await {
        Ok(reply) => {
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
