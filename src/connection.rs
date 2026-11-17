use std::sync::Arc;

use crate::{backend::ActiveConnectionGuard, metrics::Metrics};

pub(crate) enum ConnectionOutcome {
    Finished {
        bytes_to_backend: u64,
        bytes_from_backend: u64,
    },
    Failed,
    Aborted,
}

pub(crate) struct ConnectionGuard {
    _backend_guard: ActiveConnectionGuard,
    metrics: Arc<Metrics>,
    outcome: Option<ConnectionOutcome>,
}

impl ConnectionGuard {
    pub(crate) fn new(backend_guard: ActiveConnectionGuard, metrics: Arc<Metrics>) -> Self {
        Self {
            _backend_guard: backend_guard,
            metrics,
            outcome: None,
        }
    }

    pub(crate) fn finish(&mut self, bytes_to_backend: u64, bytes_from_backend: u64) {
        self.outcome = Some(ConnectionOutcome::Finished {
            bytes_to_backend,
            bytes_from_backend,
        });
    }

    pub(crate) fn fail(&mut self) {
        self.outcome = Some(ConnectionOutcome::Failed);
    }
}

impl Drop for ConnectionGuard {
    fn drop(&mut self) {
        match self.outcome.take().unwrap_or(ConnectionOutcome::Aborted) {
            ConnectionOutcome::Finished {
                bytes_to_backend,
                bytes_from_backend,
            } => self
                .metrics
                .connection_finished(bytes_to_backend, bytes_from_backend),
            ConnectionOutcome::Failed => self.metrics.connection_failed(),
            ConnectionOutcome::Aborted => self.metrics.connection_aborted(),
        }
    }
}
