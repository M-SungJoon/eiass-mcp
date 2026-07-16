# EIASS MCP 자동 등록 스크립트
# 사용법: install.bat을 더블클릭하거나, 이 폴더에서
#   powershell -ExecutionPolicy Bypass -File install.ps1
# Claude Code / Claude Desktop / Codex CLI 중 이 PC에 설치된 것을 찾아 자동으로 eiass MCP 서버를 등록한다.
# 성공하든 실패하든 마지막에 Enter를 눌러야 창이 닫힌다(더블클릭 실행 시 결과를 볼 수 있도록).
#
# 설치 폴더 구조 (v1.10.0부터):
#   <설치 폴더>/
#     install.ps1, install.bat
#     .env                  ← VWorld 키. 업데이트해도 유지된다.
#     .eiass_mcp_version
#     mcp_server/           ← 배포본. 업데이트 때 이 폴더만 통째로 교체된다.
#       mcp_server.exe
#       _internal/...
# v1.9.0까지는 <설치 폴더>/mcp_server.exe 단일 파일이었다(onefile). onefile은 실행할 때마다
# exe 전체를 %TEMP%/_MEIxxxxxx에 풀고 정상 종료 시에만 지워서, 강제 종료가 잦은 MCP 서버
# 특성상 임시 폴더가 무한정 쌓였다(실측 280개 20.7GB). 이 스크립트는 그 잔재도 청소한다.

