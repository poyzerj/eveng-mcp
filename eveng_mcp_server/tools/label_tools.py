"""Canvas label tools: put interface IPs / VLANs next to link ends, and loopbacks /
SVIs under each node, as EVE-NG text objects."""

import asyncio
import base64
from typing import List, Optional, TYPE_CHECKING

from mcp.types import TextContent
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
    from ..core.eveng_client import EVENGClientWrapper

from ..config import get_logger
from ..core.console import ConsoleError, console_port, run_commands
from ..core.labels import (
    LABEL_PREFIX, NOTE_PREFIX, encode_data, is_host, layout, node_block, note_xy, object_box,
    parse_interfaces, parse_vpcs, port_label, safe_color, text_object_html,
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
        - next to each cabled port: its IPv4 and IPv6 addresses (incl. subinterfaces such as
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
                cmds = ["show ip", "show ipv6"] if vpcs else ["show running-config | section ^interface"]
                try:
                    t, errs = await run_commands(host, console_port(int(nid), pod), cmds, timeout=30)
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
            try:   # steer around notes the user (or add_lab_text) put on the canvas
                existing0 = await eveng_client.list_text_objects(arguments.lab_path)
                avoid = [b for v in existing0.values()
                         if not str(v.get("name", "")).startswith(LABEL_PREFIX)
                         for b in [object_box(_decode(v.get("data", "")))] if b]
            except Exception:
                avoid = []
            end_xy, block_xy_ = layout(nodes, ends, blocks, avoid)
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


class AddTextArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lab_path: str = Field(description="Full path to the lab (e.g., /OSPF - Square.unl)")
    text: str = Field(description="The note. Use a new line (\\n) for each line, e.g. 'vlan 50\\nGroup50'.")
    node: Optional[str] = Field(default=None, description="Put the note beside this node (name or ID). Leave empty and give left/top for a free spot.")
    position: str = Field(default="below", description="Beside the node: below, above, left or right")
    left: Optional[int] = Field(default=None, description="Exact pixels from the left of the canvas (overrides node placement)")
    top: Optional[int] = Field(default=None, description="Exact pixels from the top of the canvas (overrides node placement)")
    color: str = Field(default="#000000", description="Text colour, e.g. #000000, red, rgb(0,51,153)")
    background: str = Field(default="rgba(255, 255, 255, 0.85)", description="Background colour, e.g. #dbeefb for a light blue box, or 'transparent'")
    font_size: int = Field(default=11, ge=8, le=28, description="Font size in px (8-28)")
    bold: bool = Field(default=False, description="Bold text")


class DeleteTextArgs(BaseModel):
    lab_path: str = Field(description="Full path to the lab (e.g., /OSPF - Square.unl)")
    ids: Optional[List[int]] = Field(default=None, description="Text object IDs to delete (from list_text_objects)")
    all_notes: bool = Field(default=False, description="Delete every note added with add_lab_text (IP labels and your own web-UI text are kept)")


def register_note_tools(mcp: "FastMCP", eveng_client: "EVENGClientWrapper") -> None:

    @mcp.tool()
    async def add_lab_text(arguments: AddTextArgs) -> list[TextContent]:
        """
        Put your own text on the lab canvas: VLAN/group names under PCs, notes like
        'D2 is acting as a L2 switch', a lab title, etc. Multi-line text: one line per \\n.
        Place it beside a node (below/above/left/right, nudged off other text) or at exact
        left/top pixels. Notes are kept when label_lab_ips reruns or clear_lab_labels runs,
        and label_lab_ips steers its IP labels around them.
        Refresh the lab in the browser to see it.
        """
        try:
            await eveng_client.ensure_connected()
            lines = [l.rstrip() for l in arguments.text.replace("\\n", "\n").split("\n")]
            while lines and not lines[-1]:
                lines.pop()
            if not lines:
                return [TextContent(type="text", text="Nothing to add - the text is empty.")]
            node = None
            if arguments.node:
                nid, _ = await asyncio.to_thread(eveng_client._node_name, arguments.lab_path, arguments.node)
                nodes = (await eveng_client.list_nodes(arguments.lab_path)).get("data") or {}
                node = next((n for k, n in nodes.items() if str(n.get("id", k)) == str(nid)), None)
            elif arguments.left is None or arguments.top is None:
                return [TextContent(type="text", text="Give a node to put the text beside, or both left and top.")]
            existing = await eveng_client.list_text_objects(arguments.lab_path)
            taken = [b for v in existing.values() for b in [object_box(_decode(v.get("data", "")))] if b]
            x, y = note_xy(node, arguments.position, lines, arguments.font_size, taken, arguments.left, arguments.top)
            color = safe_color(arguments.color, "#000000")
            bg = safe_color(arguments.background, "rgba(255, 255, 255, 0.85)")
            next_id = max([int(k) for k in existing] + [0]) + 1
            name = f"{NOTE_PREFIX}{lines[0][:40]}"
            mk = lambda i: encode_data(text_object_html(i, x, y, lines, color, bg, arguments.font_size, arguments.bold))
            got = await eveng_client.add_text_object(arguments.lab_path, name, mk(next_id))
            if got is not None and got != next_id:
                await eveng_client.update_text_object(arguments.lab_path, got, mk(got))
            return [TextContent(type="text", text=f"Added text #{got or next_id} at ({x}, {y}) px: " + " | ".join(lines)
                                + "\nRefresh the lab in the browser to see it.")]
        except Exception as e:
            logger.error(f"add_lab_text failed: {e}")
            return [TextContent(type="text", text=f"Failed to add text: {e}")]

    @mcp.tool()
    async def delete_lab_text(arguments: DeleteTextArgs) -> list[TextContent]:
        """Delete canvas text by ID (see list_text_objects), or every note made with
        add_lab_text (all_notes=true). Use clear_lab_labels for the IP labels."""
        try:
            objs = await eveng_client.list_text_objects(arguments.lab_path)
            targets = set(arguments.ids or [])
            if arguments.all_notes:
                targets |= {int(k) for k, v in objs.items() if str(v.get("name", "")).startswith(NOTE_PREFIX)}
            if not targets:
                return [TextContent(type="text", text="Nothing to delete: give ids, or all_notes=true.")]
            missing = sorted(t for t in targets if str(t) not in objs)
            gone = []
            for oid in sorted(targets - set(missing)):
                await eveng_client.delete_text_object(arguments.lab_path, oid)
                gone.append(oid)
            msg = f"Deleted {len(gone)} text object(s)" + (f": {gone}" if gone else "")
            if missing:
                msg += f". Not found: {missing}"
            return [TextContent(type="text", text=msg + ".")]
        except Exception as e:
            return [TextContent(type="text", text=f"Failed to delete text: {e}")]
