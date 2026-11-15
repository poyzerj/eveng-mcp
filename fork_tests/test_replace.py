"""replace_nodes: IOL -> vIOS/vIOSL2 with cables, names, positions and startup configs kept."""

import asyncio
import os
import urllib.request

from mcp.server.fastmcp import FastMCP

from eveng_mcp_server.core.replace import map_port, port_count, translate_config

LAB = "/CCNP - Test.unl"


def test_port_mapping_rules():
    assert map_port("switch", "e0/1") == "Gi0/1" and map_port("switch", "e1/2") == "Gi1/2"
    assert map_port("router", "e0/3") == "Gi0/3" and map_port("router", "e1/0") == "Gi0/4"
    assert map_port("router", "e2/1") == "Gi0/9" and map_port("router", "s0/0") is None
    assert port_count("switch", ["Gi1/2"]) == 8 and port_count("switch", ["Gi0/1"]) == 4
    assert port_count("router", ["Gi0/9"]) == 10 and port_count("router", []) == 4


def test_config_rewrite():
    cfg = ("interface Ethernet0/0\n ip address 10.0.0.1 255.255.255.0\ninterface Ethernet1/0.10\n"
           "router ospf 1\n passive-interface Ethernet1/0\ninterface GigabitEthernet0/0\n")
    out, n = translate_config(cfg, "router")
    assert "interface GigabitEthernet0/0\n" in out and "interface GigabitEthernet0/4.10" in out
    assert "passive-interface GigabitEthernet0/4" in out and n == 3       # existing Gi name untouched
    out, n = translate_config("interface Ethernet1/2\n channel-group 1 mode active\n", "switch")
    assert out.startswith("interface GigabitEthernet1/2") and n == 1


def _call(**args):
    from eveng_mcp_server.core.eveng_client import EVENGClientWrapper
    from eveng_mcp_server.tools.replace_tools import register_replace_tools
    mcp = FastMCP("t")
    register_replace_tools(mcp, EVENGClientWrapper())
    r = asyncio.run(mcp.call_tool("replace_nodes", {"arguments": {"lab_path": LAB, "pod": 5, **args}}))
    content = r[0] if isinstance(r, tuple) else r
    return content[0].text


def _state():
    from eveng_mcp_server.core.eveng_client import EVENGClientWrapper
    c = EVENGClientWrapper()
    asyncio.run(c.connect())
    nodes = c.api.list_nodes(LAB)["data"]
    by_name = {n["name"]: str(n["id"]) for n in nodes.values()}
    ports = {}
    for nid, n in nodes.items():
        e = c.api.get_node_interfaces(LAB, nid)["data"]["ethernet"]
        for i in (e.values() if isinstance(e, dict) else e):
            if i["network_id"]:
                ports[(n["name"], i["name"])] = i["network_id"]
    cfg = lambda nid: c.api.get_node_config_by_id(LAB, int(nid))["data"]["data"]
    return nodes, by_name, ports, cfg


def _swap_and_check(prune: bool):
    base = f"http://127.0.0.1:{os.environ['EVENG_PORT']}"
    urllib.request.urlopen(base + "/test/iol_lab")
    if prune:
        urllib.request.urlopen(base + "/test/prune_on_delete")

    plan = _call()
    assert "Plan for" in plan and "Nothing changed yet" in plan
    assert "e0/0 -> Gi0/0   (SW1 e1/2)" in plan and "e1/0 -> Gi0/4   (R3 e0/1)" in plan
    assert "SW1 (ID 2, IOL switch) -> viosl2, 8 ports" in plan
    assert "startup-config: yes, 3 interface name(s) rewritten" in plan
    assert set(_state()[1].values()) == {"1", "2", "3"}            # nothing changed

    text = _call(confirm=True)
    assert text.count("Done:") == 3 and "FAILED" not in text, text
    nodes, by_name, ports, cfg = _state()
    assert set(by_name) == {"R1", "SW1", "R3"} and not set(by_name.values()) & {"1", "2", "3"}
    tmpl = {n["name"]: n["template"] for n in nodes.values()}
    assert tmpl == {"R1": "vios", "SW1": "viosl2", "R3": "vios"}
    pos = {n["name"]: (n["left"], n["top"]) for n in nodes.values()}
    assert pos["R1"] == (100, 100) and pos["SW1"] == (300, 100)
    assert ports[("R1", "Gi0/0")] == ports[("SW1", "Gi1/2")]       # cables follow the mapping
    assert ports[("R1", "Gi0/4")] == ports[("R3", "Gi0/1")]
    assert len(ports) == 4
    r1 = cfg(by_name["R1"])
    assert "interface GigabitEthernet0/4" in r1 and "passive-interface GigabitEthernet0/4" in r1
    assert "Ethernet0/0" not in r1.replace("GigabitEthernet0/0", "")
    assert "interface GigabitEthernet1/2" in cfg(by_name["SW1"])
    assert nodes[by_name["R1"]].get("config") == 1                  # boots from the stored config
    return text


def test_replace_keeps_cables_names_and_configs(eve_env):
    text = _swap_and_check(prune=False)
    assert "rebuilt" not in text


def test_replace_rebuilds_cables_if_eve_drops_bridges(eve_env):
    text = _swap_and_check(prune=True)
    assert "rebuilt" in text


def test_replace_refuses_running_nodes_and_needs_iol(eve_env):
    import socket
    from eveng_mcp_server.core.console import console_port
    urllib.request.urlopen(f"http://127.0.0.1:{os.environ['EVENG_PORT']}/test/iol_lab")
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", console_port(2, 5))); s.listen()
    try:
        assert "Stop these nodes first" in _call(confirm=True)
    finally:
        s.close()
    assert set(_state()[1].values()) == {"1", "2", "3"}
