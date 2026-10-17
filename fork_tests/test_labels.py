"""Canvas IP/VLAN labels: parsing, text, placement, and the end-to-end tool."""

import asyncio
import base64

from mcp.server.fastmcp import FastMCP

from eveng_mcp_server.core.labels import (
    LABEL_PREFIX, block_xy, centre, link_label_xy, node_block, parse_interfaces, port_label,
    text_object_html,
)

R1 = """interface Loopback0
 ip address 1.1.1.1 255.255.255.255
 ip ospf 1 area 0
interface GigabitEthernet0/0
 ip address 10.0.12.1 255.255.255.252
interface GigabitEthernet0/2
 no ip address
interface GigabitEthernet0/2.10
 encapsulation dot1Q 10
 ip address 172.16.10.1 255.255.255.0
interface GigabitEthernet0/2.20
 encapsulation dot1Q 20
 ip address 172.16.20.1 255.255.255.0
interface GigabitEthernet0/3
 no ip address
 shutdown
"""

SW1 = """interface GigabitEthernet0/0
 description Trunk to R1 Gi0/2
 switchport trunk allowed vlan 10,20,30
 switchport trunk encapsulation dot1q
 switchport mode trunk
interface GigabitEthernet0/1
 switchport access vlan 10
 switchport mode access
interface Vlan10
 ip address 172.16.10.2 255.255.255.0
interface Vlan99
 no ip address
 shutdown
"""


def test_router_port_labels():
    i = parse_interfaces(R1)
    assert port_label("Gi0/0", i) == ["10.0.12.1/30"]
    assert port_label("GigabitEthernet0/0", i) == ["10.0.12.1/30"]
    assert port_label("Gi0/2", i) == [".10 172.16.10.1/24", ".20 172.16.20.1/24"]   # router-on-a-stick
    assert port_label("Gi0/3", i) == []


def test_switch_port_labels_and_svi_block():
    i = parse_interfaces(SW1)
    assert port_label("Gi0/0", i) == ["trunk 10,20,30"]
    assert port_label("Gi0/1", i) == ["access 10"]
    assert node_block(i) == ["Vlan10 172.16.10.2/24"]          # shutdown SVI without IP is left out


def test_loopback_block_one_per_line():
    i = parse_interfaces(R1 + "interface Loopback1\n ip address 11.11.11.11 255.255.255.255\n")
    assert node_block(i) == ["Lo0 1.1.1.1/32", "Lo1 11.11.11.11/32"]
    html = text_object_html(7, 10, 20, node_block(i))
    assert "Lo0 1.1.1.1/32<br>Lo1 11.11.11.11/32" in html       # real line break between lines
    assert 'id="customText7"' in html and "left: 10px; top: 20px" in html


def test_link_label_sits_beside_the_line_near_its_own_end():
    r1, r2 = {"left": 249, "top": 150}, {"left": 648, "top": 150}       # horizontal link
    x, y = link_label_xy(r1, r2, ["10.0.12.1/30"])
    cx1 = centre(r1)[0]
    assert cx1 < x + 40 < cx1 + 140          # near R1's end, not the middle
    assert y + 15 < centre(r1)[1]            # above the line, not on it
    x2, _ = link_label_xy(r2, r1, ["10.0.12.2/30"])
    assert x2 > x + 200                      # the other end's label is at the other end
    r4 = {"left": 249, "top": 498}           # vertical link: label beside, not on top of the line
    vx, _ = link_label_xy(r1, r4, ["10.0.14.2/30"])
    assert abs((vx + 40) - centre(r1)[0]) > 15


def test_block_goes_under_the_node():
    x, y = block_xy({"left": 249, "top": 150}, ["Lo0 1.1.1.1/32"])
    assert y >= 150 + 50 and abs((x + 45) - 274) < 20


def _call(tool, **args):
    from eveng_mcp_server.core.eveng_client import EVENGClientWrapper
    from eveng_mcp_server.tools.label_tools import register_label_tools
    mcp = FastMCP("t")
    register_label_tools(mcp, EVENGClientWrapper())
    r = asyncio.run(mcp.call_tool(tool, {"arguments": {"lab_path": "/CCNP - Test.unl", **args}}))
    content = r[0] if isinstance(r, tuple) else r
    return content[0].text


