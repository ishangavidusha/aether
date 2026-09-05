use std::sync::{Arc, Mutex};

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use tokio::sync::oneshot;

use crate::queue::WorkerQueue;

/// What the Python side hands back for one request.
pub struct Reply {
    pub status: u16,
    pub content_type: String,
    pub body: Vec<u8>,
}

/// One-shot channel back to the tokio task that owns the connection.
/// The Python runtime calls `send` or `send_json` exactly once.
#[pyclass(frozen, name = "Responder", module = "aether._core")]
pub struct Responder {
    tx: Mutex<Option<oneshot::Sender<Reply>>>,
    /// Holding the worker's queue lets the in-flight count fall when this
    /// responder is dropped, which is the only place that reliably runs whether
    /// the handler replied, raised, or was cancelled mid-await.
    queue: Arc<WorkerQueue>,
}

impl Drop for Responder {
    fn drop(&mut self) {
        self.queue.release();
    }
}

impl Responder {
    pub fn new(tx: oneshot::Sender<Reply>, queue: Arc<WorkerQueue>) -> Self {
        Self {
            tx: Mutex::new(Some(tx)),
            queue,
        }
    }

    fn deliver(&self, reply: Reply) -> PyResult<()> {
        let tx = self
            .tx
            .lock()
            .map_err(|_| PyRuntimeError::new_err("responder lock poisoned"))?
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("response already sent"))?;
        // If the client went away the receiver is gone; that's not a Python error.
        let _ = tx.send(reply);
        Ok(())
    }
}

#[pymethods]
impl Responder {
    /// Send raw bytes with an explicit content type.
    fn send(&self, status: u16, content_type: &str, body: &[u8]) -> PyResult<()> {
        self.deliver(Reply {
            status,
            content_type: content_type.to_owned(),
            body: body.to_vec(),
        })
    }

    /// Serialize a Python object to JSON *in Rust* and send it.
    /// This is the path we care about measuring: no `json.dumps` in Python.
    fn send_json(&self, status: u16, obj: &Bound<'_, PyAny>) -> PyResult<()> {
        let value: serde_json::Value = pythonize::depythonize(obj)?;
        let body = serde_json::to_vec(&value)
            .map_err(|e| PyRuntimeError::new_err(format!("json encode failed: {e}")))?;
        self.deliver(Reply {
            status,
            content_type: "application/json".to_owned(),
            body,
        })
    }
}
