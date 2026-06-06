# ============================================================
# Mithra Docker Maintenance Script
# Run as Administrator in PowerShell
# Usage: .\mithra-docker-cleanup.ps1
# Optional flags:
#   -SkipCompact   skip vhdx compaction (faster, no downtime)
#   -CompactOnly   only compact vhdx, skip docker pruning
# ============================================================

param(
    [switch]$SkipCompact,
    [switch]$CompactOnly
)

$VHDX_PATH = "C:\Users\abhi_\AppData\Local\Docker\wsl\disk\docker_data.vhdx"
$MITHRA_DIR = "C:\mithra"
$DOCKER_DESKTOP = "C:\Program Files\Docker\Docker\Docker Desktop.exe"

function Write-Step($msg) {
    Write-Host "`n==> $msg" -ForegroundColor Cyan
}

function Write-Success($msg) {
    Write-Host "    [OK] $msg" -ForegroundColor Green
}

function Write-Warn($msg) {
    Write-Host "    [!!] $msg" -ForegroundColor Yellow
}

function Get-VhdxSizeGB {
    if (Test-Path $VHDX_PATH) {
        return [math]::Round((Get-Item $VHDX_PATH).Length / 1GB, 2)
    }
    return $null
}

function Get-CDriveFreeGB {
    return [math]::Round((Get-PSDrive C).Free / 1GB, 2)
}

# ────────────────────────────────────────────
# STEP 0: Confirm
# ────────────────────────────────────────────
Write-Host ""
Write-Host "  Mithra Docker Maintenance" -ForegroundColor Magenta
Write-Host "  ─────────────────────────────────────────" -ForegroundColor DarkGray

$vhdxBefore = Get-VhdxSizeGB
$freeBefore  = Get-CDriveFreeGB

Write-Host "  VHDX size before  : $vhdxBefore GB" -ForegroundColor DarkGray
Write-Host "  C: free before    : $freeBefore GB"  -ForegroundColor DarkGray

if (-not $CompactOnly) {
    $confirm = Read-Host "`n  This will bring Mithra DOWN briefly for cleanup. Continue? (y/n)"
    if ($confirm -ne 'y') { Write-Host "Aborted." -ForegroundColor Red; exit }
}

# ────────────────────────────────────────────
# STEP 1: Docker prune (skip if CompactOnly)
# ────────────────────────────────────────────
if (-not $CompactOnly) {
    Write-Step "Pruning dangling images..."
    docker image prune -f
    Write-Success "Dangling images removed"

    Write-Step "Pruning build cache..."
    docker builder prune -af
    Write-Success "Build cache cleared"

    Write-Step "Pruning unused volumes..."
    docker volume prune -f
    Write-Success "Unused volumes removed"
}

# ────────────────────────────────────────────
# STEP 2: Compact VHDX (skip if SkipCompact)
# ────────────────────────────────────────────
if (-not $SkipCompact) {
    Write-Step "Shutting down Docker and WSL for compaction..."

    taskkill /F /IM "Docker Desktop.exe"  2>$null
    taskkill /F /IM "com.docker.build.exe" 2>$null
    taskkill /F /IM "docker-agent.exe"    2>$null
    taskkill /F /IM "docker-sandbox.exe"  2>$null
    taskkill /F /IM "wsl.exe"             2>$null
    taskkill /F /IM "wslhost.exe"         2>$null
    taskkill /F /IM "wslrelay.exe"        2>$null
    net stop WslService       2>$null | Out-Null
    net stop com.docker.service 2>$null | Out-Null
    wsl --shutdown            2>$null

    Write-Host "    Waiting 15 seconds for processes to fully exit..." -ForegroundColor DarkGray
    Start-Sleep -Seconds 15

    # Kill any lingering WSL processes
    Get-Process | Where-Object { $_.Name -like "*wsl*" -or $_.Name -like "*docker*" } | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 5

    Write-Success "All Docker/WSL processes stopped"

    Write-Step "Compacting VHDX (this takes 5-10 minutes)..."

    $diskpartScript = @"
select vdisk file="$VHDX_PATH"
attach vdisk readonly
compact vdisk
detach vdisk
exit
"@
    $diskpartScript | diskpart

    Write-Success "VHDX compaction complete"

    # ────────────────────────────────────────────
    # STEP 3: Restart Docker Desktop
    # ────────────────────────────────────────────
    Write-Step "Starting Docker Desktop..."
    Start-Process $DOCKER_DESKTOP

    Write-Host "    Waiting for Docker engine to be ready (up to 90 seconds)..." -ForegroundColor DarkGray

    $timeout = 90
    $elapsed = 0
    $ready = $false

    while ($elapsed -lt $timeout) {
        Start-Sleep -Seconds 5
        $elapsed += 5
        $result = docker info 2>&1
        if ($LASTEXITCODE -eq 0) {
            $ready = $true
            break
        }
        Write-Host "    Still waiting... ($elapsed s)" -ForegroundColor DarkGray
    }

    if (-not $ready) {
        Write-Warn "Docker didn't respond in 90s. Start Docker Desktop manually, then run:"
        Write-Warn "  cd $MITHRA_DIR && docker compose up -d"
        exit 1
    }

    Write-Success "Docker engine is ready"
}

# ────────────────────────────────────────────
# STEP 4: Restart Mithra stack
# ────────────────────────────────────────────
if (-not $SkipCompact) {
    Write-Step "Bringing Mithra stack back up..."
    Set-Location $MITHRA_DIR
    docker compose up -d
    Write-Success "Mithra stack started"
}

# ────────────────────────────────────────────
# STEP 5: Report
# ────────────────────────────────────────────
Write-Step "Results"

$vhdxAfter = Get-VhdxSizeGB
$freeAfter  = Get-CDriveFreeGB

$vhdxSaved = [math]::Round($vhdxBefore - $vhdxAfter, 2)
$freeGained = [math]::Round($freeAfter - $freeBefore, 2)

Write-Host ""
Write-Host "  ─────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host "  VHDX size   : $vhdxBefore GB  -->  $vhdxAfter GB  (saved $vhdxSaved GB)" -ForegroundColor Green
Write-Host "  C: free     : $freeBefore GB  -->  $freeAfter GB  (gained $freeGained GB)" -ForegroundColor Green
Write-Host "  ─────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host ""

Write-Step "Verifying running containers..."
docker ps --format "table {{.Names}}`t{{.Status}}"

Write-Host ""
Write-Host "  Done! Mithra is back online." -ForegroundColor Magenta
Write-Host ""
