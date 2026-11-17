use std::{
    fs, io,
    net::IpAddr,
    path::{Path, PathBuf},
    sync::Arc,
};

use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    net::{UnixListener, UnixStream},
    task::JoinHandle,
};
use tracing::{info, warn};

use crate::registry::SharedRegistry;

pub struct SdnClient {
    path: PathBuf,
}

impl SdnClient {
    pub async fn connect(path: PathBuf) -> io::Result<Self> {
        let client = Self { path };
        client.command("STATUS").await?;
        Ok(client)
    }

    pub async fn command(&self, command: &str) -> io::Result<String> {
        let mut stream = UnixStream::connect(&self.path).await?;
        stream.write_all(format!("{command}\n").as_bytes()).await?;

        let mut reader = BufReader::new(stream);
        let mut response = String::new();
        let bytes_read = reader.read_line(&mut response).await?;
        if bytes_read == 0 {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "sdn controller closed the connection",
            ));
        }

        let response = response.trim().to_owned();
        if let Some(message) = response.strip_prefix("ERR") {
            return Err(io::Error::other(message.trim().to_owned()));
        }

        Ok(response)
    }

    pub async fn block(&self, address: IpAddr) -> io::Result<String> {
        self.command(&format!("BLOCK {address}")).await
    }

    pub async fn unblock(&self, address: IpAddr) -> io::Result<String> {
        self.command(&format!("UNBLOCK {address}")).await
    }

    pub async fn status(&self) -> io::Result<String> {
        self.command("STATUS").await
    }
}

pub async fn bind_control_socket(path: &Path) -> io::Result<UnixListener> {
    if path.exists() {
        fs::remove_file(path)?;
    }
    UnixListener::bind(path)
}

pub fn spawn_control_server(
    listener: UnixListener,
    registry: SharedRegistry,
    controller: Arc<SdnClient>,
) -> JoinHandle<()> {
    tokio::spawn(async move {
        loop {
            match listener.accept().await {
                Ok((stream, _)) => {
                    let registry = Arc::clone(&registry);
                    let controller = Arc::clone(&controller);
                    tokio::spawn(async move {
                        if let Err(error) = handle_connection(stream, registry, controller).await {
                            warn!(error = %error, "sdn control connection failed");
                        }
                    });
                }
                Err(error) => {
                    warn!(error = %error, "sdn control socket accept failed");
                    break;
                }
            }
        }
    })
}

async fn handle_connection(
    stream: UnixStream,
    registry: SharedRegistry,
    controller: Arc<SdnClient>,
) -> io::Result<()> {
    let mut reader = BufReader::new(stream);
    let mut line = String::new();

    let bytes_read = reader.read_line(&mut line).await?;
    if bytes_read == 0 {
        return Ok(());
    }

    let parts: Vec<&str> = line.split_whitespace().collect();
    let response = match parts.as_slice() {
        ["STATUS"] => controller.status().await?,
        ["BLOCK", target] | ["UNBLOCK", target] => {
            let address = resolve_target(&registry, target)?;
            match parts[0] {
                "BLOCK" => controller.block(address).await?,
                _ => controller.unblock(address).await?,
            }
        }
        _ => "ERR unsupported command".to_owned(),
    };

    info!(command = %line.trim(), response = %response, "sdn control command");
    reader
        .get_mut()
        .write_all(format!("{response}\n").as_bytes())
        .await?;
    Ok(())
}

fn resolve_target(registry: &SharedRegistry, target: &str) -> io::Result<IpAddr> {
    if let Ok(address) = target.parse::<IpAddr>() {
        return Ok(address);
    }

    let snapshot = registry
        .read()
        .map_err(|error| io::Error::other(error.to_string()))?
        .health_snapshot();

    snapshot
        .iter()
        .find(|(id, _, _)| id == target)
        .map(|(_, address, _)| address.ip())
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "unknown backend"))
}
