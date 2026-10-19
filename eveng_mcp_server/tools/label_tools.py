"""Canvas label tools: put interface IPs / VLANs next to link ends, and loopbacks /
SVIs under each node, as EVE-NG text objects."""

import asyncio
import base64
from typing import List, Optional, TYPE_CHECKING

from mcp.types import TextContent
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
    from ..core.eveng_client import EVENGClientWrapper

from ..config import get_logger
from ..core.console import ConsoleError, console_port, run_commands
from ..core.labels import (
    LABEL_PREFIX, encode_data, is_host, layout, node_block, parse_interfaces, parse_vpcs,
    port_label, text_object_html,
)
from ..core.power import is_running
from .power_tools import _pod

logger = get_logger("LabelTools")


class LabelArgs(BaseModel):
    lab_path: str = Field(description="Full path to the lab (e.g., /OSPF - Square.unl)")
    loopbacks: bool = Field(default=True, description="Also add a block under each node with loopback / VLAN interface IPs")
    pod: Optional[int] = Field(default=None, description="EVE-NG POD whose running nodes to read. Default: EVENG_CONSOLE_POD env, else 0 (admin)")


class LabPathArgs(BaseModel):
    lab_path: str = Field(description="Full path to the lab (e.g., /OSPF - Square.unl)")


def _node_id(ref: str) -> str:
    return str(ref).replace("node", "") if str(ref).startswith("node") else str(ref)


def _decode(data: str) -> str:
    try:
        return base64.b64decode(data).decode("utf-8", "replace")
    except Exception:
        return data or ""


