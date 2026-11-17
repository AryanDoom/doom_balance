use std::{io, sync::Arc, time::Duration};

use tokio::sync::watch;
use tokio::task::JoinSet;
use tracing::{debug, error, info, warn};

use crate::{
    connection::ConnectionGuard, listener::ClientListener, metrics::Metrics,
    proxy::handle_client_connection, registry::SharedRegistry, scheduler::Scheduler,
};

//how long we let active connections finish after shutdown is requested
const SHUTDOWN_GRACE: Duration = Duration::from_secs(10);

//main loop for the lb, it accepts clients and sends them to the selected backend
pub async fn run_load_balancer(
    client_listener: ClientListener,
    backend_registry: SharedRegistry,
    scheduler: Arc<dyn Scheduler>,
    metrics: Arc<Metrics>,
    max_connections: Option<usize>,
    shutdown_receiver: watch::Receiver<bool>,
) -> io::Result<()> {
    run_load_balancer_with_grace(
        client_listener,
        backend_registry,
        scheduler,
        metrics,
        max_connections,
        shutdown_receiver,
        SHUTDOWN_GRACE,
    )
    .await
}

pub async fn run_load_balancer_with_grace(
    client_listener: ClientListener,
    backend_registry: SharedRegistry,
    scheduler: Arc<dyn Scheduler>,
    metrics: Arc<Metrics>,
    max_connections: Option<usize>,
    mut shutdown_receiver: watch::Receiver<bool>,
    shutdown_grace: Duration,
) -> io::Result<()> {
    let mut connections: JoinSet<()> = JoinSet::new();
    let mut listener_error: Option<io::Error> = None;

    loop {
        tokio::select! {
            //ctrl-c wins over accepting new clients
            biased;
            shutdown_changed = shutdown_receiver.changed() => {
                if shutdown_changed.is_err() || *shutdown_receiver.borrow() {
                    break;
                }
            }
            accepted_connection = client_listener.accept_client() => {
                let (client_stream, client_address) = match accepted_connection {
                    Ok(accepted_connection) => accepted_connection,
                    Err(accept_error) => {
                        error!(error = %accept_error, "failed to accept a client connection");
                        listener_error = Some(accept_error);
                        break;
                    }
                };

                //stop accepting work once the configured limit is reached
                if let Some(connection_limit) = max_connections {
                    if metrics.active_connections() >= connection_limit {
                        metrics.connection_rejected();
                        warn!(
                            client = %client_address,
                            max_connections = connection_limit,
                            "connection limit reached; dropping client"
                        );
                        drop(client_stream);
                        continue;
                    }
                }

                //pick a backend and reserve a connection slot on it
                let backend_selection = {
                    let registry = backend_registry.read().expect("registry lock poisoned");
                    let backend_list = registry.all_backends();

                    scheduler
                        .select(backend_list, client_address)
                        .and_then(|backend_index| {
                            backend_list.get(backend_index).map(|backend| {
                                (backend.address, backend.start_connection())
                            })
                        })
                };

                let (backend_address, active_connection_guard) = match backend_selection {
                    Some(backend_selection) => backend_selection,
                    None => {
                        metrics.connection_rejected();
                        warn!(
                            client = %client_address,
                            "no healthy backend available; dropping client"
                        );
                        drop(client_stream);
                        continue;
                    }
                };

                metrics.connection_accepted();
                debug!(
                    client = %client_address,
                    backend = %backend_address,
                    "proxying connection"
                );

                //each client gets its own task so the listener can keep accepting more clients
                let mut connection_guard =
                    ConnectionGuard::new(active_connection_guard, Arc::clone(&metrics));
                connections.spawn(async move {
                    //this gets dropped when the connection task ends, which lowers the active count
                    match handle_client_connection(client_stream, backend_address).await {
                        Ok((bytes_to_backend, bytes_to_client)) => {
                            connection_guard.finish(bytes_to_backend, bytes_to_client);
                            debug!(
                                client = %client_address,
                                backend = %backend_address,
                                bytes_to_backend,
                                bytes_to_client,
                                "connection closed"
                            );
                        }
                        Err(proxy_error) => {
                            connection_guard.fail();
                            warn!(
                                client = %client_address,
                                backend = %backend_address,
                                error = %proxy_error,
                                "connection failed"
                            );
                        }
                    }
                });
            }
        }
    }

    info!(
        active_connections = connections.len(),
        "listener stopped; draining active connections"
    );

    //wait for active connections, but do not hang forever on long-lived ones
    let grace_deadline = tokio::time::sleep(shutdown_grace);
    tokio::pin!(grace_deadline);

    loop {
        tokio::select! {
            _ = &mut grace_deadline => {
                warn!(
                    remaining = connections.len(),
                    "grace period ended; aborting remaining connections"
                );
                connections.abort_all();
                while connections.join_next().await.is_some() {}
                break;
            }
            joined_task = connections.join_next() => {
                match joined_task {
                    None => break,
                    Some(Ok(())) => {}
                    Some(Err(join_error)) => {
                        warn!(error = %join_error, "connection task ended abnormally");
                    }
                }
            }
        }
    }

    info!("shutdown complete");

    match listener_error {
        Some(listener_error) => Err(listener_error),
        None => Ok(()),
    }
}
