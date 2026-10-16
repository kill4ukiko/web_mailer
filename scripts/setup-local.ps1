<#
.SYNOPSIS
    Portable one-shot setup for running the webmailer stack locally.

.DESCRIPTION
    Idempotent and safe to re-run. On a fresh computer it will:
      1. Find a suitable Python (>= 3.10) - and tell you what to install if
         none is found (it never installs Python for you).
      2. Create the virtualenv (env\) and pip-install requirements.txt.
      3. Download portable tools into .tools\ (Redis for Windows, cloudflared).
      4. Create .env from .env.example (then warn about placeholders).
      5. Apply database migrations + seed settings (app.cron bootstrap).
      6. Set up the Cloudflare named tunnel for track.goprilad.com.

.PARAMETER SkipTools
    Do not download Redis / cloudflared.

.PARAMETER SkipTunnel
    Do not touch the Cloudflare tunnel (setup / login / DNS route).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\setup-local.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipTools,
    [switch]$SkipTunnel
)

$ErrorActionPreference = 'Stop'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

# --- Fixed locations / versions ------------------------------------------------
$RepoRoot   = Split-Path -Parent $PSScriptRoot
$VenvDir    = Join-Path $RepoRoot 'env'
$VenvPy     = Join-Path $VenvDir 'Scripts\python.exe'
$ToolsDir   = Join-Path $RepoRoot '.tools'
$RedisDir   = Join-Path $ToolsDir 'redis'
$CfDir      = Join-Path $ToolsDir 'cloudflared'
$CfExe      = Join-Path $CfDir 'cloudflared.exe'
$CfConfig   = Join-Path $CfDir 'config.yml'
$EnvFile    = Join-Path $RepoRoot '.env'
$EnvExample = Join-Path $RepoRoot '.env.example'

$RedisZipUrl = 'https://github.com/redis-windows/redis-windows/releases/download/8.10.2/Redis-8.10.2-Windows-x64-msys2.zip'
$RedisSha256 = '7C8CEBD50347EAA1D9E784DA842ED47A4F33394637835531D6614777B950EE85'
$CfUrl       = 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe'

$TunnelName  = 'webmailer'
$PublicHost  = 'track.goprilad.com'

# --- Helpers -------------------------------------------------------------------
function Write-Step([string]$m) { Write-Host "`n== $m" -ForegroundColor Cyan }
function Write-Ok([string]$m)   { Write-Host "   $m" -ForegroundColor Green }
function Write-Warn([string]$m) { Write-Host "   $m" -ForegroundColor Yellow }

function Download-File([string]$url, [string]$dest) {
    Write-Host "   downloading $url"
    $dir = Split-Path -Parent $dest
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
}

function Assert-Sha256([string]$path, [string]$expected) {
    $actual = (Get-FileHash -Path $path -Algorithm SHA256).Hash
    if ($actual -ne $expected) {
        throw "SHA256 mismatch for $path`n  expected $expected`n  actual   $actual"
    }
}

function Find-Python {
    $cands = @()
    if (Get-Command py      -ErrorAction SilentlyContinue) { $cands += [pscustomobject]@{ Exe = 'py';      Args = @('-3') } }
    if (Get-Command python  -ErrorAction SilentlyContinue) { $cands += [pscustomobject]@{ Exe = 'python';  Args = @() } }
    if (Get-Command python3 -ErrorAction SilentlyContinue) { $cands += [pscustomobject]@{ Exe = 'python3'; Args = @() } }
    foreach ($c in $cands) {
        try {
            $out = & $c.Exe @($c.Args) -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $out -match '^(\d+)\.(\d+)$') {
                if ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 10) { return $c }
            }
        } catch { }
    }
    return $null
}

Write-Host "webmailer - local setup" -ForegroundColor White
Write-Host "repo: $RepoRoot"

# --- 1. Python -----------------------------------------------------------------
Write-Step 'Python (>= 3.10)'
$python = Find-Python
if (-not $python) {
    Write-Host "   ERROR: no Python 3.10+ found on PATH." -ForegroundColor Red
    Write-Host ""
    Write-Host "   Install Python 3.12 for Windows, then re-run this script:" -ForegroundColor Yellow
    Write-Host "     https://www.python.org/downloads/windows/" -ForegroundColor Yellow
    Write-Host "   During install, tick 'Add python.exe to PATH'." -ForegroundColor Yellow
    Write-Host "   (Or, if you use winget:)  winget install Python.Python.3.12" -ForegroundColor Yellow
    exit 1
}
$pyVer = & $python.Exe @($python.Args) --version 2>&1
Write-Ok "using $($python.Exe) $($python.Args -join ' ')  ($pyVer)"

