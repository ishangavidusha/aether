//! Per-worker request queue.
//!
//! The whole point of this module: a tokio thread must be able to hand a
//! request to a Python worker *without attaching to the interpreter*. Attaching
//! costs an atomic refcount storm on free-threaded builds and a GIL handoff on
//! standard ones, and the milestone-1 benchmark showed that cost dominating.
//!
//! So a pending request is plain Rust data. It goes into a lock-free queue, and
//! the worker's asyncio loop is woken through a socketpair that the loop
//! already watches via `loop.add_reader`. Writing one byte to a socket is a
//! syscall, not a Python call.

use std::io::Write;
use std::os::unix::net::UnixStream;
use std::sync::atomic::{AtomicBool, Ordering};

use crossbeam_queue::SegQueue;
use tokio::sync::oneshot;

use crate::responder::Reply;

/// One request waiting for a Python worker. No Python objects: the handler is
/// referenced by index into the shared route table.
pub struct Pending {
    pub route: usize,
    pub method: String,
    pub path: String,
    pub query: Option<String>,
    pub body: Vec<u8>,
    pub reply: oneshot::Sender<Reply>,
}

pub struct WorkerQueue {
    queue: SegQueue<Pending>,
    /// True when a wake byte is in flight and not yet consumed. Collapses a
    /// burst of requests into a single wakeup.
    notified: AtomicBool,
    /// Write end of the socketpair. The read end lives in the `Drainer`.
    waker: UnixStream,
}

impl WorkerQueue {
    pub fn new(waker: UnixStream) -> Self {
        Self {
            queue: SegQueue::new(),
            notified: AtomicBool::new(false),
            waker,
        }
    }

    /// Called from tokio threads.
    pub fn push(&self, item: Pending) {
        self.queue.push(item);
        self.wake();
    }

    pub fn pop(&self) -> Option<Pending> {
        self.queue.pop()
    }

    /// Called by the drain callback before it starts popping, so that a
    /// producer racing with the drain always triggers a fresh wakeup.
    pub fn clear_notified(&self) {
        self.notified.store(false, Ordering::SeqCst);
    }

    /// Re-arm if anything arrived while we were draining.
    pub fn rewake_if_pending(&self) {
        if !self.queue.is_empty() {
            self.wake();
        }
    }

    fn wake(&self) {
        // Only the thread that flips false->true writes the byte, so at most
        // one unread byte exists and the socket buffer can never fill.
        if !self.notified.swap(true, Ordering::SeqCst) {
            let _ = (&self.waker).write(&[1u8]);
        }
    }
}
