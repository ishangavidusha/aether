//! Which origins may open a WebSocket.
//!
//! Browsers do not apply CORS to WebSockets. A page on any site can open a
//! socket to this server, and the browser attaches the user's cookies to the
//! handshake, so a socket authenticated by cookie is usable by every site the
//! user visits: cross-site WebSocket hijacking. The one signal a browser always
//! sends and a page cannot forge is `Origin`, so it is checked here, in Rust,
//! before the authorizer or the handler runs and before the 101 is sent.
//!
//! Accepted: no `Origin` at all (not a browser, and not the threat); an origin
//! naming this server, compared with `Host` or `X-Forwarded-Host`, neither of
//! which a page can set on a handshake; and the configured origins.

use std::collections::HashSet;

use hyper::header::{HeaderMap, HOST, ORIGIN};

/// (any origin, allowed origins) from `aether._app`.
pub type OriginsTuple = (bool, Vec<String>);

pub struct SocketOrigins {
    any: bool,
    allowed: HashSet<String>,
}

/// `https://example.com:443` and `example.com:443` both become `example.com`.
fn without_default_port(authority: &str, scheme: Option<&str>) -> String {
    let lowered = authority.trim().to_ascii_lowercase();
    let strip = match scheme {
        Some("https") | Some("wss") => [":443"].as_slice(),
        Some("http") | Some("ws") => [":80"].as_slice(),
        // A Host header carries no scheme, so either default port is dropped.
        _ => [":80", ":443"].as_slice(),
    };
    for suffix in strip {
        if let Some(bare) = lowered.strip_suffix(suffix) {
            return bare.to_owned();
        }
    }
    lowered
}

impl SocketOrigins {
    pub fn build(spec: OriginsTuple) -> Self {
        let (any, allowed) = spec;
        Self {
            any,
            allowed: allowed
                .into_iter()
                .map(|o| o.to_ascii_lowercase())
                .collect(),
        }
    }

    pub fn permits(&self, headers: &HeaderMap) -> bool {
        if self.any {
            return true;
        }
        let Some(origin) = headers.get(ORIGIN) else {
            return true;
        };
        let Ok(origin) = origin.to_str() else {
            return false;
        };
        let origin = origin.trim().to_ascii_lowercase();
        if self.allowed.contains(&origin) {
            return true;
        }
        // `null`, from sandboxed frames and local files, has no authority to
        // compare, and is refused unless listed.
        let Some((scheme, authority)) = origin.split_once("://") else {
            return false;
        };
        let authority = without_default_port(authority, Some(scheme));

        let named = |value: Option<&str>| {
            value.is_some_and(|host| without_default_port(host, None) == authority)
        };
        if named(headers.get(HOST).and_then(|v| v.to_str().ok())) {
            return true;
        }
        // Behind a proxy that rewrites Host — nginx does by default — the
        // original arrives here instead. Trusting it cannot help an attacking
        // page, which has no way to set it on a handshake.
        named(
            headers
                .get("x-forwarded-host")
                .and_then(|v| v.to_str().ok())
                .and_then(|v| v.split(',').next()),
        )
    }
}
