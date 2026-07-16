$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = Join-Path $env:USERPROFILE "venvs\PythonProject\Scripts\python.exe"
$DashboardFile = Join-Path $ProjectDir "dashboard.py"
$Port = 8501
$DashboardUrl = "http://127.0.0.1:$Port"

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "대시보드용 Python을 찾을 수 없습니다: $PythonExe"
}

if (-not (Test-Path -LiteralPath $DashboardFile)) {
    throw "dashboard.py를 찾을 수 없습니다: $DashboardFile"
}

$isRunning = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $isRunning) {
    $arguments = "-m streamlit run `"$DashboardFile`" --server.address 127.0.0.1 --server.port $Port"
    Start-Process `
        -FilePath $PythonExe `
        -ArgumentList $arguments `
        -WorkingDirectory $ProjectDir `
        -WindowStyle Hidden

    # 서버가 준비될 시간을 잠시 준다. 준비가 조금 늦어도 브라우저는 열어 둔다.
    Start-Sleep -Seconds 3
}

Start-Process $DashboardUrl
