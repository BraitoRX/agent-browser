pub(crate) mod chat;

use std::time::{Duration, Instant};
use tokio::sync::Notify;

pub(crate) struct IdleActivity {
    last: std::sync::Mutex<Instant>,
    notify: Notify,
}

impl IdleActivity {
    pub(crate) fn new() -> Self {
        Self {
            last: std::sync::Mutex::new(Instant::now()),
            notify: Notify::new(),
        }
    }

    pub(crate) fn mark(&self) {
        *self.last.lock().unwrap_or_else(|err| err.into_inner()) = Instant::now();
        self.notify.notify_one();
    }

    pub(crate) fn elapsed(&self) -> Duration {
        self.last
            .lock()
            .unwrap_or_else(|err| err.into_inner())
            .elapsed()
    }

    pub(crate) async fn notified(&self) {
        self.notify.notified().await;
    }
}