<#
.SYNOPSIS
    Validate that this machine is ready to run the webmailer stack.

.DESCRIPTION
    Runnable at any time (also invoked at the end of setup-local.ps1). Checks
    everything that has to be (re)created on every new computer - the virtualenv,
    the portable tools, the Cloudflare tunnel credentials and the .env secrets -
    and prints PASS/FAIL with a fix for each failure.

    Exits non-zero when anything required is missing, so it is safe to use in
    scripts and to gate "did setup actually work?".

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvPy   = Join-Path $RepoRoot 'env\Scripts\python.exe'
$CfDir    = Join-Path $RepoRoot '.tools\cloudflared'
$CfExe    = Join-Path $CfDir 'cloudflared.exe'
$CfConfig = Join-Path $CfDir 'config.yml'
$RedisDir = Join-Path $RepoRoot '.tools\redis'
$EnvFile  = Join-Path $RepoRoot '.env'

$script:Failures = 0
$script:Warnings = 0

function Pass([string]$m) { Write-Host "  [PASS] $m" -ForegroundColor Green }
function Warn([string]$m) { Write-Host "  [WARN] $m" -ForegroundColor Yellow; $script:Warnings++ }
function Fail([string]$m) { Write-Host "  [FAIL] $m" -ForegroundColor Red; $script:Failures++ }
function Section([string]$m) { Write-Host "`n$m" -ForegroundColor Cyan }

# Read a key's value from .env (empty string if absent).
function Get-EnvValue([string]$name) {
    if (-not (Test-Path -LiteralPath $EnvFile)) { return $null }
    $line = Select-String -LiteralPath $EnvFile -Pattern ("^\s*" + [regex]::Escape($name) + "\s*=(.*)$") | Select-Object -First 1
    if (-not $line) { return $null }
    return $line.Matches[0].Groups[1].Value.Trim()
}

function Test-Secret([string]$name, [string[]]$placeholders, [switch]$Soft) {
    $value = Get-EnvValue $name
    if ([string]::IsNullOrWhiteSpace($value)) {
        if ($Soft) { Warn "$name is empty" } else { Fail "$name is empty - edit .env" }
        return
    }
    foreach ($p in $placeholders) {
        if ($value -ieq $p -or $value -match [regex]::Escape($p)) {
            if ($Soft) { Warn "$name still has the example value - edit .env" } else { Fail "$name still has the example value - edit .env" }
            return
        }
    }
    Pass "$name is set"
}

Write-Host "webmailer doctor" -ForegroundColor White
Write-Host "repo: $RepoRoot"

# --- Python / virtualenv -------------------------------------------------------
Section 'Python & virtualenv'
if (Test-Path -LiteralPath $VenvPy) {
    Pass "venv python: $VenvPy"
} else {
    Fail "venv missing at $VenvPy - run setup-local.bat"
}

# --- Portable tools ------------------------------------------------------------
Section 'Portable tools (.tools)'
if (Test-Path -LiteralPath $CfExe) {
    Pass "cloudflared: $CfExe"
} else {
    Fail "cloudflared missing at $CfExe - run setup-local.bat"
}

$redisServer = Get-ChildItem -Path $RedisDir -Recurse -Filter 'redis-server.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
if ($redisServer) {
    Pass "redis-server: $($redisServer.FullName)"
} else {
    Fail "redis-server.exe missing under $RedisDir - run setup-local.bat"
}

# --- Cloudflare tunnel ---------------------------------------------------------
Section 'Cloudflare tunnel'
if (-not (Test-Path -LiteralPath $CfConfig)) {
    Fail "tunnel config missing at $CfConfig - run setup-local.bat"
} else {
    Pass "tunnel config: $CfConfig"
    $match = Select-String -LiteralPath $CfConfig -Pattern '^\s*credentials-file:\s*(.+?)\s*$' | Select-Object -First 1
    if (-not $match) {
        Fail "config.yml has no 'credentials-file:' line - run setup-local.bat"
    } else {
        $cred = $match.Matches[0].Groups[1].Value.Trim().Trim('"').Trim("'")
        if (Test-Path -LiteralPath $cred) {
            Pass "tunnel credentials: $cred"
        } else {
            Fail "tunnel credentials missing at $cred - run setup-local.bat to (re)create them (this is what crash-loops cloudflared)"
        }
    }
}

