#!/usr/bin/env python3
"""Minimal topology for balance_this_load over Mininet UserSwitch + remote controller."""

from mininet.net import Mininet
from mininet.node import RemoteController, UserSwitch
from mininet.link import TCLink
from mininet.log import setLogLevel, info
from mininet.cli import CLI


def disable_offload(net):
    for host in net.hosts:
        host.cmd(f'ethtool -K {host.defaultIntf()} tx off gso off tso off')


def build_net():
    net = Mininet(controller=None, switch=UserSwitch, link=TCLink, autoSetMacs=True)
    info('*** Adding controller (remote, expects POX on 127.0.0.1:6653)\n')
    c0 = net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6653)
    info('*** Adding switch and hosts\n')
    s1 = net.addSwitch('s1', dpid='000000000010')
    h1 = net.addHost('h1', ip='10.0.0.1/24')
    h2 = net.addHost('h2', ip='10.0.0.2/24')
    h3 = net.addHost('h3', ip='10.0.0.3/24')
    h4 = net.addHost('h4', ip='10.0.0.4/24')
    for h in (h1, h2, h3, h4):
        net.addLink(h, s1)
    return net, c0, s1


if __name__ == '__main__':
    setLogLevel('info')
    net, c0, s1 = build_net()
    net.build()
    disable_offload(net)
    c0.start()
    s1.start([c0])
    info('*** Network ready. Hosts: h1=10.0.0.1 h2=10.0.0.2 h3=10.0.0.3 h4=10.0.0.4\n')
    CLI(net)
    net.stop()
