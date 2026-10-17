"""IP/VLAN labels for the EVE-NG canvas.

Pure functions (no I/O) so they can be unit-tested:
- parse_interfaces(): `show running-config | section ^interface` -> per-interface facts
- port_label() / node_block(): the text for a link end and for the block under a node
- link_label_xy() / block_xy(): where to put them on the canvas (pixels)
- text_object_html(): the HTML EVE-NG stores (base64) for a canvas text object

All labels this server creates are named with LABEL_PREFIX so they can be
replaced or removed without touching text the user added.
"""

import base64
import html
import ipaddress
import math
import re
from typing import Dict, List, Optional, Tuple

LABEL_PREFIX = "ipl:"

# Approximate geometry of the EVE-NG canvas (pixels)
ICON = 50            # node icon is ~50x50, positioned by its top-left corner
CHAR_W = 6.2         # ~ width of one character at 10px font
LINE_H = 13          # line height at 10px font
ALONG = 78           # how far along the link from the node centre to place a port label
SIDE = 13            # sideways offset from the link line


# ---- parsing -----------------------------------------------------------------

_IF_ALIASES = [("tengigabitethernet", "Te"), ("gigabitethernet", "Gi"), ("fastethernet", "Fa"),
               ("ethernet", "E"), ("serial", "S"), ("loopback", "Lo"), ("vlan", "Vlan"),
               ("port-channel", "Po"), ("tunnel", "Tu")]


def short_if(name: str) -> str:
    n = name.strip()
    low = n.lower()
    for long, short in _IF_ALIASES:
        if low.startswith(long):
            return short + n[len(long):]
    return n


def norm_if(name: str) -> str:
    """Comparable key: 'GigabitEthernet0/0' == 'Gi0/0' == 'gi0/0'."""
    return short_if(name).lower().replace(" ", "")


def parse_interfaces(text: str) -> Dict[str, dict]:
    """Return {short_name: {ip: [cidr...], mode, access_vlan, trunk_vlans, shutdown}}."""
    out: Dict[str, dict] = {}
    cur = None
    for raw in text.replace("\r", "").split("\n"):
        line = raw.rstrip()
        m = re.match(r"^interface\s+(\S+)", line)
        if m:
            cur = short_if(m.group(1))
            out[cur] = {"ip": [], "mode": None, "access_vlan": None, "trunk_vlans": None, "shutdown": False}
            continue
        if cur is None or not line.startswith(" "):
            if line and not line.startswith(" "):
                cur = None
            continue
        s = line.strip()
        m = re.match(r"^ip address (\d+\.\d+\.\d+\.\d+) (\d+\.\d+\.\d+\.\d+)(\s+secondary)?$", s)
        if m:
            try:
                plen = ipaddress.IPv4Network(f"0.0.0.0/{m.group(2)}").prefixlen
                out[cur]["ip"].append(f"{m.group(1)}/{plen}")
            except ValueError:
                pass
            continue
        m = re.match(r"^switchport mode (access|trunk)", s)
        if m:
            out[cur]["mode"] = m.group(1)
            continue
        m = re.match(r"^switchport access vlan (\d+)", s)
        if m:
            out[cur]["access_vlan"] = m.group(1)
            continue
        m = re.match(r"^switchport trunk allowed vlan (?:add )?(\S+)", s)
        if m:
            prev = out[cur]["trunk_vlans"]
            out[cur]["trunk_vlans"] = m.group(1) if not prev else prev + "," + m.group(1)
            continue
        if s == "shutdown":
            out[cur]["shutdown"] = True
    return out


# ---- label text ----------------------------------------------------------------

def port_label(ifname: str, ifaces: Dict[str, dict]) -> List[str]:
    """Lines for the label at a link end. Routers: IP(s), plus subinterfaces of the
    port (e.g. router-on-a-stick) as '.10 172.16.10.1/24'. Switches: VLAN mode."""
    key = norm_if(ifname)
    lines: List[str] = []
    base = next((v for k, v in ifaces.items() if norm_if(k) == key), None)
    if base:
        lines += base["ip"]
        if base["mode"] == "access":
            lines.append(f"access {base['access_vlan'] or 1}")
        elif base["mode"] == "trunk":
            lines.append(f"trunk {base['trunk_vlans'] or 'all'}")
    for k, v in ifaces.items():
        if norm_if(k).startswith(key + ".") and v["ip"] and not v["shutdown"]:
            sub = k.split(".", 1)[1]
            lines += [f".{sub} {ip}" for ip in v["ip"]]
    return lines


