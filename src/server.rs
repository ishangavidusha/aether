use std::collections::HashMap;
use std::convert::Infallible;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::body::Incoming;
use hyper::header::CONTENT_TYPE;
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper::{Response, StatusCode};
use hyper_util::rt::TokioIo;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use tokio::net::TcpListener;
use tokio::sync::oneshot;

use crate::queue::Pending;
use crate::responder::Reply;
use crate::worker::Worker;

struct State {
    /// method -> path -> index into the shared route table. Two &str lookups,
    /// no per-request allocation.
    routes: HashMap<String, HashMap<String, usize>>,
    workers: Vec<Worker>,
    next_worker: AtomicUsize,
}

#[pyclass(name = "Server", module = "aether._core")]
pub struct Server {
    host: String,
    port: u16,
    worker_count: usize,
    routes: Vec<(String, String, Py<PyAny>)>,
}

#[pymethods]
impl Server {
    #[new]
    fn new(host: String, port: u16, workers: usize, routes: Vec<(String, String, Py<PyAny>)>) -> Self {
        Self {
            host,
            port,
            worker_count: workers.max(1),
            routes,
        }
    }

    /// Start workers, bind, and serve until Ctrl-C. Blocks the calling thread
    /// but detaches from the interpreter for the duration.
    fn serve(&self, py: Python<'_>) -> PyResult<()> {
        let handlers: Arc<Vec<Py<PyAny>>> =
            Arc::new(self.routes.iter().map(|(_, _, h)| h.clone_ref(py)).collect());

        let mut table: HashMap<String, HashMap<String, usize>> = HashMap::new();
        for (i, (method, path, _)) in self.routes.iter().enumerate() {
            table
                .entry(method.to_ascii_uppercase())
                .or_default()
                .insert(path.clone(), i);
        }

        let mut workers = Vec::with_capacity(self.worker_count);
        for i in 0..self.worker_count {
            workers.push(Worker::spawn(py, i, handlers.clone())?);
        }

        let state = Arc::new(State {
            routes: table,
            workers,
            next_worker: AtomicUsize::new(0),
        });

        let addr: SocketAddr = format!("{}:{}", self.host, self.port)
            .parse()
            .map_err(|e| PyRuntimeError::new_err(format!("bad address: {e}")))?;

        let runtime = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .map_err(|e| PyRuntimeError::new_err(format!("tokio runtime: {e}")))?;

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

fn plain(status: StatusCode, msg: &'static str) -> Response<Full<Bytes>> {
    Response::builder()
        .status(status)
        .header(CONTENT_TYPE, "text/plain")
        .body(Full::new(Bytes::from_static(msg.as_bytes())))
        .unwrap()
}

async fn handle(
    req: hyper::Request<Incoming>,
    state: Arc<State>,
) -> Result<Response<Full<Bytes>>, Infallible> {
    let route = state
        .routes
        .get(req.method().as_str())
        .and_then(|paths| paths.get(req.uri().path()))
        .copied();

    let Some(route) = route else {
        return Ok(plain(StatusCode::NOT_FOUND, "not found"));
    };

    let method = req.method().as_str().to_owned();
    let path = req.uri().path().to_owned();
    let query = req.uri().query().map(str::to_owned);
    let Ok(collected) = req.into_body().collect().await else {
        return Ok(plain(StatusCode::BAD_REQUEST, "bad body"));
    };

    let (reply_tx, reply_rx) = oneshot::channel::<Reply>();
    let idx = state.next_worker.fetch_add(1, Ordering::Relaxed) % state.workers.len();

    // No Python involvement on this thread: plain Rust data plus one byte
    // written to the worker's wake socket.
    state.workers[idx].queue.push(Pending {
        route,
        method,
        path,
        query,
        body: collected.to_bytes().to_vec(),
        reply: reply_tx,
    });

    match reply_rx.await {
        Ok(reply) => Ok(Response::builder()
            .status(reply.status)
            .header(CONTENT_TYPE, reply.content_type)
            .body(Full::new(Bytes::from(reply.body)))
            .unwrap()),
        Err(_) => Ok(plain(
            StatusCode::INTERNAL_SERVER_ERROR,
            "handler finished without responding",
        )),
    }
}
