<#
.SYNOPSIS
    Funnel of Thought - one-command bootstrap (Windows twin of setup.sh).

.DESCRIPTION
    Assumes SigNoz is already up (`foundryctl cast`). This script never casts,
    restarts or otherwise touches the SigNoz stack - it only reads from it and
    writes funnels/dashboards/alerts through the API.

    Interfaces this script assumes (sibling components own these):
        python -m agent.generate --runs N --model M --validate-rate R --seed S [--stub]
        python -m fot.cli apply
        python -m fot.cli show

.PARAMETER Token
    Only (re-)mint the SigNoz editor JWT, then exit.

.PARAMETER SkipBatch
    Do everything except generating traces.

.PARAMETER Count
    Number of traces to generate. Default 150.

.EXAMPLE
    .\scripts\setup.ps1
.EXAMPLE
    .\scripts\setup.ps1 -Token
#>
[CmdletBinding()]
param(
    [switch]$Token,
    [switch]$SkipBatch,
    [int]$Count = 150
)

$ErrorActionPreference = 'Continue'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

function Say  { param($m) Write-Host "==> $m" -ForegroundColor White }
function Ok   { param($m) Write-Host "  ok   $m" -ForegroundColor Green }
function Warn { param($m) Write-Host "  warn $m" -ForegroundColor Yellow }
function Die  { param($m) Write-Host "  fail $m" -ForegroundColor Red; exit 1 }
function Note { param($m) Write-Host "       $m" -ForegroundColor DarkGray }

# -----------------------------------------------------------------------------
# .env
# -----------------------------------------------------------------------------
Say "Environment file"
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Ok "created .env from .env.example"
    Warn "GEMINI_API_KEY is empty - set it before generating traces"
    Note "https://aistudio.google.com/apikey  (free tier is plenty)"
} else {
    Ok ".env exists"
}

function Import-DotEnv {
    if (-not (Test-Path .env)) { return }
    foreach ($line in Get-Content .env) {
        if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
        $k, $v = $line -split '=', 2
        if ($k.Trim()) { Set-Item -Path ("Env:" + $k.Trim()) -Value $v.Trim() }
    }
}
Import-DotEnv

if (-not $env:SIGNOZ_URL) { $env:SIGNOZ_URL = 'http://localhost:8080' }

# Rewrites KEY=value in .env without echoing the value.
function Set-DotEnv {
    param([string]$Key, [string]$Value)
    $lines = if (Test-Path .env) { @(Get-Content .env) } else { @() }
    $out = @(); $seen = $false
    foreach ($line in $lines) {
        if ($line -like "$Key=*") { $out += "$Key=$Value"; $seen = $true }
        else { $out += $line }
    }
    if (-not $seen) { $out += "$Key=$Value" }
    Set-Content -Path .env -Value $out -Encoding utf8
}

# -----------------------------------------------------------------------------
# Token - the step people get stuck on
# -----------------------------------------------------------------------------
# -Token runs before the venv section below, so resolve an interpreter on demand:
# prefer the venv (it has httpx), fall back to whatever python is on PATH.
function Get-VenvPython {
    foreach ($c in @('.venv\Scripts\python.exe', '.venv/bin/python')) {
        $p = Join-Path $PSScriptRoot "..\$c"
        if (Test-Path $p) { return (Resolve-Path $p).Path }
    }
    $sys = Get-Command python -ErrorAction SilentlyContinue
    if ($sys) { return $sys.Source }
    Die "python not found - Python 3.11+ required"
}

function New-SignozToken {
    Say "SigNoz editor token"
    Note "Funnel WRITES (/api/v1/trace-funnels/new, /steps/update) are gated on"
    Note "EditAccess. A SigNoz API key does NOT carry it - you need a login JWT"
    Note "from an admin or editor account. That is what this step mints."
    Note ""
    Note "Credentials are used once, for one POST to"
    Note "$($env:SIGNOZ_URL)/api/v2/sessions/email_password, and are never written"
    Note "to disk. Only the returned JWT is stored, in .env (gitignored)."

    $email = Read-Host "  SigNoz email"
    $secure = Read-Host "  SigNoz password" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $password = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)

    # Delegate to fot.signoz.SigNozClient.login(). It discovers the orgID that
    # /api/v2/sessions/email_password requires. Do NOT hand-roll this against the
    # legacy /api/v1/login route: it no longer exists, and the SPA answers it with
    # index.html and HTTP 200 - no token, no error, very confusing.
    $py = Get-VenvPython
    $script = @'
