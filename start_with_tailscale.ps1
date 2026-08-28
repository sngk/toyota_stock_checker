$ErrorActionPreference = "Stop"
$projectDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectDirectory

$tailscaleCommand = Get-Command tailscale -ErrorAction SilentlyContinue
if (-not $tailscaleCommand) {
    $fallback = Join-Path $env:ProgramFiles "Tailscale\tailscale.exe"
    if (Test-Path -LiteralPath $fallback) {
        $tailscaleExecutable = $fallback
    } else {
        Write-Host "Tailscale is not installed or is not available in PATH." -ForegroundColor Red
        Write-Host "Install Tailscale, sign in, and run this file again."
        Read-Host "Press Enter to close"
        exit 1
    }
} else {
    $tailscaleExecutable = $tailscaleCommand.Source
}

$tailscaleAddress = & $tailscaleExecutable ip -4 2>$null | Select-Object -First 1
if ($tailscaleAddress) {
    $tailscaleAddress = $tailscaleAddress.Trim()
}
if (-not $tailscaleAddress) {
    $tailscaleAddress = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.InterfaceAlias -match "Tailscale" -and $_.IPAddress -like "100.*" } |
        Select-Object -First 1 -ExpandProperty IPAddress
}
if (-not $tailscaleAddress) {
    Write-Host "Tailscale is installed but not connected." -ForegroundColor Red
    Write-Host "Open Tailscale, connect it, and run this file again."
    Read-Host "Press Enter to close"
    exit 1
}

$env:PRADO_HOST = "0.0.0.0"
$env:PRADO_PORT = "443"

Write-Host ""
Write-Host "WA Prado Watch is starting over Tailscale." -ForegroundColor Green
Write-Host "On your phone, connect Tailscale and open:" -ForegroundColor Cyan
Write-Host "http://${tailscaleAddress}:443" -ForegroundColor Yellow
Write-Host "Keep this window open. Press Ctrl+C to stop the app." -ForegroundColor DarkGray
Write-Host ""

python app.py
