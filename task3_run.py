#!/usr/bin/env python3
"""
Task 3 - Mininet side: redundant (diamond) topology, link failure, measurements.

Topology:
                 +-- s2 --+
   h1 -- s1 ---- |        |---- s4 -- h2
                 +-- s3 --+

Primary path: s1-s2-s4   Backup path: s1-s3-s4
(Port numbers are fixed so the controller knows the host-facing ports.)

Steps performed:
  1. start Mininet with a remote controller (the Ryu app must already be running)
  2. wait for LLDP discovery, set static ARP, check connectivity
  3. start a fast ping h1 -> h2 (1500 probes, 10 ms apart = 15 s)
  4. after 5 s bring the link s1-s2 down
  5. compute packet loss and recovery time from the ping timestamps

Usage:
  terminal 1:  ryu-manager --observe-links task3_controller.py
  terminal 2:  sudo python3 task3_run.py [--cli]
"""
import os
import re
import sys
import time
from datetime import datetime
from functools import partial

from mininet.cli import CLI
from mininet.log import setLogLevel, info
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.topo import Topo

TOTAL = 1500          # ping probes
INTERVAL = 0.01       # seconds between probes
PING_FILE = '/tmp/ping_failover.txt'


def ts():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]


class DiamondTopo(Topo):
    def build(self):
        h1 = self.addHost('h1', ip='10.0.0.1/24', mac='00:00:00:00:00:01')
        h2 = self.addHost('h2', ip='10.0.0.2/24', mac='00:00:00:00:00:02')
        s1, s2, s3, s4 = [self.addSwitch('s%d' % i) for i in (1, 2, 3, 4)]
        self.addLink(h1, s1, port2=1)                   # host ports = 1
        self.addLink(h2, s4, port2=1)
        self.addLink(s1, s2, port1=2, port2=2)          # primary path
        self.addLink(s2, s4, port1=3, port2=2)
        self.addLink(s1, s3, port1=3, port2=2)          # backup path
        self.addLink(s3, s4, port1=3, port2=3)


def dump_flows(title):
    print('\n----- %s  [%s] -----' % (title, ts()))
    for s in ('s1', 's2', 's3', 's4'):
        print('>> %s' % s)
        os.system('ovs-ofctl -O OpenFlow13 dump-flows %s | grep "cookie=0x5ec0"' % s)


def analyse(path):
    """Packet loss and outage length from `ping -D` timestamps."""
    txt = open(path).read()
    rows = [(float(t), int(s)) for t, s in
            re.findall(r'\[(\d+\.\d+)\].*icmp_seq=(\d+)', txt)]
    received = len(rows)
    lost = TOTAL - received
    gap, gap_start = 0.0, None
    for a, b in zip(rows, rows[1:]):
        if b[0] - a[0] > gap:
            gap, gap_start = b[0] - a[0], a[0]
    print('\n===== RESULTS =====')
    print('Probes sent        : %d (every %.0f ms)' % (TOTAL, INTERVAL * 1000))
    print('Probes received    : %d' % received)
    print('Packets lost       : %d  (%.2f %%)' % (lost, 100.0 * lost / TOTAL))
    print('Longest gap between replies (= recovery time): %.3f s' % gap)
    if gap_start:
        print('Outage began at    : %s' % datetime.fromtimestamp(gap_start).strftime('%H:%M:%S.%f')[:-3])
    if rows:
        span = rows[-1][0] - rows[0][0]
        print('Average probe spacing: %.1f ms  (lost x spacing = %.3f s)'
              % (1000.0 * span / max(1, rows[-1][1] - rows[0][1]),
                 lost * span / max(1, rows[-1][1] - rows[0][1])))


def run():
    net = Mininet(topo=DiamondTopo(),
                  switch=partial(OVSSwitch, protocols='OpenFlow13'),
                  controller=lambda name: RemoteController(name, ip='127.0.0.1', port=6653),
                  autoSetMacs=False)
    net.start()
    h1, h2 = net.get('h1', 'h2')

    info('*** Waiting 10 s for topology discovery (LLDP)\n')
    time.sleep(10)
    net.staticArp()                      # avoids ARP flooding in a looped topology

    info('*** Connectivity check  [%s]\n' % ts())
    net.pingAll()
    dump_flows('Flow tables BEFORE failure')

    info('\n*** Fast ping h1 -> h2 started  [%s]\n' % ts())
    h1.cmd('ping -D -i %s -c %d %s > %s 2>&1 &' % (INTERVAL, TOTAL, h2.IP(), PING_FILE))
    time.sleep(5)

    info('*** FAILING link s1 <-> s2  [%s]\n' % ts())
    net.configLinkStatus('s1', 's2', 'down')

    # Wait until the ping process has really finished. In a VM, "10 ms" probes
    # take longer than 15 s, and reading the file early would count probes that
    # were simply not sent yet as lost.
    deadline = time.time() + 90
    while time.time() < deadline and h1.cmd('pgrep -x ping').strip():
        time.sleep(1)
    time.sleep(1)
    dump_flows('Flow tables AFTER failure (backup path s1-s3-s4)')
    analyse(PING_FILE)

    info('\n*** Check the Ryu terminal for the "LINK DOWN detected" and reroute log lines.\n')
    if '--cli' in sys.argv:
        CLI(net)          # e.g.  link s1 s2 up   /   h1 ping -c 3 h2
    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    run()
