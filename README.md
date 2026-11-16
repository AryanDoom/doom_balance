# balance_this_load

A small TCP load balancer written in Rust as a learning project. Clients connect to one listening port, the load balancer picks a backend application server, and bytes are forwarded in both directions without being parsed or modified.

The point of the project is to build each part ourselves — listener, backend registry, schedulers, health checks, proxy loop, metrics, shutdown — instead of configuring an existing tool such as NGINX.

## Scope

- **Transport:** TCP only. The load balancer is protocol-agnostic: HTTP, Redis, a database protocol, or a custom echo protocol all work because payloads are never inspected.
- **UDP:** not implemented. UDP is a scope question for the mentor/reviewer. It is not a flag on this code: datagrams have no `accept`/connection, so scheduling, affinity, and idle expiry would need a separate design.
- **Mininet and SDN:** explicitly deferred to a later phase (see [Review deliverables](#review-deliverables)).

## Network architecture

```
   clients                              balance_this_load                          backends
+-----------+   TCP     +------------------------------------------+   TCP     +-------------------+
| curl /    | --------> | ClientListener::accept_client            | --------> | 127.0.0.1:9001    |
| test app  |           |            |                             |           | application 1     |
+-----------+           |            v                             |           +-------------------+
                        | Scheduler::select (healthy backends)     |
                        |            |                             |   TCP     +-------------------+
                        |            v                             | --------> | 127.0.0.1:9002    |
                        | proxy::copy_bidirectional                |           | application 2     |
                        +------------------------------------------+           +-------------------+
                              ^                        ^
                              |                        |
                   health::run (every 5s)      metrics reporter (every 15s)
                   TCP connect probes          counters + tracing logs
```

One listener accepts clients. For every accepted connection the scheduler returns a backend index, and a dedicated Tokio task connects to that backend and copies bytes in both directions. Health checks and metrics run as separate background tasks and share state through `Arc<RwLock<BackendRegistry>>` and atomic counters.

## Components

| Module | Responsibility |
| --- | --- |
| `src/main.rs` | Loads the TOML config, initializes logging, wires registry/scheduler/listener/metrics/health/shutdown together. |
| `src/config.rs` | TOML schema, file loading, defaults, validation (unique ids, non-empty backends, sane limits). |
| `src/backend.rs` | Backend identity, address, weight, health flag, and an RAII guard for active connection counting. |
| `src/registry.rs` | Owns the backend list; `SharedRegistry = Arc<RwLock<BackendRegistry>>` for accept-loop reads and health-check writes. |
| `src/scheduler/mod.rs` | `Scheduler` trait: `select(&[Backend], client_address) -> Option<usize>`. |
| `src/scheduler/algos.rs` | Round robin, weighted round robin, least connections, IP hash, random; unit tests. |
| `src/listener.rs` | Binds and accepts TCP client connections. |
| `src/proxy.rs` | Opens the backend TCP connection and runs `copy_bidirectional`, returning byte counts. |
| `src/server.rs` | Accept loop, per-client tasks, connection limit, graceful drain on shutdown, connection accounting. |
| `src/health.rs` | Background TCP connect probes; flips `is_healthy` and logs transitions. |
| `src/metrics.rs` | Atomic counters, snapshots, periodic metrics logging. |
| `src/shutdown.rs` | `watch` channel plus ctrl-c handling; one shared stop flag. |
| `tests/config.rs` | Config parsing, defaults, and rejection cases. |
| `tests/load_balancer.rs` | End-to-end traffic, distribution, health, limit, and cleanup tests with real TCP echo backends. |

## Protocols and socket behavior

- **Client to load balancer:** plain TCP. The client connects to the configured `listen` address.
- **Load balancer to backend:** one fresh TCP connection per client connection (no pooling). On connect failure the client is dropped, the failure is counted in `failed`, and the accept loop keeps running.
- **Data forwarding:** `tokio::io::copy_bidirectional` copies both directions concurrently and handles TCP half-closes. Bytes are never decoded, so protocol-level routing is not possible by design.
- **Health checks:** every interval the balancer attempts a TCP connect with a timeout. Success means healthy, failure means unhealthy. This checks reachability, not application-level readiness (no HTTP status or query probe).
- **Selection:** schedulers only consider backends with `is_healthy == true`. Existing connections are unaffected when a backend becomes unhealthy; only new selections change.
- **Graceful shutdown:** ctrl-c flips a `watch` flag. The accept loop stops, active connections get up to 10 seconds to finish, then remaining tasks are aborted.
- **Limits:** when `limits.max_connections` is set, a client accepted while `active >= limit` is dropped immediately and counted in `rejected`.

## Scheduling algorithms

| Config value | Behavior |
| --- | --- |
| `round_robin` | Cycles through healthy backends in order. |
| `weighted_round_robin` | Repeats each healthy backend according to its `weight`. |
| `least_connections` | Picks the healthy backend with the fewest active connections; ties rotate. |
| `ip_hash` | Hashes the client IP so the same client IP sticks to the same healthy backend. |
| `random` | Picks a random healthy backend. |

Weights are only used by `weighted_round_robin`.

## Configuration

The binary takes an optional config path (default: `config.toml`):

```bash
cargo run -- config.toml
```

Example (`config.toml` in the repo root):

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

| Key | Default | Meaning |
| --- | --- | --- |
| `listen` | required | Address the load balancer binds. |
| `algorithm` | `round_robin` | One of the five scheduler names. |
| `health_check.enabled` | `true` | Enable background TCP probes. |
| `health_check.interval_secs` | `5` | Seconds between probe rounds (must be >= 1). |
| `health_check.timeout_secs` | `1` | Per-probe connect timeout (must be >= 1). |
| `limits.max_connections` | unset | Maximum simultaneous proxied connections; omitted means unlimited. |
| `backends[].id` | required | Unique backend name (also used in logs). |
| `backends[].address` | required | Backend `ip:port`. |
| `backends[].weight` | `1` | Weight for weighted round robin (must be >= 1). |

## Running locally

```bash
# 1. Build
cargo build --release

# 2. Start two simple backends (any TCP service works; HTTP is convenient)
python -m http.server 9001
python -m http.server 9002     # in a second terminal

# 3. Start the load balancer
cargo run --release -- config.toml

# 4. Send requests; round robin should alternate backends
curl http://127.0.0.1:8080/
curl http://127.0.0.1:8080/
```

Logging uses `tracing` with an environment filter:

```bash
RUST_LOG=debug cargo run -- config.toml
```

At startup the balancer logs its listen address, algorithm, backend count, limit, and health-check state. Every 15 seconds it logs a metrics line; a final metrics line is written at shutdown.

## Testing

Automated tests:

```bash
cargo test
cargo clippy --all-targets
cargo fmt --check
```

Current suite (24 tests):

| Area | Test coverage |
| --- | --- |
| Schedulers | Round-robin order and unhealthy skipping, weighted distribution and unhealthy weights, least-connections choice and tie rotation, IP-hash stability, random healthy-only, all-down returns `None`. |
| Config | Full parse, defaults, empty backend list, duplicate ids, zero weight, unknown algorithm, zero health settings, zero connection limit, file loading. |
| Traffic path | Real echo backends: byte forwarding and metrics, round-robin split, weighted split, unhealthy backend skipped, health probe marks a dead backend down. |
| Connection control | Limit rejection counted and first connection preserved; active count returns to zero after clients close. |

Manual / exploratory checks:

1. Kill a backend while traffic is flowing: existing connections fail, health checks mark it unhealthy within the interval, new connections avoid it, and the logs show the transition.
2. Start with a backend address where nothing is listening: startup still works, that backend is skipped once marked unhealthy.
3. Set `max_connections = 1` and open a long-lived connection: the next client is dropped and `rejected` increases.
4. Send SIGINT / ctrl-c: listener stops, active connections drain, final metrics are logged.
5. Compare schedulers by running the same request pattern under each algorithm and watching per-backend hit counts.

## Evaluation and testing plan

For the review, evaluate in four layers:

| Layer | What to measure | Suggested method |
| --- | --- | --- |
| Functional correctness | Requests reach a backend unchanged; chosen backend matches the algorithm; health and limits behave as configured. | `cargo test` plus the manual checks above. |
| Traffic distribution | Per-backend request/connection share under each scheduler; weight ratios for weighted round robin. | Count backend accept logs while sending N requests/connections; compare against expected shares. |
| Failure behavior | Time to mark a dead backend unhealthy, new-connection avoidance, existing-connection errors, recovery when it returns. | Kill/restart a backend during a traffic run; inspect health transition logs and metrics. |
| Load and limits | Throughput, active connection count, rejection behavior at the limit, memory/task stability. | `wrk -t4 -c100 -d30s http://127.0.0.1:8080/` or `ab`; `iperf3` for raw TCP; watch metrics lines. |

Acceptance criteria used in this repo:

- Traffic is byte-for-byte unchanged in both directions.
- No healthy backend is ever selected; no unhealthy backend receives new connections.
- `accepted = successful + failed + still active` stays consistent, and `active` returns to zero after all clients disconnect.
- A failing backend or client never stops the accept loop.
- The balancer shuts down on ctrl-c without corrupting counters, draining or aborting connections within a bounded time.

For a later network-focused evaluation, the same balancer can sit between Mininet hosts, with SDN rules used to block or redirect paths and logs/metrics compared against the baseline.

## Review deliverables

| Deliverable | Status | Location |
| --- | --- | --- |
| Network architecture | Done | diagram and data-path text above |
| Component explanations | Done | Components table and module docs |
| Protocol/socket explanations | Done | Protocols and socket behavior section |
| TCP load balancer implementation | Done | `src/`, runnable with `config.toml` |
| Evaluation and testing plan | Done | sections above |
| Viva preparation | Done | Q&A below |
| Mininet implementation | Deferred | later phase |
| SDN implementation | Deferred | later phase |
| Socket–SDN integration | Deferred | later phase |
| UDP socket support | Open scope question | TCP-only today; discuss with mentor |

## Viva preparation

**Why a TCP proxy instead of an HTTP reverse proxy?**
It is protocol-agnostic and shows the socket-level mechanics: accept, backend connect, bidirectional copy, half-close, limits, and health. HTTP routing would require parsing requests and would hide those fundamentals.

**How does a backend get selected?**
The `Scheduler` trait receives the backend list and client address and returns an index. Algorithms filter to healthy backends first; `server.rs` reserves an active-connection slot and spawns the proxy task.

**Why one Tokio task per client?**
The accept loop must stay free to accept new clients. Each task owns its streams and guard, so slow clients cannot block others.

**What happens when a backend dies?**
An in-flight connection fails and is counted as `failed`. Background probes mark the backend unhealthy, so new selections avoid it. When probes succeed again, it is marked healthy and receives new connections.

**How are active connections counted accurately?**
`Backend::start_connection` increments an `AtomicUsize` and returns an RAII guard; dropping the guard at task end decrements it. Least connections and metrics both read the same counter.

**How does least connections break ties?**
It collects all healthy backends with the minimum count and rotates through them with an atomic counter, so equal-load backends are used evenly.

**How does IP hash work?**
It hashes only the client IP (not the port) with `DefaultHasher` and maps the hash into the healthy backend list. The same client IP is sticky while the healthy set is unchanged; adding/removing a backend can remap it.

**What is half-close and how is it handled?**
TCP FIN can close one direction while the other stays open. `copy_bidirectional` propagates EOF per direction, so protocols that half-close still work.

**How is the connection limit enforced?**
Before proxying, the accept loop compares `metrics.active_connections()` with `max_connections`; at the limit it drops the accepted socket and increments `rejected`. There is no queueing.

**How does graceful shutdown work?**
A `watch` channel carries a stop flag; ctrl-c sets it. The accept loop is biased toward shutdown, stops accepting, gives active tasks 10 seconds, then aborts the rest and logs final metrics.

**Why `RwLock` around the registry?**
The accept loop only reads the backend list, while health checks write the health flag. `RwLock` allows concurrent reads and short exclusive writes; the lock is never held across an await.

**Why are metrics atomic?**
Every client task and the health task update counters concurrently. Atomics avoid a global mutex on the hot path; snapshots are eventually consistent.

**Why is the health check only a TCP connect?**
It is cheap, works for any TCP service, and answers "is something accepting connections?" It does not prove the application is ready to serve; an HTTP or protocol-specific probe would be the next step.

**How would UDP change the design?**
There is no accept or per-connection stream. A UDP balancer would receive datagrams, choose a backend per packet or per flow (with an affinity table and idle timeout), and send datagrams back to the client. That is why it is a separate design decision, not a config flag.

**What is intentionally missing?**
UDP, TLS termination, HTTP host/path routing, backend connection pooling, retries, weighted least connections, persistence of metrics, and Mininet/SDN integration.

## Limitations and next steps

- TCP connect probes cannot detect an application that accepts connections but returns errors.
- Metrics are logged, not exported via Prometheus or an endpoint yet.
- No retry means a connection to a just-died backend fails rather than trying another backend.
- The IP-hash mapping is not consistent hashing, so a healthy-set change remaps many clients.
- Next code milestone: configurable shutdown grace, structured log levels per module, and an HTTP-level mode or benchmark harness.
- Next review milestone: Mininet topology, SDN baseline rules, and socket–SDN integration (deferred).

## Project layout

```
Cargo.toml
config.toml
src/
  main.rs
  lib.rs
  config.rs
  backend.rs
  registry.rs
  listener.rs
  proxy.rs
  server.rs
  health.rs
  metrics.rs
  shutdown.rs
  scheduler/
    mod.rs
    algos.rs
tests/
  config.rs
  load_balancer.rs
```
