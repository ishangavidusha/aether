use std::io::Read;
use std::os::fd::AsRawFd;
use std::os::unix::net::UnixStream;
use std::sync::mpsc;
use std::sync::Arc;
use std::thread;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use crate::queue::WorkerQueue;
use crate::request::Request;
use crate::responder::Responder;
use crate::router::{ParamValue, Router};
use crate::websocket::WebSocket;

/// Callable handed to `loop.add_reader`. asyncio invokes it on the worker's own
/// thread whenever the wake socket becomes readable, and it drains every queued
/// request in that one callback.
#[pyclass(frozen, name = "Drainer", module = "aether._core")]
struct Drainer {
    queue: Arc<WorkerQueue>,
    routes: Arc<Vec<Py<PyAny>>>,
    gates: Arc<Vec<Option<Py<PyAny>>>>,
    router: Arc<Router>,
    /// `aether._runtime.run_handler`, an async function.
    run_handler: Py<PyAny>,
    /// `aether._runtime.run_websocket`, for upgraded connections.
    run_websocket: Py<PyAny>,
    /// Constructors for parameter types Rust validated but cannot build.
    make_uuid: Py<PyAny>,
    make_date: Py<PyAny>,
    make_datetime: Py<PyAny>,
    /// Bound `loop.create_task`.
    create_task: Py<PyAny>,
    /// Returned to the client in a 500 when set. Per server, never global.
    debug: bool,
    /// Read end of the wake socketpair.
    reader: UnixStream,
    /// Handed to each `Responder` so streams can watch for disconnects.
    runtime: tokio::runtime::Handle,
}

impl Drainer {
    /// Turn a coerced parameter into a Python object.
    ///
    /// The constructors are looked up once, at worker start, rather than per
    /// request.
    fn to_python<'py>(&self, py: Python<'py>, value: ParamValue) -> PyResult<Bound<'py, PyAny>> {
        Ok(match value {
            ParamValue::Str(v) => v.into_pyobject(py)?.into_any(),
            ParamValue::Int(v) => v.into_pyobject(py)?.into_any(),
            ParamValue::Float(v) => v.into_pyobject(py)?.into_any(),
            ParamValue::Bool(v) => v.into_pyobject(py)?.to_owned().into_any(),
            ParamValue::Uuid(v) => self.make_uuid.bind(py).call1((v,))?,
            ParamValue::Date(v) => self.make_date.bind(py).call1((v,))?,
            ParamValue::DateTime(v) => self.make_datetime.bind(py).call1((v,))?,
            ParamValue::List(items) => {
                let list = PyList::empty(py);
                for item in items {
                    list.append(self.to_python(py, item)?)?;
                }
                list.into_any()
            }
            ParamValue::Null | ParamValue::Omit => py.None().into_bound(py),
        })
    }
}

#[pymethods]
impl Drainer {
    fn __call__(&self, py: Python<'_>) -> PyResult<()> {
        let mut buf = [0u8; 64];
        while let Ok(n) = (&self.reader).read(&mut buf) {
            if n < buf.len() {
                break;
            }
        }

        // Order matters: clear before popping, so a push that races with this
        // drain writes a new wake byte rather than being silently swallowed.
        self.queue.clear_notified();

        // Notifications first: they are cheap, and one of them is a stream
        // learning its client is gone, which frees a subscription and a slot.
        // A raising callback must not abandon the rest of the batch.
        while let Some(callback) = self.queue.pop_wakeup() {
            if let Err(err) = callback.call0(py) {
                err.write_unraisable(py, None);
            }
        }

        let run_handler = self.run_handler.bind(py);
        let create_task = self.create_task.bind(py);

        while let Some(item) = self.queue.pop() {
            self.queue.claim();
            let handler = match (item.gate, self.gates[item.route].as_ref()) {
                (true, Some(gate)) => gate.bind(py),
                _ => self.routes[item.route].bind(py),
            };
            let spec = self.router.spec(item.route);

            // Handlers with no path parameters skip the dict entirely, so the
            // hello-world path costs exactly what it did before routing existed.
            let params = if spec.params.is_empty() {
                None
            } else {
                let dict = PyDict::new(py);
                for (param, value) in spec.params.iter().zip(item.params) {
                    match value {
                        // Left out on purpose: the handler's own default applies.
                        ParamValue::Omit => {}
                        other => dict.set_item(&param.name, self.to_python(py, other)?)?,
                    }
                }
                Some(dict)
            };

            let request = Py::new(
                py,
                Request {
                    method: item.method,
                    path: item.path,
                    query: item.query,
                    body: item.body,
                    headers: item.headers,
                },
            )?;
            let responder = Py::new(
                py,
                Responder::new(item.reply, self.queue.clone(), self.runtime.clone()),
            )?;
            let coro = match item.websocket {
                Some(shared) => {
                    let socket = Py::new(py, WebSocket::new(shared))?;
                    self.run_websocket
                        .bind(py)
                        .call1((handler, request, responder, socket, params))?
                }
                None => run_handler.call1((handler, request, responder, params, self.debug))?,
            };
            create_task.call1((coro,))?;
        }

        self.queue.rewake_if_pending();
        Ok(())
    }
}

