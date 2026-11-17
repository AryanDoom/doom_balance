use std::{
    hash::{Hash, Hasher},
    net::SocketAddr,
    sync::atomic::{AtomicUsize, Ordering},
};

//all the algorithms that will be in the scheduler are here and will be called from here

use rand::Rng;
use serde::Deserialize;

use super::Scheduler;
use crate::backend::Backend;

#[derive(Clone, Copy, Debug, Default, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Algorithm {
    //we have 5 as of now might add a few more like least reponse time and resource based schdeuling
    #[default]
    RoundRobin,
    WeightedRoundRobin,
    LeastConnections,
    IpHash,
    Random,
}

impl Algorithm {
    //stable names used by the config file and logs
    pub fn as_str(self) -> &'static str {
        match self {
            Algorithm::RoundRobin => "round_robin",
            Algorithm::WeightedRoundRobin => "weighted_round_robin",
            Algorithm::LeastConnections => "least_connections",
            Algorithm::IpHash => "ip_hash",
            Algorithm::Random => "random",
        }
    }
}

impl std::fmt::Display for Algorithm {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.as_str())
    }
}

#[derive(Debug)]
pub struct Algos {
    //this is to hold the next algo and the next index for say the rr wr and least connection
    selected_algorithm: Algorithm,
    next_round_robin_index: AtomicUsize,
    next_weight_position: AtomicUsize,
    next_least_connections_tie: AtomicUsize,
}

//main implementaion for the scheduler
impl Algos {
    pub fn new(selected_algorithm: Algorithm) -> Self {
        //alogs is the main struct with the info about the next alorithm adn teh values we shld have with it
        Self {
            selected_algorithm,
            next_round_robin_index: AtomicUsize::new(0),
            next_weight_position: AtomicUsize::new(0),
            next_least_connections_tie: AtomicUsize::new(0),
        }
    }

    //now all taht would happen if u were to select a backend and one of these algorithms
    fn select_round_robin(&self, backend_list: &[Backend]) -> Option<usize> {
        let mut healthy_backend_indexes = Vec::new(); //healthy backend list declaration

        for (backend_index, backend) in backend_list.iter().enumerate() {
            //going throught teh list of teh available backends and selecting on the healthy ones
            if backend.is_healthy {
                healthy_backend_indexes.push(backend_index);
            }
        }

        if healthy_backend_indexes.is_empty() {
            //gone  every backend got corona
            return None;
        }

        let current_backend_index = self.next_round_robin_index.fetch_add(1, Ordering::Relaxed); //increase the index for next server

        let selected_healthy_position = current_backend_index % healthy_backend_indexes.len();
        let selected_backend_index = healthy_backend_indexes.get(selected_healthy_position);

        selected_backend_index.copied() //returns the index of the selected backend from the list of avail backends
    }

    fn select_weighted_round_robin(&self, backend_list: &[Backend]) -> Option<usize> {
        let mut total_healthy_weight = 0;

        for backend in backend_list {
            if backend.is_healthy {
                total_healthy_weight += backend.weight;
            }
        }

        if total_healthy_weight == 0 {
            return None;
        }

        let current_weight_position =
            self.next_weight_position.fetch_add(1, Ordering::Relaxed) % total_healthy_weight;

        let mut remaining_weight_position = current_weight_position;

        for (backend_index, backend) in backend_list.iter().enumerate() {
            if !backend.is_healthy {
                continue;
            }

            if remaining_weight_position < backend.weight {
                return Some(backend_index);
            }

            remaining_weight_position -= backend.weight;
        }

        None
    }

    fn select_least_connections(&self, backend_list: &[Backend]) -> Option<usize> {
        let mut least_active_connections = usize::MAX;
        let mut least_busy_backend_indexes = Vec::new();

        for (backend_index, backend) in backend_list.iter().enumerate() {
            if !backend.is_healthy {
                continue;
            }

            let active_connection_count = backend.active_connection_count();

            //the comparison logic
            if active_connection_count < least_active_connections {
                least_active_connections = active_connection_count;
                least_busy_backend_indexes.clear();
                least_busy_backend_indexes.push(backend_index);
            } else if active_connection_count == least_active_connections {
                least_busy_backend_indexes.push(backend_index);
            }
        }

        if least_busy_backend_indexes.is_empty() {
            return None;
        }

        //tie breaker if many servers wuth same number of connections
        let tie_number = self
            .next_least_connections_tie
            .fetch_add(1, Ordering::Relaxed);
        let selected_tie_position = tie_number % least_busy_backend_indexes.len();
        //return the index of the selected backend from the list of least busy backends
        least_busy_backend_indexes
            .get(selected_tie_position)
            .copied()
    }

