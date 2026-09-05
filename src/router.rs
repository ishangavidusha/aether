//! Routing and path-parameter coercion.
//!
//! One `matchit` radix tree per HTTP method. A miss in the request's own method
//! is checked against the others so a wrong method returns 405 with an `Allow`
//! header rather than a misleading 404.
//!
//! Parameters are coerced here, on the tokio thread, which means a bad path
//! like `/users/abc` for an `int` parameter is rejected without ever waking a
//! Python worker.

use std::collections::HashMap;

use matchit::Router as Matcher;

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum ParamKind {
    Str,
    Int,
    Float,
    Bool,
}

impl ParamKind {
    pub fn parse(name: &str) -> Option<Self> {
        match name {
            "str" => Some(Self::Str),
            "int" => Some(Self::Int),
            "float" => Some(Self::Float),
            "bool" => Some(Self::Bool),
            _ => None,
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::Str => "string",
            Self::Int => "integer",
            Self::Float => "number",
            Self::Bool => "boolean",
        }
    }
}

/// A coerced path parameter, still as plain Rust data. It becomes a Python
/// object only on the worker thread.
pub enum ParamValue {
    Str(String),
    Int(i64),
    Float(f64),
    Bool(bool),
}

pub struct ParamSpec {
    pub name: String,
    pub kind: ParamKind,
}

pub struct RouteSpec {
    pub params: Vec<ParamSpec>,
}

/// Why a request could not be routed.
pub enum RouteError {
    NotFound,
    /// Path exists under other methods; carries the `Allow` header value.
    MethodNotAllowed(String),
    /// A parameter did not coerce; carries a message for the 422 body.
    BadParam(String),
}

pub struct Matched {
    pub route: usize,
    pub params: Vec<ParamValue>,
}

pub struct Router {
    by_method: HashMap<String, Matcher<usize>>,
    specs: Vec<RouteSpec>,
    methods: Vec<String>,
}

impl Router {
    pub fn build(
        routes: &[(String, String, Vec<(String, String)>)],
    ) -> Result<Self, String> {
        let mut by_method: HashMap<String, Matcher<usize>> = HashMap::new();
        let mut specs = Vec::with_capacity(routes.len());

        for (index, (method, path, params)) in routes.iter().enumerate() {
            let mut spec = RouteSpec { params: Vec::new() };
            for (name, kind) in params {
                let kind = ParamKind::parse(kind)
                    .ok_or_else(|| format!("unsupported parameter type {kind:?} for {name:?}"))?;
                spec.params.push(ParamSpec {
                    name: name.clone(),
                    kind,
                });
            }
            specs.push(spec);

            by_method
                .entry(method.to_ascii_uppercase())
                .or_default()
                .insert(path.as_str(), index)
                .map_err(|e| format!("cannot register {method} {path}: {e}"))?;
        }

        let methods = by_method.keys().cloned().collect();
        Ok(Self {
            by_method,
            specs,
            methods,
        })
    }

    pub fn spec(&self, route: usize) -> &RouteSpec {
        &self.specs[route]
    }

    pub fn find(&self, method: &str, path: &str) -> Result<Matched, RouteError> {
        let Some(matcher) = self.by_method.get(method) else {
            return Err(self.other_methods(method, path));
        };
        let Ok(found) = matcher.at(path) else {
            return Err(self.other_methods(method, path));
        };

        let route = *found.value;
        let spec = &self.specs[route];
        let mut params = Vec::with_capacity(spec.params.len());

        for param in &spec.params {
            let raw = found.params.get(&param.name).unwrap_or("");
            params.push(coerce(raw, param).map_err(RouteError::BadParam)?);
        }

        Ok(Matched { route, params })
    }

    /// Distinguishes "no such path" from "wrong method for this path".
    fn other_methods(&self, method: &str, path: &str) -> RouteError {
        let mut allowed: Vec<&str> = self
            .methods
            .iter()
            .filter(|m| *m != method)
            .filter(|m| self.by_method[*m].at(path).is_ok())
            .map(String::as_str)
            .collect();

        if allowed.is_empty() {
            return RouteError::NotFound;
        }
        allowed.sort_unstable();
        RouteError::MethodNotAllowed(allowed.join(", "))
    }
}

fn coerce(raw: &str, param: &ParamSpec) -> Result<ParamValue, String> {
    let bad = || {
        format!(
            "path parameter {:?} expected {}, got {:?}",
            param.name,
            param.kind.label(),
            raw
        )
    };
    match param.kind {
        ParamKind::Str => Ok(ParamValue::Str(raw.to_owned())),
        ParamKind::Int => raw.parse().map(ParamValue::Int).map_err(|_| bad()),
        ParamKind::Float => raw
            .parse::<f64>()
            .ok()
            .filter(|v| v.is_finite())
            .map(ParamValue::Float)
            .ok_or_else(bad),
        ParamKind::Bool => match raw {
            "true" | "True" | "1" => Ok(ParamValue::Bool(true)),
            "false" | "False" | "0" => Ok(ParamValue::Bool(false)),
            _ => Err(bad()),
        },
    }
}
