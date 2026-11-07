"""add_node picks the node type from the template; start refuses a console port already in use."""

import asyncio
import socket

import pytest
from mcp.server.fastmcp import FastMCP

LAB = "/CCNP - Test.unl"


def _add(**args):
    from eveng_mcp_server.core.eveng_client import EVENGClientWrapper
    from eveng_mcp_server.tools.node_management import register_node_tools
    mcp = FastMCP("t")
    register_node_tools(mcp, EVENGClientWrapper())
    try:
        r = asyncio.run(mcp.call_tool("add_node", {"arguments": {"lab_path": LAB, **args}}))
    except Exception as e:          # argument validation errors
        return f"ERROR {e}"
    content = r[0] if isinstance(r, tuple) else r
    return content[0].text


def _stored(name):
    from eveng_mcp_server.core.eveng_client import EVENGClientWrapper
    c = EVENGClientWrapper()
    asyncio.run(c.connect())
    nodes = c.api.list_nodes(LAB)["data"]
    return next(n for n in nodes.values() if n.get("name") == name)


def test_vpcs_gets_vpcs_type_without_asking(eve_env):
    text = _add(template="vpcs", name="PC1")
    assert "Type: vpcs" in text and "Image: (none)" in text
    n = _stored("PC1")
    assert n["type"] == "vpcs" and not n.get("image")


def test_qemu_template_still_qemu_with_its_image(eve_env):
    text = _add(template="vios", name="R9")
    n = _stored("R9")
    assert n["type"] == "qemu" and n["image"] == "vios-image"
    assert "Type: qemu" in text and "Image: vios-image" in text   # read back, not echoed


def test_wrong_or_unknown_type_is_refused(eve_env):
    assert "runs as node type 'vpcs'" in _add(template="vpcs", name="PCx", node_type="iol")
    assert "Unknown node_type" in _add(template="vios", name="Rx", node_type="bogus_value_xyz")
    assert "ERROR" in _add(template="vpcs", name="PCy", type="iol")      # misspelled field is rejected
    names = [n.get("name") for n in _all_nodes()]
    assert not {"PCx", "Rx", "PCy"} & set(names)


def _all_nodes():
    from eveng_mcp_server.core.eveng_client import EVENGClientWrapper
    c = EVENGClientWrapper()
    asyncio.run(c.connect())
    return list(c.api.list_nodes(LAB)["data"].values())


def test_start_refuses_console_port_already_in_use(eve_env, ssh_env):
    from eveng_mcp_server.core.console import console_port
    from eveng_mcp_server.core.eveng_client import EVENGClientWrapper
    from eveng_mcp_server.tools.power_tools import do_power
    # another lab's node 1 in POD 4 is "running" on the same console port
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", console_port(1, 4))); s.listen()
    try:
        text = asyncio.run(do_power(EVENGClientWrapper(), LAB, "start", ["R1"], pod=4))
    finally:
        s.close()
    assert "already in use" in text and "Not started" in text
    assert not [l for l in ssh_env["log"].read_text().splitlines() if l.startswith("CMD")]
