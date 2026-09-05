use std::io::Read;
use std::os::fd::AsRawFd;
use std::os::unix::net::UnixStream;
use std::sync::mpsc;
use std::sync::Arc;
use std::thread;

use pyo3::prelude::*;

use crate::queue::WorkerQueue;
use crate::request::Request;
use crate::responder::Responder;

/// Callable handed to `loop.add_reader`. asyncio invokes it on the worker's own
/// thread whenever the wake socket becomes readable, and it drains every queued
/// request in that one callback.
#[pyclass(frozen, name = "Drainer", module = "aether._core")]
struct Drainer {
    queue: Arc<WorkerQueue>,
    routes: Arc<Vec<Py<PyAny>>>,
    /// `aether._runtime.run_handler`, an async function.
    run_handler: Py<PyAny>,
    /// Bound `loop.create_task`.
    create_task: Py<PyAny>,
    /// Read end of the wake socketpair.
    reader: UnixStream,
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

        let run_handler = self.run_handler.bind(py);
        let create_task = self.create_task.bind(py);

        while let Some(item) = self.queue.pop() {
            let handler = self.routes[item.route].bind(py);
            let request = Py::new(
                py,
                Request {
                    method: item.method,
                    path: item.path,
                    query: item.query,
                    body: item.body,
                },
            )?;
            let responder = Py::new(py, Responder::new(item.reply))?;
            let coro = run_handler.call1((handler, request, responder))?;
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
    pub fn spawn(py: Python<'_>, index: usize, routes: Arc<Vec<Py<PyAny>>>) -> PyResult<Self> {
        let (write_end, read_end) = UnixStream::pair()?;
        write_end.set_nonblocking(true)?;
        read_end.set_nonblocking(true)?;

        let queue = Arc::new(WorkerQueue::new(write_end));
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
                            run_handler: runtime.getattr("run_handler")?.unbind(),
                            create_task: event_loop.getattr("create_task")?.unbind(),
                            reader: read_end,
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