# --- 2. Virtualenv + requirements ---------------------------------------------
Write-Step 'Virtual environment'
if (-not (Test-Path -LiteralPath $VenvPy)) {
    Write-Host "   creating $VenvDir"
    & $python.Exe @($python.Args) -m venv $VenvDir
    if (-not (Test-Path -LiteralPath $VenvPy)) { throw 'venv creation failed' }
    Write-Ok 'venv created'
} else {
    Write-Ok "already present: $VenvDir"
}

Write-Host '   upgrading pip'
& $VenvPy -m pip install --upgrade pip
Write-Host '   installing requirements.txt'
& $VenvPy -m pip install -r (Join-Path $RepoRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
Write-Ok 'dependencies installed'

# --- 3. Portable tools ---------------------------------------------------------
if (-not $SkipTools) {
    Write-Step 'Portable tools (.tools)'

    $redisServer = Get-ChildItem -Path $RedisDir -Recurse -Filter 'redis-server.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($redisServer) {
        Write-Ok "Redis present: $($redisServer.FullName)"
    } else {
        $zip = Join-Path $env:TEMP 'Redis-8.10.2-Windows-x64-msys2.zip'
        Download-File $RedisZipUrl $zip
        Assert-Sha256 $zip $RedisSha256
        New-Item -ItemType Directory -Path $RedisDir -Force | Out-Null
        Expand-Archive -Path $zip -DestinationPath $RedisDir -Force
        Write-Ok 'Redis extracted to .tools\redis'
    }

    # cloudflared
    if (Test-Path -LiteralPath $CfExe) {
        Write-Ok "cloudflared present: $CfExe"
    } else {
        Download-File $CfUrl $CfExe
        Write-Ok "cloudflared downloaded to $CfExe"
    }
}

# --- 4. .env -------------------------------------------------------------------
Write-Step 'Environment file (.env)'
if (-not (Test-Path -LiteralPath $EnvFile)) {
    Copy-Item -LiteralPath $EnvExample -Destination $EnvFile
    Write-Warn '.env created from .env.example - fill in the real values.'
} else {
    Write-Ok '.env already exists'
}

$placeholderWarnings = @()
$authLine = (Select-String -Path $EnvFile -Pattern '^AUTH_ALLOWED_EMAILS=' | Select-Object -First 1)
if (-not $authLine -or $authLine.Line -match 'AUTH_ALLOWED_EMAILS=\s*(you@|$)' -or $authLine.Line -match 'you@yourdomain|you@example') {
    $placeholderWarnings += 'AUTH_ALLOWED_EMAILS is empty/placeholder -> sign-in fails closed.'
}
$pubLine = (Select-String -Path $EnvFile -Pattern '^PUBLIC_BASE_URL=' | Select-Object -First 1)
if (-not $pubLine -or $pubLine.Line -match 'your-tunnel|yourdomain|example\.com') {
    $placeholderWarnings += 'PUBLIC_BASE_URL is a placeholder -> tracking/unsubscribe links break.'
}
if ($placeholderWarnings.Count -gt 0) {
    Write-Host ''
    Write-Warn '*** ACTION REQUIRED - edit .env before sending: ***'
    foreach ($w in $placeholderWarnings) { Write-Warn "  - $w" }
}

# --- 4b. Database bootstrap ----------------------------------------------------
Write-Step 'Database bootstrap (migrations + settings)'
Push-Location $RepoRoot
try {
    & $VenvPy -m app.cron bootstrap
    if ($LASTEXITCODE -ne 0) { Write-Warn 'bootstrap returned a non-zero exit code - check the output above.' }
    # Seed the starter email templates (idempotent by name).
    & $VenvPy -m scripts.seed_templates
    if ($LASTEXITCODE -ne 0) { Write-Warn 'template seeding returned a non-zero exit code.' }
} finally {
    Pop-Location
}

