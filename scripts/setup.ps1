<#
.SYNOPSIS
    Funnel of Thought - one-command bootstrap (Windows twin of setup.sh).

.DESCRIPTION
    Assumes SigNoz is already up (`foundryctl cast`). This script never casts,
    restarts or otherwise touches the SigNoz stack - it only reads from it and
    writes funnels/dashboards/alerts through the API.

    Interfaces this script assumes (sibling components own these):
        python -m agent.generate --count N --swap-at K --seed S
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
function New-SignozToken {
    Say "SigNoz editor token"
    Note "Funnel WRITES (/api/v1/trace-funnels/new, /steps/update) are gated on"
    Note "EditAccess. A SigNoz API key does NOT carry it - you need a login JWT"
    Note "from an admin or editor account. That is what this step mints."
    Note ""
    Note "Credentials are used once, for one POST to $($env:SIGNOZ_URL)/api/v1/login,"
    Note "and are never written to disk. Only the returned JWT is stored, in"
    Note ".env (gitignored)."

    $email = Read-Host "  SigNoz email"
    $secure = Read-Host "  SigNoz password" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $password = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)

    $body = @{ email = $email; password = $password } | ConvertTo-Json -Compress
    $password = $null

    try {
        $resp = Invoke-RestMethod -Method Post -Uri "$($env:SIGNOZ_URL)/api/v1/login" `
                    -ContentType 'application/json' -Body $body -TimeoutSec 20
    } catch {
        Warn "login request failed: $($_.Exception.Message)"
        return $false
    }

    # SigNoz has moved this field between versions; try the known shapes.
    $tok = $null
    foreach ($try in @({ $resp.data.accessToken }, { $resp.accessToken }, { $resp.data.access_token })) {
        try { $v = & $try } catch { $v = $null }
        if ($v -is [string] -and $v) { $tok = $v; break }
    }
    if (-not $tok) { Warn "no token in the response - check the credentials"; return $false }

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
    Say "Generating traces (the only step that calls an LLM)"
    if (-not $env:GEMINI_API_KEY) {
        Warn "GEMINI_API_KEY is empty - skipping batch generation"
        Note "set it in .env and re-run"
    } else {
        Note "~$Count traces, offline, free-tier Gemini. Expect 5-15 min at 15 RPM."
        Note "The model swaps halfway through - that is what fragments the LLM span name."
        $seed = if ($env:FOT_SEED) { $env:FOT_SEED } else { '1337' }
        & $venvPy -m agent.generate --count $Count --swap-at ([int]($Count / 2)) --seed $seed
        if ($LASTEXITCODE -eq 0) {
            Ok "batch complete"
            Note "waiting 15s for the ingester to flush to ClickHouse"
            Start-Sleep -Seconds 15
        } else {
            Warn "batch generation failed or agent.generate is not available yet"
        }
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
    Ok "funnels, dashboard and alert applied"
} else {
    Warn "fot.cli apply failed or is not available yet"
    Note "once fot/ is wired up:  fot apply"
}

# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "Setup complete." -ForegroundColor White
Write-Host @"

  Next:
    fot show                  per-step conversion, n on every bar
    fot counter-proof         naive counter vs the ordered funnel
    fot compare               working funnel vs the fragmented one
    bash scripts/reproduce.sh the full 10-minute reproduction

  In the UI:
    SigNoz        $($env:SIGNOZ_URL)
    Funnels       $($env:SIGNOZ_URL)/traces-funnels
    Dashboards    $($env:SIGNOZ_URL)/dashboard
    Alerts        $($env:SIGNOZ_URL)/alerts

  MCP:
    SigNoz's own (41 tools)   http://localhost:8000
    ours (4 funnel tools)     signoz-funnel-mcp

  If funnel calls start returning 401, the JWT expired:
    .\scripts\setup.ps1 -Token
"@
