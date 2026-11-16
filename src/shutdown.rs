use tokio::sync::watch;
use tracing::{error, info};

//a small wrapper around a watch channel: one boolean "please stop" flag
pub fn channel() -> (watch::Sender<bool>, watch::Receiver<bool>) {
    watch::channel(false)
}

//waits for ctrl-c and then flips the shared flag
pub async fn wait_for_ctrl_c(shutdown_sender: watch::Sender<bool>) {
    match tokio::signal::ctrl_c().await {
        Ok(()) => info!("shutdown requested (ctrl-c)"),
        Err(signal_error) => error!(error = %signal_error, "failed to listen for ctrl-c"),
    }

    //ignored if the receiver is already gone
    let _ = shutdown_sender.send(true);
}