def test_label_tool_end_to_end_and_rerun_replaces(eve_env, fake_ios2):
    from eveng_mcp_server.core.eveng_client import EVENGClientWrapper
    c = EVENGClientWrapper()
    asyncio.run(c.connect_node_to_node("/CCNP - Test.unl", "1", "Gi0/0", "2", "Gi0/0"))
    # a text object the user made themselves must survive
    asyncio.run(c.add_text_object("/CCNP - Test.unl", "my note", base64.b64encode(b"<div>hi</div>").decode()))

    text = _call("label_lab_ips", pod=3)       # only R1 runs (fake_ios2 on POD 3)
    assert "R1:Gi0/0: 10.0.12.1/30" in text and "R1:block: Lo0 1.1.1.1/32" in text
    assert "Not running, so not labelled: R2, R3" in text
    objs = asyncio.run(c.list_text_objects("/CCNP - Test.unl"))
    mine = {k: v for k, v in objs.items() if v["name"].startswith(LABEL_PREFIX)}
    assert len(mine) == 2 and any(v["name"] == "my note" for v in objs.values())
    for k, v in mine.items():                   # HTML id matches EVE-NG's object id
        assert f'id="customText{k}"' in base64.b64decode(v["data"]).decode()

    _call("label_lab_ips", pod=3)               # rerun: replaced, not duplicated
    objs = asyncio.run(c.list_text_objects("/CCNP - Test.unl"))
    assert sum(v["name"].startswith(LABEL_PREFIX) for v in objs.values()) == 2

    assert "Removed 2 labels" in _call("clear_lab_labels")
    objs = asyncio.run(c.list_text_objects("/CCNP - Test.unl"))
    assert [v["name"] for v in objs.values()] == ["my note"]


def test_layout_avoids_lines_labels_and_nodes_on_the_real_ospf_lab():
    """Positions and configs from the real 'OSPF - Square' lab (the preview that caught overlaps)."""
    from eveng_mcp_server.core.labels import layout, text_size, _boxes_overlap, _seg_hits_box
    nodes = {"R1": {"left": 249, "top": 150}, "R2": {"left": 648, "top": 150}, "R3": {"left": 650, "top": 500},
             "R4": {"left": 249, "top": 498}, "SW1": {"left": 60, "top": 150}}
    r1 = parse_interfaces(R1 + "interface GigabitEthernet0/2.30\n ip address 172.16.30.1 255.255.255.0\n"
                          "interface GigabitEthernet0/1\n ip address 10.0.14.2 255.255.255.252\n")
    sw = parse_interfaces(SW1)
    lo = lambda n: parse_interfaces(f"interface Loopback0\n ip address {n}.{n}.{n}.{n} 255.255.255.255\n"
                                    f"interface GigabitEthernet0/0\n ip address 10.0.{n}.1 255.255.255.252\n"
                                    f"interface GigabitEthernet0/1\n ip address 10.0.{n}.5 255.255.255.252\n")
    P = {"R1": r1, "R2": lo(2), "R3": lo(3), "R4": lo(4), "SW1": sw}
    links = [("R1", "Gi0/0", "R2", "Gi0/0"), ("R2", "Gi0/1", "R3", "Gi0/0"), ("R3", "Gi0/1", "R4", "Gi0/0"),
             ("R4", "Gi0/1", "R1", "Gi0/1"), ("R1", "Gi0/2", "SW1", "Gi0/0")]
    ends = [(me, o, port_label(p, P[me])) for a, pa, b, pb in links for me, o, p in ((a, b, pa), (b, a, pb))]
    blocks = [(n, node_block(P[n])) for n in nodes if node_block(P[n])]
    exy, bxy = layout(nodes, ends, blocks)
    boxes = [(x, y, *text_size(l)) for (x, y), (_, _, l) in zip(exy, ends)] + \
            [(x, y, *text_size(l)) for (x, y), (_, l) in zip(bxy, blocks)]
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            assert not _boxes_overlap(a, b, pad=0), (a, b)
    segs = [(centre(nodes[a]), centre(nodes[b])) for a, _, b, _ in links]
    for box in boxes[len(ends):]:                       # node blocks never sit on a link line
        assert not any(_seg_hits_box(p, q, box, pad=0) for p, q in segs), box