    //ip hash basically same ip goes to teh same server unless its down and shi
    fn select_ip_hash(
        &self,
        backend_list: &[Backend],
        client_address: SocketAddr,
    ) -> Option<usize> {
        let mut healthy_backend_indexes = Vec::new();

        for (backend_index, backend) in backend_list.iter().enumerate() {
            if backend.is_healthy {
                healthy_backend_indexes.push(backend_index);
            }
        }

        if healthy_backend_indexes.is_empty() {
            return None;
        }

        let client_ip_address = client_address.ip();
        let mut address_hasher = std::collections::hash_map::DefaultHasher::new();
        client_ip_address.hash(&mut address_hasher);

        let hash_value = address_hasher.finish();
        let selected_healthy_position = hash_value as usize % healthy_backend_indexes.len();

        healthy_backend_indexes
            .get(selected_healthy_position)
            .copied()
    }
    //tis is the basicest thing ever
    fn select_random(&self, backend_list: &[Backend]) -> Option<usize> {
        let mut healthy_backend_indexes = Vec::new();

        for (backend_index, backend) in backend_list.iter().enumerate() {
            if backend.is_healthy {
                healthy_backend_indexes.push(backend_index);
            }
        }

        if healthy_backend_indexes.is_empty() {
            return None;
        }

        let mut random_generator = rand::thread_rng();
        let selected_healthy_position =
            random_generator.gen_range(0..healthy_backend_indexes.len());

        healthy_backend_indexes
            .get(selected_healthy_position)
            .copied()
    }
}
//all the function calls
impl Scheduler for Algos {
    fn select(&self, backend_list: &[Backend], client_address: SocketAddr) -> Option<usize> {
        match self.selected_algorithm {
            Algorithm::RoundRobin => self.select_round_robin(backend_list),
            Algorithm::WeightedRoundRobin => self.select_weighted_round_robin(backend_list),
            Algorithm::LeastConnections => self.select_least_connections(backend_list),
            Algorithm::IpHash => self.select_ip_hash(backend_list, client_address),
            Algorithm::Random => self.select_random(backend_list),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::{IpAddr, Ipv4Addr};

    fn backend(backend_id: &str, port: u16) -> Backend {
        Backend::new(
            backend_id,
            SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), port),
        )
    }

    fn test_client_address() -> SocketAddr {
        SocketAddr::new(IpAddr::V4(Ipv4Addr::new(127, 0, 0, 2)), 50000)
    }

    #[test]
    fn round_robin_cycles_through_healthy_backends() {
        let algos = Algos::new(Algorithm::RoundRobin);
        let backends = vec![backend("a", 9001), backend("b", 9002)];

        let picked: Vec<usize> = (0..4)
            .map(|_| algos.select(&backends, test_client_address()).unwrap())
            .collect();

        assert_eq!(picked, vec![0, 1, 0, 1]);
    }

    #[test]
    fn round_robin_skips_unhealthy_backends() {
        let algos = Algos::new(Algorithm::RoundRobin);
        let mut backends = vec![backend("a", 9001), backend("b", 9002), backend("c", 9003)];
        backends[0].is_healthy = false;
        backends[2].is_healthy = false;

        let picked: Vec<usize> = (0..3)
            .map(|_| algos.select(&backends, test_client_address()).unwrap())
            .collect();

        assert_eq!(picked, vec![1, 1, 1]);
    }

    #[test]
    fn schedulers_return_none_when_no_backend_is_healthy() {
        let mut backends = vec![backend("a", 9001), backend("b", 9002)];
        backends[0].is_healthy = false;
        backends[1].is_healthy = false;

        for algorithm in [
            Algorithm::RoundRobin,
            Algorithm::WeightedRoundRobin,
            Algorithm::LeastConnections,
            Algorithm::IpHash,
            Algorithm::Random,
        ] {
            let algos = Algos::new(algorithm);
            assert_eq!(algos.select(&backends, test_client_address()), None);
        }
    }

