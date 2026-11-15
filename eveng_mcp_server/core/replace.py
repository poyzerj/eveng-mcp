"""Replace IOL nodes with vIOS (routers) / vIOSL2 (switches): port mapping and config rewrite.

Port mapping:
  switch  IOL e<slot>/<port> -> vIOSL2 Gi<slot>/<port>   (vIOSL2 numbers 4 ports per slot, like IOL)
  router  IOL e<slot>/<port> -> vIOS   Gi0/<slot*4+port> (vIOS has a single slot)
Serial ports have no vIOS equivalent, so a node with a cabled serial port is skipped.
"""

import re
from typing import Dict, List, Optional, Tuple

_IOL_PORT = re.compile(r"^(?:e|eth|ethernet)\s*(\d+)/(\d+)$", re.I)
# A full interface name in a saved IOS config. Not preceded by a letter, so
# GigabitEthernet/FastEthernet are left alone.
_CFG_ETH = re.compile(r"(?<![A-Za-z])Ethernet(\d+)/(\d+)")


def is_iol(node: dict) -> bool:
    return str(node.get("type", "")).lower() == "iol" or str(node.get("template", "")).lower() == "iol"


def iol_kind(node: dict) -> str:
    """'switch' for the IOL L2 image (i86bi_linux_l2...), else 'router'."""
    image = str(node.get("image", "")).lower()
    return "switch" if "l2" in image else "router"


def map_port(kind: str, name: str) -> Optional[str]:
    """New vIOS/vIOSL2 port name for an IOL port, or None (serial or unknown)."""
    m = _IOL_PORT.match(str(name).strip())
    if not m:
        return None
    slot, port = int(m.group(1)), int(m.group(2))
    return f"Gi{slot}/{port}" if kind == "switch" else f"Gi0/{slot * 4 + port}"


def port_count(kind: str, new_ports: List[str], minimum: int = 4) -> int:
    """How many ethernet ports the new node needs to have every mapped port."""
    need = minimum
    for p in new_ports:
        m = re.match(r"Gi(\d+)/(\d+)$", p)
        if not m:
            continue
        slot, port = int(m.group(1)), int(m.group(2))
        need = max(need, (slot + 1) * 4 if kind == "switch" else port + 1)
    if kind == "switch":
        need = -(-need // 4) * 4          # whole slots of 4
    return need


def translate_config(config: str, kind: str) -> Tuple[str, int]:
    """Rewrite IOL interface names (Ethernet0/1) to the new node's (GigabitEthernet...).
    Returns (new_config, number_of_names_changed)."""
    count = 0

    def sub(m):
        nonlocal count
        count += 1
        slot, port = int(m.group(1)), int(m.group(2))
        return (f"GigabitEthernet{slot}/{port}" if kind == "switch"
                else f"GigabitEthernet0/{slot * 4 + port}")
    return _CFG_ETH.sub(sub, config or ""), count


def plan_node(node: dict, ports: Dict[str, Tuple[str, int]], serial_cabled: List[str],
              router_template: str, switch_template: str) -> dict:
    """Plan one node. ports: {old_port_name: (port_index, network_id)} for cabled ports."""
    kind = iol_kind(node)
    mapping, problems = [], []
    for old, (_idx, net) in sorted(ports.items(), key=lambda kv: int(kv[1][0])):
        new = map_port(kind, old)
        if new is None:
            problems.append(f"port {old} has no vIOS equivalent")
        else:
            mapping.append((old, new, net))
    for s in serial_cabled:
        problems.append(f"serial port {s} is cabled (vIOS has no serial ports)")
    return {
        "id": str(node.get("id")), "name": node.get("name", ""), "kind": kind,
        "template": switch_template if kind == "switch" else router_template,
        "left": node.get("left", 0), "top": node.get("top", 0),
        "mapping": mapping, "problems": problems,
        "ethernet": port_count(kind, [n for _, n, _ in mapping]),
    }
