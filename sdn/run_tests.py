#!/usr/bin/env python3
import os, time, subprocess, socket

HOME = '/home/doom'
LB = f'{HOME}/balance_this_load/target/release/balance_this_load'
CFG = f'{HOME}/balance_this_load/sdn/config_mininet.toml'
ECHO = f'{HOME}/balance_this_load/sdn/echo_backend.py'
POX = f'{HOME}/pox/pox.py'

from mininet.net import Mininet
from mininet.node import RemoteController, UserSwitch
from mininet.link import TCLink
from mininet.log import setLogLevel, info


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


def main():
    setLogLevel('info')
    pox_log = open('/tmp/pox.log', 'w')
    pox = subprocess.Popen(
        ['python3', POX, 'log.level', '--INFO', 'openflow.of_01', '--port=6653', 'forwarding.l2_learning'],
        stdout=pox_log, stderr=subprocess.STDOUT, text=True, cwd=f'{HOME}/pox'
    )
    ready = wait_port('127.0.0.1', 6653, 10)
    print('POX_READY:', ready)
    try:
        net = Mininet(controller=None, switch=UserSwitch, link=TCLink, autoSetMacs=True)
        c0 = net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6653)
        s1 = net.addSwitch('s1', dpid='000000000010')
        h1 = net.addHost('h1', ip='10.0.0.1/24')
        h2 = net.addHost('h2', ip='10.0.0.2/24')
        h3 = net.addHost('h3', ip='10.0.0.3/24')
        h4 = net.addHost('h4', ip='10.0.0.4/24')
        for h in (h1, h2, h3, h4):
            net.addLink(h, s1)
        net.build()
        disable_offload(net)
        c0.start()

        s1.start([c0])
        time.sleep(2)

        info('*** Pingall basic connectivity\n')
        loss = net.pingAll()
        print('PINGALL_LOSS_PERCENT:', loss)

        info('*** Step 6: plain TCP echo through topology (no LB)\n')
        h3.cmd(f'python3 {ECHO} 10.0.0.3 9000 > /tmp/h3.log 2>&1 &')
        h4.cmd(f'python3 {ECHO} 10.0.0.4 9000 > /tmp/h4.log 2>&1 &')
        time.sleep(1)
        print('H3_LISTEN:', h3.cmd('ss -ltnp | grep 9000 || echo none').strip())
        print('H4_LISTEN:', h4.cmd('ss -ltnp | grep 9000 || echo none').strip())
        r3 = h1.cmd("""python3 -c "import socket;s=socket.create_connection(('10.0.0.3',9000),3);s.sendall(b'hi-h3');print(s.recv(10).decode());s.close()" 2>&1""")
        r4 = h1.cmd("""python3 -c "import socket;s=socket.create_connection(('10.0.0.4',9000),3);s.sendall(b'hi-h4');print(s.recv(10).decode());s.close()" 2>&1""")
        print('TCP_h3:', r3.strip())
        print('TCP_h4:', r4.strip())
        h3.cmd('pkill -f echo_backend.py || true')
        h4.cmd('pkill -f echo_backend.py || true')
        time.sleep(0.5)

        info('*** Step 7: run Rust LB on h2, test round-robin\n')
        h3.cmd('rm -f /tmp/h3b.count /tmp/h4b.count')
        h3.cmd(f'python3 {ECHO} 10.0.0.3 9000 /tmp/h3b.count > /tmp/h3b.log 2>&1 &')
        h4.cmd(f'python3 {ECHO} 10.0.0.4 9000 /tmp/h4b.count > /tmp/h4b.log 2>&1 &')
        time.sleep(1)
        print('H3_LISTEN_B:', h3.cmd('ss -ltnp | grep 9000 || echo none').strip())
        print('H4_LISTEN_B:', h4.cmd('ss -ltnp | grep 9000 || echo none').strip())
        h2.cmd(f'RUST_LOG=info {LB} {CFG} > /tmp/lb.log 2>&1 &')
        time.sleep(2)
        print('LB_LISTEN:', h2.cmd('ss -ltnp | grep 8080 || echo none').strip())
        results = []
        for i in range(8):
            out = h1.cmd(f"""python3 -c "import socket;s=socket.create_connection(('10.0.0.2',8080),3);m=b'req{i}';s.sendall(m);print(s.recv(20).decode());s.close()" 2>&1""")
            results.append(out.strip())
        print('LB_RESULTS:', results)
        h3n = h3.cmd('wc -l < /tmp/h3b.count 2>/dev/null || echo 0').strip()
        h4n = h4.cmd('wc -l < /tmp/h4b.count 2>/dev/null || echo 0').strip()
        print('H3_ACCEPTED:', h3n)
        print('H4_ACCEPTED:', h4n)
        print('LB_LOG_TAIL:')
        print(h2.cmd('tail -n 40 /tmp/lb.log'))
        print('H3_LOG_TAIL:', h3.cmd('tail -n 5 /tmp/h3b.log 2>&1'))
        print('H4_LOG_TAIL:', h4.cmd('tail -n 5 /tmp/h4b.log 2>&1'))
        h2.cmd(f'pkill -f {LB} || true')

        h3.cmd('pkill -f echo_backend.py || true')
        h4.cmd('pkill -f echo_backend.py || true')

        net.stop()
    finally:
        pox.terminate()
        try:
            pox.wait(timeout=5)
        except Exception:
            pox.kill()
        pox_log.close()
    print('POX_LOG_TAIL:')
    print(subprocess.getoutput('tail -n 40 /tmp/pox.log 2>&1'))
    print('DONE')


if __name__ == '__main__':
    main()