# --- .env ----------------------------------------------------------------------
Section 'Environment (.env)'
if (-not (Test-Path -LiteralPath $EnvFile)) {
    Fail ".env missing at $EnvFile - copy .env.example to .env and fill it in"
} else {
    Pass ".env present"
    Test-Secret 'SECRET_KEY'                 @('change-me-to-a-long-random-string')
    Test-Secret 'DATABASE_URL'               @('your-project-ref', 'your-password')
    Test-Secret 'SUPABASE_URL'               @('your-project-ref')
    Test-Secret 'SUPABASE_ANON_KEY'          @('your-anon-key')
    Test-Secret 'SUPABASE_SERVICE_ROLE_KEY'  @('your-service-role-key')
    Test-Secret 'BREVO_API_KEY'              @('your-brevo-api-key')
    Test-Secret 'BREVO_WEBHOOK_SECRET'       @('your-brevo-webhook-token') -Soft

    $authRequired = Get-EnvValue 'AUTH_REQUIRED'
    $allowed = Get-EnvValue 'AUTH_ALLOWED_EMAILS'
    if ($authRequired -ne 'false') {
        if ([string]::IsNullOrWhiteSpace($allowed) -or $allowed -match 'you@example') {
            Fail "AUTH_ALLOWED_EMAILS is empty/placeholder while AUTH_REQUIRED != false - nobody can sign in"
        } else {
            Pass "AUTH_ALLOWED_EMAILS is set"
        }
    }
    $public = Get-EnvValue 'PUBLIC_BASE_URL'
    if ([string]::IsNullOrWhiteSpace($public) -or $public -match 'yourdomain|your-tunnel|example\.com') {
        Warn "PUBLIC_BASE_URL is empty/placeholder - tracking + unsubscribe links break"
    }
}

# --- Database ------------------------------------------------------------------
if (Test-Path -LiteralPath $VenvPy) {
    Section 'Database'
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $py = "import psycopg2`nfrom app.config import get_settings`nu=get_settings().database_url`nc=psycopg2.connect(u)`nc.close()`nprint('DOCTOR_OK')"
        $out = & $VenvPy -c $py 2>&1
        if ($LASTEXITCODE -eq 0 -and (($out | Out-String) -match 'DOCTOR_OK')) {
            Pass "database reachable (DATABASE_URL)"
        } else {
            Fail "database not reachable - check DATABASE_URL / Supabase in .env"
            foreach ($l in @($out)) { Write-Host "         $l" -ForegroundColor DarkGray }
        }
    } catch {
        Fail "database check failed: $_"
    } finally {
        $ErrorActionPreference = $prev
    }
}

# --- Account-level reminders (shared, set once) --------------------------------
Section 'Reminders (account-level, set once - not per machine)'
Warn "Supabase dashboard: add your sign-in user + redirect URLs (https://track.goprilad.com/auth/callback, http://localhost:5000/auth/callback)"
Warn "Brevo: register webhook https://track.goprilad.com/api/webhooks/brevo?token=<BREVO_WEBHOOK_SECRET>"

# --- Summary -------------------------------------------------------------------
Write-Host ""
if ($script:Failures -gt 0) {
    Write-Host "doctor: $($script:Failures) problem(s), $($script:Warnings) warning(s)." -ForegroundColor Red
    Write-Host "Fix the [FAIL] items above, then re-run doctor.bat." -ForegroundColor Red
    exit 1
}
Write-Host "doctor: all checks passed ($($script:Warnings) warning(s))." -ForegroundColor Green
exit 0