def node_block(ifaces: Dict[str, dict]) -> List[str]:
    """Lines for the block under a node: loopbacks and VLAN interfaces (SVIs)."""
    lines = []
    for k, v in ifaces.items():
        if (k.startswith("Lo") or k.startswith("Vlan")) and v["ip"] and not v["shutdown"]:
            lines += [f"{k} {ip}" for ip in v["ip"]]
    return lines


# ---- geometry ------------------------------------------------------------------

def centre(node: dict) -> Tuple[float, float]:
    return float(node.get("left", 0)) + ICON / 2, float(node.get("top", 0)) + ICON / 2


def text_size(lines: List[str]) -> Tuple[float, float]:
    w = max((len(l) for l in lines), default=0) * CHAR_W + 8
    return w, len(lines) * LINE_H + 4


def link_label_xy(a: dict, b: dict, lines: List[str]) -> Tuple[int, int]:
    """Top-left for a label near node a's end of the a-b link: ALONG px toward b
    (or 35% of the way on short links), shifted SIDE px to one side of the line."""
    (ax, ay), (bx, by) = centre(a), centre(b)
    dx, dy = bx - ax, by - ay
    dist = math.hypot(dx, dy) or 1.0
    ux, uy = dx / dist, dy / dist
    along = min(ALONG, dist * 0.35)
    px, py = ax + ux * along, ay + uy * along
    # perpendicular, always pointing "up-ish" so labels sit above horizontal links
    nx, ny = uy, -ux
    if ny > 0 or (ny == 0 and nx < 0):
        nx, ny = -nx, -ny
    w, h = text_size(lines)
    cx, cy = px + nx * (SIDE + h / 2), py + ny * (SIDE + h / 2)
    return int(round(cx - w / 2)), int(round(cy - h / 2))


def block_xy(node: dict, lines: List[str]) -> Tuple[int, int]:
    """Top-left for the block centred under a node, below EVE-NG's name label."""
    cx, _ = centre(node)
    w, _ = text_size(lines)
    return int(round(cx - w / 2)), int(round(float(node.get("top", 0)) + ICON + 22))


# ---- EVE-NG text object -----------------------------------------------------------

def text_object_html(obj_id: int, left: int, top: int, lines: List[str],
                     color: str = "rgb(0, 51, 153)") -> str:
    body = "<br>".join(html.escape(l) for l in lines)
    return (
        f'<div id="customText{obj_id}" class="customShape customText context-menu jtk-draggable dragstopped ui-selectee" '
        f'data-path="{obj_id}" style="display: inline; position: absolute; left: {left}px; top: {top}px; '
        f'cursor: move; z-index: 1001;">'
        f'<p align="left" style="vertical-align: top; color: {color}; background-color: rgba(255, 255, 255, 0.85); '
        f'font-size: 10px; font-weight: normal; line-height: {LINE_H}px; margin: 0px; padding: 1px 3px;">{body}</p></div>'
    )


def encode_data(markup: str) -> str:
    return base64.b64encode(markup.encode("utf-8")).decode("ascii")


# ---- collision-aware layout ----------------------------------------------------

Box = Tuple[float, float, float, float]   # x, y, w, h


def _boxes_overlap(a: Box, b: Box, pad: float = 3) -> bool:
    return not (a[0] + a[2] + pad <= b[0] or b[0] + b[2] + pad <= a[0] or
                a[1] + a[3] + pad <= b[1] or b[1] + b[3] + pad <= a[1])


