import os
import socket
import threading

from pox.core import core
import pox.openflow.libopenflow_01 as of

log = core.getLogger()

BASE_PRIORITY = 30000
BLOCK_PRIORITY = 40000


class LoadBalancerController(object):
    def __init__(self, dpid, control_socket, ports):
        self.dpid = dpid
        self.control_socket = control_socket
        self.ports = ports
        self.connection = None
        self.blocked = set()
        self.server = None

    def _handle_ConnectionUp(self, event):
        if event.dpid != self.dpid:
            log.info('ignoring switch %s', event.dpid)
            return

        self.connection = event.connection
        log.info('controlling switch %s', event.dpid)
        self.install_base_flows()
        self.start_control_server()

    def _send_flow(self, match, actions, priority, command=None):
        if self.connection is None:
            return False

        msg = of.ofp_flow_mod()
        msg.match = match
        msg.priority = priority
        msg.idle_timeout = 0
        msg.hard_timeout = 0
        if command is not None:
            msg.command = command
        for action in actions:
            msg.actions.append(action)
        self.connection.send(msg)
        return True

    def install_base_flows(self):
        for ip, port in self.ports.items():
            self._send_flow(
                of.ofp_match(dl_type=0x0800, nw_dst=ip),
                [of.ofp_action_output(port=port)],
                BASE_PRIORITY,
            )
        self._send_flow(
            of.ofp_match(dl_type=0x0806),
            [of.ofp_action_output(port=of.OFPP_FLOOD)],
            BASE_PRIORITY,
        )
        log.info('installed explicit IP forwarding rules')

    def block(self, ip):
        if self.connection is None:
            return 'ERR switch not connected'
        if ip not in self.ports:
            return 'ERR unknown backend ' + ip

        self._send_flow(of.ofp_match(dl_type=0x0800, nw_dst=ip), [], BLOCK_PRIORITY)
        self._send_flow(of.ofp_match(dl_type=0x0800, nw_src=ip), [], BLOCK_PRIORITY)
        self.blocked.add(ip)
        log.info('blocked backend %s', ip)
        return 'OK blocked ' + ip

    def unblock(self, ip):
        if self.connection is None:
            return 'ERR switch not connected'
        if ip not in self.ports:
            return 'ERR unknown backend ' + ip

        delete = of.OFPFC_DELETE_STRICT
        self._send_flow(
            of.ofp_match(dl_type=0x0800, nw_dst=ip), [], BLOCK_PRIORITY, delete
        )
        self._send_flow(
            of.ofp_match(dl_type=0x0800, nw_src=ip), [], BLOCK_PRIORITY, delete
        )
        self.blocked.discard(ip)
        log.info('unblocked backend %s', ip)
        return 'OK unblocked ' + ip

    def status(self):
        blocked = ','.join(sorted(self.blocked)) or 'none'
        return 'OK blocked=' + blocked

    def start_control_server(self):
        if self.server is not None:
            return

        if os.path.exists(self.control_socket):
            os.unlink(self.control_socket)

        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.control_socket)
        self.server.listen(8)

        thread = threading.Thread(target=self.serve_control, daemon=True)
        thread.start()
        log.info('control socket ready at %s', self.control_socket)

    def serve_control(self):
        while True:
            connection, _ = self.server.accept()
            try:
                data = b''
                while b'\n' not in data:
                    chunk = connection.recv(1024)
                    if not chunk:
                        break
                    data += chunk
                    if len(data) > 4096:
                        break

                command = data.decode('utf-8', 'replace').strip()
                parts = command.split()
                if not parts:
                    continue
                if parts[0] == 'BLOCK' and len(parts) == 2:
                    response = self.block(parts[1])
                elif parts[0] == 'UNBLOCK' and len(parts) == 2:
                    response = self.unblock(parts[1])
                elif parts[0] == 'STATUS' and len(parts) == 1:
                    response = self.status()
                else:
                    response = 'ERR unsupported command'

                connection.sendall((response + '\n').encode('utf-8'))
            except Exception as error:
                try:
                    connection.sendall(('ERR ' + str(error) + '\n').encode('utf-8'))
                except Exception:
                    pass
            finally:
                connection.close()


def launch(
    dpid=0x000000000010,
    control_socket='/tmp/balance_this_load_sdn.sock',
    h1_port=1,
    h2_port=2,
    h3_port=3,
    h4_port=4,
):
    ports = {
        '10.0.0.1': int(h1_port),
        '10.0.0.2': int(h2_port),
        '10.0.0.3': int(h3_port),
        '10.0.0.4': int(h4_port),
    }
    controller = LoadBalancerController(int(dpid, 0), control_socket, ports)
    core.openflow.addListeners(controller)
