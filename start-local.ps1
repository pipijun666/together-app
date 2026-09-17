param(
    [string]$PythonExe = '',
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
$appDir = $PSScriptRoot
if (-not $PythonExe) {
    $projectPython = Join-Path $appDir '.venv\Scripts\python.exe'
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    $bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    $candidates = @($projectPython)
    if ($pythonCommand) { $candidates += $pythonCommand.Source }
    $candidates += $bundledPython
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        try {
            & $candidate -c 'import PIL' 2>$null
            if ($LASTEXITCODE -eq 0) {
                $PythonExe = $candidate
                break
            }
        } catch {
            continue
        }
    }
}
if (-not $PythonExe) { throw 'Python with Pillow was not found. Install Python 3.11+ and run: python -m pip install -r requirements.txt' }
& $PythonExe -c 'import PIL'
if ($LASTEXITCODE -ne 0) { throw 'Pillow is missing. Run: python -m pip install -r requirements.txt' }
if ($CheckOnly) {
    Write-Host ('Startup check passed with: ' + $PythonExe)
    exit 0
}
$envFile = Join-Path $appDir '.env'
if (-not (Test-Path -LiteralPath $envFile)) {
    & $PythonExe (Join-Path $appDir 'setup.py')
    if ($LASTEXITCODE -ne 0) { throw 'Local setup failed.' }
}
$dataDir = Join-Path $appDir 'data'
New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
$running = $false
try { $health = Invoke-RestMethod 'http://127.0.0.1:8765/healthz' -TimeoutSec 2; $running = [bool]$health.ok } catch {}
if ($running) { throw 'Port 8765 is already in use. Open the existing preview or stop the old service.' }
foreach ($name in @('server','worker')) {
    $scriptPath = Join-Path $appDir ($name + '.py')
    $arguments = @('-u', ('"' + $scriptPath + '"'), '--env', ('"' + $envFile + '"'))
    $proc = Start-Process -FilePath $PythonExe -ArgumentList $arguments -WorkingDirectory $appDir -WindowStyle Hidden -RedirectStandardOutput (Join-Path $dataDir ($name + '.log')) -RedirectStandardError (Join-Path $dataDir ($name + '-error.log')) -PassThru
    $proc.Id | Set-Content -LiteralPath (Join-Path $dataDir ($name + '.pid'))
}
Write-Host 'Local test started: http://127.0.0.1:8765'
Write-Host 'Login passwords are in .env: USER_ONE_PASSWORD / USER_TWO_PASSWORD.'
Write-Host 'Android USB test: adb reverse tcp:8765 tcp:8765; use http://127.0.0.1:8765 in the app.'
Write-Host 'For the two-server setup, follow README.md.'