# --- 5. Cloudflare named tunnel ------------------------------------------------
if (-not $SkipTunnel) {
    Write-Step "Cloudflare tunnel ($PublicHost)"
    # cloudflared writes progress to stderr; don't let that abort the script.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'

    if (-not (Test-Path -LiteralPath $CfExe)) { throw "cloudflared not found at $CfExe - re-run without -SkipTools." }

    $cfHome = Join-Path $env:USERPROFILE '.cloudflared'
    $cert   = Join-Path $cfHome 'cert.pem'
    if (-not (Test-Path -LiteralPath $cert)) {
        Write-Warn 'No Cloudflare certificate found - opening browser to log in...'
        & $CfExe tunnel login
        if (-not (Test-Path -LiteralPath $cert)) { throw 'cloudflared login did not complete (no cert.pem).' }
    } else {
        Write-Ok 'already logged in (cert.pem present)'
    }

    function Get-Tunnel([string]$name) {
        try {
            $json = & $CfExe tunnel list --output json 2>$null
            if (-not $json) { return $null }
            return ($json | ConvertFrom-Json | Where-Object { $_.name -eq $name } | Select-Object -First 1)
        } catch { return $null }
    }

    $tunnel = Get-Tunnel $TunnelName
    if (-not $tunnel) {
        Write-Host "   creating tunnel '$TunnelName'"
        & $CfExe tunnel create $TunnelName
        $tunnel = Get-Tunnel $TunnelName
    } else {
        Write-Ok "tunnel '$TunnelName' exists"
    }
    if (-not $tunnel) { throw "could not create/find tunnel '$TunnelName'." }

    $tunnelId = $tunnel.id
    if (-not $tunnelId) { $tunnelId = $tunnel.uuid }
    if (-not $tunnelId) { throw "could not determine the UUID of tunnel '$TunnelName'." }
    $credFile = Join-Path $cfHome "$tunnelId.json"

    # `cloudflared tunnel create` is the only step that writes the per-tunnel
    # credentials file. On a second machine the tunnel already exists, so that
    # step is skipped and the file is missing -> cloudflared exits immediately.
    # Re-materialize it from the tunnel token (same UUID + DNS, no re-create).
    if (-not (Test-Path -LiteralPath $credFile)) {
        Write-Warn "credentials file missing for existing tunnel - fetching via 'tunnel token'"
        & $CfExe tunnel token --cred-file $credFile $TunnelName
        if (-not (Test-Path -LiteralPath $credFile)) {
            throw "could not fetch credentials for tunnel '$TunnelName' to $credFile. Make sure you are logged into the Cloudflare account that owns '$TunnelName'."
        }
        Write-Ok "wrote credentials $credFile"
    } else {
        Write-Ok "credentials present: $credFile"
    }

    Write-Host "   routing DNS $PublicHost -> tunnel '$TunnelName'"
    & $CfExe tunnel route dns $TunnelName $PublicHost
    if ($LASTEXITCODE -ne 0) { Write-Warn "DNS route returned non-zero (often harmless if the record already exists)." }

    $cfg = @(
        "tunnel: $tunnelId",
        "credentials-file: $credFile",
        "ingress:",
        "  - hostname: $PublicHost",
        "    service: http://127.0.0.1:5000",
        "  - service: http_status:404"
    )
    [System.IO.File]::WriteAllText($CfConfig, ($cfg -join "`r`n") + "`r`n", (New-Object System.Text.UTF8Encoding($false)))
    Write-Ok "wrote $CfConfig"
    $ErrorActionPreference = $prevEAP
}

# --- Done ----------------------------------------------------------------------
Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor White
Write-Host "  1. Edit .env  -> set AUTH_ALLOWED_EMAILS (and any secrets)."
Write-Host "  2. Set the public base URL (once):"
Write-Host "       .\env\Scripts\python.exe -m app.cron bootstrap"
Write-Host "     then in the app's Settings page set Public base URL to https://$PublicHost"
Write-Host "  3. Register the Brevo webhook:"
Write-Host "       https://$PublicHost/api/webhooks/brevo?token=<BREVO_WEBHOOK_SECRET>"
Write-Host "  4. Start everything:  .\run-local.bat"
Write-Host ""

# --- Verify --------------------------------------------------------------------
Write-Host "Verifying this machine (doctor)..." -ForegroundColor Cyan
& (Join-Path $PSScriptRoot 'doctor.ps1')
$doctorRc = $LASTEXITCODE
if ($doctorRc -ne 0) {
    Write-Host ""
    Write-Host "Setup finished, but doctor found the problems listed above." -ForegroundColor Yellow
    Write-Host "Fix them and re-run doctor.bat." -ForegroundColor Yellow
}
exit $doctorRc