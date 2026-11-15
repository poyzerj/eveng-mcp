"""replace_nodes: swap IOL routers/switches for vIOS / vIOSL2, keeping cables, names,
positions and (rewritten) startup configs."""

import asyncio
import json
from typing import Dict, List, Optional, TYPE_CHECKING

from mcp.types import TextContent
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
    from ..core.eveng_client import EVENGClientWrapper

from ..config import get_logger
from ..core.power import is_running
from ..core.replace import is_iol, plan_node, translate_config
from .power_tools import _pod

logger = get_logger("ReplaceTools")


class ReplaceNodesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lab_path: str = Field(description="Full path to the lab (e.g., /CCNP/Lab 09.unl)")
    nodes: Optional[List[str]] = Field(default=None, description="IOL nodes to replace, by name or ID. Default: every IOL node in the lab. Use IDs if names repeat.")
    router_template: str = Field(default="vios", description="Template for IOL routers")
    switch_template: str = Field(default="viosl2", description="Template for IOL L2 switches")
    confirm: bool = Field(default=False, description="false: only show the plan (nothing changes). true: do it. Only set true after the user has approved the plan shown.")
    pod: Optional[int] = Field(default=None, description="POD to check for running nodes. Default: EVENG_CONSOLE_POD env, else 0 (admin)")


def _ports(ifaces: dict, media: str) -> Dict[str, tuple]:
    """{port_name: (index, network_id)} for cabled ports; handles list (qemu) and dict (IOL)."""
    e = (ifaces or {}).get(media) or []
    pairs = list(e.items()) if isinstance(e, dict) else list(enumerate(e))
    return {v.get("name", ""): (str(k), int(v.get("network_id") or 0))
            for k, v in pairs if isinstance(v, dict) and int(v.get("network_id") or 0)}


def register_replace_tools(mcp: "FastMCP", eveng_client: "EVENGClientWrapper") -> None:

    @mcp.tool()
    async def replace_nodes(arguments: ReplaceNodesArgs) -> list[TextContent]:
        """
        Replace IOL routers with vIOS and IOL L2 switches with vIOSL2, in place.
        For each node: creates the new node at the same spot, moves every cable to the
        matching port, deletes the old node, gives the new one the old name, and, if the old
        node has a startup-config, stores it with interface names rewritten.
        Port mapping: switches e<s>/<p> -> Gi<s>/<p>; routers e<s>/<p> -> Gi0/<s*4+p>.
        Nodes must be stopped. Call first with confirm=false to show the plan, show it to the
        user, and call again with confirm=true only after they approve.
        """
        try:
            await eveng_client.ensure_connected()
            lab = arguments.lab_path
            api = eveng_client.api
            nodes = (await asyncio.to_thread(api.list_nodes, lab)).get("data") or {}
            nodes = {str(n.get("id", k)): n for k, n in nodes.items()}

            if arguments.nodes:
                ids = [(await asyncio.to_thread(eveng_client._node_name, lab, r))[0] for r in arguments.nodes]
                not_iol = [f"{nodes[i]['name']} (ID {i})" for i in ids if not is_iol(nodes[i])]
                if not_iol:
                    return [TextContent(type="text", text="Not IOL nodes, nothing to replace: " + ", ".join(not_iol))]
            else:
                ids = [i for i, n in nodes.items() if is_iol(n)]
            if not ids:
                return [TextContent(type="text", text=f"No IOL nodes in {lab}.")]

            host, pod = eveng_client.config.eveng.host, _pod(arguments.pod)
            up = await asyncio.gather(*(is_running(host, int(i), pod) for i in ids))
            running = [f"{nodes[i]['name']} (ID {i})" for i, u in zip(ids, up) if u]
            if running:
                return [TextContent(type="text", text=(
                    "Stop these nodes first (their console ports are open): " + ", ".join(running)
                    + ". If they aren't running in this lab, another lab in the same POD may be using the same node IDs."))]

            # who is on each network, to describe the far end of each cable
            ifs = {i: (await asyncio.to_thread(api.get_node_interfaces, lab, i)).get("data") or {} for i in nodes}
            members: Dict[int, List[str]] = {}
            for i, d in ifs.items():
                for pname, (_idx, net) in _ports(d, "ethernet").items():
                    members.setdefault(net, []).append(f"{nodes[i]['name']} {pname}")
            netnames = {int(k): v.get("name", "") for k, v in
                        ((await asyncio.to_thread(api.list_lab_networks, lab)).get("data") or {}).items()}

            plans = []
            for i in ids:
                p = plan_node(nodes[i], _ports(ifs[i], "ethernet"), list(_ports(ifs[i], "serial")),
                              arguments.router_template, arguments.switch_template)
                cfg = await asyncio.to_thread(api.get_node_config_by_id, lab, int(i))
                cfg = (cfg or {}).get("data")
                cfg = cfg.get("data", "") if isinstance(cfg, dict) else (cfg or "")
                p["config"], p["renamed"] = translate_config(cfg, p["kind"]) if cfg.strip() else ("", 0)
                plans.append(p)

            lines = [f"{'Replacing' if arguments.confirm else 'Plan for'} {lab}:"]
            for p in plans:
                lines.append(f"\n{p['name']} (ID {p['id']}, IOL {p['kind']}) -> {p['template']}, {p['ethernet']} ports")
                for old, new, net in p["mapping"]:
                    far = [m for m in members.get(net, []) if not m.startswith(f"{p['name']} {old}")]
                    lines.append(f"   {old} -> {new}   ({', '.join(far) or 'network ' + netnames.get(net, str(net))})")
                if not p["mapping"]:
                    lines.append("   (no cabled ports)")
                lines.append(f"   startup-config: {'yes, ' + str(p['renamed']) + ' interface name(s) rewritten' if p['config'] else 'none'}")
                for prob in p["problems"]:
                    lines.append(f"   SKIPPED: {prob}")
            todo = [p for p in plans if not p["problems"]]

            if not arguments.confirm:
                lines.append("\nNothing changed yet. Show this to the user; run again with confirm=true once they approve.")
                return [TextContent(type="text", text="\n".join(lines))]

            done, failed = [], []
            for p in todo:
                try:
                    # refresh this node's cables: an earlier swap may have rebuilt a shared bridge
                    cur = (await asyncio.to_thread(api.get_node_interfaces, lab, p["id"])).get("data") or {}
                    fresh = plan_node(nodes[p["id"]], _ports(cur, "ethernet"), [],
                                      arguments.router_template, arguments.switch_template)
                    p["mapping"], p["ethernet"] = fresh["mapping"], fresh["ethernet"]
                    done.append(await _replace_one(eveng_client, lab, p))
                except Exception as e:
                    logger.error(f"replace {p['name']} failed: {e}")
                    failed.append(f"{p['name']} (ID {p['id']}): {e}")
                    break   # stop: later nodes may be cabled to this one
            lines.append("")
            lines += [f"Done: {d}" for d in done]
            if failed:
                lines.append("FAILED, stopped here: " + failed[0])
                rest = [p["name"] for p in todo[len(done) + 1:]]
                if rest:
                    lines.append("Not attempted: " + ", ".join(rest))
            lines.append("Refresh the lab in the browser. Run label_lab_ips again once the new nodes are running.")
            return [TextContent(type="text", text="\n".join(lines))]
        except Exception as e:
            logger.error(f"replace_nodes failed: {e}")
            return [TextContent(type="text", text=f"Failed to replace nodes: {e}")]


