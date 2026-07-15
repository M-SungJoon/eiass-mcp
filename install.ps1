# EIASS MCP 자동 등록 스크립트
# 사용법: install.bat을 더블클릭하거나, 이 폴더(mcp_server.exe가 있는 곳)에서
#   powershell -ExecutionPolicy Bypass -File install.ps1
# Claude Code / Claude Desktop / Codex CLI 중 이 PC에 설치된 것을 찾아 자동으로 eiass MCP 서버를 등록한다.
# 성공하든 실패하든 마지막에 Enter를 눌러야 창이 닫힌다(더블클릭 실행 시 결과를 볼 수 있도록).

param(
    [string]$ExePath = (Join-Path $PSScriptRoot "mcp_server.exe"),
    [switch]$SkipUpdateCheck
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"  # Invoke-WebRequest 진행률 렌더링이 큰 파일에서 매우 느려서 끈다
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$OutputEncoding = [System.Text.Encoding]::UTF8

function Wait-BeforeExit {
    if ([Environment]::UserInteractive -and -not ([Console]::IsInputRedirected)) {
        Write-Host ""
        Read-Host "계속하려면 Enter를 누르세요"
    }
}

try {
    # ExePath가 상대경로거나 아직 파일이 없어도 처리할 수 있도록 디렉터리 기준으로 절대경로화한다
    # (최초 설치 시 exe가 아직 없는 상태에서 이 스크립트만 받아 실행하는 경우도 지원).
    $ExeDir = Split-Path $ExePath -Parent
    if (-not $ExeDir) { $ExeDir = $PSScriptRoot }
    if (-not (Test-Path $ExeDir)) { New-Item -ItemType Directory -Path $ExeDir -Force | Out-Null }
    $ExePath = Join-Path (Resolve-Path $ExeDir).Path (Split-Path $ExePath -Leaf)

    # 0) 버전 확인 + 자동 업데이트 — git 없이 GitHub API/raw 파일 URL만 사용한다.
    # 업데이트가 필요한지 여부는 mcp_server.exe의 최신 커밋 SHA로 판단하고(정확함),
    # 사람이 보는 버전 번호는 저장소 루트의 VERSION 파일(예: "1.1.0")에서 가져온다.
    # 두 값을 로컬 .eiass_mcp_version 파일에 "SHA<개행>버전" 두 줄로 저장해뒀다가
    # 다음 실행 때 "현재 설치된 버전"으로 보여준다. 설치된 버전/Git 최신 버전은
    # SkipUpdateCheck 여부와 무관하게 항상 화면에 표시한다.
    $RepoOwner = "M-SungJoon"
    $RepoName = "eiass-mcp"
    $versionFile = Join-Path $ExeDir ".eiass_mcp_version"
    $ghHeaders = @{ "User-Agent" = "eiass-mcp-install-script" }

    $localSha = $null
    $localVersion = "알 수 없음(최초 설치 또는 이전 버전의 설치 스크립트로 설치됨)"
    if (Test-Path $versionFile) {
        $versionLines = @(Get-Content $versionFile)
        if ($versionLines.Count -ge 1 -and $versionLines[0]) { $localSha = $versionLines[0].Trim() }
        if ($versionLines.Count -ge 2 -and $versionLines[1]) { $localVersion = $versionLines[1].Trim() }
    }
    $localShaShort = if ($localSha) { $localSha.Substring(0, [Math]::Min(7, $localSha.Length)) } else { "알 수 없음" }
    Write-Host "현재 설치된 버전: $localVersion (commit $localShaShort)"

    if (-not $SkipUpdateCheck) {
        $latestSha = $null
        try {
            $apiUrl = "https://api.github.com/repos/$RepoOwner/$RepoName/commits?path=mcp_server.exe&per_page=1"
            $commits = Invoke-RestMethod -Uri $apiUrl -Headers $ghHeaders -TimeoutSec 10
            $latestSha = $commits[0].sha
        } catch {
            Write-Host "⚠ 최신 버전 확인 실패(네트워크 문제일 수 있음) — 기존 mcp_server.exe로 계속 진행합니다.`n"
        }

        $latestVersion = "알 수 없음"
        if ($latestSha) {
            try {
                $versionUrl = "https://raw.githubusercontent.com/$RepoOwner/$RepoName/$latestSha/VERSION"
                $latestVersion = (Invoke-RestMethod -Uri $versionUrl -Headers $ghHeaders -TimeoutSec 10).Trim()
            } catch {
                # VERSION 파일이 없던 옛 커밋일 수도 있음 — 버전 번호 표시만 못 할 뿐 업데이트 자체는 진행한다.
            }
            $latestShaShort = $latestSha.Substring(0, [Math]::Min(7, $latestSha.Length))
            Write-Host "Git에 푸시된 최신 버전: $latestVersion (commit $latestShaShort)"
        }

        if ($latestSha) {
            if (($latestSha -ne $localSha) -or (-not (Test-Path $ExePath))) {
                Write-Host "새 버전 발견 — mcp_server.exe 다운로드 중... ($latestVersion / $latestSha)"
                $downloadUrl = "https://raw.githubusercontent.com/$RepoOwner/$RepoName/$latestSha/mcp_server.exe"
                $manifestUrl = "https://raw.githubusercontent.com/$RepoOwner/$RepoName/$latestSha/mcp_server.exe.sha256"
                $tmpPath = "$ExePath.new"
                $tmpManifestPath = "$ExePath.new.sha256"
                try {
                    Invoke-WebRequest -Uri $downloadUrl -OutFile $tmpPath -Headers $ghHeaders -TimeoutSec 180
                    Invoke-WebRequest -Uri $manifestUrl -OutFile $tmpManifestPath -Headers $ghHeaders -TimeoutSec 30
                    $expectedHash = ((Get-Content $tmpManifestPath -Raw).Trim() -split '\s+')[0].ToUpperInvariant()
                    $actualHash = (Get-FileHash -Path $tmpPath -Algorithm SHA256).Hash.ToUpperInvariant()
                    if ($actualHash -ne $expectedHash) {
                        throw "SHA-256 불일치: expected=$expectedHash actual=$actualHash"
                    }
                    Move-Item -Path $tmpPath -Destination $ExePath -Force
                    Set-Content -Path $versionFile -Value @($latestSha, $latestVersion) -Encoding utf8
                    Remove-Item $tmpManifestPath -Force -ErrorAction SilentlyContinue
                    Write-Host "✅ mcp_server.exe를 최신 버전($latestVersion)으로 업데이트했습니다.`n"
                } catch {
                    Write-Host "⚠ 업데이트 다운로드/교체 실패: $($_.Exception.Message)"
                    Write-Host "   (Claude Code/Codex가 실행 중이면 mcp_server.exe가 잠겨 있을 수 있습니다 — 완전히 종료한 뒤 다시 실행해보세요.)"
                    if (Test-Path $tmpPath) { Remove-Item $tmpPath -Force -ErrorAction SilentlyContinue }
                    if (Test-Path $tmpManifestPath) { Remove-Item $tmpManifestPath -Force -ErrorAction SilentlyContinue }
                    if (-not (Test-Path $ExePath)) {
                        throw "mcp_server.exe를 받지 못해 설치를 진행할 수 없습니다."
                    }
                    Write-Host "   기존 mcp_server.exe로 계속 진행합니다.`n"
                }
            } else {
                Write-Host "mcp_server.exe는 이미 최신 버전입니다.`n"
            }
        }
    }

    if (-not (Test-Path $ExePath)) {
        throw "mcp_server.exe를 찾을 수 없습니다: $ExePath (자동 업데이트도 실패했습니다. -SkipUpdateCheck 없이 다시 실행하거나 저장소에서 직접 받아주세요.)"
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
} catch {
    Write-Host ""
    Write-Host "❌ 오류가 발생해 설치를 완료하지 못했습니다: $($_.Exception.Message)" -ForegroundColor Red
} finally {
    Wait-BeforeExit
}
