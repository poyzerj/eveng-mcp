# setup-windows.ps1
# Installs the EVE-NG MCP server for Claude Desktop on Windows.
#   1. Asks for your EVE-NG address (IP or FQDN), the EVE-NG user Claude will log in as,
#      its password (hidden), and whether to set up SSH for starting/stopping nodes
#   2. Installs Git and uv (if missing) and clones this repo to C:\eveng-mcp
#   3. Checks it can log in to EVE-NG (HTTPS first, then HTTP)
#   4. Writes the Claude Desktop config: every location it may read, including the current
#      %LOCALAPPDATA%\Claude-Data\claude_<id>\ folder, changing only this server's entry
#   5. Optionally runs setup-ssh-key.ps1 (asks for the EVE-NG SSH password once; it isn't stored)
#
# Before you start:
#   - In EVE-NG, create a user for Claude: Management > User management > Add user
#     (e.g. 'claude', role Administrator). Use a separate user: EVE-NG allows one login per
#     user, so sharing yours would keep logging you out of the web UI.
#   - Quit Claude Desktop (system tray > Quit): it rewrites its config while running.
#
# Run in PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\setup-windows.ps1
# Optional: -EveHost <ip-or-fqdn>  -EveUser <user>  -SshUser <user>  -SkipSsh

param(
    [string]$EveHost = "",
    [string]$EveUser = "",
    [string]$SshUser = "",
    [switch]$SkipSsh,
    [string]$RepoDir = "C:\eveng-mcp"
)

$ErrorActionPreference = "Stop"
# Claude Desktop rewrites its config file while it runs, which would undo the changes below.
if (Get-Process -Name "claude" -ErrorAction SilentlyContinue) {
    throw "Claude Desktop is running. Quit it first (system tray > Quit), then rerun this script."
}