param(
    [string]$InstallRoot = $PSScriptRoot,
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

function Get-FreeSpaceMB {
    param([string]$Path)
    try {
        $drive = (Get-Item -LiteralPath $Path).PSDrive.Name
        return [math]::Round((Get-PSDrive -Name $drive).Free / 1MB)
    } catch { return $null }
}

# 옛 onefile 버전이 %TEMP%에 남긴 고아 _MEI 폴더를 회수한다.
# 삭제 전에 폴더 이름부터 바꾼다 — Windows는 안에 열린 파일이 하나라도 있으면 폴더 rename을
# 거부하므로, rename 성공 자체가 "이 폴더를 쓰는 프로세스가 없다"는 증거다. 실행 중인 서버의
# 폴더는 rename이 실패해 자동으로 건너뛰어진다.
function Clear-OrphanMeiDirs {
    $tmp = [System.IO.Path]::GetTempPath()
    $before = Get-FreeSpaceMB -Path $tmp
    $removed = 0
    Get-ChildItem -LiteralPath $tmp -Directory -Filter "_MEI*" -ErrorAction SilentlyContinue | ForEach-Object {
        $staging = "$($_.FullName).stale"
        try {
            if (-not ($_.Name -like "*.stale")) {
                Rename-Item -LiteralPath $_.FullName -NewName (Split-Path $staging -Leaf) -ErrorAction Stop
            } else {
                $staging = $_.FullName
            }
            Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
            $removed++
        } catch {
            # 사용 중(현재 실행 중인 MCP 서버 등)이라 건너뛴다 — 다음 실행 때 다시 시도한다.
        }
    }
    if ($removed -gt 0) {
        $after = Get-FreeSpaceMB -Path $tmp
        if ($null -ne $before -and $null -ne $after -and $after -gt $before) {
            $freedGB = [math]::Round(($after - $before) / 1024, 1)
            Write-Host "🧹 이전 버전이 남긴 임시 폴더 $removed 개를 정리했습니다 (약 ${freedGB}GB 확보).`n"
        } else {
            Write-Host "🧹 이전 버전이 남긴 임시 폴더 $removed 개를 정리했습니다.`n"
        }
    }
}

try {
    # 배포본이 아직 없는 상태에서 이 스크립트만 받아 실행하는 경우도 지원해야 하므로,
    # 폴더가 없으면 만들고 절대경로로 정규화한 뒤 진행한다.
    if (-not $InstallRoot) { $InstallRoot = $PSScriptRoot }
    if (-not (Test-Path $InstallRoot)) { New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null }
    $ExeDir = (Resolve-Path $InstallRoot).Path
    $AppDir = Join-Path $ExeDir "mcp_server"
    $ExePath = Join-Path $AppDir "mcp_server.exe"

    # 옛 onefile 버전이 남긴 %TEMP%/_MEI 폴더 정리 — 업데이트 여부와 무관하게 매번 한다.
    Clear-OrphanMeiDirs

    # 이전 업데이트에서 물러난 구버전 폴더. 그때는 서버가 실행 중이라 못 지웠을 수 있으니
    # 매번 다시 시도한다(아직 쓰는 중이면 조용히 실패하고 다음 기회에 지운다).
    Get-ChildItem -LiteralPath $ExeDir -Directory -Filter "mcp_server.old*" -ErrorAction SilentlyContinue |
        ForEach-Object { Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }

    # 0) 버전 확인 + 자동 업데이트 — git 없이 GitHub API/raw 파일 URL만 사용한다.
    # 업데이트가 필요한지 여부는 mcp_server_dist.zip의 최신 커밋 SHA로 판단하고(정확함),
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
            $apiUrl = "https://api.github.com/repos/$RepoOwner/$RepoName/commits?path=mcp_server_dist.zip&per_page=1"
            $commits = Invoke-RestMethod -Uri $apiUrl -Headers $ghHeaders -TimeoutSec 10
            $latestSha = $commits[0].sha
        } catch {
            Write-Host "⚠ 최신 버전 확인 실패(네트워크 문제일 수 있음) — 기존 배포본으로 계속 진행합니다.`n"
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
                Write-Host "새 버전 발견 — 배포본 다운로드 중... ($latestVersion / $latestSha)"
                $downloadUrl = "https://raw.githubusercontent.com/$RepoOwner/$RepoName/$latestSha/mcp_server_dist.zip"
                $manifestUrl = "https://raw.githubusercontent.com/$RepoOwner/$RepoName/$latestSha/mcp_server_dist.zip.sha256"
                # 확장자가 반드시 .zip이어야 한다 — Expand-Archive는 그 외 확장자를 아예 거부한다
                # (".zip.new"로 뒀다가 모든 업데이트가 실패했다).
                $tmpZip = Join-Path $ExeDir "mcp_server_dist.download.zip"
                $tmpManifestPath = "$tmpZip.sha256"
                $stageDir = Join-Path $ExeDir "mcp_server.new"
                # 물러날 폴더는 매번 새 이름을 쓴다. 이전 구버전이 아직 실행 중이라 삭제되지 않고
                # 남아 있으면, 고정 이름이면 rename 대상이 이미 존재해 교체가 통째로 막힌다.
                $retireDir = Join-Path $ExeDir ("mcp_server.old-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
                try {
                    Invoke-WebRequest -Uri $downloadUrl -OutFile $tmpZip -Headers $ghHeaders -TimeoutSec 300
                    Invoke-WebRequest -Uri $manifestUrl -OutFile $tmpManifestPath -Headers $ghHeaders -TimeoutSec 30
                    $expectedHash = ((Get-Content $tmpManifestPath -Raw).Trim() -split '\s+')[0].ToUpperInvariant()
                    $actualHash = (Get-FileHash -Path $tmpZip -Algorithm SHA256).Hash.ToUpperInvariant()
                    if ($actualHash -ne $expectedHash) {
                        throw "SHA-256 불일치: expected=$expectedHash actual=$actualHash"
                    }

                    # 새 배포본을 먼저 옆에 풀어두고, 검증이 끝난 뒤에만 기존 폴더와 바꿔치기한다.
                    # 압축 해제 도중 실패해도 돌아가던 버전은 그대로 남는다.
                    if (Test-Path $stageDir) { Remove-Item $stageDir -Recurse -Force }
                    Expand-Archive -Path $tmpZip -DestinationPath $stageDir -Force
                    $extracted = Join-Path $stageDir "mcp_server"
                    if (-not (Test-Path (Join-Path $extracted "mcp_server.exe"))) {
                        throw "배포 zip 구조가 예상과 다릅니다 (mcp_server/mcp_server.exe 없음)."
                    }

                    # 교체는 반드시 [System.IO.Directory]::Move로 한다. Move-Item은 폴더를 통째로
                    # rename하지 못하면 "파일 하나씩 옮기기"로 알아서 대체 실행되는데, 서버가 실행 중이라
                    # 일부 파일이 잠겨 있으면 옮길 수 있는 것만 옮기고 멈춰 설치본을 반쯤 부순다
                    # (실제로 exe만 빠져나가고 _internal만 남아 MCP가 통째로 죽었다).
                    # Directory.Move는 진짜 rename이라 막히면 아무것도 건드리지 않고 예외를 던진다.
                    $retired = $false
                    if (Test-Path $AppDir) {
                        try {
                            [System.IO.Directory]::Move($AppDir, $retireDir)
                            $retired = $true
                        } catch {
                            throw ("기존 배포본이 사용 중이라 교체할 수 없습니다. Claude Code/Codex를 " +
                                   "완전히 종료한 뒤 다시 실행하세요. (기존 버전은 그대로 유지됩니다)")
                        }
                    }
                    try {
                        [System.IO.Directory]::Move($extracted, $AppDir)
                    } catch {
                        # 새 배포본을 못 넣었으면 치워둔 구버전을 반드시 제자리로 되돌린다.
                        if ($retired) { [System.IO.Directory]::Move($retireDir, $AppDir) }
                        throw
                    }

                    Set-Content -Path $versionFile -Value @($latestSha, $latestVersion) -Encoding utf8
                    Write-Host "✅ 배포본을 최신 버전($latestVersion)으로 업데이트했습니다.`n"
                } catch {
                    Write-Host "⚠ 업데이트 다운로드/교체 실패: $($_.Exception.Message)"
                    Write-Host "   (Claude Code/Codex가 실행 중이면 파일이 잠겨 있을 수 있습니다 — 완전히 종료한 뒤 다시 실행해보세요.)"
                    # 실행 파일이 제자리에 없으면 아직 복구가 안 된 것이다. 폴더 존재 여부로 판단하면
                    # 안 된다 — 껍데기만 남은 폴더를 "복구 완료"로 오인해 그냥 지나친다(실제로 겪음).
                    if ((-not (Test-Path $ExePath)) -and (Test-Path $retireDir)) {
                        try { [System.IO.Directory]::Move($retireDir, $AppDir) } catch {}
                    }
                    if (-not (Test-Path $ExePath)) {
                        throw "배포본을 받지 못해 설치를 진행할 수 없습니다."
                    }
                    Write-Host "   기존 배포본으로 계속 진행합니다.`n"
                } finally {
                    foreach ($leftover in @($tmpZip, $tmpManifestPath, $stageDir)) {
                        if (Test-Path $leftover) { Remove-Item $leftover -Recurse -Force -ErrorAction SilentlyContinue }
                    }
                    # 물러난 구버전은 새 배포본이 제자리에 있는 게 확인된 뒤에만 지운다.
                    # 복구까지 실패한 상황이라면 유일하게 남은 정상 배포본이므로 보존한다.
                    if ((Test-Path $ExePath) -and (Test-Path $retireDir)) {
                        Remove-Item $retireDir -Recurse -Force -ErrorAction SilentlyContinue
                    }
                }
            } else {
                Write-Host "배포본은 이미 최신 버전입니다.`n"
            }
        }
    }

    if (-not (Test-Path $ExePath)) {
        throw "mcp_server.exe를 찾을 수 없습니다: $ExePath (자동 업데이트도 실패했습니다. -SkipUpdateCheck 없이 다시 실행하거나 저장소에서 직접 받아주세요.)"
    }
    $ExePath = (Resolve-Path $ExePath).Path
    Write-Host "대상 실행 파일: $ExePath`n"

    # v1.9.0 이하에서 쓰던 단일 exe가 설치 폴더에 남아 있으면 치운다 — 남겨두면 어느 쪽이
    # 등록됐는지 헷갈리고, 그 exe를 실행하면 다시 %TEMP%에 _MEI 폴더를 만든다.
    $legacyExe = Join-Path $ExeDir "mcp_server.exe"
    if (Test-Path $legacyExe) {
        try {
            Remove-Item $legacyExe -Force -ErrorAction Stop
            Remove-Item "$legacyExe.sha256" -Force -ErrorAction SilentlyContinue
            Write-Host "🧹 이전 버전의 단일 실행 파일을 제거했습니다: $legacyExe`n"
        } catch {
            Write-Host "⚠ 이전 버전 exe를 지우지 못했습니다(실행 중일 수 있음): $legacyExe`n"
        }
    }

    # 1) VWorld API 키 (.env) — 대화형 콘솔에서만 물어보고, 아니면 건너뛴다.
    # exe 폴더가 아니라 설치 폴더에 둔다. 업데이트가 mcp_server 폴더를 통째로 갈아끼우므로
    # 그 안에 두면 키가 매번 날아간다(서버는 두 위치를 모두 찾는다).
    $envPath = Join-Path $ExeDir ".env"
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
