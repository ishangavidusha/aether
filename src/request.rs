use pyo3::prelude::*;
use pyo3::types::PyBytes;

/// Immutable view of an incoming HTTP request, handed to the Python handler.
/// `frozen` means no Python-side mutation, so no locking is needed even on
/// free-threaded builds.
#[pyclass(frozen, name = "Request", module = "aether._core")]
pub struct Request {
    pub method: String,
    pub path: String,
    pub query: Option<String>,
    pub body: Vec<u8>,
}

#[pymethods]
impl Request {
    #[getter]
    fn method(&self) -> &str {
        &self.method
    }

    #[getter]
    fn path(&self) -> &str {
        &self.path
    }

    #[getter]
    fn query(&self) -> Option<&str> {
        self.query.as_deref()
    }

    #[getter]
    fn body<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.body)
    }

    fn __repr__(&self) -> String {
        format!("<Request {} {}>", self.method, self.path)
    }
}
