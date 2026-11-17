use std::{
    fs,
    net::SocketAddr,
    path::{Path, PathBuf},
    time::Duration,
};

use serde::Deserialize;

use crate::{backend::Backend, scheduler::Algorithm};

pub const DEFAULT_HEALTH_INTERVAL_SECS: u64 = 5;
pub const DEFAULT_HEALTH_TIMEOUT_SECS: u64 = 1;

//settings for the background backend health checks
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HealthCheckConfig {
    pub enabled: bool,
    pub interval: Duration,
    pub timeout: Duration,
}

impl Default for HealthCheckConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            interval: Duration::from_secs(DEFAULT_HEALTH_INTERVAL_SECS),
            timeout: Duration::from_secs(DEFAULT_HEALTH_TIMEOUT_SECS),
        }
    }
}

//the whole runtime configuration of the load balancer
#[derive(Debug)]
pub struct Config {
    pub listener_address: SocketAddr,
    pub backends: Vec<Backend>,
    pub algorithm: Algorithm,
    pub health_check: HealthCheckConfig,
    pub max_connections: Option<usize>,
    pub sdn: SdnConfig,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SdnConfig {
    pub controller_socket: Option<PathBuf>,
    pub control_listen: Option<PathBuf>,
}

//the raw shapes that come straight out of the TOML file
#[derive(Debug, Deserialize)]
struct RawConfig {
    listen: SocketAddr,
    #[serde(default)]
    algorithm: Algorithm,
    #[serde(default)]
    health_check: RawHealthCheck,
    #[serde(default)]
    limits: RawLimits,
    #[serde(default)]
    backends: Vec<RawBackend>,
    #[serde(default)]
    sdn: RawSdn,
}

#[derive(Debug, Default, Deserialize)]
struct RawSdn {
    controller_socket: Option<PathBuf>,
    control_listen: Option<PathBuf>,
}

#[derive(Debug, Deserialize)]
struct RawHealthCheck {
    #[serde(default = "default_health_enabled")]
    enabled: bool,
    #[serde(default = "default_health_interval_secs")]
    interval_secs: u64,
    #[serde(default = "default_health_timeout_secs")]
    timeout_secs: u64,
}

impl Default for RawHealthCheck {
    fn default() -> Self {
        Self {
            enabled: default_health_enabled(),
            interval_secs: default_health_interval_secs(),
            timeout_secs: default_health_timeout_secs(),
        }
    }
}

#[derive(Debug, Default, Deserialize)]
struct RawLimits {
    max_connections: Option<usize>,
}

#[derive(Debug, Deserialize)]
struct RawBackend {
    id: String,
    address: SocketAddr,
    #[serde(default = "default_weight")]
    weight: usize,
}

fn default_health_enabled() -> bool {
    true
}

fn default_health_interval_secs() -> u64 {
    DEFAULT_HEALTH_INTERVAL_SECS
}

fn default_health_timeout_secs() -> u64 {
    DEFAULT_HEALTH_TIMEOUT_SECS
}

fn default_weight() -> usize {
    1
}

impl Config {
    //in-memory constructor used by tests and simple embeddings
    pub fn new(listener_address: SocketAddr, backends: Vec<Backend>) -> Self {
        Self {
            listener_address,
            backends,
            algorithm: Algorithm::RoundRobin,
            health_check: HealthCheckConfig::default(),
            max_connections: None,
            sdn: SdnConfig::default(),
        }
    }

    //read a TOML file from disk and turn it into a validated config
    pub fn load(config_path: impl AsRef<Path>) -> Result<Self, String> {
        let config_path = config_path.as_ref();
        let contents = fs::read_to_string(config_path).map_err(|read_error| {
            format!(
                "failed to read config file '{}': {read_error}",
                config_path.display()
            )
        })?;

        Self::from_toml_str(&contents).map_err(|parse_error| {
            format!(
                "invalid config file '{}': {parse_error}",
                config_path.display()
            )
        })
    }

    //parse + validate a TOML string (also used by tests)
    pub fn from_toml_str(contents: &str) -> Result<Self, String> {
        let raw_config: RawConfig =
            toml::from_str(contents).map_err(|parse_error| parse_error.to_string())?;

        if raw_config.health_check.interval_secs == 0 {
            return Err("health_check.interval_secs must be at least 1".to_owned());
        }

        if raw_config.health_check.timeout_secs == 0 {
            return Err("health_check.timeout_secs must be at least 1".to_owned());
        }

        if raw_config.limits.max_connections == Some(0) {
            return Err("limits.max_connections must be at least 1 (or omit it)".to_owned());
        }

        let mut backends = Vec::with_capacity(raw_config.backends.len());

        for raw_backend in raw_config.backends {
            let backend =
                Backend::new_with_weight(raw_backend.id, raw_backend.address, raw_backend.weight)?;
            backends.push(backend);
        }

        let config = Self {
            listener_address: raw_config.listen,
            backends,
            algorithm: raw_config.algorithm,
            health_check: HealthCheckConfig {
                enabled: raw_config.health_check.enabled,
                interval: Duration::from_secs(raw_config.health_check.interval_secs),
                timeout: Duration::from_secs(raw_config.health_check.timeout_secs),
            },
            max_connections: raw_config.limits.max_connections,
            sdn: SdnConfig {
                controller_socket: raw_config.sdn.controller_socket,
                control_listen: raw_config.sdn.control_listen,
            },
        };

        config.validate()?;

        Ok(config)
    }

    //to check if the config is valid or nah
    pub fn validate(&self) -> Result<(), String> {
        if self.backends.is_empty() {
            return Err("add at least one backend before starting the load balancer".to_owned());
        }

        if let (Some(controller_socket), Some(control_listen)) =
            (&self.sdn.controller_socket, &self.sdn.control_listen)
        {
            if controller_socket == control_listen {
                return Err("sdn.controller_socket and sdn.control_listen must differ".to_owned());
            }
        }

        for backend_index in 0..self.backends.len() {
            let current_backend = &self.backends[backend_index];

            for other_backend_index in (backend_index + 1)..self.backends.len() {
                let other_backend = &self.backends[other_backend_index];

                if current_backend.id == other_backend.id {
                    let error_message = format!(
                        "backend id '{}' is configured more than once",
                        current_backend.id
                    );
                    return Err(error_message);
                }
            }
        }

        Ok(())
    }
}
