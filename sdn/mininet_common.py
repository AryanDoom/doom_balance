import os
import re
import socket
import subprocess
import time

from mininet.net import Mininet
from mininet.node import RemoteController, UserSwitch
from mininet.link import TCLink
from mininet.log import setLogLevel, info

HOME = '/home/doom'
LB = f'{HOME}/balance_this_load/target/release/balance_this_load'
ECHO = f'{HOME}/balance_this_load/sdn/echo_backend.py'
CLIENT = f'{HOME}/balance_this_load/sdn/echo_client.py'
POX = f'{HOME}/pox/pox.py'

HOST_IPS = {
    'h1': '10.0.0.1/24',
    'h2': '10.0.0.2/24',
    'h3': '10.0.0.3/24',
    'h4': '10.0.0.4/24',
}


def set_mininet_log_level():
    setLogLevel('info')


def disable_offload(net):
    for host in net.hosts:
        host.cmd(f'ethtool -K {host.defaultIntf()} tx off gso off tso off')


def wait_port(host, port, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), 1):
                return True
        except Exception:
            time.sleep(0.2)
    return False


def start_pox(app='forwarding.l2_learning', extra_args=None):
    log_path = '/tmp/pox_lb.log'
    log_file = open(log_path, 'w')
    command = [
        'python3', POX,
        'log.level', '--INFO',
        'openflow.of_01', '--port=6653',
        app,
    ]
    if extra_args:
        command.extend(extra_args)
    process = subprocess.Popen(
        command,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=f'{HOME}/pox',
    )
    ready = wait_port('127.0.0.1', 6653, 10)
    print('POX_READY:', ready)
    if not ready:
        stop_pox(process, log_file)
        raise RuntimeError('POX did not open port 6653')
    return process, log_file, log_path


def stop_pox(process, log_file):
    process.terminate()
    try:
        process.wait(timeout=5)
    except Exception:
        process.kill()
    log_file.close()


def build_net():
    net = Mininet(controller=None, switch=UserSwitch, link=TCLink, autoSetMacs=True)
    controller = net.addController(
        'c0', controller=RemoteController, ip='127.0.0.1', port=6653
    )
    switch = net.addSwitch('s1', dpid='000000000010')
    hosts = {}
    for name, ip in HOST_IPS.items():
        hosts[name] = net.addHost(name, ip=ip)
    for host in hosts.values():
        net.addLink(host, switch)
    net.build()
    disable_offload(net)
    controller.start()
    switch.start([controller])
    time.sleep(2)
    return net, switch, hosts


def wait_for_listener(host, port, bind_ip='127.0.0.1', timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        result = host.cmd(f'ss -ltn | grep -c ":{port} " || true').strip()
        if result and int(result.split()[0]) > 0:
            return True
        time.sleep(0.2)
    return False


def wait_for_listener_to_close(host, port, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        result = host.cmd(f'ss -ltn | grep -c ":{port} " || true').strip()
        if not result or int(result.split()[0]) == 0:
            return True
        time.sleep(0.2)
    return False


def start_echo_backend(host, bind_ip, count_file, log_file):
    host.cmd(f'rm -f {count_file} {log_file}')
    host.cmd(
        f'python3 {ECHO} {bind_ip} 9000 {count_file} > {log_file} 2>&1 &'
    )
    if not wait_for_listener(host, 9000, bind_ip):
        print(f'ECHO_DEBUG {host.name} PROCS:')
        print(host.cmd('ps aux | grep echo_backend.py | grep -v grep || echo none'))
        print(f'ECHO_DEBUG {host.name} LISTEN:')
        print(host.cmd('ss -ltnp | grep 9000 || echo none'))
        raise RuntimeError(f'{host.name} echo backend did not listen on 9000')
    time.sleep(1)


def start_load_balancer(host, config_path, log_file):
    # matching the full path only so pkill doesnt match this command line too
    host.cmd("pkill -f '^/home/doom/balance_this_load/target/release/balance_this_load( |$)' || true")
    if not wait_for_listener_to_close(host, 8080):
        print('LB_DEBUG listener did not close before restart:')
        print(host.cmd('ss -ltnp | grep 8080 || echo none'))
        raise RuntimeError(f'{host.name} old load balancer did not stop')

    host.cmd(f'RUST_LOG=info {LB} {config_path} > {log_file} 2>&1 &')
    if not wait_for_listener(host, 8080, '10.0.0.2'):
        print(f'LB_DEBUG log for {host.name}:')
        print(host.cmd(f'tail -n 40 {log_file} 2>&1'))
        print('LB_DEBUG processes:')
        print(host.cmd("ps -ef | grep '[b]alance_this_load' || echo none"))
        raise RuntimeError(f'{host.name} load balancer did not listen on 8080')
    time.sleep(2)
    listener = host.cmd('ss -ltn | grep -c ":8080 " || true').strip()
    if not listener or int(listener.split()[0]) == 0:
        print(f'LB_DEBUG log for {host.name}:')
        print(host.cmd(f'tail -n 40 {log_file} 2>&1'))
        raise RuntimeError(f'{host.name} load balancer stopped after starting')


def stop_host_processes(hosts):
    for name in ('h2', 'h3', 'h4'):
        if name in hosts:
            hosts[name].cmd("pkill -f 'target/release/balance_this_load' || true")
            hosts[name].cmd('pkill -f echo_backend.py || true')


def write_lb_config(
    name,
    algorithm='round_robin',
    health_enabled=False,
    weights=(1, 1),
    health_interval=1,
    sdn_controller_socket=None,
    sdn_control_listen=None,
):
    path = f'/tmp/btl_{name}.toml'
    health = 'true' if health_enabled else 'false'
    lines = [
        'listen = "10.0.0.2:8080"',
        f'algorithm = "{algorithm}"',
        '',
        '[health_check]',
        f'enabled = {health}',
        f'interval_secs = {health_interval}',
        'timeout_secs = 1',
        '',
        '[limits]',
        'max_connections = 1024',
        '',
        '[[backends]]',
        'id = "backend-h3"',
        'address = "10.0.0.3:9000"',
        f'weight = {weights[0]}',
        '',
        '[[backends]]',
        'id = "backend-h4"',
        'address = "10.0.0.4:9000"',
        f'weight = {weights[1]}',
        '',
    ]
    if sdn_controller_socket or sdn_control_listen:
        lines.append('[sdn]')
        if sdn_controller_socket:
            lines.append(f'controller_socket = "{sdn_controller_socket}"')
        if sdn_control_listen:
            lines.append(f'control_listen = "{sdn_control_listen}"')
        lines.append('')
    with open(path, 'w') as config_file:
        config_file.write('\n'.join(lines))
    return path


def run_client(host, count, tag, target='10.0.0.2', port=8080):
    output = host.cmd(f'python3 {CLIENT} {target} {port} {count} {tag}')
    lines = [
        re.sub(r'\x1b\[[0-9;]*m', '', line).strip()
        for line in output.splitlines()
        if line.strip()
    ]
    expected = [f'{tag}-{index}' for index in range(count)]
    if lines != expected:
        raise RuntimeError(
            f'client echo mismatch: expected {expected}, got {lines}'
        )
    return lines


def accept_count(host, count_file):
    output = host.cmd(
        f'if [ -f {count_file} ]; then wc -l < {count_file}; else echo 0; fi'
    ).strip()
    return int(output.split()[0])


def print_log(label, host, log_file):
    print(f'{label}:')
    print(host.cmd(f'tail -n 30 {log_file} 2>&1'))
