# EIASS MCP 자동 등록 스크립트
# 사용법: 이 폴더(mcp_server.exe가 있는 곳)에서
#   powershell -ExecutionPolicy Bypass -File install.ps1
# Claude Code / Claude Desktop / Codex CLI 중 이 PC에 설치된 것을 찾아 자동으로 eiass MCP 서버를 등록한다.

param(
    [string]$ExePath = (Join-Path $PSScriptRoot "mcp_server.exe")
)

$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$OutputEncoding = [System.Text.Encoding]::UTF8

if (-not (Test-Path $ExePath)) {
    Write-Error "mcp_server.exe를 찾을 수 없습니다: $ExePath"
    exit 1
}
$ExePath = (Resolve-Path $ExePath).Path
Write-Host "대상 실행 파일: $ExePath`n"

# 1) VWorld API 키 (.env) — 대화형 콘솔에서만 물어보고, 아니면 건너뛴다.
$envPath = Join-Path (Split-Path $ExePath) ".env"
if (-not (Test-Path $envPath)) {
    $key = $null
    if ([Environment]::UserInteractive -and -not ([Console]::IsInputRedirected)) {
        Write-Host "지오코딩/보호구역 조회에 VWorld API 키가 필요합니다 (https://www.vworld.kr/dev/v4api.do 에서 무료 발급)."
        try { $key = Read-Host "VWorld API 키 입력 (나중에 하려면 Enter)" } catch { $key = $null }
    }
    if ($key) {
        "VWORLD_API_KEY=$key" | Out-File -FilePath $envPath -Encoding utf8 -NoNewline
        Write-Host "  .env 생성 완료: $envPath`n"
    } else {
        Write-Host "  건너뜀 — 나중에 $envPath 파일에 VWORLD_API_KEY=... 를 직접 추가하면 됩니다.`n"
    }
} else {
    Write-Host "기존 .env 발견, 그대로 사용합니다: $envPath`n"
}

# 2) Claude Code (user scope: 모든 프로젝트에서 사용 가능)
$claude = Get-Command claude -ErrorAction SilentlyContinue
if ($claude) {
    try {
        & claude mcp remove eiass --scope user 2>$null | Out-Null
    } catch {}
    & claude mcp add eiass --scope user -- "$ExePath"
    Write-Host "✅ Claude Code에 등록 완료 (scope: user)`n"
} else {
    Write-Host "⚠ claude CLI를 찾지 못해 Claude Code 등록을 건너뜁니다.`n"
}

# 3) Codex CLI
$codex = Get-Command codex -ErrorAction SilentlyContinue
if ($codex) {
    try {
        & codex mcp remove eiass 2>$null | Out-Null
    } catch {}
    & codex mcp add eiass -- "$ExePath"
    Write-Host "✅ Codex에 등록 완료`n"
} else {
    Write-Host "⚠ codex CLI를 찾지 못해 Codex 등록을 건너뜁니다.`n"
}

# 4) Claude Desktop — claude_desktop_config.json은 앱마다 실제 구조가 달라(단순 mcpServers만
# 있는 경우도, 앱 내부 상태까지 같이 들어있는 경우도 있음) 자동으로 덮어쓰면 앱 상태를 깨뜨릴 위험이
# 있다. 그래서 자동 수정 대신 직접 추가할 스니펫만 안내한다.
$desktopConfigDir = Join-Path $env:APPDATA "Claude"
$desktopConfigPath = Join-Path $desktopConfigDir "claude_desktop_config.json"
if (Test-Path $desktopConfigDir) {
    Write-Host "ℹ Claude Desktop을 쓴다면 아래 항목을 $desktopConfigPath 의 `"mcpServers`" 안에 직접 추가하고 앱을 재시작하세요:"
    Write-Host "    `"eiass`": { `"command`": `"$($ExePath -replace '\\','\\')`" }`n"
} else {
    Write-Host "ℹ Claude Desktop이 설치되어 있지 않은 것으로 보여 건너뜁니다.`n"
}

Write-Host "완료! Claude Code / Codex를 재시작하면 eiass 도구를 쓸 수 있습니다."
