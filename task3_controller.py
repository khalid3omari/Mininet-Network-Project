"""
Task 3 - Ryu controller with link-failure detection and automatic rerouting.

How it works
------------
* Run with --observe-links so Ryu discovers switches and inter-switch links (LLDP).
* The controller reads the topology graph from Ryu's topology API.
* For the two hosts (h1 <-> h2) it computes the shortest path with BFS and
  installs proactive flows (match: eth_dst) along that path.
* When a link goes down Ryu raises EventLinkDelete. The controller then
  - logs the detection time,
  - removes its old flows (identified by a cookie, so Ryu's own LLDP flows stay),
  - recomputes the path on the remaining links and installs the new flows,
  - logs how long the rerouting took.

Run:  ryu-manager --observe-links task3_controller.py
"""
import time

from ryu.base import app_manager
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.topology import event
from ryu.topology.api import get_link, get_switch

COOKIE = 0x5EC0                 # marks flows installed by this app
MASK = 0xFFFFFFFFFFFFFFFF
PRIORITY = 10

# Hosts: MAC -> (switch dpid, host-facing port). Matches the topology in task3_run.py
H1 = '00:00:00:00:00:01'
H2 = '00:00:00:00:00:02'
HOSTS = {H1: (1, 1), H2: (4, 1)}


def stamp():
    t = time.time()
    return time.strftime('%H:%M:%S', time.localtime(t)) + '.%03d' % (int(t * 1000) % 1000)


class LinkFailureController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(LinkFailureController, self).__init__(*args, **kwargs)
        self.current_path = None      # list of dpids currently used by h1 -> h2

    # ---------------- topology events ----------------
    @set_ev_cls(event.EventSwitchEnter)
    def _switch_enter(self, ev):
        self._update('switch enter')

    @set_ev_cls(event.EventLinkAdd)
    def _link_add(self, ev):
        self._update('link add %s -> %s' % (ev.link.src.dpid, ev.link.dst.dpid))

    @set_ev_cls(event.EventLinkDelete)
    def _link_delete(self, ev):
        t0 = time.time()
        self.logger.warning('[%s] LINK DOWN detected: s%s <-> s%s',
                            stamp(), ev.link.src.dpid, ev.link.dst.dpid)
        self._update('link delete', t0=t0, ignore=ev.link)

    # ---------------- core logic ----------------
    def _update(self, reason, t0=None, ignore=None):
        t0 = t0 or time.time()
        dps = {sw.dp.id: sw.dp for sw in get_switch(self, None)}

        # directed adjacency: adj[a][b] = port on a that leads to b
        adj = {}
        for l in get_link(self, None):
            if ignore is not None and {l.src.dpid, l.dst.dpid} == {ignore.src.dpid, ignore.dst.dpid}:
                continue                      # treat the failed link as gone
            adj.setdefault(l.src.dpid, {})[l.dst.dpid] = l.src.port_no

        src_dpid, _ = HOSTS[H1]
        dst_dpid, _ = HOSTS[H2]
        if src_dpid not in dps or dst_dpid not in dps:
            return                            # switches not connected yet

        path = self._bfs(adj, src_dpid, dst_dpid)
        if path is None:
            if self.current_path is not None:
                self.logger.warning('[%s] no path left between h1 and h2', stamp())
                self._clear_flows(dps)
                self.current_path = None
            return
        if path == self.current_path:
            return                            # nothing to change

        if not self._install(dps, adj, path):
            return                            # reverse direction not discovered yet
        old = self.current_path
        self.current_path = path
        self.logger.warning('[%s] (%s) path h1->h2 = %s%s | reroute time %.1f ms',
                            stamp(), reason,
                            ' -> '.join('s%d' % d for d in path),
                            '' if old is None else '  (was %s)' % ' -> '.join('s%d' % d for d in old),
                            (time.time() - t0) * 1000)

    @staticmethod
    def _bfs(adj, src, dst):
        """Shortest path by hop count; neighbours visited in dpid order (deterministic)."""
        queue, seen = [[src]], {src}
        while queue:
            path = queue.pop(0)
            node = path[-1]
            if node == dst:
                return path
            for nb in sorted(adj.get(node, {})):
                if nb not in seen:
                    seen.add(nb)
                    queue.append(path + [nb])
        return None

    def _install(self, dps, adj, path):
        """Install flows for h1->h2 along `path` and h2->h1 along the reverse path."""
        plan = []                                           # (dpid, dst_mac, out_port)
        for macs, nodes in ((H2, path), (H1, list(reversed(path)))):
            host_port = HOSTS[macs][1]
            for i, dpid in enumerate(nodes):
                if i == len(nodes) - 1:
                    out = host_port
                else:
                    out = adj.get(dpid, {}).get(nodes[i + 1])
                    if out is None:
                        return False
                plan.append((dpid, macs, out))
        self._clear_flows(dps)
        for dpid, mac, out in plan:
            self._add_flow(dps[dpid], mac, out)
        return True

    def _add_flow(self, dp, mac, out_port):
        p, o = dp.ofproto_parser, dp.ofproto
        match = p.OFPMatch(eth_dst=mac)
        inst = [p.OFPInstructionActions(o.OFPIT_APPLY_ACTIONS, [p.OFPActionOutput(out_port)])]
        dp.send_msg(p.OFPFlowMod(datapath=dp, cookie=COOKIE, priority=PRIORITY,
                                 match=match, instructions=inst))

    def _clear_flows(self, dps):
        """Delete only the flows that carry our cookie (keeps Ryu's LLDP flows)."""
        for dp in dps.values():
            p, o = dp.ofproto_parser, dp.ofproto
            dp.send_msg(p.OFPFlowMod(datapath=dp, cookie=COOKIE, cookie_mask=MASK,
                                     table_id=o.OFPTT_ALL, command=o.OFPFC_DELETE,
                                     out_port=o.OFPP_ANY, out_group=o.OFPG_ANY,
                                     match=p.OFPMatch()))
