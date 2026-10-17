"""Adds node layout support to eveng-mcp:
- add_node auto-places new nodes on a grid instead of stacking them at (50,50)
- new move_node tool (set one node's position)
- new arrange_nodes tool (lay out every node in a lab as a row or grid)
EVE-NG positions are in pixels, not percent; labels are fixed to match.
Usage: python layout_patch.py <repo_dir>   (safe on CRLF or LF files)"""
import sys, pathlib

repo = pathlib.Path(sys.argv[1])


def patch(rel, pairs):
    p = repo / rel
    raw = p.read_bytes().decode("utf-8")
    crlf = "\r\n" in raw
    s = raw.replace("\r\n", "\n")
    for old, new in pairs:
        if new.strip() and new in s:
            continue  # already applied
        if s.count(old) != 1:
            raise SystemExit(f"{rel}: expected 1 match, found {s.count(old)} for:\n{old[:120]}")
        s = s.replace(old, new)
    if crlf:
        s = s.replace("\n", "\r\n")
    p.write_bytes(s.encode("utf-8"))
    print("patched", rel)


CLIENT_METHOD = '''
    async def move_node(self, lab_path: str, node_id: str, left: int, top: int) -> Dict[str, Any]:
        """Set a node's position on the lab canvas (pixels)."""
        await self.ensure_connected()

        def _do():
            norm = self.api.normalize_path(lab_path)
            payload = {"id": int(node_id), "left": int(left), "top": int(top)}
            return self.client.put(f"/labs{norm}/nodes/{node_id}", data=json.dumps(payload))
        try:
            return await asyncio.to_thread(_do)
        except Exception as e:
            raise EVENGAPIError(f"Failed to move node {node_id}: {str(e)}")

    async def get_lab_topology(self, lab_path: str) -> Dict[str, Any]:'''

patch("eveng_mcp_server/core/eveng_client.py", [
    ("import asyncio\n", "import asyncio\nimport json\n"),
    ("\n    async def get_lab_topology(self, lab_path: str) -> Dict[str, Any]:", CLIENT_METHOD),
])

TOOLS = '''

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
            return [TextContent(type="text", text=f"Arranged {len(items)} nodes ({arguments.layout}):\\n" + "\\n".join(lines) +
                                "\\n\\nRefresh the lab in the EVE-NG web UI to see the new layout.")]
        except Exception as e:
            return [TextContent(type="text", text=f"Failed to arrange nodes: {str(e)}")]
'''

ARGS = '''

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


def register_node_tools('''

AUTO_PLACE = '''            # Auto-place on a grid when no position given (EVE-NG uses pixels)
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
                "top": top,'''

p = repo / "eveng_mcp_server/tools/node_management.py"
s = p.read_bytes().decode().replace("\r\n", "\n")
already_tools = "async def arrange_nodes" in s

pairs = [
    ('''    left: int = Field(default=50, description="Position from left (percentage, 0-100)")
    top: int = Field(default=50, description="Position from top (percentage, 0-100)")
    delay''',
     '''    left: Optional[int] = Field(default=None, description="Pixels from the left of the canvas. Leave empty to auto-place without overlapping.")
    top: Optional[int] = Field(default=None, description="Pixels from the top of the canvas. Leave empty to auto-place without overlapping.")
    delay'''),
    ('''            # Prepare node parameters
            node_params = {
                "name": arguments.name,
                "node_type": arguments.node_type,
                "left": arguments.left,
                "top": arguments.top,''', AUTO_PLACE),
    ('''f"Position: ({arguments.left}%, {arguments.top}%)\\n\\n"''', '''f"Position: ({left}, {top}) px\\n\\n"'''),
    ("\n\ndef register_node_tools(", ARGS),
]
patch("eveng_mcp_server/tools/node_management.py", pairs)

if not already_tools:
    raw = p.read_bytes().decode()
    crlf = "\r\n" in raw
    add = TOOLS.replace("\n", "\r\n") if crlf else TOOLS
    p.write_bytes((raw.rstrip() + add).encode())
    print("added move_node / arrange_nodes tools")
