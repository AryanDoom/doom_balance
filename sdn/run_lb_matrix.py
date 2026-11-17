import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mininet_common import (
    accept_count,
    build_net,
    print_log,
    run_client,
    set_mininet_log_level,
    start_echo_backend,
    start_load_balancer,
    start_pox,
    stop_host_processes,
    stop_pox,
    write_lb_config,
)


def report(name, algorithm, requests, h3, h4, expectation, duration_seconds):
    print(
        f'RESULT {name} algorithm={algorithm} requests={requests} '
        f'h3={h3} h4={h4} h3_share={h3 / requests:.2f} '
        f'h4_share={h4 / requests:.2f} expectation={expectation} '
        f'duration_s={duration_seconds:.3f} '
        f'requests_per_second={requests / duration_seconds:.2f}'
    )


def main():
    set_mininet_log_level()
    pox = None
    net = None
    hosts = {}

    try:
        pox = start_pox()
        net, switch, hosts = build_net()
        print('SWITCH_DPID:', switch.dpid)

        start_echo_backend(
            hosts['h3'], '10.0.0.3', '/tmp/h3_matrix.count', '/tmp/h3_matrix.log'
        )
        start_echo_backend(
            hosts['h4'], '10.0.0.4', '/tmp/h4_matrix.count', '/tmp/h4_matrix.log'
        )

        run_client(hosts['h1'], 1, 'direct-h3', target='10.0.0.3', port=9000)
        run_client(hosts['h1'], 1, 'direct-h4', target='10.0.0.4', port=9000)
        run_client(hosts['h2'], 1, 'direct-h2-h3', target='10.0.0.3', port=9000)
        run_client(hosts['h2'], 1, 'direct-h2-h4', target='10.0.0.4', port=9000)
        print('DIRECT_TCP_PASS')

        cases = [
            ('round_robin', 'round_robin', 8, (1, 1), '4/4'),
            ('weighted_round_robin', 'weighted_round_robin', 12, (2, 1), '8/4'),
            ('least_connections', 'least_connections', 8, (1, 1), '4/4'),
            ('ip_hash', 'ip_hash', 8, (1, 1), 'all to one backend'),
            ('random', 'random', 100, (1, 1), 'non-exact distribution'),
        ]

        for name, algorithm, requests, weights, expectation in cases:
            h3_count_file = f'/tmp/h3_{name}.count'
            h4_count_file = f'/tmp/h4_{name}.count'
            hosts['h3'].cmd('pkill -f echo_backend.py || true')
            hosts['h4'].cmd('pkill -f echo_backend.py || true')
            time.sleep(0.3)
            start_echo_backend(hosts['h3'], '10.0.0.3', h3_count_file, f'/tmp/h3_{name}.log')
            start_echo_backend(hosts['h4'], '10.0.0.4', h4_count_file, f'/tmp/h4_{name}.log')

            config = write_lb_config(
                name, algorithm=algorithm, health_enabled=False, weights=weights
            )
            start_load_balancer(hosts['h2'], config, f'/tmp/lb_{name}.log')
            started_at = time.perf_counter()
            try:
                run_client(hosts['h1'], requests, name)
            except RuntimeError:
                print_log(f'LB_{name.upper()}_FAILED_LOG', hosts['h2'], f'/tmp/lb_{name}.log')
                print('LB_FAILED_LISTENER:')
                print(hosts['h2'].cmd('ss -ltnp | grep 8080 || echo none'))
                print('LB_FAILED_PROCESSES:')
                print(hosts['h2'].cmd("ps -ef | grep '[b]alance_this_load' || echo none"))
                raise
            duration_seconds = time.perf_counter() - started_at

            h3 = accept_count(hosts['h3'], h3_count_file)
            h4 = accept_count(hosts['h4'], h4_count_file)
            report(name, algorithm, requests, h3, h4, expectation, duration_seconds)
            print_log(f'LB_{name.upper()}_LOG_TAIL', hosts['h2'], f'/tmp/lb_{name}.log')

            if h3 + h4 != requests:
                raise RuntimeError(
                    f'{name}: backend counts sum to {h3 + h4}, expected {requests}'
                )
            if name in ('round_robin', 'least_connections'):
                if (h3, h4) != (4, 4):
                    raise RuntimeError(f'{name}: expected 4/4, got {h3}/{h4}')
            if name == 'weighted_round_robin' and (h3, h4) != (8, 4):
                raise RuntimeError(f'{name}: expected 8/4, got {h3}/{h4}')
            if name == 'ip_hash' and not (
                (h3, h4) == (requests, 0) or (h3, h4) == (0, requests)
            ):
                raise RuntimeError(f'{name}: expected all traffic on one backend')
            if name == 'random' and (h3 == 0 or h4 == 0):
                raise RuntimeError(f'{name}: expected both backends to be selected')

        print_log('LB_RANDOM_LOG_TAIL', hosts['h2'], '/tmp/lb_random.log')
        print('SCHEDULER_MATRIX_PASS')
    finally:
        if hosts:
            stop_host_processes(hosts)
        if net is not None:
            net.stop()
        if pox is not None:
            process, log_file, log_path = pox
            stop_pox(process, log_file)
            print('POX_LOG_TAIL:')
            print(subprocess.getoutput(f'tail -n 30 {log_path} 2>&1'))


if __name__ == '__main__':
    main()
