use std::{net::SocketAddr, time::Duration};

use tokio::net::TcpStream;
use tokio::time::{timeout, MissedTickBehavior};
use tracing::{info, warn};

use crate::{config::HealthCheckConfig, registry::SharedRegistry};

//background loop that probes every backend and updates its health flag
pub async fn run(shared_registry: SharedRegistry, settings: HealthCheckConfig) {
    if !settings.enabled {
        info!("health checks are disabled in the config");
        return;
    }

    let mut ticker = tokio::time::interval(settings.interval);
    ticker.set_missed_tick_behavior(MissedTickBehavior::Skip);

    loop {
        ticker.tick().await;

        //snapshot first so we never hold the lock across an await
        let backends = shared_registry
            .read()
            .expect("registry lock poisoned")
            .health_snapshot();

        for (backend_id, backend_address, was_healthy) in backends {
            let is_healthy = probe(backend_address, settings.timeout).await;

            if is_healthy == was_healthy {
                continue;
            }

            let updated = shared_registry
                .write()
                .expect("registry lock poisoned")
                .set_backend_health(&backend_id, is_healthy);

            if !updated {
                continue;
            }

            if is_healthy {
                info!(
                    backend = %backend_id,
                    address = %backend_address,
                    "backend is healthy again"
                );
            } else {
                warn!(
                    backend = %backend_id,
                    address = %backend_address,
                    "backend is unhealthy; new connections will avoid it"
                );
            }
        }
    }
}

//a backend counts as healthy when a TCP connection can be opened in time
async fn probe(backend_address: SocketAddr, probe_timeout: Duration) -> bool {
    matches!(
        timeout(probe_timeout, TcpStream::connect(backend_address)).await,
        Ok(Ok(_))
    )
}
