use std::{
    io,
    sync::{Arc, RwLock},
    time::Duration,
};

#[cfg(unix)]
use balance_this_load::sdn::{bind_control_socket, spawn_control_server, SdnClient};
use balance_this_load::{
    config::Config,
    health,
    listener::ClientListener,
    metrics::{spawn_reporter, Metrics},
    registry::{BackendRegistry, SharedRegistry},
    scheduler::{Algos, Scheduler},
    server::run_load_balancer,
    shutdown,
};
use tracing::{info, warn};
use tracing_subscriber::EnvFilter;

const DEFAULT_CONFIG_PATH: &str = "config.toml";
const METRICS_LOG_INTERVAL: Duration = Duration::from_secs(15);

fn config_error(message: String) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let env_filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    tracing_subscriber::fmt().with_env_filter(env_filter).init();

    let config_path = std::env::args()
        .nth(1)
        .unwrap_or_else(|| DEFAULT_CONFIG_PATH.to_owned());

    let config = Config::load(&config_path).map_err(config_error)?;
    let algorithm = config.algorithm;
    let health_check = config.health_check.clone();
    let max_connections = config.max_connections;
    let backend_count = config.backends.len();

    let mut backend_registry = BackendRegistry::new();
    for backend in config.backends {
        backend_registry
            .add_backend(backend)
            .map_err(config_error)?;
    }

    let backend_registry: SharedRegistry = Arc::new(RwLock::new(backend_registry));

    #[cfg(unix)]
    let sdn_client: Option<Arc<SdnClient>> = match config.sdn.controller_socket.as_ref() {
        Some(path) => {
            let client = SdnClient::connect(path.clone()).await.map_err(|error| {
                config_error(format!("failed to connect to sdn controller: {error}"))
            })?;
            info!(socket = %path.display(), "connected to sdn controller");
            Some(Arc::new(client))
        }
        None => None,
    };

    #[cfg(unix)]
    let sdn_task = match config.sdn.control_listen.as_ref() {
        Some(path) => {
            let client = sdn_client.as_ref().ok_or_else(|| {
                config_error("sdn.control_listen requires sdn.controller_socket".to_owned())
            })?;
            let listener = bind_control_socket(path).await.map_err(|error| {
                config_error(format!("failed to bind sdn control socket: {error}"))
            })?;
            info!(socket = %path.display(), "sdn control socket ready");
            Some(spawn_control_server(
                listener,
                Arc::clone(&backend_registry),
                Arc::clone(client),
            ))
        }
        None => None,
    };

    let scheduler: Arc<dyn Scheduler> = Arc::new(Algos::new(algorithm));
    let metrics = Arc::new(Metrics::new());

    let client_listener = ClientListener::bind(config.listener_address).await?;
    let listener_address = client_listener.local_address()?;

    info!(
        listen = %listener_address,
        algorithm = %algorithm,
        backends = backend_count,
        max_connections = max_connections.unwrap_or(0),
        health_checks = health_check.enabled,
        "load balancer started"
    );

    if max_connections.is_none() {
        warn!("limits.max_connections is not set; the connection count is unlimited");
    }

    let health_task = tokio::spawn(health::run(
        Arc::clone(&backend_registry),
        health_check.clone(),
    ));
    let metrics_task = spawn_reporter(Arc::clone(&metrics), METRICS_LOG_INTERVAL);

    let (shutdown_sender, shutdown_receiver) = shutdown::channel();
    let shutdown_task = tokio::spawn(shutdown::wait_for_ctrl_c(shutdown_sender));

    let run_result = run_load_balancer(
        client_listener,
        backend_registry,
        scheduler,
        Arc::clone(&metrics),
        max_connections,
        shutdown_receiver,
    )
    .await;

    health_task.abort();
    metrics_task.abort();
    shutdown_task.abort();
    #[cfg(unix)]
    if let Some(sdn_task) = sdn_task {
        sdn_task.abort();
    }

    info!(metrics = %metrics.snapshot(), "final metrics");

    run_result?;

    Ok(())
}
