use hyper::header::{HeaderMap, HeaderName, HeaderValue};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};

/// Immutable view of an incoming HTTP request, handed to the Python handler.
/// `frozen` means no Python-side mutation, so no locking is needed even on
/// free-threaded builds.
#[pyclass(frozen, name = "Request", module = "aether._core")]
pub struct Request {
    pub method: String,
    pub path: String,
    pub query: Option<String>,
    pub body: Vec<u8>,
    /// Kept as hyper's own map rather than converted up front. Most handlers
    /// never look at a header, and converting every one into Python strings on
    /// every request would be paid by all of them.
    pub headers: HeaderMap,
}

#[pymethods]
impl Request {
    /// Build one from Python.
    ///
    /// Needed because a capability invoked over MCP never came in over HTTP,
    /// but the handler it calls still expects a request. The test client uses
    /// it too.
    #[new]
    #[pyo3(signature = (method = "GET".to_string(), path = "/".to_string(), query = None, body = None, headers = None))]
    fn py_new(
        method: String,
        path: String,
        query: Option<String>,
        body: Option<Vec<u8>>,
        headers: Option<Vec<(String, String)>>,
    ) -> Self {
        let mut map = HeaderMap::new();
        for (name, value) in headers.unwrap_or_default() {
            if let (Ok(name), Ok(value)) = (
                HeaderName::from_bytes(name.as_bytes()),
                HeaderValue::from_str(&value),
            ) {
                map.append(name, value);
            }
        }
        Self {
            method,
            path,
            query,
            body: body.unwrap_or_default(),
            headers: map,
        }
    }

    /// The HTTP method, uppercase.
    #[getter]
    fn method(&self) -> &str {
        &self.method
    }

    /// The request path, without the query string.
    #[getter]
    fn path(&self) -> &str {
        &self.path
    }

    /// The raw query string, or None. Declared query parameters are already
    /// coerced and passed as handler arguments; this is for the rest.
    #[getter]
    fn query(&self) -> Option<&str> {
        self.query.as_deref()
    }

    /// The raw request body. A pydantic-annotated argument is the usual way
    /// to read a body; this is for handlers that parse it themselves.
    #[getter]
    fn body<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.body)
    }

    /// One header by name, case-insensitively. None if absent.
    ///
    /// This is the cheap path: no dict is built, and a header that is not
    /// valid UTF-8 reads as absent rather than raising.
    #[pyo3(signature = (name, default = None))]
    fn header(&self, name: &str, default: Option<String>) -> Option<String> {
        self.headers
            .get(name)
            .and_then(|value| value.to_str().ok())
            .map(str::to_owned)
            .or(default)
    }

    /// Every header, lowercased. Repeated headers are joined with ", " as
    /// HTTP itself defines.
    #[getter]
    fn headers<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let dict = PyDict::new(py);
        for name in self.headers.keys() {
            let joined = self
                .headers
                .get_all(name)
                .iter()
                .filter_map(|value| value.to_str().ok())
                .collect::<Vec<_>>()
                .join(", ");
            dict.set_item(name.as_str(), joined)?;
        }
        Ok(dict)
    }

    /// Cookies parsed from the `Cookie` header.
    #[getter]
    fn cookies<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let dict = PyDict::new(py);
        for header in self.headers.get_all(hyper::header::COOKIE) {
            let Ok(raw) = header.to_str() else { continue };
            for pair in raw.split(';') {
                let pair = pair.trim();
                if pair.is_empty() {
                    continue;
                }
                // A cookie with no "=" is malformed; skip it rather than
                // inventing a name or a value for it.
                if let Some((name, value)) = pair.split_once('=') {
                    dict.set_item(name.trim(), value.trim())?;
                }
            }
        }
        Ok(dict)
    }

    fn __repr__(&self) -> String {
        format!("<Request {} {}>", self.method, self.path)
    }
}
