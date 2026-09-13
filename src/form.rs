//! Form bodies: `application/x-www-form-urlencoded` and `multipart/form-data`.
//!
//! Parsed on demand, on the worker thread, when a handler calls
//! `request.form()`, so a route that never reads a form pays nothing. The body
//! is already whole and bounded by `max_body` by the time it gets here; streaming
//! a very large upload is `BodyStream`'s job, not this.

use std::convert::Infallible;
use std::future::Future;
use std::pin::pin;
use std::task::{Context, Poll, Waker};

use bytes::Bytes;

pub enum Part {
    Field {
        name: String,
        value: String,
    },
    File {
        name: String,
        filename: String,
        content_type: Option<String>,
        data: Vec<u8>,
    },
}

pub enum FormError {
    /// Not a form content type at all.
    Unsupported,
    Malformed(String),
    TooManyParts(usize),
}

/// Parse a body according to its content type.
pub fn parse(
    content_type: Option<&str>,
    body: &[u8],
    max_parts: usize,
) -> Result<Vec<Part>, FormError> {
    let content_type = content_type.ok_or(FormError::Unsupported)?;
    let essence = content_type
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase();
    match essence.as_str() {
        "application/x-www-form-urlencoded" => urlencoded(body, max_parts),
        "multipart/form-data" => multipart(content_type, body, max_parts),
        _ => Err(FormError::Unsupported),
    }
}

fn urlencoded(body: &[u8], max_parts: usize) -> Result<Vec<Part>, FormError> {
    let mut parts = Vec::new();
    for (name, value) in form_urlencoded::parse(body) {
        if parts.len() == max_parts {
            return Err(FormError::TooManyParts(max_parts));
        }
        parts.push(Part::Field {
            name: name.into_owned(),
            value: value.into_owned(),
        });
    }
    Ok(parts)
}

fn multipart(content_type: &str, body: &[u8], max_parts: usize) -> Result<Vec<Part>, FormError> {
    let boundary =
        multer::parse_boundary(content_type).map_err(|e| FormError::Malformed(e.to_string()))?;
    let stream = futures_util::stream::once(std::future::ready(Ok::<Bytes, Infallible>(
        Bytes::copy_from_slice(body),
    )));
    let mut reader = multer::Multipart::new(stream, boundary);

    run_ready(async move {
        let mut parts = Vec::new();
        while let Some(field) = reader
            .next_field()
            .await
            .map_err(|e| FormError::Malformed(e.to_string()))?
        {
            if parts.len() == max_parts {
                return Err(FormError::TooManyParts(max_parts));
            }
            let name = field
                .name()
                .ok_or_else(|| FormError::Malformed("a part has no name".into()))?
                .to_owned();
            let filename = field.file_name().map(str::to_owned);
            let content_type = field.content_type().map(|m| m.to_string());
            let data = field
                .bytes()
                .await
                .map_err(|e| FormError::Malformed(e.to_string()))?;
            parts.push(match filename {
                Some(filename) => Part::File {
                    name,
                    filename,
                    content_type,
                    data: data.to_vec(),
                },
                // Undecodable text becomes replacement characters, as it does
                // in a query string, rather than failing the whole form.
                None => Part::Field {
                    name,
                    value: String::from_utf8_lossy(&data).into_owned(),
                },
            });
        }
        Ok(parts)
    })
}

/// Drive a future whose every input is already in memory.
///
/// multer is written against an async stream; here the stream is one buffer
/// that is ready immediately, so the future can never be waiting on anything.
/// Polling it to completion with a no-op waker avoids pulling in an executor,
/// or blocking on the tokio runtime from a Python worker thread.
fn run_ready<T>(future: impl Future<Output = Result<T, FormError>>) -> Result<T, FormError> {
    let mut future = pin!(future);
    let mut context = Context::from_waker(Waker::noop());
    match future.as_mut().poll(&mut context) {
        Poll::Ready(result) => result,
        Poll::Pending => Err(FormError::Malformed(
            "form parser waited on input that should already be present".into(),
        )),
    }
}
