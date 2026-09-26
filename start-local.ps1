[CmdletBinding()]
param(
    [switch]$StartStorage,
    [switch]$OpenBrowser
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$backendRoot = Join-Path $projectRoot 'py'
$pythonPath = Join-Path $backendRoot '.venv\Scripts\python.exe'
$frontendScript = Join-Path $projectRoot 'scripts\serve_frontend.py'
$runtimeRoot = Join-Path $projectRoot '.runtime.local'

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw '缺少 py/.venv/Scripts/python.exe。请先在 py 目录运行 uv sync --locked。'
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'index.html') -PathType Leaf)) {
    throw '项目根目录缺少 index.html。'
}
if (-not (Test-Path -LiteralPath $runtimeRoot)) {
    New-Item -ItemType Directory -Path $runtimeRoot | Out-Null
}

function Get-ExpectedListener {
    param([int]$Port, [ValidateSet('api', 'frontend')][string]$Kind)
    $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if ($listeners.Count -eq 0) { return $null }
    $owners = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($owners.Count -ne 1 -or @($listeners | Where-Object LocalAddress -ne '127.0.0.1').Count -gt 0) {
        throw "端口 $Port 已被其他或非回环服务占用。请检查后重试；脚本不会终止现有进程。"
    }
    $ownerId = [int]$owners[0]
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ownerId"
    $commandLine = [string]$process.CommandLine
    $executable = [string]$process.ExecutablePath
    $expected = $executable -ieq $pythonPath
    if ($Kind -eq 'api') {
        $expected = $expected -and $commandLine.Contains('uvicorn') -and $commandLine.Contains('knowpath_backend.learning.main:app')
    } else {
        $expected = $expected -and $commandLine.Contains($frontendScript) -and -not $commandLine.Contains('--open-browser')
    }
    if (-not $expected) {
        throw "无法确认端口 $Port 属于本项目服务。请检查后重试；脚本不会终止现有进程。"
    }
    return $ownerId
}

function Test-ServiceReady {
    param([ValidateSet('api', 'frontend')][string]$Kind)
    try {
        if ($Kind -eq 'api') {
            $response = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/api/v1/health' -UseBasicParsing -TimeoutSec 3
            $body = $response.Content | ConvertFrom-Json
            return $response.StatusCode -eq 200 -and $body.status -eq 'ok' -and [bool]$response.Headers['X-Request-ID']
        }
        $response = Invoke-WebRequest -Uri 'http://127.0.0.1:5173/' -Method Head -UseBasicParsing -TimeoutSec 3
        return $response.StatusCode -eq 200 -and $response.Headers['X-KnowPath-Service'] -eq 'knowpath-index-only-v1'
    } catch {
        return $false
    }
}

function Wait-ServiceReady {
    param([int]$Port, [ValidateSet('api', 'frontend')][string]$Kind, $StartedProcess)
    $deadline = (Get-Date).AddSeconds(45)
    do {
        if ($null -ne $StartedProcess) {
            $StartedProcess.Refresh()
            if ($StartedProcess.HasExited) {
                throw "$Kind 服务提前退出。请查看 .runtime.local 中对应日志。"
            }
        }
        $ownerId = Get-ExpectedListener -Port $Port -Kind $Kind
        if ($null -ne $ownerId -and (Test-ServiceReady -Kind $Kind)) { return }
        Start-Sleep -Milliseconds 400
    } while ((Get-Date) -lt $deadline)
    throw "$Kind 服务未在 45 秒内就绪。请查看 .runtime.local 日志；不会终止其他进程。"
}

if ($StartStorage) {
    $composeFile = Join-Path $projectRoot 'infra\docker-compose.yml'
    & docker compose -f $composeFile up -d --no-recreate
    if ($LASTEXITCODE -ne 0) { throw '存储服务启动失败，请确认 Docker Desktop 已运行。' }
}

$apiOwner = Get-ExpectedListener -Port 8000 -Kind api
$apiProcess = $null
if ($null -eq $apiOwner) {
    $apiProcess = Start-Process -FilePath $pythonPath `
        -ArgumentList @('-m', 'uvicorn', 'knowpath_backend.learning.main:app', '--host', '127.0.0.1', '--port', '8000') `
        -WorkingDirectory $backendRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $runtimeRoot 'api.stdout.log') `
        -RedirectStandardError (Join-Path $runtimeRoot 'api.stderr.log')
}
Wait-ServiceReady -Port 8000 -Kind api -StartedProcess $apiProcess

$frontendOwner = Get-ExpectedListener -Port 5173 -Kind frontend
$frontendProcess = $null
if ($null -eq $frontendOwner) {
    $frontendProcess = Start-Process -FilePath $pythonPath `
        -ArgumentList @('-u', ('"{0}"' -f $frontendScript)) `
        -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $runtimeRoot 'frontend.stdout.log') `
        -RedirectStandardError (Join-Path $runtimeRoot 'frontend.stderr.log')
}
Wait-ServiceReady -Port 5173 -Kind frontend -StartedProcess $frontendProcess

Write-Host '本地 API 已连接：http://127.0.0.1:8000/api/v1/health'
Write-Host '前端已启动：http://127.0.0.1:5173/'
Write-Host '日志位于 .runtime.local。API 模型调用仍需单独配置模型密钥。'

if ($OpenBrowser) {
    # Only the Python helper reads the token; it is never echoed or embedded here.
    & $pythonPath $frontendScript --open-browser
    if ($LASTEXITCODE -ne 0) { throw '打开已认证页面失败。服务仍在运行，请检查本地令牌配置。' }
}
