# setup-ssh-key.ps1
# Switches the EVE-NG MCP server from an SSH password to an SSH key.
#   1. Creates a key just for this server: ~\.ssh\eveng_mcp_ed25519 (no passphrase)
#   2. Installs its public key on EVE-NG (asks for the EVE-NG SSH password ONE time)
#   3. Confirms key login works and records EVE-NG's host key for the MCP server
#   4. Updates the Claude Desktop config: sets EVENG_SSH_KEY, removes EVENG_SSH_PASSWORD
#
# Run in PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\scripts\setup-ssh-key.ps1
# Optional: -EveHost 192.168.40.250  -User root
# Quit Claude Desktop first (system tray > Quit): it rewrites its config while running.

param(
    [string]$EveHost = "",
    [string]$User = ""
)

$ErrorActionPreference = "Stop"
function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    $m" -ForegroundColor Green }

# ---- find the Claude Desktop config(s) and current settings -----------------
. (Join-Path $PSScriptRoot "claude-config.ps1")
Assert-ClaudeClosed
$found = Get-EvengServerEntry
if (-not $found) { throw "No 'eveng-mcp-server' entry in any Claude Desktop config. Run scripts\setup-windows.ps1 first." }
$env0 = $found.Server.env
if (-not $EveHost) { $EveHost = $env0.EVENG_HOST }
if (-not $User)    { $User = $(if ($env0.EVENG_SSH_USER) { $env0.EVENG_SSH_USER } else { "root" }) }
if (-not $EveHost) { throw "EVE-NG host unknown. Rerun with -EveHost <ip>." }

# ---- OpenSSH client ---------------------------------------------------------
Step "Checking the Windows OpenSSH client"
$ssh = (Get-Command ssh -ErrorAction SilentlyContinue).Source
$keygen = (Get-Command ssh-keygen -ErrorAction SilentlyContinue).Source
if (-not $ssh -or -not $keygen) {
    throw ("OpenSSH client not found. Install it (as Administrator):`n" +
           "  Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0`nthen rerun this script.")
}
Ok "$ssh"

# ---- 1. key -----------------------------------------------------------------
Step "Creating the SSH key"
$sshDir = Join-Path $HOME ".ssh"
New-Item -ItemType Directory -Force -Path $sshDir | Out-Null
$key = Join-Path $sshDir "eveng_mcp_ed25519"
if (Test-Path $key) {
    Ok "Already exists: $key"
} else {
    & $keygen -q -t ed25519 -f $key -N '""' -C "eveng-mcp@$env:COMPUTERNAME"
    if ($LASTEXITCODE -ne 0) { throw "ssh-keygen failed" }
    Ok "Created $key"
}
$pub = (Get-Content "$key.pub" -Raw).Trim()

# ---- 2. install on EVE-NG ---------------------------------------------------
Step "Installing the public key on $User@$EveHost"
Write-Host "    You'll be asked for the EVE-NG SSH password for '$User' (one time)." -ForegroundColor Yellow
# The remote script goes over base64 so no quoting can break it.
$remote = "umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; " +
          "grep -qxF '$pub' ~/.ssh/authorized_keys || echo '$pub' >> ~/.ssh/authorized_keys; echo KEY_INSTALLED"
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($remote))
$out = & $ssh -o StrictHostKeyChecking=accept-new "$User@$EveHost" "echo $b64 | base64 -d | bash"
if ($LASTEXITCODE -ne 0 -or ($out -notmatch "KEY_INSTALLED")) { throw "Installing the key failed: $out" }
Ok "Public key installed"

# ---- 3. test key login + record host key for the MCP server -----------------
Step "Testing key login"
$test = & $ssh -i $key -o BatchMode=yes -o StrictHostKeyChecking=yes "$User@$EveHost" "test -x /opt/unetlab/wrappers/unl_wrapper && echo WRAPPER_OK"
if ($LASTEXITCODE -ne 0) { throw "Key login failed." }
if ($test -notmatch "WRAPPER_OK") { Write-Host "    Logged in, but /opt/unetlab/wrappers/unl_wrapper wasn't found." -ForegroundColor Yellow }
else { Ok "Key login works and EVE-NG's unl_wrapper is there" }

$mcpKh = Join-Path $HOME ".eveng-mcp\known_hosts"
New-Item -ItemType Directory -Force -Path (Split-Path $mcpKh) | Out-Null
$line = & $keygen -F $EveHost -f (Join-Path $sshDir "known_hosts") | Where-Object { $_ -and $_ -notmatch '^#' } | Select-Object -First 1
if ($line) {
    $existing = if (Test-Path $mcpKh) { Get-Content $mcpKh } else { @() }
    if ($existing -notcontains $line) { Add-Content -Path $mcpKh -Value $line -Encoding ascii }
    Ok "EVE-NG host key pinned for the MCP server in $mcpKh"
}

# ---- 4. Claude Desktop config ------------------------------------------------
Step "Updating Claude Desktop config"
Update-ClaudeConfigs {
    param($e0)
    if (-not $e0) { $e0 = $found.Server }        # a config location that didn't have the entry yet
    $e = $e0.env
    $e | Add-Member -Force EVENG_SSH_USER $User
    $e | Add-Member -Force EVENG_SSH_KEY $key
    $e.PSObject.Properties.Remove("EVENG_SSH_PASSWORD")
    $e0
}
Ok "SSH key set, SSH password removed"

Write-Host "`nDone. Fully quit Claude Desktop (system tray > Quit) and reopen it." -ForegroundColor Green
