<#
    Agent Coworker (Agent Relay) - one-click launcher
    Usage:
        .\start-agent-coworker.ps1                              # check mode: doctor + unit tests + mock end-to-end
        .\start-agent-coworker.ps1 -Mode dry                    # dry-run: render real commands, no model call
        .\start-agent-coworker.ps1 -Mode real                   # real run: codex planner + codewhale(deepseek) worker
        .\start-agent-coworker.ps1 -Mode real -Config relay.codewhale-only.json  # codewhale for both roles
        .\start-agent-coworker.ps1 -Task .\my.md                # override the task file (real/dry modes)
        .\start-agent-coworker.ps1 -Mode real -OpenRunDir       # open the run directory in Explorer after completion

    Config files:
        relay.codewhale.json        codex planner + codewhale worker (default for dry/real)
        relay.codewhale-only.json   codewhale as both planner and worker (fallback if codex unavailable)
        relay.example.json          codex planner + reasonix worker (reference, reasonix not installed)
        relay.local.json            codex as both planner and worker (legacy)
#>
[CmdletBinding()]
param(
    [ValidateSet('check','dry','real')]
    [string]$Mode = 'check',
    [string]$Config = 'relay.codewhale.json',
    [string]$Task = 'tests/fixtures/task.md',
    [switch]$OpenRunDir
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# Always run from the script's own directory (the project root).
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

# --- Add CodeWhale (DeepSeek CLI) to PATH ---
$CodeWhaleBin = Join-Path $env:LOCALAPPDATA 'Programs\CodeWhale\bin'
if ((Test-Path $CodeWhaleBin) -and ($env:PATH -notlike "*$CodeWhaleBin*")) {
    $env:PATH = "$CodeWhaleBin;$env:PATH"
    Write-Host "[setup] CodeWhale bin added to PATH: $CodeWhaleBin" -ForegroundColor DarkGray
}

function Find-Python {
    # Prefer the WorkBuddy managed Python, then any versioned managed build, then PATH.
    $candidates = @(
        "$env:USERPROFILE\.workbuddy\binaries\python\versions\3.13.12\python.exe"
    )
    $glob = Get-ChildItem "$env:USERPROFILE\.workbuddy\binaries\python\versions" -Directory -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending |
            ForEach-Object { Join-Path $_.FullName 'python.exe' }
    $candidates += $glob
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    foreach ($n in @('python','py')) {
        $p = Get-Command $n -ErrorAction SilentlyContinue
        if ($p) { return $p.Source }
    }
    throw 'No Python interpreter found. Install Python 3.11+ or the WorkBuddy managed runtime.'
}

function Test-Executable {
    param([string]$Name)
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

$Py = Find-Python
$CodexPath = Test-Executable 'codex'
$CodeWhalePath = Test-Executable 'codewhale'

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " Agent Coworker launcher" -ForegroundColor Cyan
Write-Host " root     : $Root"
Write-Host " python   : $Py"
Write-Host " mode     : $Mode"
if ($Mode -ne 'check') {
    Write-Host " config   : $Config"
    Write-Host " task     : $Task"
}
Write-Host " codex    : $(if ($CodexPath) { $CodexPath } else { 'NOT FOUND' })"
Write-Host " codewhale: $(if ($CodeWhalePath) { $CodeWhalePath } else { 'NOT FOUND' })"
Write-Host "==================================================" -ForegroundColor Cyan

function Invoke-Step {
    param([string]$Title, [scriptblock]$Body)
    Write-Host ""
    Write-Host ">> $Title" -ForegroundColor Yellow
    & $Body
    if ($LASTEXITCODE -ne $null -and $LASTEXITCODE -gt 0) {
        Write-Host "   step exit code: $LASTEXITCODE" -ForegroundColor DarkYellow
    }
}

switch ($Mode) {

    'check' {
        # 1) doctor against the reference config (shows tool readiness; exit 2 if a role is missing - non-fatal here)
        Invoke-Step 'doctor (relay.example.json)' {
            & $Py -m handoff_relay doctor --config relay.example.json
        }
        # 2) unit tests - must pass
        Invoke-Step 'unit tests' {
            & $Py -m unittest discover -s tests -v
        }
        if ($LASTEXITCODE -ne 0) { throw "Unit tests failed (exit $LASTEXITCODE)." }
        # 3) mock end-to-end - deterministic functional acceptance
        Invoke-Step 'mock end-to-end run' {
            & $Py -m handoff_relay run --config tests/fixtures/mock-relay.json --task-file tests/fixtures/task.md
        }
        if ($LASTEXITCODE -ne 0) { throw "Mock run failed (exit $LASTEXITCODE)." }
        Write-Host ""
        Write-Host "USABILITY CHECK PASSED: orchestrator + contracts + mock delegation all green." -ForegroundColor Green
        $latest = Get-ChildItem '.agent-relay/test-runs' -Directory -ErrorAction SilentlyContinue |
                  Sort-Object Name -Descending | Select-Object -First 1
        if ($latest) {
            Write-Host "latest run dir: $($latest.FullName)"
            if ($OpenRunDir) { Start-Process explorer.exe $latest.FullName }
        }
    }

    'dry' {
        Invoke-Step "dry-run render ($Config, task=$Task)" {
            & $Py -m handoff_relay run --config $Config --task-file $Task --dry-run
        }
        Write-Host ""
        Write-Host "DRY-RUN DONE: commands rendered, no model was called." -ForegroundColor Green
    }

    'real' {
        # Pre-flight: verify required executables exist
        $needCodex = $false
        $needCodeWhale = $false
        try {
            $cfg = Get-Content $Config -Raw | ConvertFrom-Json
            $needCodex = $cfg.roles.PSObject.Properties.Name | ForEach-Object { $cfg.roles.$_.executable -eq 'codex' } | Where-Object { $_ }
            $needCodeWhale = $cfg.roles.PSObject.Properties.Name | ForEach-Object { $cfg.roles.$_.executable -eq 'codewhale' } | Where-Object { $_ }
        } catch {
            Write-Host "[warn] could not parse config to pre-check executables" -ForegroundColor DarkYellow
        }

        if ($needCodex -and -not $CodexPath) {
            Write-Host "[error] Config requires codex but it is not in PATH. Use -Config relay.codewhale-only.json as fallback." -ForegroundColor Red
            throw "codex not found"
        }
        if ($needCodeWhale -and -not $CodeWhalePath) {
            Write-Host "[error] Config requires codewhale but it is not in PATH. Install CodeWhale or check LOCALAPPDATA\Programs\CodeWhale\bin." -ForegroundColor Red
            throw "codewhale not found"
        }

        Invoke-Step "doctor ($Config)" {
            & $Py -m handoff_relay doctor --config $Config
        }
        Write-Host ""
        Write-Host "Starting REAL run. This calls the model(s)." -ForegroundColor Yellow
        Invoke-Step "real run (task=$Task)" {
            & $Py -m handoff_relay run --config $Config --task-file $Task
        }
        if ($LASTEXITCODE -ne 0) {
            Write-Host ""
            Write-Host "REAL run did not complete. Check .agent-relay/runs/ for logs." -ForegroundColor Red
            Write-Host "Tips:" -ForegroundColor DarkYellow
            Write-Host "  - If codex fails (HTTP 400 / model mismatch), try: -Config relay.codewhale-only.json" -ForegroundColor DarkYellow
            Write-Host "  - Ensure codex login is valid:  codex login" -ForegroundColor DarkYellow
            Write-Host "  - Ensure codewhale login is valid: codewhale login" -ForegroundColor DarkYellow
        } else {
            Write-Host ""
            Write-Host "REAL run completed." -ForegroundColor Green
        }
        $latest = Get-ChildItem '.agent-relay/runs' -Directory -ErrorAction SilentlyContinue |
                  Where-Object { $_.Name -ne 'DRY-RUN' } |
                  Sort-Object Name -Descending | Select-Object -First 1
        if ($latest) {
            Write-Host "latest run dir: $($latest.FullName)"
            if ($OpenRunDir) { Start-Process explorer.exe $latest.FullName }
        }
    }
}
