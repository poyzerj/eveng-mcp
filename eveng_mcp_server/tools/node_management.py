"""Node management tools for EVE-NG MCP Server."""

import json
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from mcp.types import TextContent, Tool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
    from ..core.eveng_client import EVENGClientWrapper

from ..config import get_logger
from ..core.exceptions import EVENGAPIError


logger = get_logger("NodeManagementTools")


def _get_status_text(status: int) -> str:
    """Convert node status code to human-readable text."""
    status_map = {
        0: "Stopped",
        1: "Starting",
        2: "Running",
        3: "Stopping"
    }
    return status_map.get(status, f"Unknown ({status})")


class ListNodesArgs(BaseModel):
    """Arguments for list_nodes tool."""
    lab_path: str = Field(description="Full path to the lab (e.g., /lab_name.unl)")


class AddNodeArgs(BaseModel):
    """Arguments for add_node tool."""
    lab_path: str = Field(description="Full path to the lab (e.g., /lab_name.unl)")
    template: str = Field(description="Node template name (e.g., 'vios', 'linux', 'iol')")
    name: str = Field(default="", description="Node name (optional, auto-generated if empty)")
    node_type: str = Field(default="qemu", description="Node type (qemu, iol, dynamips)")
    left: Optional[int] = Field(default=None, description="Pixels from the left of the canvas. Leave empty to auto-place without overlapping.")
    top: Optional[int] = Field(default=None, description="Pixels from the top of the canvas. Leave empty to auto-place without overlapping.")
    delay: int = Field(default=0, description="Seconds to wait before starting node")
    console: str = Field(default="telnet", description="Console type (telnet, vnc)")
    config: str = Field(default="Unconfigured", description="Config state (Unconfigured, Saved)")
    ethernet: Optional[int] = Field(default=None, description="Number of ethernet interfaces")
    serial: Optional[int] = Field(default=None, description="Number of serial interfaces")
    image: Optional[str] = Field(default=None, description="Specific image to use")
    ram: Optional[int] = Field(default=None, description="RAM in MB")
    cpu: Optional[int] = Field(default=None, description="Number of CPUs")


class NodeControlArgs(BaseModel):
    """Arguments for node control operations."""
    lab_path: str = Field(description="Full path to the lab (e.g., /lab_name.unl)")
    node_id: str = Field(description="Node ID to control")


class BulkNodeControlArgs(BaseModel):
    """Arguments for bulk node operations."""
    lab_path: str = Field(description="Full path to the lab (e.g., /lab_name.unl)")


class GetNodeDetailsArgs(BaseModel):
    """Arguments for get_node_details tool."""
    lab_path: str = Field(description="Full path to the lab (e.g., /lab_name.unl)")
    node_id: str = Field(description="Node ID to get details for")


class DeleteNodeArgs(BaseModel):
    """Arguments for delete_node tool."""
    lab_path: str = Field(description="Full path to the lab (e.g., /lab_name.unl)")
    node_id: str = Field(description="Node ID to delete")


class ListTemplatesArgs(BaseModel):
    """Arguments for list_node_templates tool."""
    pass  # No arguments needed


class MoveNodeArgs(BaseModel):
    """Arguments for move_node tool."""
    lab_path: str = Field(description="Full path to the lab (e.g., /lab_name.unl)")
    node_id: str = Field(description="Node ID to move")
    left: int = Field(description="Pixels from the left edge of the canvas")
    top: int = Field(description="Pixels from the top edge of the canvas")


class ArrangeNodesArgs(BaseModel):
    """Arguments for arrange_nodes tool."""
    lab_path: str = Field(description="Full path to the lab (e.g., /lab_name.unl)")
    layout: str = Field(default="row", description="'row', 'column', or 'grid'")
    spacing: int = Field(default=350, description="Pixels between nodes")
    columns: int = Field(default=4, description="Nodes per row when layout is 'grid'")
    start_left: int = Field(default=150, description="Pixels from left for the first node")
    start_top: int = Field(default=200, description="Pixels from top for the first node")
    order: Optional[List[str]] = Field(default=None, description="Optional node names or IDs in the order to place them")