async def _replace_one(c: "EVENGClientWrapper", lab: str, p: dict) -> str:
    api = c.api

    def _do():
        # 1. new node beside the old one's spot, under a temporary name (EVE-NG won't add a duplicate name)
        ntype = "qemu"
        r = api.add_node(lab, p["template"], name=f"{p['name']}-new", node_type=ntype,
                         left=int(p["left"]), top=int(p["top"]), ethernet=int(p["ethernet"]))
        new_id = str(((r or {}).get("data") or {}).get("id") or "")
        if not new_id:
            raise ValueError(f"EVE-NG didn't create the {p['template']} node: {r}")
        # 2. move every cable: attach the new port to the same network (old port is still on it)
        for old, new, net in p["mapping"]:
            idx, _ = c._interface_index(lab, new_id, new)
            c._attach(lab, new_id, idx, net)
        # remember who else is on each network, in case EVE-NG drops a bridge with the old node
        nets = {net for _, _, net in p["mapping"]}
        others: Dict[int, list] = {n: [] for n in nets}
        for nid in (api.list_nodes(lab).get("data") or {}):
            if str(nid) in (p["id"],):
                continue
            d = (api.get_node_interfaces(lab, nid) or {}).get("data") or {}
            for _name, (idx, net) in _ports(d, "ethernet").items():
                if net in others:
                    others[net].append((str(nid), idx))
        # 3. delete the old node, then make sure every cable still has its network
        api.delete_node(lab, p["id"])
        alive = {int(k) for k in (api.list_lab_networks(lab).get("data") or {})}
        rebuilt = 0
        for net in nets:
            if net in alive:
                continue
            nr = c._retry_lab_write(lambda: api.add_lab_network(lab, network_type="bridge", visibility="1",
                                                                name=f"{p['name']}-{net}-relink"))
            nid2 = (nr or {}).get("data", {}).get("id")
            for node_id, idx in others[net]:
                c._attach(lab, node_id, idx, nid2)
            c._retry_lab_write(lambda: api.edit_lab_network(lab, nid2, data={"visibility": "0"}))
            rebuilt += 1
        # 4. give it the old name
        norm = api.normalize_path(lab)
        c._retry_lab_write(lambda: c.client.put(f"/labs{norm}/nodes/{new_id}",
                                                data=json.dumps({"id": int(new_id), "name": p["name"]})))
        return new_id, rebuilt
    new_id, rebuilt = await asyncio.to_thread(_do)
    # 5. startup-config, rewritten for the new interface names
    cfg_note = "no startup-config"
    if p["config"]:
        n = await c.set_startup_config(lab, new_id, p["config"])
        cfg_note = f"startup-config stored ({n} chars, {p['renamed']} interface name(s) rewritten)"
    extra = f", {rebuilt} cable(s) rebuilt" if rebuilt else ""
    return f"{p['name']}: old ID {p['id']} -> new ID {new_id} ({p['template']}), {len(p['mapping'])} cable(s) moved{extra}, {cfg_note}"
