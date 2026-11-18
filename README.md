# balance_this_load

I started this as my own project idea because I wanted to understand how a load balancer works by building a small one myself. It later became part of my CN mini project (2 birds one stone typeshi)

`balance_this_load` is a TCP load balancer written in Rust. A client connects to one address, the balancer picks an available backend, and then passes the TCP data between them. I built it in small modules so I could learn what each part does instead of setting up an existing tool like NGINX.

## What it does

- Accepts TCP connections from clients.
- Picks a healthy backend with one of five scheduling algorithms.
- Opens a TCP connection to that backend and copies data in both directions.
- Checks whether backends are accepting TCP connections.
- Tracks connection outcomes and bytes sent in each direction.
- Runs in a Mininet topology and can send backend block/unblock commands to a POX SDN controller.

This is TCP only. It does not parse HTTP, terminate TLS, or change the data being sent. UDP is not implemented; it would need a separate design because UDP has datagrams instead of accepted TCP connections.

(All of this is supposed to be done if i dont get distracted fr)

## How a request moves

```text
client
  |
  | TCP
  v
load balancer listener
  |
  v
scheduler picks a healthy backend
  |
  | TCP
  v
backend application
```

The load balancer runs a task for each client connection, so one slow connection does not stop it from accepting others. The proxy copies bytes both ways and carries TCP half-closes through. It does not reuse backend connections: each client connection gets a new one.

## Network setup

The Mininet demo has four hosts on one OpenFlow UserSwitch:

| Host | Address | Role |
| --- | --- | --- |
| h1 | `10.0.0.1` | Sends test requests |
| h2 | `10.0.0.2` | Runs the Rust load balancer on port `8080` |
| h3 | `10.0.0.3` | Backend on port `9000` |
| h4 | `10.0.0.4` | Backend on port `9000` |

POX controls the switch over OpenFlow 1.0. The basic tests use POX's learning-switch app. The SDN test uses my custom POX app, which installs explicit forwarding rules and can block or unblock a backend IP. Rust still chooses the backend; POX controls whether the network can reach it.

The Mininet runs are done in Fedora WSL. The Windows checkout is the main copy, and `sdn/capture_evidence_wsl.sh` syncs it into the WSL copy before running the checks.

## Code layout

| File or module | What it does |
| --- | --- |
| `src/main.rs` | Loads the config and starts the balancer parts. |
| `src/config.rs` | Reads and checks the TOML config. |
| `src/backend.rs` | Stores a backend's address, weight, health, and active connection count. |
| `src/registry.rs` | Holds the backend list used by the listener and health checks. |
| `src/scheduler/algos.rs` | Has the five backend selection algorithms. |
| `src/listener.rs` | Binds the TCP listen address and accepts clients. |
| `src/server.rs` | Runs the accept loop and starts a task for each connection. |
| `src/proxy.rs` | Connects to the backend and copies data both directions. |
| `src/connection.rs` | Cleans up connection counts and metrics when a connection ends. |
| `src/health.rs` | Probes backends and updates their healthy/unhealthy state. |
| `src/metrics.rs` | Tracks counters and writes metric snapshots to the logs. |
| `src/shutdown.rs` | Stops accepting clients and gives active connections time to finish. |
| `src/sdn.rs` | Sends Rust control commands to the POX controller over Unix sockets. |
| `sdn/controller/lb_controller.py` | POX app for explicit switch rules and backend blocking. |
| `sdn/run_*.py` | Mininet test scripts for connectivity, algorithms, health, and SDN. |

## Scheduling algorithms

The scheduler only considers healthy backends.

| Config name | What it does |
| --- | --- |
| `round_robin` | Takes the next healthy backend in order. |
| `weighted_round_robin` | Sends more connections to backends with a higher weight. |
| `least_connections` | Picks the healthy backend with the fewest active connections. Ties rotate. |
| `ip_hash` | Hashes the client's IP so the same IP keeps landing on the same backend while the healthy set stays the same. |
| `random` | Picks a random healthy backend. |

Only weighted round robin uses the backend weights.

## Health checks, metrics, and logs

Health checks try to open a TCP connection to each backend. A successful connection marks it healthy; a failed or timed-out connection marks it unhealthy. The default interval is five seconds. The health test uses a one-second interval so failure and recovery are quicker to show.

This check only tells us whether something accepts a TCP connection. It does not send an HTTP request or prove that the application can answer correctly. When a backend goes down, existing connections can fail, but new connections avoid that backend. When it comes back, it can be selected again.

The metrics module tracks accepted, active, finished, failed, aborted, and rejected connections, plus bytes from client to backend and backend to client. It logs a snapshot every 15 seconds and a final snapshot at shutdown. Metrics are not saved to a database or exposed through Prometheus.

## SDN control

The custom POX app installs destination-IP forwarding rules and an ARP flood rule when the switch connects. It also listens on a Unix socket for:

