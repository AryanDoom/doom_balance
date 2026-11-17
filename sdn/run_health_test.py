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

H3_COUNT = '/tmp/h3_health.count'
H4_COUNT = '/tmp/h4_health.count'
LB_LOG = '/tmp/lb_health.log'


def wait_for_log(host, log_file, needle, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        output = host.cmd(f'grep -F "{needle}" {log_file} || true')
        if output.strip():
            return True
        time.sleep(0.3)
    return False


def start_h4(hosts):
    start_echo_backend(hosts['h4'], '10.0.0.4', H4_COUNT, '/tmp/h4_health.log')


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
            hosts['h3'], '10.0.0.3', H3_COUNT, '/tmp/h3_health.log'
        )
        start_h4(hosts)

        config = write_lb_config(
            'health', algorithm='round_robin', health_enabled=True, health_interval=1
        )
        start_load_balancer(hosts['h2'], config, LB_LOG)

        run_client(hosts['h1'], 8, 'health-before')
        h3_before = accept_count(hosts['h3'], H3_COUNT)
        h4_before = accept_count(hosts['h4'], H4_COUNT)
        print(
            f'HEALTH_BASELINE h3={h3_before} h4={h4_before}'
        )
        if h3_before == 0 or h4_before == 0:
            raise RuntimeError(
                f'health baseline did not reach both backends: {h3_before}/{h4_before}'
            )

        hosts['h4'].cmd("pkill -f 'echo_backend.py 10.0.0.4' || true")
        if not wait_for_log(hosts['h2'], LB_LOG, 'backend is unhealthy'):
            raise RuntimeError('load balancer did not mark h4 unhealthy')

        h3_before_failure = accept_count(hosts['h3'], H3_COUNT)
        h4_before_failure = accept_count(hosts['h4'], H4_COUNT)
        run_client(hosts['h1'], 8, 'health-down')
        h3_down = accept_count(hosts['h3'], H3_COUNT)
        h4_down = accept_count(hosts['h4'], H4_COUNT)
        print(
            f'HEALTH_AFTER_FAILURE h3_added={h3_down - h3_before_failure} '
            f'h4_added={h4_down - h4_before_failure}'
        )
        if h4_down != h4_before_failure:
            raise RuntimeError('unhealthy h4 still received traffic')
        if h3_down - h3_before_failure < 8:
            raise RuntimeError('healthy h3 did not receive all failure-period traffic')

        start_h4(hosts)
        if not wait_for_log(hosts['h2'], LB_LOG, 'backend is healthy again'):
            raise RuntimeError('load balancer did not mark h4 healthy again')

        h4_before_recovery = accept_count(hosts['h4'], H4_COUNT)
        run_client(hosts['h1'], 8, 'health-recovered')
        h4_after_recovery = accept_count(hosts['h4'], H4_COUNT)
        print(
            f'HEALTH_AFTER_RECOVERY h4_added={h4_after_recovery - h4_before_recovery}'
        )
        if h4_after_recovery <= h4_before_recovery:
            raise RuntimeError('recovered h4 did not receive traffic again')

        print_log('LB_HEALTH_LOG_TAIL', hosts['h2'], LB_LOG)
        print('HEALTH_TEST_PASS')
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
