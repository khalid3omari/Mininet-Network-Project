#!/usr/bin/env python3
r"""
Task 2 - Congestion and queue management on a 10 Mb/s bottleneck.

Topology:
   h1 (TCP) --\                                   /-- h4 (TCP server)
   h2 (TCP) ---- s1 ====== bottleneck ====== s2 ----- h5 (TCP server)
   h3 (UDP) --/   (s1-eth4, 10 Mb/s)             \-- h6 (UDP server)

Access links: 100 Mb/s, 5 ms each (base RTT ~ 20 ms).
The 10 Mb/s limit and the queue discipline are applied with tc on s1-eth4:
    htb (10mbit)  ->  child qdisc (pfifo / red / codel / fq_codel)

Scenarios:
  1. tcp_only   - one TCP flow alone              (fifo, 1000 pkts)
  2. udp_only   - UDP at 12 Mb/s alone            (fifo, 1000 pkts)
  3. mixed_*    - 2 TCP flows + 1 UDP (8 Mb/s) at the same time, repeated
                  for each queue discipline

Metrics: TCP throughput, UDP throughput/loss/jitter, ping RTT under load,
         ping loss, and packets dropped by the queue (from tc statistics).

Usage:  sudo python3 task2_congestion.py [--cli]
Needs:  iperf (v2), tc, stdbuf.   Raw outputs are saved in /tmp/lab2/
"""
import csv
import os
import re
import sys
import time
from datetime import datetime

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel, info
from mininet.net import Mininet
from mininet.node import OVSBridge
from mininet.topo import Topo

DURATION = 20                  # seconds of traffic per scenario
BOTTLENECK_IF = 's1-eth4'      # s1 interface that faces s2
OUT = '/tmp/lab2'

# Queue disciplines attached below the 10 Mb/s htb class
QDISCS = {
    'fifo_1000': 'pfifo limit 1000',          # big buffer  -> bufferbloat
    'fifo_50':   'pfifo limit 50',            # small buffer
    'red':       'red limit 400000 min 30000 max 90000 avpkt 1000 '
                 'burst 55 bandwidth 10mbit probability 0.02',
    'codel':     'codel limit 1000',
    'fq_codel':  'fq_codel limit 1000',
}


class BottleneckTopo(Topo):
    def build(self):
        s1 = self.addSwitch('s1')
        s2 = self.addSwitch('s2')
        left = [self.addHost('h%d' % i) for i in (1, 2, 3)]    # senders
        right = [self.addHost('h%d' % i) for i in (4, 5, 6)]   # receivers
        for port, h in enumerate(left, start=1):
            self.addLink(h, s1, port2=port, cls=TCLink, bw=100, delay='5ms')
        for port, h in enumerate(right, start=1):
            self.addLink(h, s2, port2=port, cls=TCLink, bw=100, delay='5ms')
        # Bottleneck link: port 4 on both switches (shaped later with tc)
        self.addLink(s1, s2, port1=4, port2=4)


def set_queue(s1, name):
    """(Re)create htb 10 Mb/s on the bottleneck interface with the chosen qdisc."""
    intf = BOTTLENECK_IF
    s1.cmd('tc qdisc del dev %s root 2>/dev/null' % intf)
    s1.cmd('tc qdisc add dev %s root handle 1: htb default 10' % intf)
    s1.cmd('tc class add dev %s parent 1: classid 1:10 htb '
           'rate 10mbit ceil 10mbit burst 15k' % intf)
    s1.cmd('tc qdisc add dev %s parent 1:10 handle 10: %s' % (intf, QDISCS[name]))


# Throughput is measured at the RECEIVER (iperf server), not at the sender:
# the sender also counts bytes that are only sitting in socket buffers / the
# bottleneck queue, which can show more than the 10 Mb/s link can carry.
# We average the 1-second interval lines and skip the first SKIP seconds
# (TCP slow start) so the numbers describe the steady state.
SKIP = 3
IV = r'\[\s*\d+\]\s+([\d.]+)-\s*([\d.]+)\s+sec\s+[\d.]+\s+\w+\s+([\d.]+)\s+Mbits/sec'


def server_mbps(path):
    """Average receiver-side TCP throughput (Mb/s) in the steady state."""
    rows = re.findall(IV, open(path).read())
    vals = [float(v) for a, b, v in rows if float(b) - float(a) <= 1.5 and float(a) >= SKIP]
    return round(sum(vals) / len(vals), 2) if vals else None


def udp_report(path):
    """(throughput, jitter ms, lost, total, loss %) from the iperf UDP server,
    computed from the 1-second interval lines in the steady state."""
    rows = re.findall(IV + r'\s+([\d.]+)\s+ms\s+(-?\d+)/\s*(\d+)', open(path).read())
    rows = [(float(v), float(j), int(l), int(t)) for a, b, v, j, l, t in rows
            if float(b) - float(a) <= 1.5 and float(a) >= SKIP]
    if not rows:
        return None
    n = len(rows)
    lost = sum(r[2] for r in rows)
    total = sum(r[3] for r in rows)
    return (round(sum(r[0] for r in rows) / n, 2), round(sum(r[1] for r in rows) / n, 3),
            lost, total, round(100.0 * lost / total, 1) if total else 0.0)