import os, sys
try:
    from fot.signoz import SigNozClient, SigNozError
except ImportError as exc:
    sys.exit(f"cannot import fot.signoz ({exc}); run .\scripts\setup.ps1 first")
try:
    with SigNozClient(os.environ.get("SIGNOZ_URL", "http://localhost:8080")) as client:
        print(client.login(sys.argv[1], sys.argv[2]))
except SigNozError as exc:
    sys.exit(f"login failed: {exc}")
except Exception as exc:
    sys.exit(f"login failed: {type(exc).__name__}: {exc}")
'@
    $tok = ($script | & $py - $email $password) 2>&1 | Select-Object -Last 1
    $password = $null

    if ($LASTEXITCODE -ne 0 -or -not $tok -or $tok -notmatch '^[A-Za-z0-9_.\-]+$') {
        Warn "no token returned - check the credentials. $tok"
        return $false
    }
    $tok = "$tok".Trim()

    Set-DotEnv -Key 'SIGNOZ_JWT' -Value $tok
    $env:SIGNOZ_JWT = $tok
    Ok "editor JWT written to .env ($($tok.Length) chars)"
    Note "JWTs expire. When funnel calls start 401ing, re-run: .\scripts\setup.ps1 -Token"
    return $true
}

if ($Token) { if (New-SignozToken) { exit 0 } else { exit 1 } }

# -----------------------------------------------------------------------------
# Preflight
# -----------------------------------------------------------------------------
Say "Preflight"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) { Die "python not found - Python 3.11+ required" }
$pyVer = & python -c "import sys;print('%d.%d'%sys.version_info[:2])"
if ([version]$pyVer -lt [version]'3.11') { Die "python $pyVer found, 3.11+ required" }
Ok "python $pyVer"

if (Get-Command docker -ErrorAction SilentlyContinue) { Ok "docker present" } else { Warn "docker not on PATH" }

try {
    $ver = Invoke-RestMethod -Uri "$($env:SIGNOZ_URL)/api/v1/version" -TimeoutSec 10
    Ok "SigNoz reachable at $($env:SIGNOZ_URL) - $($ver.version)"
} catch {
    Die "SigNoz not reachable at $($env:SIGNOZ_URL). Run 'foundryctl cast' first."
}

# -----------------------------------------------------------------------------
# Virtualenv + deps
# -----------------------------------------------------------------------------
Say "Python environment"
if (-not (Test-Path .venv)) {
    & python -m venv .venv
    if ($LASTEXITCODE -ne 0) { Die "venv creation failed" }
    Ok "created .venv"
} else { Ok ".venv exists" }

$venvPy = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) { $venvPy = Join-Path $repoRoot '.venv/bin/python' }
if (-not (Test-Path $venvPy)) { Die "cannot locate the venv interpreter" }

& $venvPy -m pip install --quiet --upgrade pip 2>&1 | Out-Null
& $venvPy -m pip install --quiet -e ".[all]" 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) {
    Ok "installed funnel-of-thought[all]"
} else {
    Warn "editable install failed - a sibling package may not be importable yet"
    Note "re-run once agent/, fot/ and signoz_funnel_mcp/ are importable"
}

