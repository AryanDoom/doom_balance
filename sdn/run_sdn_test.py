import os
import socket
import subprocess
import time

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
    wait_for_listener,
    write_lb_config,
)

CONTROLLER_SOCKET = '/tmp/balance_this_load_sdn.sock'
CONTROL_SOCKET = '/tmp/balance_this_load_control.sock'
H3_COUNT = '/tmp/h3_sdn.count'
H4_COUNT = '/tmp/h4_sdn.count'


def wait_for_unix_socket(path, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if os.path.exists(path):
            return True
        time.sleep(0.2)
    return False


def socket_command(path, command):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(path)
        client.sendall((command + '\n').encode())
        data = b''
        while b'\n' not in data:
            chunk = client.recv(1024)
            if not chunk:
                break
            data += chunk
        return data.decode(errors='replace').strip()


def expect_failure(host, target, port, tag):
    try:
        run_client(host, 1, tag, target=target, port=port)
    except RuntimeError:
        print('EXPECTED_FAILURE_PASS')
        return
    raise RuntimeError(f'expected connection to {target}:{port} to fail')


def main():
    set_mininet_log_level()
    subprocess.run(['pkill', '-f', 'pox.py'], check=False)
    time.sleep(1)
    for path in (CONTROLLER_SOCKET, CONTROL_SOCKET, H3_COUNT, H4_COUNT):
        if os.path.exists(path):
            os.remove(path)

    pox_process = None
    pox_log_file = None
    pox_log_path = None
    net = None
    hosts = None

    try:
        pox_process, pox_log_file, pox_log_path = start_pox(
            app='lb_controller',
            extra_args=[
                '--dpid=16',
                f'--control_socket={CONTROLLER_SOCKET}',
                '--h1_port=1',
                '--h2_port=2',
                '--h3_port=3',
                '--h4_port=4',
            ],
        )
        net, switch, hosts = build_net()
        if not wait_for_unix_socket(CONTROLLER_SOCKET, 10):
            raise RuntimeError('custom POX controller did not open its control socket')
        print('SWITCH_DPID:', switch.dpid)

        start_echo_backend(hosts['h3'], '10.0.0.3', H3_COUNT, '/tmp/h3_sdn.log')
        start_echo_backend(hosts['h4'], '10.0.0.4', H4_COUNT, '/tmp/h4_sdn.log')

        response = socket_command(CONTROLLER_SOCKET, 'BLOCK 10.0.0.4')
        print('CONTROLLER_BLOCK:', response)
        expect_failure(hosts['h1'], '10.0.0.4', 9000, 'controller-blocked')
        response = socket_command(CONTROLLER_SOCKET, 'UNBLOCK 10.0.0.4')
        print('CONTROLLER_UNBLOCK:', response)
        run_client(hosts['h1'], 1, 'controller-unblocked', target='10.0.0.4', port=9000)
        print('DIRECT_CONTROLLER_BLOCK_PASS')

        config = write_lb_config(
            'sdn',
            health_enabled=True,
            health_interval=1,
            sdn_controller_socket=CONTROLLER_SOCKET,
            sdn_control_listen=CONTROL_SOCKET,
        )
        if os.path.exists(CONTROL_SOCKET):
            raise RuntimeError('stale Rust control socket was not removed')

        start_load_balancer(hosts['h2'], config, '/tmp/lb_sdn.log')
        if not wait_for_unix_socket(CONTROL_SOCKET, 10):
            raise RuntimeError('Rust control socket did not open')

        run_client(hosts['h1'], 8, 'sdn-baseline')
        h3_before = accept_count(hosts['h3'], H3_COUNT)
        h4_before = accept_count(hosts['h4'], H4_COUNT)
        if h3_before == 0 or h4_before == 0:
            raise RuntimeError(f'baseline did not reach both backends: {h3_before}/{h4_before}')
        print(f'SDN_BASELINE h3={h3_before} h4={h4_before}')

        hosts['h4'].cmd("pkill -f 'echo_backend.py 10.0.0.4' || true")
        time.sleep(3)
        h4_after_kill = accept_count(hosts['h4'], H4_COUNT)

        response = socket_command(CONTROL_SOCKET, 'BLOCK backend-h4')
        print('RUST_BLOCK:', response)
        status = socket_command(CONTROL_SOCKET, 'STATUS')
        print('RUST_STATUS:', status)
        if '10.0.0.4' not in status:
            raise RuntimeError(f'controller did not report blocked backend: {status}')

        run_client(hosts['h1'], 8, 'sdn-blocked')
        h3_blocked = accept_count(hosts['h3'], H3_COUNT)
        h4_blocked = accept_count(hosts['h4'], H4_COUNT)
        if h3_blocked - h3_before < 8:
            raise RuntimeError('h3 did not receive traffic while h4 was blocked')
        if h4_blocked != h4_after_kill:
            raise RuntimeError('blocked h4 still received connections')
        print(f'SDN_BLOCKED h3_added={h3_blocked - h3_before} h4_added={h4_blocked - h4_after_kill}')

        start_echo_backend(hosts['h4'], '10.0.0.4', H4_COUNT, '/tmp/h4_sdn.log')
        h4_restarted = accept_count(hosts['h4'], H4_COUNT)
        response = socket_command(CONTROL_SOCKET, 'UNBLOCK backend-h4')
        print('RUST_UNBLOCK:', response)
        time.sleep(3)

        run_client(hosts['h1'], 8, 'sdn-recovered')
        h4_recovered = accept_count(hosts['h4'], H4_COUNT)
        if h4_recovered <= h4_restarted:
            raise RuntimeError('h4 did not receive traffic after recovery')
        print(f'SDN_RECOVERED h4_added={h4_recovered - h4_restarted}')

        print_log('LB_SDN_LOG_TAIL', hosts['h2'], '/tmp/lb_sdn.log')
        print_log('POX_LOG_TAIL', hosts['h2'], pox_log_path)
        print('SDN_TEST_PASS')
    finally:
        if hosts is not None:
            stop_host_processes(hosts)
        if net is not None:
            net.stop()
        if pox_process is not None and pox_log_file is not None:
            stop_pox(pox_process, pox_log_file)
        for path in (CONTROLLER_SOCKET, CONTROL_SOCKET):
            if os.path.exists(path):
                os.remove(path)


if __name__ == '__main__':
    main()
