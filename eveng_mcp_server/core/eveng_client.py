"""EVE-NG client wrapper with enhanced error handling and logging."""

import asyncio
import json
import re
import urllib3
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager

from evengsdk.client import EvengClient
from evengsdk.api import EvengApi
from evengsdk.exceptions import EvengHTTPError, EvengLoginError

from ..config import get_config, LoggerMixin, log_api_call, log_error
from .exceptions import (
    EVENGConnectionError,
    EVENGAuthenticationError,
    EVENGAPIError,
    EVENGTimeoutError,
    handle_eveng_api_error
)


class EVENGClientWrapper(LoggerMixin):
    """Enhanced wrapper around the EVE-NG SDK client."""
    
    def __init__(self):
        self.config = get_config()
        self._client: Optional[EvengClient] = None
        self._api: Optional[EvengApi] = None
        self._authenticated = False
        self._session_lock = asyncio.Lock()

        # Disable SSL warnings if configured
        if self.config.security.disable_ssl_warnings:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    @property
    def client(self) -> EvengClient:
        """Get the EVE-NG client instance."""
        if not self._client:
            raise EVENGConnectionError("Client not initialized. Call connect() first.")
        return self._client

    @property
    def api(self) -> EvengApi:
        """Get the EVE-NG API instance."""
        if not self._api:
            raise EVENGConnectionError("API not initialized. Call connect() first.")
        return self._api
    
    @property
    def is_connected(self) -> bool:
        """Check if client is connected and authenticated."""
        return self._client is not None and self._api is not None and self._authenticated
    
    async def connect(self) -> None:
        """Connect to EVE-NG server and authenticate."""
        async with self._session_lock:
            if self.is_connected:
                self.logger.info("Already connected to EVE-NG server")
                return
            
            try:
                self.logger.info(
                    "Connecting to EVE-NG server",
                    **log_api_call(
                        "CONNECT",
                        self.config.eveng.base_url,
                        username=self.config.eveng.username
                    )
                )
                
                # Initialize client
                self._client = EvengClient(
                    host=self.config.eveng.host,
                    port=self.config.eveng.port,
                    protocol=self.config.eveng.protocol,
                    disable_insecure_warnings=self.config.security.disable_ssl_warnings,
                    ssl_verify=self.config.eveng.ssl_verify
                )
                
                # Set timeout if session exists
                if hasattr(self._client, 'session') and self._client.session:
                    self._client.session.timeout = self.config.eveng.timeout
                
                # Authenticate
                await asyncio.to_thread(
                    self._client.login,
                    username=self.config.eveng.username,
                    password=self.config.eveng.password
                )
                self._authenticated = True
                self._install_reauth()

                # Initialize API wrapper
                self._api = EvengApi(self._client)

                self.logger.info(
                    "Successfully connected to EVE-NG server",
                    server_url=self.config.eveng.base_url
                )
                
            except EvengLoginError as e:
                self.logger.error(
                    "Authentication failed",
                    **log_error(e, {"username": self.config.eveng.username})
                )
                raise EVENGAuthenticationError(f"Authentication failed: {str(e)}")
            
            except EvengHTTPError as e:
                self.logger.error(
                    "HTTP error during connection",
                    **log_error(e)
                )
                raise EVENGConnectionError(f"Connection failed: {str(e)}")
            
            except Exception as e:
                self.logger.error(
                    "Unexpected error during connection",
                    **log_error(e)
                )
                raise EVENGConnectionError(f"Unexpected connection error: {str(e)}")
    
    async def disconnect(self) -> None:
        """Disconnect from EVE-NG server."""
        async with self._session_lock:
            if not self._client:
                return
            
            try:
                self.logger.info("Disconnecting from EVE-NG server")
                if hasattr(self._client, 'logout'):
                    await asyncio.to_thread(self._client.logout)
                self.logger.info("Successfully disconnected from EVE-NG server")
            except Exception as e:
                self.logger.warning(
                    "Error during disconnect",
                    **log_error(e)
                )
            finally:
                self._client = None
                self._api = None
                self._authenticated = False
    
    def _install_reauth(self) -> None:
        """Wrap the SDK request method so an expired session re-logs in once and retries.

        EVE-NG drops the session cookie (HTTP 412 / 401) after idle time or when the
        same user logs in elsewhere (e.g. the web UI). The SDK never recovers on its
        own, which caused the "connect works, next call fails" behavior.
        """
        client = self._client
        original = client._make_request
        cfg = self.config.eveng

        def make_request(method, url, *args, **kwargs):
            try:
                return original(method, url, *args, **kwargs)
            except (EvengHTTPError, ValueError) as e:
                msg = str(e)
                if not any(k in msg for k in ("412", "401", "No valid session", "Unauthorized")):
                    raise
                self.logger.info("EVE-NG session expired, re-authenticating")
                client.session = None
                client.login(username=cfg.username, password=cfg.password)
                return original(method, url, *args, **kwargs)

        client._make_request = make_request

    # ---- ID / name / interface resolution -------------------------------------
    # The SDK link functions look nodes and networks up by NAME, but the MCP tools
    # pass IDs ("1", "2"). That mismatch is what produced "NoneType has no attribute
    # get" and "node 1 not found or invalid".

    def _node_name(self, lab_path: str, ref: str) -> tuple[str, str]:
        """Return (node_id, node_name) for a node given its ID or name."""
        nodes = (self.api.list_nodes(lab_path) or {}).get("data") or {}
        ref = str(ref).strip()
        for nid, n in nodes.items():
            if str(nid) == ref or str(n.get("id")) == ref:
                return str(n.get("id", nid)), n["name"]
        for nid, n in nodes.items():
            if n.get("name", "").lower() == ref.lower():
                return str(n.get("id", nid)), n["name"]
        names = ", ".join(f"{n.get('id', k)}={n.get('name')}" for k, n in nodes.items())
        raise ValueError(f"Node '{ref}' not found in {lab_path}. Nodes: {names or 'none'}")

    def _network_name(self, lab_path: str, ref: str) -> str:
        """Return the network name for a network given its ID or name."""
        nets = (self.api.list_lab_networks(lab_path) or {}).get("data") or {}
        ref = str(ref).strip()
        for nid, n in nets.items():
            if str(nid) == ref or str(n.get("id")) == ref:
                return n["name"]
        for _, n in nets.items():
            if n.get("name", "").lower() == ref.lower():
                return n["name"]
        names = ", ".join(f"{n.get('id', k)}={n.get('name')}" for k, n in nets.items())
        raise ValueError(f"Network '{ref}' not found in {lab_path}. Networks: {names or 'none'}")

    _IF_ALIASES = {
        "gigabitethernet": "gi", "fastethernet": "fa", "ethernet": "e",
        "tengigabitethernet": "te", "serial": "s", "port": "port",
    }

    @classmethod
    def _norm_if(cls, name: str) -> str:
        n = re.sub(r"[\s_-]", "", str(name).lower())
        for long, short in sorted(cls._IF_ALIASES.items(), key=lambda x: -len(x[0])):
            if n.startswith(long):
                return short + n[len(long):]
        m = re.match(r"([a-z]+)(.*)", n)
        if m:
            for long, short in cls._IF_ALIASES.items():
                if long.startswith(m.group(1)) and len(m.group(1)) >= len(short):
                    return short + m.group(2)
        return n

    def _interface_label(self, lab_path: str, node_id: str, label: str,
                         media: str = "ethernet") -> str:
        """Map a user-friendly interface name (Gi0/0, GigabitEthernet0/0, e0/1,
        port1) to the exact label EVE-NG uses for this node."""
        data = (self.api.get_node_interfaces(lab_path, node_id) or {}).get("data") or {}
        ifaces = data.get(media) or []
        if isinstance(ifaces, dict):
            ifaces = list(ifaces.values())
        names = [i.get("name", "") for i in ifaces]
        if label in names:
            return label
        for n in names:
            if n.lower() == str(label).lower():
                return n
        want = self._norm_if(label)
        for n in names:
            if self._norm_if(n) == want:
                return n
        raise ValueError(
            f"Interface '{label}' not found on node {node_id}. "
            f"Available {media} interfaces: {', '.join(names) or 'none'}"
        )

    async def ensure_connected(self) -> None:
        """Ensure client is connected, reconnect if necessary."""
        if not self.is_connected:
            await self.connect()
    
    @asynccontextmanager
    async def connection(self):
        """Context manager for EVE-NG connection."""
        await self.connect()
        try:
            yield self
        finally:
            await self.disconnect()
    
    async def get_server_status(self) -> Dict[str, Any]:
        """Get EVE-NG server status."""
        await self.ensure_connected()

        try:
            # For now, return basic connection info
            # TODO: Implement proper server status retrieval
            status = {
                "status": "connected",
                "server": self.config.eveng.base_url,
                "version": "Unknown",
                "uptime": "Unknown"
            }
            self.logger.debug("Retrieved server status", status=status)
            return status
        except Exception as e:
            self.logger.error("Failed to get server status", **log_error(e))
            raise EVENGAPIError(f"Failed to get server status: {str(e)}")
    
    async def test_connection(self) -> bool:
        """Test connection to EVE-NG server."""
        try:
            await self.ensure_connected()
            # Just check if we're authenticated
            return self.is_connected
        except Exception as e:
            self.logger.error("Connection test failed", **log_error(e))
            return False
    
    # Lab management methods
    async def list_labs(self, path: str = "/") -> List[Dict[str, Any]]:
        """List available labs."""
        await self.ensure_connected()

        try:
            if path == "/":
                # Get all folders and labs
                folders_response = await asyncio.to_thread(self.api.list_folders)

                # DEBUG: Log the raw response
                self.logger.info(f"DEBUG: list_folders() response type: {type(folders_response)}")
                self.logger.info(f"DEBUG: list_folders() response: {folders_response}")

                labs = []

                # Handle the correct API response format
                if isinstance(folders_response, dict):
                    data = folders_response.get('data', {})

                    # Get labs from root folder
                    root_labs = data.get('labs', [])
                    if isinstance(root_labs, list):
                        for lab_info in root_labs:
                            if isinstance(lab_info, dict):
                                lab_data = {
                                    'name': lab_info.get('file', '').replace('.unl', ''),
                                    'path': '/',
                                    'full_path': lab_info.get('path', ''),
                                    'file': lab_info.get('file', ''),
                                    'mtime': lab_info.get('mtime', ''),
                                    'umtime': lab_info.get('umtime', 0)
                                }
                                labs.append(lab_data)

                    # Get labs from subfolders
                    folders = data.get('folders', [])
                    if isinstance(folders, list):
                        for folder_info in folders:
                            if isinstance(folder_info, dict):
                                folder_path = folder_info.get('path', '')
                                if folder_path and folder_path != '/':
                                    # Get labs from this subfolder
                                    try:
                                        subfolder_response = await asyncio.to_thread(self.api.get_folder, folder_path)
                                        subfolder_data = subfolder_response.get('data', {})
                                        subfolder_labs = subfolder_data.get('labs', [])

                                        if isinstance(subfolder_labs, list):
                                            for lab_info in subfolder_labs:
                                                if isinstance(lab_info, dict):
                                                    lab_data = {
                                                        'name': lab_info.get('file', '').replace('.unl', ''),
                                                        'path': folder_path,
                                                        'full_path': lab_info.get('path', ''),
                                                        'file': lab_info.get('file', ''),
                                                        'mtime': lab_info.get('mtime', ''),
                                                        'umtime': lab_info.get('umtime', 0)
                                                    }
                                                    labs.append(lab_data)
                                    except Exception as subfolder_error:
                                        self.logger.warning(f"Failed to get labs from folder {folder_path}: {subfolder_error}")

                self.logger.debug(f"Listed {len(labs)} labs", path=path)
                return labs
            else:
                # Get specific folder
                folder_response = await asyncio.to_thread(self.api.get_folder, path)

                # DEBUG: Log the raw response
                self.logger.info(f"DEBUG: get_folder({path}) response type: {type(folder_response)}")
                self.logger.info(f"DEBUG: get_folder({path}) response: {folder_response}")

                labs = []

                # Extract labs from the folder data
                if isinstance(folder_response, dict):
                    folder_data = folder_response.get('data', {})
                    folder_labs = folder_data.get('labs', [])

                    if isinstance(folder_labs, list):
                        for lab_info in folder_labs:
                            if isinstance(lab_info, dict):
                                lab_data = {
                                    'name': lab_info.get('file', '').replace('.unl', ''),
                                    'path': path,
                                    'full_path': lab_info.get('path', ''),
                                    'file': lab_info.get('file', ''),
                                    'mtime': lab_info.get('mtime', ''),
                                    'umtime': lab_info.get('umtime', 0)
                                }
                                labs.append(lab_data)

                self.logger.debug(f"Listed {len(labs)} labs in {path}", path=path)
                return labs

        except Exception as e:
            self.logger.error("Failed to list labs", **log_error(e, {"path": path}))
            raise EVENGAPIError(f"Failed to list labs: {str(e)}")
    
    async def get_lab(self, lab_path: str) -> Dict[str, Any]:
        """Get lab details."""
        await self.ensure_connected()

        try:
            lab = await asyncio.to_thread(self.api.get_lab, lab_path)
            self.logger.debug("Retrieved lab details", lab_path=lab_path)
            return lab
        except Exception as e:
            self.logger.error("Failed to get lab", **log_error(e, {"lab_path": lab_path}))
            raise EVENGAPIError(f"Failed to get lab: {str(e)}")
    
    async def create_lab(self, name: str, path: str = "/", **kwargs) -> Dict[str, Any]:
        """Create a new lab."""
        await self.ensure_connected()

        try:
            lab = await asyncio.to_thread(
                self.api.create_lab,
                name=name,
                path=path,
                **kwargs
            )
            self.logger.info("Created lab", lab_name=name, lab_path=path)
            return lab
        except Exception as e:
            self.logger.error(
                "Failed to create lab",
                **log_error(e, {"name": name, "path": path})
            )
            raise EVENGAPIError(f"Failed to create lab: {str(e)}")

    # Node Management Methods
    async def list_node_templates(self) -> Dict[str, Any]:
        """List available node templates."""
        await self.ensure_connected()

        try:
            templates = await asyncio.to_thread(self.api.list_node_templates)
            self.logger.debug("Listed node templates")
            return templates
        except Exception as e:
            self.logger.error("Failed to list node templates", **log_error(e))
            raise EVENGAPIError(f"Failed to list node templates: {str(e)}")

    async def node_template_detail(self, node_type: str) -> Dict[str, Any]:
        """Get details for a specific node template."""
        await self.ensure_connected()

        try:
            details = await asyncio.to_thread(self.api.node_template_detail, node_type)
            self.logger.debug("Retrieved node template details", node_type=node_type)
            return details
        except Exception as e:
            self.logger.error("Failed to get node template details", **log_error(e, {"node_type": node_type}))
            raise EVENGAPIError(f"Failed to get node template details: {str(e)}")

    async def list_nodes(self, lab_path: str) -> Dict[str, Any]:
        """List all nodes in a lab."""
        await self.ensure_connected()

        try:
            nodes = await asyncio.to_thread(self.api.list_nodes, lab_path)
            self.logger.debug("Listed nodes", lab_path=lab_path)
            return nodes
        except Exception as e:
            self.logger.error("Failed to list nodes", **log_error(e, {"lab_path": lab_path}))
            raise EVENGAPIError(f"Failed to list nodes: {str(e)}")

    async def get_node(self, lab_path: str, node_id: str) -> Dict[str, Any]:
        """Get details for a specific node."""
        await self.ensure_connected()

        try:
            node = await asyncio.to_thread(self.api.get_node, lab_path, node_id)
            self.logger.debug("Retrieved node details", lab_path=lab_path, node_id=node_id)
            return node
        except Exception as e:
            self.logger.error("Failed to get node", **log_error(e, {"lab_path": lab_path, "node_id": node_id}))
            raise EVENGAPIError(f"Failed to get node: {str(e)}")

    async def get_node_by_name(self, lab_path: str, name: str) -> Dict[str, Any]:
        """Get node by name."""
        await self.ensure_connected()

        try:
            node = await asyncio.to_thread(self.api.get_node_by_name, lab_path, name)
            self.logger.debug("Retrieved node by name", lab_path=lab_path, name=name)
            return node
        except Exception as e:
            self.logger.error("Failed to get node by name", **log_error(e, {"lab_path": lab_path, "name": name}))
            raise EVENGAPIError(f"Failed to get node by name: {str(e)}")

    async def add_node(self, lab_path: str, template: str, **kwargs) -> Dict[str, Any]:
        """Add a node to a lab."""
        await self.ensure_connected()

        try:
            node = await asyncio.to_thread(self.api.add_node, lab_path, template, **kwargs)
            self.logger.info("Added node to lab", lab_path=lab_path, template=template, name=kwargs.get('name', ''))
            return node
        except Exception as e:
            self.logger.error("Failed to add node", **log_error(e, {"lab_path": lab_path, "template": template}))
            raise EVENGAPIError(f"Failed to add node: {str(e)}")

    async def delete_node(self, lab_path: str, node_id: str) -> Dict[str, Any]:
        """Delete a node from a lab."""
        await self.ensure_connected()

        try:
            result = await asyncio.to_thread(self.api.delete_node, lab_path, node_id)
            self.logger.info("Deleted node", lab_path=lab_path, node_id=node_id)
            return result
        except Exception as e:
            self.logger.error("Failed to delete node", **log_error(e, {"lab_path": lab_path, "node_id": node_id}))
            raise EVENGAPIError(f"Failed to delete node: {str(e)}")

    async def start_node(self, lab_path: str, node_id: str) -> Dict[str, Any]:
        """Start a specific node."""
        await self.ensure_connected()

        try:
            result = await asyncio.to_thread(self.api.start_node, lab_path, node_id)
            self.logger.info("Started node", lab_path=lab_path, node_id=node_id)
            return result
        except Exception as e:
            self.logger.error("Failed to start node", **log_error(e, {"lab_path": lab_path, "node_id": node_id}))
            raise EVENGAPIError(f"Failed to start node: {str(e)}")

    async def stop_node(self, lab_path: str, node_id: str) -> Dict[str, Any]:
        """Stop a specific node."""
        await self.ensure_connected()

        try:
            result = await asyncio.to_thread(self.api.stop_node, lab_path, node_id)
            self.logger.info("Stopped node", lab_path=lab_path, node_id=node_id)
            return result
        except Exception as e:
            self.logger.error("Failed to stop node", **log_error(e, {"lab_path": lab_path, "node_id": node_id}))
            raise EVENGAPIError(f"Failed to stop node: {str(e)}")

    async def start_all_nodes(self, lab_path: str) -> Dict[str, Any]:
        """Start all nodes in a lab."""
        await self.ensure_connected()

        try:
            result = await asyncio.to_thread(self.api.start_all_nodes, lab_path)
            self.logger.info("Started all nodes", lab_path=lab_path)
            return result
        except Exception as e:
            self.logger.error("Failed to start all nodes", **log_error(e, {"lab_path": lab_path}))
            raise EVENGAPIError(f"Failed to start all nodes: {str(e)}")

    async def stop_all_nodes(self, lab_path: str) -> Dict[str, Any]:
        """Stop all nodes in a lab."""
        await self.ensure_connected()

        try:
            result = await asyncio.to_thread(self.api.stop_all_nodes, lab_path)
            self.logger.info("Stopped all nodes", lab_path=lab_path)
            return result
        except Exception as e:
            self.logger.error("Failed to stop all nodes", **log_error(e, {"lab_path": lab_path}))
            raise EVENGAPIError(f"Failed to stop all nodes: {str(e)}")

    async def wipe_node(self, lab_path: str, node_id: str) -> Dict[str, Any]:
        """Wipe a specific node (reset to factory state)."""
        await self.ensure_connected()

        try:
            result = await asyncio.to_thread(self.api.wipe_node, lab_path, int(node_id))
            self.logger.info("Wiped node", lab_path=lab_path, node_id=node_id)
            return result
        except Exception as e:
            self.logger.error("Failed to wipe node", **log_error(e, {"lab_path": lab_path, "node_id": node_id}))
            raise EVENGAPIError(f"Failed to wipe node: {str(e)}")

    async def wipe_all_nodes(self, lab_path: str) -> Dict[str, Any]:
        """Wipe all nodes in a lab (reset to factory state)."""
        await self.ensure_connected()

        try:
            result = await asyncio.to_thread(self.api.wipe_all_nodes, lab_path)
            self.logger.info("Wiped all nodes", lab_path=lab_path)
            return result
        except Exception as e:
            self.logger.error("Failed to wipe all nodes", **log_error(e, {"lab_path": lab_path}))
            raise EVENGAPIError(f"Failed to wipe all nodes: {str(e)}")

    # Network Management Methods
    async def list_network_types(self) -> Dict[str, Any]:
        """List available network types."""
        await self.ensure_connected()

        try:
            networks = await asyncio.to_thread(self.api.list_networks)
            self.logger.debug("Listed network types")
            return networks
        except Exception as e:
            self.logger.error("Failed to list network types", **log_error(e))
            raise EVENGAPIError(f"Failed to list network types: {str(e)}")

    async def list_lab_networks(self, lab_path: str) -> Dict[str, Any]:
        """List all networks in a lab."""
        await self.ensure_connected()

        try:
            networks = await asyncio.to_thread(self.api.list_lab_networks, lab_path)
            self.logger.debug("Listed lab networks", lab_path=lab_path)
            return networks
        except Exception as e:
            self.logger.error("Failed to list lab networks", **log_error(e, {"lab_path": lab_path}))
            raise EVENGAPIError(f"Failed to list lab networks: {str(e)}")

    async def get_lab_network(self, lab_path: str, net_id: int) -> Dict[str, Any]:
        """Get details for a specific network."""
        await self.ensure_connected()

        try:
            network = await asyncio.to_thread(self.api.get_lab_network, lab_path, net_id)
            self.logger.debug("Retrieved lab network details", lab_path=lab_path, net_id=net_id)
            return network
        except Exception as e:
            self.logger.error("Failed to get lab network", **log_error(e, {"lab_path": lab_path, "net_id": net_id}))
            raise EVENGAPIError(f"Failed to get lab network: {str(e)}")

    async def add_lab_network(self, lab_path: str, network_type: str, **kwargs) -> Dict[str, Any]:
        """Add a network to a lab."""
        await self.ensure_connected()

        try:
            network = await asyncio.to_thread(self.api.add_lab_network, lab_path, network_type, **kwargs)
            self.logger.info("Added network to lab", lab_path=lab_path, network_type=network_type)
            return network
        except Exception as e:
            self.logger.error("Failed to add lab network", **log_error(e, {"lab_path": lab_path, "network_type": network_type}))
            raise EVENGAPIError(f"Failed to add lab network: {str(e)}")

    async def delete_lab_network(self, lab_path: str, net_id: int) -> Dict[str, Any]:
        """Delete a network from a lab."""
        await self.ensure_connected()

        try:
            result = await asyncio.to_thread(self.api.delete_lab_network, lab_path, net_id)
            self.logger.info("Deleted lab network", lab_path=lab_path, net_id=net_id)
            return result
        except Exception as e:
            self.logger.error("Failed to delete lab network", **log_error(e, {"lab_path": lab_path, "net_id": net_id}))
            raise EVENGAPIError(f"Failed to delete lab network: {str(e)}")

    async def connect_node_to_cloud(self, lab_path: str, src: str, src_label: str, dst: str) -> Dict[str, Any]:
        """Connect a node to a cloud network."""
        await self.ensure_connected()

        try:
            def _do():
                nid, sname = self._node_name(lab_path, src)
                label = self._interface_label(lab_path, nid, src_label)
                dname = self._network_name(lab_path, dst)
                return self.api.connect_node_to_cloud(lab_path, sname, label, dname)
            result = await asyncio.to_thread(_do)
            self.logger.info("Connected node to cloud", lab_path=lab_path, src=src, dst=dst)
            return result
        except Exception as e:
            self.logger.error("Failed to connect node to cloud", **log_error(e, {"lab_path": lab_path, "src": src, "dst": dst}))
            raise EVENGAPIError(f"Failed to connect node to cloud: {str(e)}")

    async def connect_node_to_node(self, lab_path: str, src: str, src_label: str, dst: str, dst_label: str) -> Dict[str, Any]:
        """Connect two nodes together."""
        await self.ensure_connected()

        try:
            def _do():
                sid, sname = self._node_name(lab_path, src)
                did, dname = self._node_name(lab_path, dst)
                slabel = self._interface_label(lab_path, sid, src_label)
                dlabel = self._interface_label(lab_path, did, dst_label)
                return self.api.connect_node_to_node(lab_path, sname, slabel, dname, dlabel)
            result = await asyncio.to_thread(_do)
            self.logger.info("Connected nodes", lab_path=lab_path, src=src, dst=dst)
            return result
        except Exception as e:
            self.logger.error("Failed to connect nodes", **log_error(e, {"lab_path": lab_path, "src": src, "dst": dst}))
            raise EVENGAPIError(f"Failed to connect nodes: {str(e)}")

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

    async def get_lab_topology(self, lab_path: str) -> Dict[str, Any]:
        """Get lab topology information."""
        await self.ensure_connected()

        try:
            topology = await asyncio.to_thread(self.api.get_lab_topology, lab_path)
            self.logger.debug("Retrieved lab topology", lab_path=lab_path)
            return topology
        except Exception as e:
            self.logger.error("Failed to get lab topology", **log_error(e, {"lab_path": lab_path}))
            raise EVENGAPIError(f"Failed to get lab topology: {str(e)}")


# Global client instance
_client_instance: Optional[EVENGClientWrapper] = None


def get_eveng_client() -> EVENGClientWrapper:
    """Get the global EVE-NG client instance."""
    global _client_instance
    if _client_instance is None:
        _client_instance = EVENGClientWrapper()
    return _client_instance
