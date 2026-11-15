# claude-config.ps1 - shared by the setup scripts (dot-source it).
# Finds every claude_desktop_config.json that Claude Desktop on Windows may read and
# changes only the 'eveng-mcp-server' entry, keeping all of Claude's own settings.
#
# Where Claude Desktop reads its config depends on the version and how it was installed:
#   %LOCALAPPDATA%\Claude-Data\claude_<id>\        current versions ("Edit config" opens this)
#   %LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\   Microsoft Store / MSIX
#   %LOCALAPPDATA%\Claude\  and  %APPDATA%\Claude\  older versions

$ServerName = "eveng-mcp-server"

function Get-ClaudeConfigFiles {
    # Every location that exists on this machine, current one first.
    $dirs = @()
    $dirs += (Get-ChildItem "$env:LOCALAPPDATA\Claude-Data" -Directory -Filter "claude_*" -ErrorAction SilentlyContinue).FullName
    $dirs += (Get-ChildItem "$env:LOCALAPPDATA\Packages" -Directory -Filter "Claude_*" -ErrorAction SilentlyContinue |
              ForEach-Object { "$($_.FullName)\LocalCache\Roaming\Claude" })
    $dirs += "$env:LOCALAPPDATA\Claude", "$env:APPDATA\Claude"
    $files = $dirs | Where-Object { $_ -and (Test-Path $_) } | ForEach-Object { Join-Path $_ "claude_desktop_config.json" }
    if (-not $files) { $files = @("$env:APPDATA\Claude\claude_desktop_config.json") }   # nothing yet: create the classic one
    return @($files | Select-Object -Unique)
}

function Get-EvengServerEntry {
    # The server entry from the first config that has one (current location first).
    foreach ($f in Get-ClaudeConfigFiles) {
        if (-not (Test-Path $f)) { continue }
        try { $c = Get-Content $f -Raw | ConvertFrom-Json } catch { continue }
        if ($c -and $c.mcpServers -and $c.mcpServers.$ServerName) { return @{ File = $f; Server = $c.mcpServers.$ServerName } }
    }
    return $null
}

function Assert-ClaudeClosed {
    # Claude Desktop keeps its settings in the same file and rewrites it while running,
    # which would undo our change.
    if (Get-Process -Name "claude" -ErrorAction SilentlyContinue) {
        throw "Claude Desktop is running. Quit it first (system tray > Quit), then rerun this script."
    }
}

function Update-ClaudeConfigs([scriptblock]$Change) {
    # Runs $Change on the 'eveng-mcp-server' entry of every config file (creating the entry
    # if needed) and saves it with a timestamped backup. Everything else in the file is kept.
    foreach ($f in Get-ClaudeConfigFiles) {
        $dir = Split-Path $f -Parent
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        $cfg = $null
        if (Test-Path $f) {
            Copy-Item $f "$f.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
            try { $cfg = Get-Content $f -Raw | ConvertFrom-Json }
            catch { Write-Host "    Skipped $f (not valid JSON - left as it was)" -ForegroundColor Yellow; continue }
        }
        if (-not $cfg) { $cfg = New-Object PSObject }
        if (-not $cfg.PSObject.Properties["mcpServers"]) {
            $cfg | Add-Member -NotePropertyName mcpServers -NotePropertyValue (New-Object PSObject)
        }
        $entry = $cfg.mcpServers.$ServerName
        $entry = & $Change $entry
        $cfg.mcpServers | Add-Member -Force -NotePropertyName $ServerName -NotePropertyValue $entry
        [IO.File]::WriteAllText($f, ($cfg | ConvertTo-Json -Depth 100), (New-Object Text.UTF8Encoding($false)))
        Write-Host "    Updated $f" -ForegroundColor Green
    }
}

function Read-Secret([string]$Prompt) {
    # Password prompt that doesn't show what you type.
    $s = Read-Host $Prompt -AsSecureString
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}
