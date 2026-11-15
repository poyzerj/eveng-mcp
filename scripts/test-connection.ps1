# Test the EVE-NG MCP server's login with the exact settings Claude Desktop uses.
# Reads claude_desktop_config.json, never prints the password.
. (Join-Path $PSScriptRoot "claude-config.ps1")
$found = Get-EvengServerEntry
if (-not $found) { Write-Host "No 'eveng-mcp-server' entry in any Claude Desktop config:`n  $((Get-ClaudeConfigFiles) -join "`n  ")" -ForegroundColor Red; exit 1 }
$cfgPath = $found.File
$srv = $found.Server
foreach ($p in $srv.env.PSObject.Properties) { Set-Item -Path "env:$($p.Name)" -Value $p.Value }

$eveHost = $env:EVENG_HOST
$port = if ($env:EVENG_PORT) { $env:EVENG_PORT } else { "80" }
Write-Host "Config: $cfgPath"
Write-Host "Server: $($env:EVENG_PROTOCOL)://$($eveHost):$port   user: $($env:EVENG_USERNAME)"
Write-Host "Command: $($srv.command) $($srv.args -join ' ')"
Write-Host ""

foreach ($t in @(@($port, "web"), @("22", "SSH"))) {
    $ok = (Test-NetConnection -ComputerName $eveHost -Port $t[0] -WarningAction SilentlyContinue).TcpTestSucceeded
    Write-Host ("{0,-5} port {1,-5}: {2}" -f $t[1], $t[0], $(if ($ok) { "open" } else { "NOT reachable" }))
}
Write-Host ""

$repo = Split-Path $PSScriptRoot -Parent
$py = @'
import asyncio, traceback
from eveng_mcp_server.core.eveng_client import EVENGClientWrapper
async def main():
    c = EVENGClientWrapper()
    try:
        await c.connect()
        labs = await c.list_labs("/")
        print("LOGIN OK - labs found:", len(labs) if isinstance(labs, list) else labs)
    except Exception as e:
        print("LOGIN FAILED:", type(e).__name__, e)
        traceback.print_exc()
asyncio.run(main())
'@
$uv = if ($srv.command) { $srv.command } else { "uv" }
$tmp = Join-Path $env:TEMP "eveng-mcp-test.py"
Set-Content -Path $tmp -Value $py -Encoding UTF8
try { & $uv --directory $repo run python $tmp } finally { Remove-Item $tmp -ErrorAction SilentlyContinue }