| Command | Result |
| --- | --- |
| `STATUS` | Shows the backend IPs currently blocked by the controller. |
| `BLOCK <ip>` | Installs drop rules for that backend IP. |
| `UNBLOCK <ip>` | Removes the drop rules for that backend IP. |

When the optional `[sdn]` config is set, Rust connects to POX and opens its own control socket. Rust accepts `BLOCK <backend-id-or-ip>`, `UNBLOCK <backend-id-or-ip>`, and `STATUS`, then forwards the command to POX. This bridge uses Unix sockets, so it is used in the Linux/WSL demo.

## Configuration

The program reads `config.toml` by default. You can pass a different file as the first argument.

```toml
listen = "127.0.0.1:8080"
algorithm = "round_robin"

[health_check]
enabled = true
interval_secs = 5
timeout_secs = 1

[limits]
max_connections = 1024

[[backends]]
id = "backend-1"
address = "127.0.0.1:9001"
weight = 1

[[backends]]
id = "backend-2"
address = "127.0.0.1:9002"
weight = 2
```

For the Mininet run, the matching config is `sdn/config_mininet.toml`. The `[sdn]` section is optional; leave it out for normal local use.

## Run it locally

Build the project:

```bash
cargo build --release
```

Start two TCP backends in separate terminals. The included echo server is handy for checking the proxy:

```bash
python sdn/echo_backend.py 127.0.0.1 9001
python sdn/echo_backend.py 127.0.0.1 9002
```

Start the balancer in another terminal:

```bash
RUST_LOG=info cargo run --release -- config.toml
```

Then connect a test client to `127.0.0.1:8080`. For example, the echo client can send five connections:

```bash
python sdn/echo_client.py 127.0.0.1 8080 5 local-test
```

## Tests and results

Run the Rust checks with:

```bash
cargo fmt --check
cargo test --all-targets
cargo clippy --all-targets -- -D warnings
```

The captured run passed all 30 Rust tests, formatting, and Clippy. The Rust tests cover scheduler behavior, config validation, forwarding, health checks, connection limits, and cleanup.

For the Mininet tests, open Fedora WSL and run:

```bash
bash /mnt/c/Code/balance_this_load/sdn/capture_evidence_wsl.sh
```

The script syncs the Windows repo into `/home/doom/balance_this_load`, builds the release binary, runs the Rust checks and Mininet scripts, and saves logs plus a report under `sdn/evidence/` in the WSL copy. Mininet uses `sudo` for its network setup.

Latest captured Mininet results:

| Check | Result |
| --- | --- |
| `pingall` | 0% loss; 12 of 12 pings received. |
| Direct TCP echo | h1 reached h3 and h4; both echoed the test payload back. |
| Round robin baseline | 8 client connections split 4/4 between h3 and h4. |
| Round robin | 8 requests: h3 4, h4 4. |
| Weighted round robin, weights 2:1 | 12 requests: h3 8, h4 4. |
| Least connections | 8 requests: h3 4, h4 4. |
| IP hash | 8 requests from the same client IP all went to h3. |
| Random | 100 requests: h3 53, h4 47. |
| Health failure | With h4 down, the next 8 requests went to h3; h4 got 0. |
| Health recovery | After h4 restarted, it received new traffic again. |
| SDN blocking | Blocking h4 stopped new traffic from reaching it; unblocking restored traffic. |

The scheduler script also reports the time for one sequential batch. Those measurements are rough and the batch sizes differ, so they are not a fair benchmark of which algorithm is fastest. The useful result here is the distribution: weighted round robin matched the configured 2:1 weights, while IP hash kept one client IP on one backend.

The Mininet run was on Fedora 44 in WSL2 with Mininet 2.3.1, POX 0.7.0, Python 3.14.7, and the UserSwitch datapath. POX warned that Python 3.14 is outside its listed supported versions, but it started and the tests completed. The scripts use a non-default switch DPID and turn off TX/GSO/TSO offload on the host links because both settings were needed for the UserSwitch TCP tests in this setup.

## Things I have not added

- UDP support
- TLS termination
- HTTP path or host routing
- Backend connection pooling or retries
- Prometheus export or persistent metric storage
- More switches or automatic backend discovery in the SDN controller

Those are possible next steps, but they are not part of the current project.

## Project files

```text
Cargo.toml
config.toml
src/
  main.rs
  config.rs
  backend.rs
  registry.rs
  listener.rs
  proxy.rs
  server.rs
  connection.rs
  health.rs
  metrics.rs
  shutdown.rs
  sdn.rs
  scheduler/
    mod.rs
    algos.rs
tests/
  config.rs
  load_balancer.rs
sdn/
  topo_lb.py
  run_tests.py
  run_lb_matrix.py
  run_health_test.py
  run_sdn_test.py
  mininet_common.py
  capture_evidence.py
  capture_evidence_wsl.sh
  config_mininet.toml
  echo_backend.py
  echo_client.py
  controller/
    lb_controller.py
```