/// A Python worker: one OS thread, one asyncio loop, one request queue.
pub struct Worker {
    pub queue: Arc<WorkerQueue>,
    event_loop: Py<PyAny>,
    call_soon_threadsafe: Py<PyAny>,
}

impl Worker {
    /// Eight arguments, one over the limit. Called once per worker at start,
    /// with a different value for each, so grouping them would be a struct
    /// that exists only to satisfy a count.
    #[allow(clippy::too_many_arguments)]
    pub fn spawn(
        py: Python<'_>,
        index: usize,
        routes: Arc<Vec<Py<PyAny>>>,
        gates: Arc<Vec<Option<Py<PyAny>>>>,
        router: Arc<Router>,
        limit: usize,
        debug: bool,
        tokio_handle: tokio::runtime::Handle,
    ) -> PyResult<Self> {
        let (write_end, read_end) = UnixStream::pair()?;
        write_end.set_nonblocking(true)?;
        read_end.set_nonblocking(true)?;

        let queue = Arc::new(WorkerQueue::new(write_end, limit));
        let worker_queue = queue.clone();
        let (tx, rx) = mpsc::channel::<PyResult<(Py<PyAny>, Py<PyAny>)>>();

        thread::Builder::new()
            .name(format!("aether-py-{index}"))
            .spawn(move || {
                Python::attach(|py| {
                    let started = (|| -> PyResult<(Bound<'_, PyAny>, Py<PyAny>)> {
                        let runtime = py.import("aether._runtime")?;
                        let event_loop = runtime.call_method0("make_worker_loop")?;
                        let drainer = Drainer {
                            queue: worker_queue,
                            routes,
                            gates,
                            router,
                            run_handler: runtime.getattr("run_handler")?.unbind(),
                            run_websocket: runtime.getattr("run_websocket")?.unbind(),
                            make_uuid: runtime.getattr("make_uuid")?.unbind(),
                            make_date: runtime.getattr("make_date")?.unbind(),
                            make_datetime: runtime.getattr("make_datetime")?.unbind(),
                            create_task: event_loop.getattr("create_task")?.unbind(),
                            debug,
                            reader: read_end,
                            runtime: tokio_handle,
                        };
                        let fd = drainer.reader.as_raw_fd();
                        event_loop.call_method1("add_reader", (fd, Py::new(py, drainer)?))?;
                        let csts = event_loop.getattr("call_soon_threadsafe")?.unbind();
                        Ok((event_loop, csts))
                    })();

                    match started {
                        Ok((event_loop, csts)) => {
                            let _ = tx.send(Ok((event_loop.clone().unbind(), csts)));
                            if let Err(e) = event_loop.call_method0("run_forever") {
                                e.print(py);
                            }
                        }
                        Err(e) => {
                            let _ = tx.send(Err(e));
                        }
                    }
                });
            })
            .expect("failed to spawn python worker thread");

        // Detach while waiting: blocking in native code while attached stalls
        // free-threaded CPython's stop-the-world and deadlocks startup.
        let (event_loop, call_soon_threadsafe) = py
            .detach(move || rx.recv())
            .expect("worker thread died before reporting its event loop")?;

        Ok(Self {
            queue,
            event_loop,
            call_soon_threadsafe,
        })
    }

    pub fn stop(&self, py: Python<'_>) {
        if let Ok(stop) = self.event_loop.getattr(py, "stop") {
            let _ = self.call_soon_threadsafe.call1(py, (stop,));
        }
    }
}