def ping_stats(txt):
    loss = re.search(r'([\d.]+)% packet loss', txt)
    rtt = re.search(r'= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms', txt)
    return (float(loss.group(1)) if loss else None,
            float(rtt.group(2)) if rtt else None,
            float(rtt.group(3)) if rtt else None)


def scenario(net, tag, qdisc, tcp=True, udp_mbps=None):
    s1 = net.get('s1')
    h = {n: net.get(n) for n in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6')}
    f = lambda k: '%s/%s_%s.txt' % (OUT, tag, k)

    info('\n=== [%s] %s | queue=%s ===\n' % (datetime.now().strftime('%H:%M:%S'), tag, qdisc))
    set_queue(s1, qdisc)

    # servers
    h['h4'].cmd('stdbuf -oL iperf -s -i 1 -f m > %s 2>&1 &' % f('srv4'))
    h['h5'].cmd('stdbuf -oL iperf -s -i 1 -f m > %s 2>&1 &' % f('srv5'))
    h['h6'].cmd('stdbuf -oL iperf -s -u -i 1 -f m > %s 2>&1 &' % f('srv6'))
    time.sleep(1)

    # clients (simultaneous)
    if tcp:
        h['h1'].cmd('stdbuf -oL iperf -c %s -t %d -i 1 -f m > %s 2>&1 &' % (h['h4'].IP(), DURATION, f('tcp1')))
        if udp_mbps:    # second TCP flow only in the mixed scenario
            h['h2'].cmd('stdbuf -oL iperf -c %s -t %d -i 1 -f m > %s 2>&1 &' % (h['h5'].IP(), DURATION, f('tcp2')))
    if udp_mbps:
        h['h3'].cmd('iperf -c %s -u -b %dM -t %d -f m > %s 2>&1 &'
                    % (h['h6'].IP(), udp_mbps, DURATION, f('udp')))

    # latency probe under load: h1 -> h4, 28 probes every 0.5 s (blocks ~14 s)
    start = time.time()
    time.sleep(2)
    ping_out = h['h1'].cmd('ping -c 28 -i 0.5 %s' % h['h4'].IP())
    open(f('ping'), 'w').write(ping_out)
    time.sleep(max(0, DURATION + 5 - (time.time() - start)))

    # queue statistics from tc (drops caused by the queue)
    tc_out = s1.cmd('tc -s qdisc show dev %s' % BOTTLENECK_IF)
    open(f('tc'), 'w').write(tc_out)
    m = re.search(r'parent 1:10.*?dropped (\d+)', tc_out, re.S)
    qdrops = int(m.group(1)) if m else None

    os.system('pkill -f iperf')
    time.sleep(1)

    row = {'scenario': tag, 'queue': qdisc}
    row['tcp1_mbps'] = server_mbps(f('srv4')) if tcp else None
    row['tcp2_mbps'] = server_mbps(f('srv5')) if (tcp and udp_mbps) else None
    u = udp_report(f('srv6')) if udp_mbps else None
    row['udp_mbps'] = u[0] if u else None
    row['udp_jitter_ms'] = u[1] if u else None
    row['udp_loss_pct'] = u[4] if u else None
    loss, avg, mx = ping_stats(ping_out)
    row['ping_loss_pct'] = loss
    row['rtt_avg_ms'] = avg
    row['rtt_max_ms'] = mx
    row['queue_drops'] = qdrops
    info('    result: %s\n' % row)
    return row


def run():
    os.makedirs(OUT, exist_ok=True)
    net = Mininet(topo=BottleneckTopo(), switch=OVSBridge, controller=None)
    net.start()
    info('*** Start: %s\n' % datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    net.pingAll()

    rows = [
        scenario(net, 'tcp_only', 'fifo_1000', tcp=True, udp_mbps=None),
        scenario(net, 'udp_only', 'fifo_1000', tcp=False, udp_mbps=12),
    ]
    for q in QDISCS:
        rows.append(scenario(net, 'mixed_' + q, q, tcp=True, udp_mbps=8))

    # summary table + CSV
    cols = ['scenario', 'queue', 'tcp1_mbps', 'tcp2_mbps', 'udp_mbps', 'udp_jitter_ms',
            'udp_loss_pct', 'ping_loss_pct', 'rtt_avg_ms', 'rtt_max_ms', 'queue_drops']
    with open(OUT + '/results.csv', 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print('\n' + ' | '.join(cols))
    for r in rows:
        print(' | '.join(str(r[c]) for c in cols))
    print('\nEnd: %s   (raw files + results.csv in %s)' % (
        datetime.now().strftime('%Y-%m-%d %H:%M:%S'), OUT))

    if '--cli' in sys.argv:
        CLI(net)
    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    run()