    #[test]
    fn weighted_round_robin_respects_weights() {
        let algos = Algos::new(Algorithm::WeightedRoundRobin);
        let backends = vec![
            Backend::new_with_weight("a", SocketAddr::from(([127, 0, 0, 1], 9001)), 2).unwrap(),
            Backend::new_with_weight("b", SocketAddr::from(([127, 0, 0, 1], 9002)), 1).unwrap(),
        ];

        let picked: Vec<usize> = (0..6)
            .map(|_| algos.select(&backends, test_client_address()).unwrap())
            .collect();

        assert_eq!(picked, vec![0, 0, 1, 0, 0, 1]);
    }

    #[test]
    fn weighted_round_robin_ignores_unhealthy_weight() {
        let algos = Algos::new(Algorithm::WeightedRoundRobin);
        let mut backends = vec![
            Backend::new_with_weight("a", SocketAddr::from(([127, 0, 0, 1], 9001)), 5).unwrap(),
            Backend::new_with_weight("b", SocketAddr::from(([127, 0, 0, 1], 9002)), 1).unwrap(),
        ];
        backends[0].is_healthy = false;

        for _ in 0..5 {
            assert_eq!(algos.select(&backends, test_client_address()), Some(1));
        }
    }

    #[test]
    fn least_connections_picks_the_least_busy_backend() {
        let algos = Algos::new(Algorithm::LeastConnections);
        let backends = vec![backend("a", 9001), backend("b", 9002)];

        let _one_on_a = backends[0].start_connection();
        let _one_on_b = backends[1].start_connection();
        let _two_on_b = backends[1].start_connection();

        assert_eq!(algos.select(&backends, test_client_address()), Some(0));
    }

    #[test]
    fn least_connections_rotates_between_tied_backends() {
        let algos = Algos::new(Algorithm::LeastConnections);
        let backends = vec![backend("a", 9001), backend("b", 9002)];

        let first = algos.select(&backends, test_client_address()).unwrap();
        let second = algos.select(&backends, test_client_address()).unwrap();

        assert_ne!(first, second);
    }

    #[test]
    fn ip_hash_is_stable_for_the_same_client_ip() {
        let algos = Algos::new(Algorithm::IpHash);
        let backends = vec![backend("a", 9001), backend("b", 9002), backend("c", 9003)];

        let first = algos.select(&backends, test_client_address()).unwrap();

        for _ in 0..10 {
            assert_eq!(
                algos.select(&backends, test_client_address()).unwrap(),
                first
            );
        }
    }

    #[test]
    fn random_only_returns_healthy_backends() {
        let algos = Algos::new(Algorithm::Random);
        let mut backends = vec![backend("a", 9001), backend("b", 9002), backend("c", 9003)];
        backends[1].is_healthy = false;

        for _ in 0..50 {
            assert_ne!(algos.select(&backends, test_client_address()).unwrap(), 1);
        }
    }

    #[test]
    fn active_connection_count_returns_to_zero_when_guard_drops() {
        let backend = backend("a", 9001);

        {
            let _connection = backend.start_connection();
            assert_eq!(backend.active_connection_count(), 1);
        }

        assert_eq!(backend.active_connection_count(), 0);
    }

    #[test]
    fn least_connections_skips_unhealthy_backends() {
        let algos = Algos::new(Algorithm::LeastConnections);
        let mut backends = vec![backend("busy-but-healthy", 9001), backend("down", 9002)];
        let _busy_connection = backends[0].start_connection();
        backends[1].is_healthy = false;

        assert_eq!(algos.select(&backends, test_client_address()), Some(0));
    }

    #[test]
    fn ip_hash_skips_unhealthy_backends() {
        let algos = Algos::new(Algorithm::IpHash);
        let mut backends = vec![backend("a", 9001), backend("b", 9002)];

        for healthy_index in 0..backends.len() {
            for (backend_index, backend) in backends.iter_mut().enumerate() {
                backend.is_healthy = backend_index == healthy_index;
            }

            assert_eq!(
                algos.select(&backends, test_client_address()),
                Some(healthy_index)
            );
        }
    }
}