def register_label_tools(mcp: "FastMCP", eveng_client: "EVENGClientWrapper") -> None:

    async def _clear(lab_path: str) -> int:
        objs = await eveng_client.list_text_objects(lab_path)
        mine = [int(k) for k, v in objs.items() if str(v.get("name", "")).startswith(LABEL_PREFIX)]
        for oid in mine:
            await eveng_client.delete_text_object(lab_path, oid)
        return len(mine)

    @mcp.tool()
    async def label_lab_ips(arguments: LabelArgs) -> list[TextContent]:
        """
        Label the lab canvas with addressing read from the running devices:
        - next to each cabled port: its IP/prefix (incl. subinterfaces such as
          router-on-a-stick); switch access ports facing an end host (e.g. VPCS) get
          'access 10'; trunks and switch-to-switch access ports get no label
        - under each node: loopbacks and VLAN interface (SVI) IPs, one per line
        Replaces labels from earlier runs; never touches text the user added.
        Close or refresh the lab in the browser afterwards to see them.
        """
        try:
            pod = _pod(arguments.pod)
            host = eveng_client.config.eveng.host
            await eveng_client.ensure_connected()
            nodes = (await eveng_client.list_nodes(arguments.lab_path)).get("data") or {}
            nodes = {str(n.get("id", k)): n for k, n in nodes.items()}
            topo = (await eveng_client.get_lab_topology(arguments.lab_path)).get("data") or []
            links = topo.values() if isinstance(topo, dict) else topo

            ids = sorted(nodes, key=int)
            up = await asyncio.gather(*(is_running(host, int(n), pod) for n in ids))
            running = [n for n, u in zip(ids, up) if u]

            async def read(nid):
                vpcs = str(nodes[nid].get("template", nodes[nid].get("type", ""))).lower() == "vpcs"
                cmd = "show ip" if vpcs else "show running-config | section ^interface"
                try:
                    t, errs = await run_commands(host, console_port(int(nid), pod), [cmd], timeout=30)
                    body = t.split("\n", 1)[1] if "\n" in t else ""
                    return parse_vpcs(body) if vpcs else parse_interfaces(body)
                except ConsoleError as e:
                    logger.warning(f"label read failed on node {nid}: {e}")
                    return None
            parsed = dict(zip(running, await asyncio.gather(*(read(n) for n in running))))

            ends, end_names = [], []
            for l in links:
                if l.get("source_type") != "node" or l.get("destination_type") != "node":
                    continue
                a, b = _node_id(l.get("source", "")), _node_id(l.get("destination", ""))
                for me, other, port in ((a, b, l.get("source_label")), (b, a, l.get("destination_label"))):
                    if me not in nodes or other not in nodes or not parsed.get(me) or not port:
                        continue
                    lines = port_label(port, parsed[me], to_host=is_host(nodes[other]))
                    if lines:
                        ends.append((me, other, lines))
                        end_names.append(f"{LABEL_PREFIX}{nodes[me]['name']}:{port}")
            blocks = []
            if arguments.loopbacks:
                for nid, ifaces in parsed.items():
                    lines = node_block(ifaces or {})
                    if lines:
                        blocks.append((nid, lines))
            end_xy, block_xy_ = layout(nodes, ends, blocks)
            labels = [(nm, x, y, e[2]) for nm, (x, y), e in zip(end_names, end_xy, ends)]
            labels += [(f"{LABEL_PREFIX}{nodes[n]['name']}:block", x, y, ln) for (n, ln), (x, y) in zip(blocks, block_xy_)]

            removed = await _clear(arguments.lab_path)
            existing = await eveng_client.list_text_objects(arguments.lab_path)
            next_id = max([int(k) for k in existing] + [0]) + 1
            made = 0
            for name, x, y, lines in labels:
                data = encode_data(text_object_html(next_id, x, y, lines))
                got = await eveng_client.add_text_object(arguments.lab_path, name, data)
                if got is not None and got != next_id:   # keep the HTML id in step with EVE-NG's id
                    await eveng_client.update_text_object(arguments.lab_path, got,
                                                          encode_data(text_object_html(got, x, y, lines)))
                next_id = (got or next_id) + 1
                made += 1

            skipped = [nodes[n]["name"] for n in ids if n not in running]
            summary = [f"{arguments.lab_path}: {made} labels added" + (f", {removed} old ones replaced" if removed else "")]
            summary += [f"   {name[len(LABEL_PREFIX):]}: " + " | ".join(lines) for name, _, _, lines in labels]
            if skipped:
                summary.append("Not running, so not labelled: " + ", ".join(skipped))
            unread = [nodes[n]["name"] for n, p in parsed.items() if not p]
            if unread:
                summary.append("Couldn't read config from: " + ", ".join(unread))
            summary.append("Refresh the lab in the browser to see them.")
            return [TextContent(type="text", text="\n".join(summary))]
        except Exception as e:
            logger.error(f"label_lab_ips failed: {e}")
            return [TextContent(type="text", text=f"Failed to label lab: {e}")]

    @mcp.tool()
    async def clear_lab_labels(arguments: LabPathArgs) -> list[TextContent]:
        """Remove the IP/VLAN labels added by label_lab_ips. Text you added yourself stays."""
        try:
            n = await _clear(arguments.lab_path)
            return [TextContent(type="text", text=f"Removed {n} labels from {arguments.lab_path}.")]
        except Exception as e:
            return [TextContent(type="text", text=f"Failed to clear labels: {e}")]

    @mcp.tool()
    async def list_text_objects(arguments: LabPathArgs) -> list[TextContent]:
        """List the text objects on a lab's canvas with their decoded HTML (for checking
        what EVE-NG stores, e.g. compare a label added in the web UI with ours)."""
        try:
            objs = await eveng_client.list_text_objects(arguments.lab_path)
            if not objs:
                return [TextContent(type="text", text="No text objects in this lab.")]
            out = []
            for k, v in sorted(objs.items(), key=lambda kv: int(kv[0])):
                out.append(f"#{k} name={v.get('name')!r} type={v.get('type')!r}\n{_decode(v.get('data', ''))[:600]}")
            return [TextContent(type="text", text="\n\n".join(out))]
        except Exception as e:
            return [TextContent(type="text", text=f"Failed to list text objects: {e}")]
