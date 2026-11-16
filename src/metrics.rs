use std::{
    fmt,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
    time::Duration,
};

use tracing::info;

//connection and byte counters, all atomic so every client task can update them
#[derive(Debug, Default)]
pub struct Metrics {
    accepted: AtomicU64,
    active: AtomicU64,
    failed: AtomicU64,
    rejected: AtomicU64,
    bytes_client_to_backend: AtomicU64,
    bytes_backend_to_client: AtomicU64,
}

impl Metrics {
    pub fn new() -> Self {
        Self::default()
    }

    //called once a backend has been chosen and the client is about to be proxied
    pub fn connection_accepted(&self) {
        self.accepted.fetch_add(1, Ordering::Relaxed);
        self.active.fetch_add(1, Ordering::Relaxed);
    }

    //called after copy_bidirectional finishes normally
    pub fn connection_finished(&self, bytes_to_backend: u64, bytes_to_client: u64) {
        self.active.fetch_sub(1, Ordering::Relaxed);
        self.bytes_client_to_backend
            .fetch_add(bytes_to_backend, Ordering::Relaxed);
        self.bytes_backend_to_client
            .fetch_add(bytes_to_client, Ordering::Relaxed);
    }

    //called when the backend connection or the proxy copy fails
    pub fn connection_failed(&self) {
        self.active.fetch_sub(1, Ordering::Relaxed);
        self.failed.fetch_add(1, Ordering::Relaxed);
    }

    //called when a client is dropped before proxying (limit reached or no healthy backend)
    pub fn connection_rejected(&self) {
        self.rejected.fetch_add(1, Ordering::Relaxed);
    }

    pub fn active_connections(&self) -> usize {
        self.active.load(Ordering::Relaxed) as usize
    }

    pub fn snapshot(&self) -> MetricsSnapshot {
        MetricsSnapshot {
            accepted: self.accepted.load(Ordering::Relaxed),
            active: self.active.load(Ordering::Relaxed),
            failed: self.failed.load(Ordering::Relaxed),
            rejected: self.rejected.load(Ordering::Relaxed),
            bytes_client_to_backend: self.bytes_client_to_backend.load(Ordering::Relaxed),
            bytes_backend_to_client: self.bytes_backend_to_client.load(Ordering::Relaxed),
        }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct MetricsSnapshot {
    pub accepted: u64,
    pub active: u64,
    pub failed: u64,
    pub rejected: u64,
    pub bytes_client_to_backend: u64,
    pub bytes_backend_to_client: u64,
}

impl fmt::Display for MetricsSnapshot {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "accepted={} active={} failed={} rejected={} bytes_client_to_backend={} bytes_backend_to_client={}",
            self.accepted,
            self.active,
            self.failed,
            self.rejected,
            self.bytes_client_to_backend,
            self.bytes_backend_to_client
        )
    }
}

//logs a metrics line every interval so the running server is observable
pub fn spawn_reporter(metrics: Arc<Metrics>, interval: Duration) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        let mut ticker = tokio::time::interval(interval);
        ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);

        //skip the immediate first tick so we do not log all-zero counters at startup
        ticker.tick().await;

        loop {
            ticker.tick().await;
            info!(metrics = %metrics.snapshot(), "metrics");
        }
    })
}
