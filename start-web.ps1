<#
    Agent Coworker - web UI launcher
    Invoked by start-agent-coworker.bat via `start` (detached, hidden),
    so the server keeps running even after the launcher window closes.
    Stop it from the web page with the "Shutdown" button.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Find-Python {
    $candidates = @(
        "$env:USERPROFILE\.workbuddy\binaries\python\versions\3.13.12\python.exe"
    )
    $glob = Get-ChildItem "$env:USERPROFILE\.workbuddy\binaries\python\versions" -Directory -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending |
            ForEach-Object { Join-Path $_.FullName 'python.exe' }
    $candidates += $glob
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    foreach ($n in @('python', 'py')) {
        $p = Get-Command $n -ErrorAction SilentlyContinue
        if ($p) { return $p.Source }
    }
    throw 'No Python interpreter found.'
}

try {
    $Py = Find-Python
}
catch {
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue
        [System.Windows.Forms.MessageBox]::Show("Python not found. Please install Python 3.11+ or the WorkBuddy runtime, then re-run start-agent-coworker.bat.", "Agent Coworker", "OK", "Error") | Out-Null
    }
    catch { Write-Host "ERROR: $_" }
    exit 1
}

# Run the server in the foreground of this (detached) shell.
# The .bat launched us with `start`, so closing the launcher window
# does NOT kill this process or the server.
& $Py "$Root\web_server.py"
