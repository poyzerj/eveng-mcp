# setup-eveng-mcp.ps1
# Rebuilds the EVE-NG MCP server for Claude Desktop on a fresh Windows install.
#   1. Installs Git and uv (if missing)
#   2. Clones the poyzerj/eveng-mcp fork (all fixes + console/layout tools) to C:\eveng-mcp
#   3. Finds your EVE-NG box (192.168.20.250, 192.168.40.250, then scans both /24s)
#   4. Writes the Claude Desktop config
#
# Run in PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\setup-eveng-mcp.ps1
# Optional: -EveHost 192.168.20.250  -EveUser claude  -EvePass <password>
# Create the 'claude' user in EVE-NG first: Management > User management > Add user (Administrator).

param(
    [string]$EveHost = "",
    [string]$EveUser = "claude",   # a separate EVE-NG user, so Claude doesn't log you out of the web UI
    [string]$EvePass = "",
    [string]$RepoDir = "C:\eveng-mcp"
)

$ErrorActionPreference = "Stop"
if (-not $EvePass) { $EvePass = Read-Host "EVE-NG password for user '$EveUser'" }
function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    $m" -ForegroundColor Yellow }

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User") + ";" +
                "$env:USERPROFILE\.local\bin;$env:LOCALAPPDATA\Microsoft\WinGet\Links"
}

# ---------------------------------------------------------------- 1. tools
Step "Checking Git and uv"
Refresh-Path
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Warn "Git not found - installing"
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id Git.Git -e --accept-source-agreements --accept-package-agreements
    } else {
        Warn "winget not available - downloading Git for Windows from GitHub"
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $rel = Invoke-RestMethod "https://api.github.com/repos/git-for-windows/git/releases/latest" -UseBasicParsing
        $url = ($rel.assets | Where-Object { $_.name -match "^Git-.*-64-bit\.exe$" } | Select-Object -First 1).browser_download_url
        $exe = Join-Path $env:TEMP "git-setup.exe"
        Invoke-WebRequest $url -OutFile $exe -UseBasicParsing
        Start-Process $exe -ArgumentList "/VERYSILENT", "/NORESTART" -Wait
    }
    Refresh-Path
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Warn "uv not found - installing"
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements
    } else {
        Warn "winget not available - using the official uv installer"
        powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    }
    Refresh-Path
}
$git = (Get-Command git -ErrorAction SilentlyContinue).Source
$uv  = (Get-Command uv  -ErrorAction SilentlyContinue).Source
if (-not $git) { throw "Git still not found. Close PowerShell, open a new one, and rerun." }
if (-not $uv)  { throw "uv still not found. Close PowerShell, open a new one, and rerun." }
Ok "git: $git"
Ok "uv:  $uv"

# ---------------------------------------------------------------- 2. repo
Step "Setting up $RepoDir"
if (Test-Path $RepoDir) {
    $bak = "$RepoDir.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
    Warn "Existing folder found - moving it to $bak"
    Move-Item $RepoDir $bak
}
& $git -c core.autocrlf=false clone -q https://github.com/poyzerj/eveng-mcp.git $RepoDir
if ($LASTEXITCODE -ne 0) { throw "git clone failed" }

Push-Location $RepoDir
& $uv sync -q
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "uv sync failed" }
Pop-Location
Ok "Dependencies installed"

