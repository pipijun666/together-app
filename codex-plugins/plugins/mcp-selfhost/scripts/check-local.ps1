$ErrorActionPreference = 'Stop'
$endpoint = 'http://127.0.0.1:18080/mcp'
Write-Host '=== MCP Selfhost local health ==='
$listener = Get-NetTCPConnection -LocalPort 18080 -State Listen -ErrorAction SilentlyContinue
if ($listener) {
  $listener | Select-Object LocalAddress,LocalPort,OwningProcess
} else {
  Write-Host 'PORT_18080_NOT_LISTENING'
}
Write-Host '=== Scheduled tasks ==='
Get-ScheduledTask -TaskName 'MCP Selfhost*' -ErrorAction SilentlyContinue |
  Select-Object TaskName,State
Write-Host '=== MCP HTTP probe ==='
try {
  $r = Invoke-WebRequest -Uri $endpoint -Method Get -UseBasicParsing -TimeoutSec 5
  Write-Host "HTTP_STATUS=$($r.StatusCode)"
} catch {
  if ($_.Exception.Response) {
    Write-Host "HTTP_STATUS=$([int]$_.Exception.Response.StatusCode)"
  } else {
    Write-Host "HTTP_ERROR=$($_.Exception.Message)"
  }
}
