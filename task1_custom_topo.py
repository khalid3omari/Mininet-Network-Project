#!/usr/bin/env python3
"""
Task 1 - Custom topology with Mininet's Python API.

Topology (4 hosts, 3 switches in a line):

    h1 --\
          s1 ---- s2 ---- s3 -- h4
    h2 --/        |
                  h3

Usage:
  sudo python3 task1_custom_topo.py                # build, test, open CLI
  sudo mn --custom task1_custom_topo.py --topo customtopo --test pingall
"""
from datetime import datetime

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel, info
from mininet.net import Mininet
from mininet.node import OVSBridge
from mininet.topo import Topo


class CustomTopo(Topo):
    """Custom topology: three switches in a line, hosts hanging off them."""

    def build(self):
        # --- switches ---
        s1 = self.addSwitch('s1')
        s2 = self.addSwitch('s2')
        s3 = self.addSwitch('s3')

        # --- hosts (explicit IPs so the addressing is easy to read) ---
        h1 = self.addHost('h1', ip='10.0.0.1/24')
        h2 = self.addHost('h2', ip='10.0.0.2/24')
        h3 = self.addHost('h3', ip='10.0.0.3/24')
        h4 = self.addHost('h4', ip='10.0.0.4/24')

        # --- host links (default parameters) ---
        self.addLink(h1, s1)
        self.addLink(h2, s1)
        self.addLink(h3, s2)
        self.addLink(h4, s3)

        # --- switch-to-switch links with bandwidth/delay (TCLink) ---
        self.addLink(s1, s2, cls=TCLink, bw=10, delay='5ms')
        self.addLink(s2, s3, cls=TCLink, bw=10, delay='5ms')


# Makes the topology available to:  sudo mn --custom <file> --topo customtopo
topos = {'customtopo': (lambda: CustomTopo())}


def run():
    # OVSBridge = standalone learning switch, so no external controller is needed.
    net = Mininet(topo=CustomTopo(), switch=OVSBridge, controller=None)
    net.start()

    info('\n*** Date/time: %s\n' % datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    info('*** Nodes\n')
    net.pingAll()

    info('\n*** h1 -> h4 ping (3 packets)\n')
    print(net.get('h1').cmd('ping -c 3 10.0.0.4'))

    info('*** Inspect: run "net" and "dump" in the CLI below\n')
    CLI(net)          # type: net, dump, links, pingall, h1 ping -c 3 h4, sh date
    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    run()