# -----------------------------------------------------------------------------
# Token (unless one is already valid)
# -----------------------------------------------------------------------------
if (-not $env:SIGNOZ_JWT) {
    if (-not (New-SignozToken)) { Warn "continuing without a JWT - funnel creation will fail" }
} else {
    Say "SigNoz editor token"
    try {
        Invoke-RestMethod -Uri "$($env:SIGNOZ_URL)/api/v1/trace-funnels/list" `
            -Headers @{ Authorization = "Bearer $($env:SIGNOZ_JWT)" } -TimeoutSec 10 | Out-Null
        Ok "existing SIGNOZ_JWT is valid"
    } catch {
        Warn "existing SIGNOZ_JWT rejected - re-minting"
        if (-not (New-SignozToken)) { Warn "continuing without a valid JWT" }
    }
}

# -----------------------------------------------------------------------------
# Generate traces
# -----------------------------------------------------------------------------
if (-not $SkipBatch) {
    Say "Generating traces"

    # No Gemini key is not a blocker. --stub emits the identical span shape with
    # zero LLM calls, so every funnel result is reproducible offline in seconds.
    $stub = @()
    if (-not $env:GEMINI_API_KEY) {
        Warn "GEMINI_API_KEY is empty - using --stub (no LLM calls, same spans)"
        Note "for live Gemini latencies: set GEMINI_API_KEY in .env and re-run"
        $stub = @('--stub')
    } else {
        Note "live Gemini on the free tier: expect 5-15 min at 15 RPM."
    }

    # Two models on purpose: the GenAI convention puts the model INSIDE the span
    # name, so a funnel keyed on `chat <model>` only sees that model's share of
    # traffic. Neither is FOT_MODEL_SWAP, so the `fragmented` funnel keyed on the
    # bumped version matches zero traces and triggers the NaN 500.
    $seed    = if ($env:FOT_SEED) { $env:FOT_SEED } else { '1337' }
    # Injected ground truth, pinned so the README's 64% reproduces exactly.
    $rate    = if ($env:FOT_VALIDATE_RATE) { $env:FOT_VALIDATE_RATE } else { '0.64' }
    $modelA  = if ($env:FOT_MODEL)   { $env:FOT_MODEL }   else { 'gemini-3.1-flash-lite' }
    $modelB  = if ($env:FOT_MODEL_B) { $env:FOT_MODEL_B } else { 'gemini-3.1-flash' }
    $runsA   = [int]($Count * 2 / 3)
    $runsB   = $Count - $runsA
    Note "$Count traces, validate-rate=$rate (injected ground truth)"

    $genOk = $true
    foreach ($pair in @(@($modelA, $runsA), @($modelB, $runsB))) {
        if ($pair[1] -le 0) { continue }
        Note "  $($pair[1]) runs on $($pair[0])"
        & $venvPy -m agent.generate --runs $pair[1] --model $pair[0] `
            --validate-rate $rate --seed $seed @stub
        if ($LASTEXITCODE -ne 0) { $genOk = $false; break }
    }

    if ($genOk) {
        Ok "batch complete ($Count traces across 2 models)"
        Note "waiting 15s for the ingester to flush to ClickHouse"
        Start-Sleep -Seconds 15
    } else {
        Die "trace generation failed - nothing below can work without traces"
    }
} else {
    Say "Generating traces"; Note "skipped (-SkipBatch)"
}

# -----------------------------------------------------------------------------
# Apply funnels / dashboard / alert
# -----------------------------------------------------------------------------
Say "Applying SigNoz objects"
& $venvPy -m fot.cli apply
if ($LASTEXITCODE -eq 0) {
    Ok "funnels applied"
} else {
    Die "fot.cli apply failed - check the JWT (funnel writes need EditAccess)"
}

# `apply` creates funnels only. The dashboard and the alert rule are separate
# objects, and BOTH read the metric `fot.funnel.step.conversion` - which only
# `fot gauges` emits. Miss any of these three and the dashboard renders empty
# panels and the alert never evaluates.
& $venvPy -m fot.cli dashboard apply
if ($LASTEXITCODE -eq 0) { Ok "dashboard applied" }
else { Warn "dashboard apply failed - panels will be missing" }

& $venvPy -m fot.cli alert apply
if ($LASTEXITCODE -eq 0) { Ok "alert rule applied" }
else { Warn "alert apply failed - the drop-off alert will not fire" }

& $venvPy -m fot.cli gauges all
if ($LASTEXITCODE -eq 0) {
    Ok "step conversions published as OTel gauges"
    Note "this is what fills the dashboard and arms the alert. Re-run after any"
    Note "new batch to turn the funnel into a time series."
} else {
    Warn "gauge emission failed - dashboard/alert will have no data to read"
}

# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "Setup complete." -ForegroundColor White
Write-Host @"

  Next:
    fot show                  per-step conversion, n on every bar
    fot counter-proof         naive counter vs the ordered funnel
    fot compare               stable-name funnel vs the GenAI-keyed one
    bash scripts/reproduce.sh asserts every claim in the README (needs Git Bash)

  In the UI:
    SigNoz        $($env:SIGNOZ_URL)
    Funnels       $($env:SIGNOZ_URL)/traces-funnels
    Dashboards    $($env:SIGNOZ_URL)/dashboard
    Alerts        $($env:SIGNOZ_URL)/alerts

  MCP:
    SigNoz's own (41 tools)   http://localhost:8000
    ours (5 funnel tools)     signoz-funnel-mcp

  If funnel calls start returning 401, the JWT expired:
    .\scripts\setup.ps1 -Token
"@