def register_node_tools(mcp: "FastMCP", eveng_client: "EVENGClientWrapper") -> None:
    """Register node management tools."""

    @mcp.tool()
    async def list_node_templates(arguments: ListTemplatesArgs) -> list[TextContent]:
        """
        List available node templates in EVE-NG.

        This tool retrieves all available node templates that can be used
        to create nodes in labs, including their supported images and options.
        """
        try:
            logger.info("Listing available node templates")

            try:

                await eveng_client.ensure_connected()

            except Exception:

                pass

            if not eveng_client.is_connected:
                return [TextContent(
                    type="text",
                    text="Not connected to EVE-NG server. Use connect_eveng_server tool first."
                )]

            # Get templates
            templates = await eveng_client.list_node_templates()

            if not templates.get('data'):
                return [TextContent(
                    type="text",
                    text="No node templates found on the server."
                )]

            # Format templates information
            templates_text = "Available Node Templates:\n\n"

            for template_name, template_info in templates['data'].items():
                # Newer EVE-NG returns {name: "Description"}; '.missing' means no image installed
                if isinstance(template_info, str):
                    if template_info.endswith('.missing'):
                        continue
                    template_info = {'description': template_info}
                templates_text += f"📦 {template_name}\n"
                templates_text += f"   Type: {template_info.get('type', 'Unknown')}\n"
                templates_text += f"   Description: {template_info.get('description', 'No description')}\n"

                # Show available images if any
                if 'listimages' in template_info and template_info['listimages']:
                    templates_text += f"   Images: {', '.join(template_info['listimages'])}\n"

                templates_text += "\n"

            return [TextContent(
                type="text",
                text=templates_text
            )]

        except Exception as e:
            logger.error(f"Failed to list node templates: {e}")
            return [TextContent(
                type="text",
                text=f"Failed to list node templates: {str(e)}"
            )]

    @mcp.tool()
    async def list_nodes(arguments: ListNodesArgs) -> list[TextContent]:
        """
        List all nodes in a lab.

        This tool retrieves information about all nodes in the specified lab,
        including their status, configuration, and connectivity.
        """
        try:
            logger.info(f"Listing nodes in lab: {arguments.lab_path}")

            try:

                await eveng_client.ensure_connected()

            except Exception:

                pass

            if not eveng_client.is_connected:
                return [TextContent(
                    type="text",
                    text="Not connected to EVE-NG server. Use connect_eveng_server tool first."
                )]

            # Get nodes
            nodes = await eveng_client.list_nodes(arguments.lab_path)

            if not nodes.get('data'):
                return [TextContent(
                    type="text",
                    text=f"No nodes found in lab: {arguments.lab_path}"
                )]

            # Format nodes information
            nodes_text = f"Nodes in {arguments.lab_path}:\n\n"

            for node_id, node in nodes['data'].items():
                status_icon = "🟢" if node.get('status') == 2 else "🔴" if node.get('status') == 1 else "⚪"
                nodes_text += f"{status_icon} {node.get('name', f'Node {node_id}')} (ID: {node_id})\n"
                nodes_text += f"   Template: {node.get('template', 'Unknown')}\n"
                nodes_text += f"   Type: {node.get('type', 'Unknown')}\n"
                nodes_text += f"   Image: {node.get('image', 'Unknown')}\n"
                nodes_text += f"   Status: {_get_status_text(node.get('status', 0))}\n"
                nodes_text += f"   Console: {node.get('console', 'Unknown')}\n"
                nodes_text += f"   CPU: {node.get('cpu', 'Unknown')}\n"
                nodes_text += f"   RAM: {node.get('ram', 'Unknown')} MB\n"
                nodes_text += f"   Position: ({node.get('left', 0)}, {node.get('top', 0)}) px\n"
                nodes_text += "\n"

            return [TextContent(
                type="text",
                text=nodes_text
            )]

        except Exception as e:
            logger.error(f"Failed to list nodes: {e}")
            return [TextContent(
                type="text",
                text=f"Failed to list nodes: {str(e)}"
            )]

    @mcp.tool()
    async def add_node(arguments: AddNodeArgs) -> list[TextContent]:
        """
        Add a node to a lab.

        This tool adds a new node to an existing lab with the specified
        template, configuration, and positioning. The node will be created
        but not automatically started.
        """
        try:
            logger.info(f"Adding node to lab: {arguments.lab_path}")

            try:

                await eveng_client.ensure_connected()

            except Exception:

                pass

            if not eveng_client.is_connected:
                return [TextContent(
                    type="text",
                    text="Not connected to EVE-NG server. Use connect_eveng_server tool first."
                )]

            # Auto-place on a grid when no position given (EVE-NG uses pixels)
            left, top = arguments.left, arguments.top
            if left is None or top is None:
                try:
                    existing = len((await eveng_client.list_nodes(arguments.lab_path)).get("data") or {})
                except Exception:
                    existing = 0
                left = 150 + (existing % 4) * 350 if left is None else left
                top = 200 + (existing // 4) * 200 if top is None else top

            # Prepare node parameters
            node_params = {
                "name": arguments.name,
                "node_type": arguments.node_type,
                "left": left,
                "top": top,
                "delay": arguments.delay,
                "console": arguments.console,
                "config": arguments.config,
            }

            # Add optional parameters if specified
            if arguments.ethernet is not None:
                node_params["ethernet"] = arguments.ethernet
            if arguments.serial is not None:
                node_params["serial"] = arguments.serial
            if arguments.image is not None:
                node_params["image"] = arguments.image
            if arguments.ram is not None:
                node_params["ram"] = arguments.ram
            if arguments.cpu is not None:
                node_params["cpu"] = arguments.cpu

            # Add node
            result = await eveng_client.add_node(arguments.lab_path, arguments.template, **node_params)

            if result.get('status') == 'success':
                node_id = result.get('data', {}).get('id', 'Unknown')
                return [TextContent(
                    type="text",
                    text=f"Successfully added node to lab!\n\n"
                         f"Lab: {arguments.lab_path}\n"
                         f"Template: {arguments.template}\n"
                         f"Node ID: {node_id}\n"
                         f"Name: {arguments.name or f'Node{node_id}'}\n"
                         f"Type: {arguments.node_type}\n"
                         f"Position: ({left}, {top}) px\n\n"
                         f"Node created successfully. Use start_node to power it on."
                )]
            else:
                return [TextContent(
                    type="text",
                    text=f"Failed to add node: {result.get('message', 'Unknown error')}"
                )]

        except Exception as e:
            logger.error(f"Failed to add node: {e}")
            return [TextContent(
                type="text",
                text=f"Failed to add node: {str(e)}"
            )]

    @mcp.tool()
    async def get_node_details(arguments: GetNodeDetailsArgs) -> list[TextContent]:
        """
        Get detailed information about a specific node.

        This tool retrieves comprehensive information about a node including
        its configuration, status, interfaces, and connectivity.
        """
        try:
            logger.info(f"Getting node details: {arguments.node_id} in {arguments.lab_path}")

            try:

                await eveng_client.ensure_connected()

            except Exception:

                pass

            if not eveng_client.is_connected:
                return [TextContent(
                    type="text",
                    text="Not connected to EVE-NG server. Use connect_eveng_server tool first."
                )]

            # Get node details
            node = await eveng_client.get_node(arguments.lab_path, arguments.node_id)

            if not node.get('data'):
                return [TextContent(
                    type="text",
                    text=f"Node {arguments.node_id} not found in lab {arguments.lab_path}"
                )]

            node_data = node['data']
            status_icon = "🟢" if node_data.get('status') == 2 else "🔴" if node_data.get('status') == 1 else "⚪"

            # Format node information
            details_text = f"Node Details: {node_data.get('name', f'Node {arguments.node_id}')}\n\n"

            details_text += f"{status_icon} Basic Information:\n"
            details_text += f"   ID: {arguments.node_id}\n"
            details_text += f"   Name: {node_data.get('name', 'Unknown')}\n"
            details_text += f"   Template: {node_data.get('template', 'Unknown')}\n"
            details_text += f"   Type: {node_data.get('type', 'Unknown')}\n"
            details_text += f"   Image: {node_data.get('image', 'Unknown')}\n"
            details_text += f"   Status: {_get_status_text(node_data.get('status', 0))}\n\n"

            details_text += f"⚙️  Configuration:\n"
            details_text += f"   Console: {node_data.get('console', 'Unknown')}\n"
            details_text += f"   CPU: {node_data.get('cpu', 'Unknown')}\n"
            details_text += f"   RAM: {node_data.get('ram', 'Unknown')} MB\n"
            details_text += f"   Ethernet Interfaces: {node_data.get('ethernet', 'Unknown')}\n"
            details_text += f"   Serial Interfaces: {node_data.get('serial', 'Unknown')}\n"
            details_text += f"   Delay: {node_data.get('delay', 0)} seconds\n\n"

            details_text += f"📍 Position:\n"
            details_text += f"   Left: {node_data.get('left', 0)} px\n"
            details_text += f"   Top: {node_data.get('top', 0)} px\n"

            return [TextContent(
                type="text",
                text=details_text
            )]

        except Exception as e:
            logger.error(f"Failed to get node details: {e}")
            return [TextContent(
                type="text",
                text=f"Failed to get node details: {str(e)}"
            )]

    # The REST API starts nodes for THIS server's own login (the 'claude' user), which
    # boots hidden copies the web UI user can't see. These tools keep their names so
    # existing prompts still work, but route to power_nodes (unl_wrapper over SSH, POD 0).
    async def _power(lab_path: str, action: str, node_id: Optional[str] = None) -> list[TextContent]:
        from .power_tools import do_power
        from ..core.power import PowerError
        try:
            text = await do_power(eveng_client, lab_path, action, [node_id] if node_id else None)
        except PowerError as e:
            text = (f"Power error: {e}\n\nNot falling back to the REST API: it would start hidden "
                    "copies under this server's own user that the web UI can't see.")
        except Exception as e:
            text = f"Failed to {action}: {e}"
        return [TextContent(type="text", text=text)]

    @mcp.tool()
    async def start_node(arguments: NodeControlArgs) -> list[TextContent]:
        """Start one node as the web UI user (same as power_nodes action=start)."""
        return await _power(arguments.lab_path, "start", arguments.node_id)

    @mcp.tool()
    async def stop_node(arguments: NodeControlArgs) -> list[TextContent]:
        """Stop one node as the web UI user (same as power_nodes action=stop)."""
        return await _power(arguments.lab_path, "stop", arguments.node_id)

    @mcp.tool()
    async def start_all_nodes(arguments: BulkNodeControlArgs) -> list[TextContent]:
        """Start every node in the lab as the web UI user (same as power_nodes action=start)."""
        return await _power(arguments.lab_path, "start")

    @mcp.tool()
    async def stop_all_nodes(arguments: BulkNodeControlArgs) -> list[TextContent]:
        """Stop every node in the lab as the web UI user (same as power_nodes action=stop)."""
        return await _power(arguments.lab_path, "stop")

    @mcp.tool()
    async def wipe_node(arguments: NodeControlArgs) -> list[TextContent]:
        """Wipe one STOPPED node back to its lab startup config (same as power_nodes action=wipe)."""
        return await _power(arguments.lab_path, "wipe", arguments.node_id)

    @mcp.tool()
    async def wipe_all_nodes(arguments: BulkNodeControlArgs) -> list[TextContent]:
        """Wipe every STOPPED node back to its lab startup config (same as power_nodes action=wipe)."""
        return await _power(arguments.lab_path, "wipe")

    @mcp.tool()
    async def delete_node(arguments: DeleteNodeArgs) -> list[TextContent]:
        """
        Delete a node from a lab.

        This tool permanently removes a node from the lab. All node data
        and configuration will be lost. This action cannot be undone.
        """
        try:
            logger.info(f"Deleting node {arguments.node_id} from {arguments.lab_path}")

            try:

                await eveng_client.ensure_connected()

            except Exception:

                pass

            if not eveng_client.is_connected:
                return [TextContent(
                    type="text",
                    text="Not connected to EVE-NG server. Use connect_eveng_server tool first."
                )]

            # Delete node
            result = await eveng_client.delete_node(arguments.lab_path, arguments.node_id)

            if result.get('status') == 'success':
                return [TextContent(
                    type="text",
                    text=f"Successfully deleted node {arguments.node_id} from {arguments.lab_path}\n\n"
                         f"⚠️  The node has been permanently removed from the lab.\n"
                         f"This action cannot be undone."
                )]
            else:
                return [TextContent(
                    type="text",
                    text=f"Failed to delete node: {result.get('message', 'Unknown error')}"
                )]

        except Exception as e:
            logger.error(f"Failed to delete node: {e}")
            return [TextContent(
                type="text",
                text=f"Failed to delete node: {str(e)}"
            )]

    # ---- layout --------------------------------------------------------------
    # EVE-NG canvas positions are pixels from the top-left corner.
    def _layout_positions(count: int, layout: str, spacing: int, start_left: int, start_top: int, columns: int):
        if layout == "row":
            return [(start_left + i * spacing, start_top) for i in range(count)]
        if layout == "column":
            return [(start_left, start_top + i * spacing) for i in range(count)]
        cols = max(1, columns)
        return [(start_left + (i % cols) * spacing, start_top + (i // cols) * spacing) for i in range(count)]

    @mcp.tool()
    async def move_node(arguments: MoveNodeArgs) -> list[TextContent]:
        """
        Move a node to a new position on the lab canvas.

        Positions are in pixels from the top-left of the canvas. A comfortable
        spacing between nodes is about 200-250 pixels.
        """
        try:
            await eveng_client.ensure_connected()
            await eveng_client.move_node(arguments.lab_path, arguments.node_id, arguments.left, arguments.top)
            return [TextContent(type="text", text=f"Moved node {arguments.node_id} to ({arguments.left}, {arguments.top}) px.")]
        except Exception as e:
            return [TextContent(type="text", text=f"Failed to move node: {str(e)}")]

    @mcp.tool()
    async def arrange_nodes(arguments: ArrangeNodesArgs) -> list[TextContent]:
        """
        Spread out all nodes in a lab so they don't overlap.

        layout: 'row' (left to right, good for a chain like R1-R2-R3),
        'column' (top to bottom), or 'grid'. Nodes are placed in ID order
        unless 'order' lists node names or IDs.
        """
        try:
            await eveng_client.ensure_connected()
            nodes = (await eveng_client.list_nodes(arguments.lab_path)).get("data") or {}
            items = [(str(n.get("id", k)), n.get("name", str(k))) for k, n in nodes.items()]
            items.sort(key=lambda x: int(x[0]) if x[0].isdigit() else 0)
            if arguments.order:
                want = [str(o).strip().lower() for o in arguments.order]
                rank = {}
                for i, w in enumerate(want):
                    for nid, name in items:
                        if w in (nid.lower(), name.lower()):
                            rank[nid] = i
                items.sort(key=lambda x: rank.get(x[0], len(want) + int(x[0]) if x[0].isdigit() else 999))
            pos = _layout_positions(len(items), arguments.layout, arguments.spacing,
                                    arguments.start_left, arguments.start_top, arguments.columns)
            lines = []
            for (nid, name), (x, y) in zip(items, pos):
                await eveng_client.move_node(arguments.lab_path, nid, x, y)
                lines.append(f"   {name} (ID {nid}) -> ({x}, {y})")
            return [TextContent(type="text", text=f"Arranged {len(items)} nodes ({arguments.layout}):\n" + "\n".join(lines) +
                                "\n\nRefresh the lab in the EVE-NG web UI to see the new layout.")]
        except Exception as e:
            return [TextContent(type="text", text=f"Failed to arrange nodes: {str(e)}")]