# ---------------------------------------------------------------- 3. find EVE-NG
function Test-Eve([string]$ip) {
    foreach ($proto in "http", "https") {
        try {
            $body = @{ username = $EveUser; password = $EvePass; html5 = "-1" } | ConvertTo-Json
            if ($proto -eq "https") {
                [Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
            }
            $r = Invoke-WebRequest -Uri "${proto}://$ip/api/auth/login" -Method Post -Body $body `
                 -ContentType "application/json" -TimeoutSec 4 -UseBasicParsing
            if ($r.Content -match "logged in|success") { return $proto }
        } catch {
            # EVE-NG answers a bad login with its own JSON error ({"code":...,"status":...})
            $c = "$($_.ErrorDetails.Message) $_"
            if ($c -match '"code"\s*:\s*\d+' -and $c -match '"status"') { return "$proto-badcreds" }
        }
    }
    return $null
}

function Find-Web([string]$prefix) {
    # Fast parallel check for anything listening on port 80 in the /24
    $tasks = @{}
    1..254 | ForEach-Object {
        $ip = "$prefix.$_"; $c = New-Object Net.Sockets.TcpClient
        $tasks[$ip] = @{ c = $c; t = $c.ConnectAsync($ip, 80) }
    }
    Start-Sleep -Milliseconds 1500
    $up = @()
    foreach ($k in $tasks.Keys) {
        if ($tasks[$k].t.Status -eq "RanToCompletion" -and $tasks[$k].c.Connected) { $up += $k }
        $tasks[$k].c.Dispose()
    }
    return $up | Sort-Object { [int]($_.Split(".")[3]) }
}

Step "Looking for EVE-NG"
$proto = $null
$candidates = @()
if ($EveHost) { $candidates += $EveHost } else { $candidates += "192.168.20.250", "192.168.40.250" }

foreach ($ip in $candidates) {
    Write-Host "    trying $ip ..."
    $p = Test-Eve $ip
    if ($p) { $EveHost = $ip; $proto = $p; break }
}
if (-not $proto -and -not $PSBoundParameters.ContainsKey("EveHost")) {
    foreach ($net in "192.168.40", "192.168.20") {
        Write-Host "    scanning $net.0/24 for web servers ..."
        foreach ($ip in (Find-Web $net)) {
            $p = Test-Eve $ip
            if ($p) { $EveHost = $ip; $proto = $p; break }
        }
        if ($proto) { break }
    }
}
if (-not $proto) {
    throw "Couldn't find EVE-NG. Check its IP in the Proxmox console (run 'ip a' on the VM), then rerun with -EveHost <ip>."
}
if ($proto -like "*-badcreds") {
    throw "Found EVE-NG at $EveHost but login failed. Rerun with -EveUser and -EvePass."
}
Ok "EVE-NG found at ${proto}://$EveHost"

# ---------------------------------------------------------------- 4. Claude Desktop config
Step "Writing Claude Desktop config"
$cfgDirs = @("$env:APPDATA\Claude")
$store = Get-ChildItem "$env:LOCALAPPDATA\Packages" -Directory -Filter "Claude_*" -ErrorAction SilentlyContinue
foreach ($s in $store) { $cfgDirs += "$($s.FullName)\LocalCache\Roaming\Claude" }

$server = [ordered]@{
    command = $uv
    args    = @("--directory", $RepoDir, "run", "eveng-mcp-server", "run", "--transport", "stdio")
    env     = [ordered]@{
        EVENG_HOST     = $EveHost
        EVENG_PORT     = $(if ($proto -eq "https") { "443" } else { "80" })
        EVENG_PROTOCOL = $proto
        EVENG_USERNAME = $EveUser
        EVENG_PASSWORD = $EvePass
    }
}

foreach ($dir in $cfgDirs) {
    if (-not (Test-Path $dir) -and $dir -ne "$env:APPDATA\Claude") { continue }
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $cfgPath = Join-Path $dir "claude_desktop_config.json"
    $cfg = $null
    if (Test-Path $cfgPath) {
        Copy-Item $cfgPath "$cfgPath.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
        try { $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json } catch { $cfg = $null }
    }
    if (-not $cfg) { $cfg = New-Object PSObject }
    if (-not $cfg.PSObject.Properties["mcpServers"]) {
        $cfg | Add-Member -NotePropertyName mcpServers -NotePropertyValue (New-Object PSObject)
    }
    if ($cfg.mcpServers.PSObject.Properties["eveng-mcp-server"]) {
        $cfg.mcpServers."eveng-mcp-server" = $server
    } else {
        $cfg.mcpServers | Add-Member -NotePropertyName "eveng-mcp-server" -NotePropertyValue $server
    }
    $json = $cfg | ConvertTo-Json -Depth 20
    [IO.File]::WriteAllText($cfgPath, $json, (New-Object Text.UTF8Encoding($false)))
    Ok "Updated $cfgPath"
}

# ---------------------------------------------------------------- 5. smoke test
Step "Testing the MCP server against EVE-NG"
$env:EVENG_HOST = $EveHost; $env:EVENG_USERNAME = $EveUser; $env:EVENG_PASSWORD = $EvePass
$env:EVENG_PROTOCOL = $proto; $env:EVENG_PORT = $server.env.EVENG_PORT
Push-Location $RepoDir
& $uv run python -c "import asyncio; from eveng_mcp_server.core.eveng_client import EVENGClientWrapper as W; c=W(); s=asyncio.run(c.get_server_status()); print('EVE-NG version:', (s.get('data') or {}).get('version'))" 2>$null
Pop-Location

Write-Host "`nDone. Fully quit Claude Desktop (system tray > Quit), then open it again." -ForegroundColor Green
Write-Host "In a new chat try: 'List my EVE-NG labs.'" -ForegroundColor Green