function Ask([string]$prompt, [string]$default = "") {
    $shown = if ($default) { "$prompt [$default]" } else { $prompt }
    do { $v = (Read-Host $shown).Trim(); if (-not $v) { $v = $default } } while (-not $v)
    return $v
}
function Ask-Secret([string]$prompt) {   # hidden: not shown on screen or in a pasted transcript
    do {
        $sec = Read-Host $prompt -AsSecureString
        $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
        try { $v = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
    } while (-not $v)
    return $v
}

Write-Host "`nEVE-NG MCP server setup. Nothing is sent anywhere except your own EVE-NG server." -ForegroundColor Cyan
Write-Host "You need an EVE-NG user for Claude first (e.g. 'claude', Administrator role).`n"
if (-not $EveHost) { $EveHost = Ask "EVE-NG server address (IP or FQDN, optional :port, e.g. 192.168.1.50 or eve.example.com)" }
$EveHost = ($EveHost -replace '^https?://', '').TrimEnd('/')
$EvePort = ""
if ($EveHost -match '^(.+):(\d+)$') { $EveHost = $Matches[1]; $EvePort = $Matches[2] }   # e.g. eve.example.com:8443
$web = if ($EvePort) { "${EveHost}:$EvePort" } else { $EveHost }
if (-not $EveUser) { $EveUser = Ask "EVE-NG user for Claude" "claude" }
$EvePass = Ask-Secret "Password for EVE-NG user '$EveUser'"
$doSsh = $false
if (-not $SkipSsh) {
    $a = (Read-Host "Set up SSH so Claude can start/stop nodes as your web UI user? [Y/n]").Trim()
    $doSsh = -not ($a -match '^(n|no)$')
    if ($doSsh -and -not $SshUser) { $SshUser = Ask "SSH user on the EVE-NG server" "root" }
}

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

# ---------------------------------------------------------------- 3. check EVE-NG
function Test-Eve([string]$h, [string]$proto, [bool]$verify) {
    # "ok", "badcreds", "certfail" or $null (nothing answering)
    $body = @{ username = $EveUser; password = $EvePass; html5 = "-1" } | ConvertTo-Json
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    [Net.ServicePointManager]::ServerCertificateValidationCallback = $(if ($verify) { $null } else { { $true } })
    try {
        $r = Invoke-WebRequest -Uri "${proto}://$h/api/auth/login" -Method Post -Body $body `
             -ContentType "application/json" -TimeoutSec 8 -UseBasicParsing
        if ($r.Content -match "logged in|success") { return "ok" }
    } catch {
        # EVE-NG answers a bad login with its own JSON error ({"code":...,"status":...})
        $c = "$($_.ErrorDetails.Message) $_"
        if ($c -match '"code"\s*:\s*\d+' -and $c -match '"status"') { return "badcreds" }
        if ($proto -eq "https" -and $verify -and $c -match 'trust|certificate|SSL|TLS') { return "certfail" }
    } finally {
        [Net.ServicePointManager]::ServerCertificateValidationCallback = $null
    }
    return $null
}

Step "Checking EVE-NG at $web"
$proto = $null; $sslVerify = $false
$r = Test-Eve $web "https" $true
if ($r -eq "ok") { $proto = "https"; $sslVerify = $true; Ok "HTTPS with a valid certificate" }
elseif ($r -eq "badcreds") { throw "EVE-NG answered at https://$web but refused user '$EveUser'. Check the user and password in EVE-NG, then rerun." }
else {
    if ($r -eq "certfail") {
        $r2 = Test-Eve $web "https" $false
        if ($r2 -eq "ok") { $proto = "https"; Warn "HTTPS works but the certificate isn't trusted (self-signed?) - certificate checks turned off" }
        elseif ($r2 -eq "badcreds") { throw "EVE-NG answered at https://$web but refused user '$EveUser'. Check the user and password, then rerun." }
    }
    if (-not $proto) {
        $r3 = Test-Eve $web "http" $false
        if ($r3 -eq "ok") { $proto = "http"; Ok "HTTP (no HTTPS found)" }
        elseif ($r3 -eq "badcreds") { throw "EVE-NG answered at http://$web but refused user '$EveUser'. Check the user and password, then rerun." }
    }
}
if (-not $proto) {
    throw "No EVE-NG login page answered at $web on HTTPS or HTTP. Check the address (open it in a browser from this PC) and any firewall, then rerun."
}
Ok "Logged in to ${proto}://$web as '$EveUser'"

# ---------------------------------------------------------------- 4. Claude Desktop config
Step "Writing Claude Desktop config"
. (Join-Path $RepoDir "scripts\claude-config.ps1")
$port = $(if ($EvePort) { $EvePort } elseif ($proto -eq "https") { "443" } else { "80" })
$server = [ordered]@{
    command = $uv
    args    = @("--directory", $RepoDir, "run", "eveng-mcp-server", "run", "--transport", "stdio")
    env     = [ordered]@{
        EVENG_HOST     = $EveHost
        EVENG_PORT     = $port
        EVENG_PROTOCOL = $proto
        EVENG_USERNAME = $EveUser
        EVENG_PASSWORD = $EvePass
        EVENG_SSL_VERIFY = $(if ($sslVerify) { "true" } else { "false" })
    }
}
# Writes every location Claude Desktop may read (current: %LOCALAPPDATA%\Claude-Data\claude_<id>),
# changing only this server's entry. SSH settings from setup-ssh-key.ps1 are kept on a rerun.
Update-ClaudeConfigs {
    param($old)
    if ($old -and $old.env) {
        foreach ($p in $old.env.PSObject.Properties) {
            if ($p.Name -like "EVENG_SSH_*" -and $p.Name -ne "EVENG_SSH_PASSWORD") { $server.env[$p.Name] = $p.Value }
        }
    }
    [pscustomobject]$server
}

# ---------------------------------------------------------------- 5. smoke test
Step "Testing the MCP server against EVE-NG"
$env:EVENG_HOST = $EveHost; $env:EVENG_USERNAME = $EveUser; $env:EVENG_PASSWORD = $EvePass
$env:EVENG_PROTOCOL = $proto; $env:EVENG_PORT = $port; $env:EVENG_SSL_VERIFY = $server.env.EVENG_SSL_VERIFY
Push-Location $RepoDir
& $uv run python -c "import asyncio; from eveng_mcp_server.core.eveng_client import EVENGClientWrapper as W; c=W(); labs=asyncio.run(c.list_labs('/')); print('    Server login OK -', len(labs), 'labs found')" 2>$null
$smoke = $LASTEXITCODE
Pop-Location
Remove-Item env:EVENG_PASSWORD -ErrorAction SilentlyContinue
if ($smoke -ne 0) { Warn "The server's own login test failed. Run scripts\test-connection.ps1 to see why." }

# ---------------------------------------------------------------- 6. SSH key (optional)
if ($doSsh) {
    & (Join-Path $RepoDir "scripts\setup-ssh-key.ps1") -EveHost $EveHost -User $SshUser
} else {
    Write-Host "`nSkipped SSH setup. Without it Claude can still build labs and use consoles of nodes"
    Write-Host "you start in the web UI. Run scripts\setup-ssh-key.ps1 any time to add it."
}

Write-Host "`nDone. Open Claude Desktop, check Settings > Desktop app > Extensions > Local MCP servers" -ForegroundColor Green
Write-Host "shows 'eveng-mcp-server' Running, then in a new chat try: 'List my EVE-NG labs.'" -ForegroundColor Green