def _seg_hits_box(p1, p2, box: Box, pad: float = 4) -> bool:
    """Does the segment p1-p2 pass through the (padded) box? Sampled, good enough here."""
    x, y, w, h = box[0] - pad, box[1] - pad, box[2] + 2 * pad, box[3] + 2 * pad
    steps = max(2, int(math.hypot(p2[0] - p1[0], p2[1] - p1[1]) / 4))
    for i in range(steps + 1):
        t = i / steps
        px, py = p1[0] + (p2[0] - p1[0]) * t, p1[1] + (p2[1] - p1[1]) * t
        if x <= px <= x + w and y <= py <= y + h:
            return True
    return False


def layout(nodes: Dict[str, dict], ends: List[Tuple[str, str, List[str]]],
           blocks: List[Tuple[str, List[str]]]) -> Tuple[List[Tuple[int, int]], List[Tuple[int, int]]]:
    """Place link-end labels and node blocks without overlapping each other, nodes or
    link lines. ends: (node, other_node, lines); blocks: (node, lines).
    Returns top-left positions in the same order."""
    segs = []
    seen = set()
    for me, other, _ in ends:
        key = tuple(sorted((me, other)))
        if key not in seen:
            seen.add(key)
            segs.append((centre(nodes[me]), centre(nodes[other])))
    placed: List[Box] = [(n.get("left", 0) - 4, n.get("top", 0) - 4, ICON + 8, ICON + 26) for n in nodes.values()]
    for p, q in segs:                       # EVE-NG's own port tags sit ~42px from each node
        d = math.hypot(q[0] - p[0], q[1] - p[1]) or 1.0
        for (sx, sy), (tx, ty) in ((p, q), (q, p)):
            mx, my = sx + (tx - sx) / d * 42, sy + (ty - sy) / d * 42
            placed.append((mx - 24, my - 9, 48, 18))

    def free(box: Box, own_seg=None) -> bool:
        if any(_boxes_overlap(box, b) for b in placed):
            return False
        return not any(_seg_hits_box(p, q, box) for p, q in segs if (p, q) != own_seg)

    end_pos = []
    for me, other, lines in ends:
        a, b = nodes[me], nodes[other]
        (ax, ay), (bx, by) = centre(a), centre(b)
        dist = math.hypot(bx - ax, by - ay) or 1.0
        ux, uy = (bx - ax) / dist, (by - ay) / dist
        nx, ny = uy, -ux
        if ny > 0 or (ny == 0 and nx < 0):
            nx, ny = -nx, -ny
        w, h = text_size(lines)
        cands = []
        for along in (ALONG, ALONG + 24, ALONG + 48, ALONG + 72):
            along = min(along, dist * 0.45)
            for side in (1, -1):
                off = SIDE + (abs(nx) * w + abs(ny) * h) / 2
                cx = ax + ux * along + side * nx * off
                cy = ay + uy * along + side * ny * off
                cands.append((cx - w / 2, cy - h / 2, w, h))
        own = None
        for p, q in segs:
            if {p, q} == {centre(a), centre(b)}:
                own = (p, q)
        box = next((c for c in cands if free(c, None)), None) or next((c for c in cands if free(c, own)), cands[0])
        placed.append(box)
        end_pos.append((int(round(box[0])), int(round(box[1]))))

    block_pos = []
    for n, lines in blocks:
        node = nodes[n]
        (cx, cy), (w, h) = centre(node), text_size(lines)
        top, left = float(node.get("top", 0)), float(node.get("left", 0))
        cands = [(cx - w / 2, top + ICON + 22, w, h),                 # below (under the name)
                 (left + ICON + 12, cy - h / 2, w, h),                 # right
                 (left - w - 12, cy - h / 2, w, h),                    # left
                 (cx - w / 2, top - h - 8, w, h),                      # above
                 (left - w - 14, top + ICON + 14, w, h),               # below-left
                 (left + ICON + 14, top + ICON + 14, w, h),            # below-right
                 (left - w - 14, top - h - 6, w, h),                   # above-left
                 (left + ICON + 14, top - h - 6, w, h),                # above-right
                 (cx - w / 2, top + ICON + 22 + 30, w, h)]             # further below
        box = next((c for c in cands if free(c)), cands[0])
        placed.append(box)
        block_pos.append((int(round(box[0])), int(round(box[1]))))
    return end_pos, block_pos
