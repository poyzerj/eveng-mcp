"""Connection management tools for EVE-NG MCP Server."""

from typing import Any, Dict, Optional, TYPE_CHECKING
from mcp.types import TextContent, Tool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
    from ..core.eveng_client import EVENGClientWrapper

from ..config import get_logger
from ..core.exceptions import EVENGConnectionError, EVENGAuthenticationError


logger = get_logger("ConnectionTools")


class ConnectServerArgs(BaseModel):
    """Arguments for connect_eveng_server tool. All optional: by default the server
    (re)connects with the settings from the Claude Desktop config."""
    host: Optional[str] = Field(default=None, description="Leave empty to use EVENG_HOST from the Claude Desktop config. Only set it if the user gave you an address in this conversation - never from memory or a guess.")
    username: Optional[str] = Field(default=None, description="Leave empty to use the configured user. Only set it if the user gave it in this conversation.")
    password: Optional[str] = Field(default=None, description="Leave empty to use the configured password. Never guess or use defaults like 'eve'.")
    port: Optional[int] = Field(default=None, description="Leave empty for the configured port")
    protocol: Optional[str] = Field(default=None, description="Leave empty for the configured protocol (http/https)")


class TestConnectionArgs(BaseModel):
    """Arguments for test_connection tool."""
    pass  # No arguments needed


def register_connection_tools(mcp: "FastMCP", eveng_client: "EVENGClientWrapper") -> None:
    """Register connection management tools."""
    
    @mcp.tool()
    async def connect_eveng_server(arguments: ConnectServerArgs) -> list[TextContent]:
        """
        (Re)connect to EVE-NG. Normally NOT needed: every tool logs in by itself with the
        settings from the Claude Desktop config. Call it with NO arguments to retry that
        login (e.g. after EVE-NG rebooted). Pass host/username/password only when the user
        gives them in this conversation - never from memory or guesses such as admin/eve.
        If a login with other settings fails, the server goes back to the configured ones.
        """
        overrides = {k: v for k, v in {"host": arguments.host, "username": arguments.username,
                                        "password": arguments.password, "port": arguments.port,
                                        "protocol": arguments.protocol}.items() if v not in (None, "")}
        config = eveng_client.config.eveng
        target = {**eveng_client.configured, **overrides}
        try:
            await eveng_client.disconnect()
            for k, v in target.items():
                setattr(config, k, v)
            await eveng_client.connect()
            src = "settings you gave" if overrides else "the Claude Desktop config"
            return [TextContent(type="text", text=(
                f"Connected to EVE-NG at {config.protocol}://{config.host}:{config.port} as "
                f"'{config.username}' (from {src})."))]
        except Exception as e:
            reason = eveng_client.last_error or str(e)
            note = ""
            if overrides:      # don't leave the server stuck on settings that don't work
                for k, v in eveng_client.configured.items():
                    setattr(config, k, v)
                note = (f"\nWent back to the configured settings "
                        f"({config.protocol}://{config.host}:{config.port}, user '{config.username}').")
            logger.error(f"connect_eveng_server failed: {reason}")
            return [TextContent(type="text", text=(
                f"Couldn't connect to EVE-NG at {target['protocol']}://{target['host']}:{target['port']} "
                f"as '{target['username']}'.\nReason: {reason}{note}\n"
                "Check EVENG_HOST / EVENG_USERNAME / EVENG_PASSWORD in the Claude Desktop config "
                "(then fully quit and reopen Claude Desktop). Don't guess credentials."))]

    @mcp.tool()
    async def disconnect_eveng_server() -> list[TextContent]:
        """
        Disconnect from EVE-NG server.
        
        This tool closes the connection to the EVE-NG server and cleans up
        any active sessions.
        """
        try:
            logger.info("Disconnecting from EVE-NG server")
            await eveng_client.disconnect()
            
            return [TextContent(
                type="text",
                text="Successfully disconnected from EVE-NG server."
            )]
            
        except Exception as e:
            logger.error(f"Error during disconnect: {e}")
            return [TextContent(
                type="text",
                text=f"Warning: Error during disconnect: {str(e)}\n\n"
                     f"Connection may have been closed already."
            )]
    
    @mcp.tool()
    async def test_connection(arguments: TestConnectionArgs) -> list[TextContent]:
        """
        Test connection to EVE-NG server.
        
        This tool verifies that the connection to the EVE-NG server is working
        properly and returns server status information.
        """
        try:
            logger.info("Testing EVE-NG server connection")
            
            try:
            
                await eveng_client.ensure_connected()
            
            except Exception:
            
                pass
            
            if not eveng_client.is_connected:
                return [TextContent(
                    type="text",
                    text=eveng_client.not_connected_message()
                )]
            
            # Test connection by getting server status
            status = await eveng_client.get_server_status()
            
            return [TextContent(
                type="text",
                text=f"Connection test successful!\n\n"
                     f"Server: {eveng_client.config.eveng.base_url}\n"
                     f"Status: Connected\n"
                     f"Server Version: {status.get('version', 'Unknown')}\n"
                     f"Server Status: {status.get('status', 'Unknown')}\n"
                     f"Uptime: {status.get('uptime', 'Unknown')}"
            )]
            
        except Exception as e:
            logger.error(f"Connection test failed: {e}")
            return [TextContent(
                type="text",
                text=f"Connection test failed: {str(e)}\n\n"
                     f"Please check your connection and try again."
            )]
    
    @mcp.tool()
    async def get_server_info() -> list[TextContent]:
        """
        Get EVE-NG server information and status.
        
        This tool retrieves detailed information about the EVE-NG server
        including version, status, capabilities, and system information.
        """
        try:
            logger.info("Retrieving EVE-NG server information")
            
            try:
            
                await eveng_client.ensure_connected()
            
            except Exception:
            
                pass
            
            if not eveng_client.is_connected:
                return [TextContent(
                    type="text",
                    text=eveng_client.not_connected_message()
                )]
            
            # Get server status and information
            status = await eveng_client.get_server_status()
            
            # Format the information nicely
            info_text = "EVE-NG Server Information:\n\n"
            info_text += f"Server URL: {eveng_client.config.eveng.base_url}\n"
            info_text += f"Version: {status.get('version', 'Unknown')}\n"
            info_text += f"Status: {status.get('status', 'Unknown')}\n"
            info_text += f"Uptime: {status.get('uptime', 'Unknown')}\n"
            
            if 'cpu' in status:
                info_text += f"CPU Usage: {status['cpu']}%\n"
            if 'memory' in status:
                info_text += f"Memory Usage: {status['memory']}%\n"
            if 'disk' in status:
                info_text += f"Disk Usage: {status['disk']}%\n"
            
            return [TextContent(
                type="text",
                text=info_text
            )]
            
        except Exception as e:
            logger.error(f"Failed to get server info: {e}")
            return [TextContent(
                type="text",
                text=f"Failed to get server information: {str(e)}"
            )]
