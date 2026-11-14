"""connect_eveng_server uses the Claude Desktop config by default and never stays stuck
on wrong settings a chat passed in (e.g. an old IP from memory, or admin/eve)."""

import asyncio

from mcp.server.fastmcp import FastMCP


def _tools():
    from eveng_mcp_server.core.eveng_client import EVENGClientWrapper
    from eveng_mcp_server.tools.connection import register_connection_tools
    c = EVENGClientWrapper()
    mcp = FastMCP("t")
    register_connection_tools(mcp, c)

    def call(name, **args):
        r = asyncio.run(mcp.call_tool(name, {"arguments": args}))
        content = r[0] if isinstance(r, tuple) else r
        return content[0].text
    return c, call


def test_no_arguments_uses_the_config(eve_env):
    c, call = _tools()
    text = call("connect_eveng_server")
    assert "Connected to EVE-NG at http://127.0.0.1:18099" in text and "Claude Desktop config" in text


def test_bad_override_falls_back_to_config(eve_env):
    c, call = _tools()
    text = call("connect_eveng_server", host="127.0.0.1", port=1, username="admin", password="eve")
    assert "Couldn't connect" in text and "Went back to the configured settings" in text
    assert c.config.eveng.port == 18099 and c.config.eveng.username == "admin"   # configured values
    asyncio.run(c.ensure_connected())          # the next tool call logs in with the config again
    assert c.is_connected


def test_not_connected_message_explains_and_warns_against_guessing(eve_env, monkeypatch):
    c, _ = _tools()
    c.config.eveng.port = 1                    # unreachable
    try:
        asyncio.run(c.ensure_connected())
    except Exception:
        pass
    msg = c.not_connected_message()
    assert "Reason:" in msg and "NO arguments" in msg and "Don't guess" in msg


def test_repo_env_file_beats_the_launcher_environment(monkeypatch, tmp_path):
    """A stale EVENG_HOST from Claude Desktop is overridden by <repo>/.env."""
    from eveng_mcp_server.config.settings import EVENGConfig
    monkeypatch.setenv("EVENG_HOST", "192.168.20.250")      # what a stale config hands over
    monkeypatch.setenv("EVENG_USERNAME", "claude")
    env = tmp_path / ".env"
    env.write_text("EVENG_HOST=192.168.40.250\nEVENG_SSH_KEY=C:/x\n")   # unrelated keys are ignored
    c = EVENGConfig(_env_file=str(env))
    assert c.host == "192.168.40.250" and c.username == "claude"   # file wins, env fills the rest
    assert EVENGConfig(_env_file=str(tmp_path / "missing")).host == "192.168.20.250"
