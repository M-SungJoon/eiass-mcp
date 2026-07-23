# EIASS MCP 자동 설치 스크립트
# 사용법: 'EIASS MCP 설치.bat'을 더블클릭한다(사용자는 이것만 하면 된다).
#   터미널에서 직접 돌리려면: irm https://raw.githubusercontent.com/M-SungJoon/eiass-mcp/main/install.ps1 | iex
# Claude Code / Claude Desktop / Codex CLI 중 이 PC에 설치된 것을 찾아 자동으로 eiass MCP 서버를 등록한다.
# 성공하든 실패하든 마지막에 Enter를 눌러야 창이 닫힌다(더블클릭 실행 시 결과를 볼 수 있도록).
#
# 설치 폴더는 %LOCALAPPDATA%\Programs\EIASS MCP로 고정한다(v1.12.0부터):
#   <설치 폴더>/
#     .env                  ← VWorld 키. 업데이트해도 유지된다.
#     .eiass_mcp_version
#     mcp_server/           ← 배포본. 업데이트 때 이 폴더만 통째로 교체된다.
#       mcp_server.exe
#       _internal/...
# 경로를 고정하는 이유:
#   (1) 'irm | iex'로 실행하면 스크립트가 디스크에 없어서 $PSScriptRoot가 비어버린다.
#       예전처럼 스크립트 위치를 설치 위치로 삼으면 이 방식 자체가 동작하지 않는다.
#   (2) 등록 경로가 영구히 고정돼 업데이트해도 재등록이 필요 없다. 특히 Claude Desktop은
#       설정 JSON에 경로를 직접 넣어야 해서, 경로가 바뀌면 사용자가 매번 손을 봐야 한다.
#   (3) 사용자가 설치 폴더를 고르지 않아도 된다.
#
# v1.9.0까지는 <설치 폴더>/mcp_server.exe 단일 파일이었다(onefile). onefile은 실행할 때마다
# exe 전체를 %TEMP%/_MEIxxxxxx에 풀고 정상 종료 시에만 지워서, 강제 종료가 잦은 MCP 서버
# 특성상 임시 폴더가 무한정 쌓였다(실측 280개 20.7GB). 이 스크립트는 그 잔재도 청소한다.

param(
    [string]$InstallRoot,
    [switch]$SkipUpdateCheck,
    [ValidateSet("Menu", "Latest", "Specific", "Reinstall")]
    [string]$InstallMode = "Menu",
    [string]$TargetVersion,
    [switch]$AllowDowngrade,
    [switch]$DryRun
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

# 이미 등록된 eiass MCP의 실행 경로에서 예전 설치 폴더를 역추적한다.
# v1.9.0 이하: <설치폴더>\mcp_server.exe      → 설치폴더 = 부모
# v1.10~1.11 : <설치폴더>\mcp_server\mcp_server.exe → 설치폴더 = 조부모
# JSON 설정 파일은 반드시 이걸로 읽는다. Get-Content -Raw는 UTF-8 파일을 시스템 ANSI
# 코드페이지로 디코딩해서 한글을 깨뜨리고, 그 깨진 문자열은 ConvertFrom-Json에서 파싱 실패한다
# (실측: claude_desktop_config.json 6326바이트가 5954자로 읽혀 파싱 실패, 올바르게 읽으면 5846자).
function Read-Utf8Text {
    param([string]$Path)
    return [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
}

function Write-Utf8NoBomText {
    param([string]$Path, [string]$Text)
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

function Get-EiassCommandFromConfig {
    param([string]$ConfigPath)
    if (-not (Test-Path $ConfigPath)) { return $null }
    $raw = Read-Utf8Text -Path $ConfigPath
    # ConvertFrom-Json을 쓰면 안 된다. Windows PowerShell 5.1의 파서는 실제 설정 파일의
    # 크기/구조를 감당하지 못하고 "':' 또는 '}'가 필요합니다"로 실패한다(실측). 그러면 예전
    # 설치를 못 찾아 VWorld 키 이주가 조용히 건너뛰어진다. 그래서 텍스트에서 직접 뽑는다.
    try {
        $config = $raw | ConvertFrom-Json
        if ($config.mcpServers.eiass.command) { return $config.mcpServers.eiass.command }
    } catch {
        # 아래 텍스트 추출로 넘어간다.
    }
    $idx = $raw.IndexOf('"eiass"')
    if ($idx -lt 0) { return $null }
    $match = [regex]::Match($raw.Substring($idx), '"command"\s*:\s*"([^"]*)"')
    if (-not $match.Success) { return $null }
    return ($match.Groups[1].Value -replace '\\\\', '\')
}

# Claude Desktop 설정 파일 후보 경로를 모두 반환한다.
#   - 일반 설치: %APPDATA%\Claude\claude_desktop_config.json
#   - Microsoft Store(MSIX) 설치: Store 앱은 %APPDATA%(Roaming) 쓰기를 패키지 내부로
#     리다이렉트해서, 실제 파일은 %LOCALAPPDATA%\Packages\<패키지>\LocalCache\Roaming\Claude\에 있다.
#     일반 프로세스가 $env:APPDATA를 읽으면 진짜 Roaming 경로가 나오지 실제 패키지 경로가
#     아니므로, Store 위치는 따로 뒤져야 한다(실제 사용자가 이 경우에 걸려 등록이 건너뛰어졌다).
function Get-DesktopConfigPaths {
    $paths = @(Join-Path $env:APPDATA "Claude\claude_desktop_config.json")
    $pkgRoot = Join-Path $env:LOCALAPPDATA "Packages"
    if (Test-Path $pkgRoot) {
        Get-ChildItem $pkgRoot -Filter "*Claude*" -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            $paths += (Join-Path $_.FullName "LocalCache\Roaming\Claude\claude_desktop_config.json")
        }
    }
    return ($paths | Select-Object -Unique)
}

# 등록해 넣을 설정 파일 경로를 고른다. 이미 있는 파일이 최우선이고, 없으면 Claude 설정 폴더가
# 실제로 존재하는 후보를 쓴다(설치돼 있지도 않은데 엉뚱한 곳에 파일을 새로 만들지 않기 위함).
# Claude Desktop 흔적이 전혀 없으면 $null을 반환한다.
function Get-DesktopConfigForWrite {
    $paths = Get-DesktopConfigPaths
    $existing = $paths | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($existing) { return $existing }
    return ($paths | Where-Object { Test-Path (Split-Path $_ -Parent) } | Select-Object -First 1)
}

function Get-RegisteredEiassCommand {
    # 예전 설치는 Claude Code(.claude.json)와 Claude Desktop(claude_desktop_config.json) 중
    # 어디에 등록돼 있을지 모른다. 특히 터미널을 안 쓰는 사용자는 Desktop만 쓸 가능성이 높은데,
    # .claude.json만 보면 그들의 예전 설치를 못 찾아 VWorld 키 이주가 통째로 건너뛰어진다.
    # 우리가 설치했던 흔적(mcp_server\mcp_server.exe 또는 단일 mcp_server.exe)을 가리키는 값을
    # 우선한다 — npx 같은 외부 명령이 아니라 실제 이주 대상 경로여야 하기 때문이다.
    $configs = @(Join-Path $env:USERPROFILE ".claude.json") + @(Get-DesktopConfigPaths)
    $fallback = $null
    foreach ($configPath in $configs) {
        $command = Get-EiassCommandFromConfig -ConfigPath $configPath
        if (-not $command) { continue }
        $leaf = Split-Path $command -Leaf
        if ($leaf -ieq "mcp_server.exe") { return $command }  # 우리가 설치한 배포본
        if (-not $fallback) { $fallback = $command }
    }
    return $fallback
}

# Claude Desktop 설정에 eiass를 직접 등록한다.
# 이 파일에는 mcpServers 말고 앱이 관리하는 상태(preferences 등)도 들어 있어서, 잘못 쓰면 앱
# 설정을 통째로 날린다. 그래서 (1) 백업하고 (2) eiass 항목만 손대고 (3) 쓴 결과를 다시 읽어
# 최상위 키와 기존 서버가 모두 살아있는지 확인한 뒤 (4) 하나라도 어긋나면 백업으로 되돌린다.
# 파싱조차 안 되면 아예 건드리지 않고 수동 안내로 넘어간다.
function Register-ClaudeDesktop {
    param([string]$ConfigPath, [string]$ExePath)

    if (-not (Test-Path $ConfigPath)) {
        # 설정 파일이 없으면 새로 만든다(앱 상태가 없으므로 위험하지 않다).
        Write-Utf8NoBomText -Path $ConfigPath -Text (@{ mcpServers = @{ eiass = @{ command = $ExePath } } } |
            ConvertTo-Json -Depth 100)
        return 'created'
    }

    try {
        $config = Read-Utf8Text -Path $ConfigPath | ConvertFrom-Json
    } catch {
        return 'unparsable'
    }

    $keysBefore = @($config.PSObject.Properties.Name)
    $serversBefore = if ($config.mcpServers) { @($config.mcpServers.PSObject.Properties.Name) } else { @() }

    $backup = "$ConfigPath.bak"
    Copy-Item $ConfigPath $backup -Force

    try {
        if (-not $config.mcpServers) {
            $config | Add-Member -NotePropertyName 'mcpServers' -NotePropertyValue (New-Object PSObject) -Force
        }
        $config.mcpServers | Add-Member -NotePropertyName 'eiass' `
            -NotePropertyValue ([PSCustomObject]@{ command = $ExePath }) -Force

        # -Depth 100이 없으면 안 된다. Windows PowerShell 5.1의 ConvertTo-Json은 기본 깊이가 2라
        # 그 아래 구조가 "System.Object[]" 문자열로 뭉개진다(이 설정 파일의 실제 깊이는 6).
        $json = $config | ConvertTo-Json -Depth 100
        if ($json -match 'System\.(Object|Management)') { throw "직렬화 중 구조가 손상되었습니다." }
        Write-Utf8NoBomText -Path $ConfigPath -Text $json

        $verify = Read-Utf8Text -Path $ConfigPath | ConvertFrom-Json
        $keysAfter = @($verify.PSObject.Properties.Name)
        $serversAfter = @($verify.mcpServers.PSObject.Properties.Name)
        foreach ($key in $keysBefore) {
            if ($keysAfter -notcontains $key) { throw "최상위 항목이 사라졌습니다: $key" }
        }
        foreach ($server in $serversBefore) {
            if ($serversAfter -notcontains $server) { throw "MCP 서버 등록이 사라졌습니다: $server" }
        }
        if ($verify.mcpServers.eiass.command -ne $ExePath) { throw "eiass 경로가 기록되지 않았습니다." }
    } catch {
        Copy-Item $backup $ConfigPath -Force   # 원복
        return "failed:$($_.Exception.Message)"
    }
    return 'updated'
}

# Codex CLI와 캡처된 ChatGPT Desktop의 Codex 환경은 같은 ~/.codex/config.toml을 쓴다.
# Store/MSIX 설치에서는 codex.exe가 앱 패키지 안에 있으므로 PATH 등록이 빠진 PC도 직접 찾는다.
function Get-CodexCommandPath {
    $command = Get-Command codex -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($command) {
        foreach ($property in @('Path', 'Source', 'Definition')) {
            $candidate = [string]$command.$property
            if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
                return $candidate
            }
        }
    }

    $candidates = @()
    foreach ($packagePattern in @('OpenAI.Codex', 'OpenAI.ChatGPT', '*Codex*', '*ChatGPT*')) {
        try {
            foreach ($package in @(Get-AppxPackage -Name $packagePattern -ErrorAction Stop)) {
                if (-not $package.InstallLocation) { continue }
                $candidates += (Join-Path $package.InstallLocation 'app\resources\codex.exe')
                $candidates += (Join-Path $package.InstallLocation 'resources\codex.exe')
                $candidates += (Join-Path $package.InstallLocation 'codex.exe')
            }
        } catch {
            # AppX 조회가 제한된 환경이면 아래 일반 설치 경로 탐색으로 계속한다.
        }
    }

    foreach ($root in @($env:LOCALAPPDATA, $env:ProgramFiles)) {
        if (-not $root) { continue }
        foreach ($relativePath in @(
            'Programs\ChatGPT\app\resources\codex.exe',
            'Programs\ChatGPT\resources\codex.exe',
            'Programs\Codex\app\resources\codex.exe',
            'Programs\Codex\resources\codex.exe',
            'ChatGPT\app\resources\codex.exe',
            'ChatGPT\resources\codex.exe',
            'Codex\app\resources\codex.exe',
            'Codex\resources\codex.exe'
        )) {
            $candidates += (Join-Path $root $relativePath)
        }
    }

    foreach ($candidate in @($candidates | Select-Object -Unique)) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    return $null
}

function Register-CodexClients {
    param([string]$CodexCommand, [string]$ExePath)
    try {
        & $CodexCommand mcp remove eiass 2>$null | Out-Null
        & $CodexCommand mcp add eiass -- $ExePath 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "codex mcp add가 종료 코드 $LASTEXITCODE 을 반환했습니다." }

        $verifyLines = @(& $CodexCommand mcp get eiass 2>&1)
        $verifyExitCode = $LASTEXITCODE
        $verifyText = $verifyLines -join "`n"
        if ($verifyExitCode -ne 0) { throw "codex mcp get이 종료 코드 $verifyExitCode 을 반환했습니다." }
        if ($verifyText -notmatch '(?m)^\s*enabled:\s*true\s*$') {
            throw '등록 결과가 enabled 상태가 아닙니다.'
        }
        if ($verifyText.IndexOf($ExePath, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
            throw '등록 결과의 실행 파일 경로가 설치 경로와 다릅니다.'
        }
        return 'registered'
    } catch {
        return "failed:$($_.Exception.Message)"
    }
}

function Get-LegacyInstallRoot {
    param([string]$CurrentRoot)
    $command = Get-RegisteredEiassCommand
    if (-not $command) { return $null }
    $parent = Split-Path $command -Parent
    if (-not $parent) { return $null }
    if ((Split-Path $parent -Leaf) -eq "mcp_server") { $parent = Split-Path $parent -Parent }
    if (-not $parent) { return $null }
    # 이미 새 위치에 설치돼 있으면 이주할 것이 없다.
    if ($parent.TrimEnd('\') -ieq $CurrentRoot.TrimEnd('\')) { return $null }
    if (-not (Test-Path $parent)) { return $null }
    return $parent
}

function Test-InteractiveConsole {
    return ([Environment]::UserInteractive -and -not ([Console]::IsInputRedirected))
}

function Normalize-ReleaseTag {
    param([string]$VersionOrTag)
    if (-not $VersionOrTag) { return $null }
    $value = $VersionOrTag.Trim()
    if ($value -notmatch '^v') { $value = "v$value" }
    return $value
}

function ConvertTo-ReleaseDescriptor {
    param($Release)
    if (-not $Release -or -not $Release.tag_name -or $Release.draft -or $Release.prerelease) {
        return $null
    }
    $downloadUrl = $null
    $manifestUrl = $null
    foreach ($asset in @($Release.assets)) {
        if ($asset.name -eq "mcp_server_dist.zip") { $downloadUrl = $asset.browser_download_url }
        if ($asset.name -eq "mcp_server_dist.zip.sha256") { $manifestUrl = $asset.browser_download_url }
    }
    if (-not $downloadUrl -or -not $manifestUrl) { return $null }
    return [PSCustomObject]@{
        Tag = [string]$Release.tag_name
        Version = ([string]$Release.tag_name -replace '^v', '')
        DownloadUrl = [string]$downloadUrl
        ManifestUrl = [string]$manifestUrl
        PublishedAt = [string]$Release.published_at
    }
}

function Get-ReleaseByTag {
    param(
        [string]$RepoOwner,
        [string]$RepoName,
        [string]$Tag,
        [hashtable]$Headers
    )
    $escapedTag = [Uri]::EscapeDataString($Tag)
    $url = "https://api.github.com/repos/$RepoOwner/$RepoName/releases/tags/$escapedTag"
    $release = Invoke-RestMethod -Uri $url -Headers $Headers -TimeoutSec 15
    return (ConvertTo-ReleaseDescriptor -Release $release)
}

function Get-CompatibleReleases {
    param(
        [string]$RepoOwner,
        [string]$RepoName,
        [hashtable]$Headers
    )
    $url = "https://api.github.com/repos/$RepoOwner/$RepoName/releases?per_page=100"
    # Windows PowerShell 5.1에서 Invoke-RestMethod의 JSON 배열 응답을 명령 호출째
    # @()로 감싸면 Object[] 하나가 다시 배열 안에 들어갈 수 있다. 그러면 foreach가
    # 전체 릴리스 배열을 단일 릴리스처럼 검사해 모든 항목을 탈락시킨다.
    # 먼저 응답을 변수에 받은 뒤 배열로 정규화해야 0/1/N건을 모두 같은 형태로 처리한다.
    $rawReleases = Invoke-RestMethod -Uri $url -Headers $Headers -TimeoutSec 15 -ErrorAction Stop
    $releases = @($rawReleases)
    $result = @()
    foreach ($release in $releases) {
        $descriptor = ConvertTo-ReleaseDescriptor -Release $release
        if ($descriptor) { $result += $descriptor }
    }
    return $result
}

function ConvertTo-SemanticVersion {
    param([string]$Value)
    if (-not $Value) { return $null }
    try { return [Version]($Value.Trim() -replace '^v', '') } catch { return $null }
}

function Backup-DowngradeJobDatabase {
    param([string]$TargetVersion)
    $dataDir = Join-Path $env:LOCALAPPDATA "DOHWA EIASS Agent"
    $names = @("mcp_jobs.sqlite3", "mcp_jobs.sqlite3-wal", "mcp_jobs.sqlite3-shm")
    $existing = @()
    foreach ($name in $names) {
        $source = Join-Path $dataDir $name
        if (Test-Path -LiteralPath $source) { $existing += $source }
    }
    if ($existing.Count -eq 0) { return $null }

    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $safeVersion = $TargetVersion -replace '[^0-9A-Za-z._-]', '_'
    $backupDir = Join-Path $dataDir "job-backup-before-downgrade-$safeVersion-$stamp"
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
    $moved = @()
    try {
        foreach ($source in $existing) {
            $destination = Join-Path $backupDir (Split-Path $source -Leaf)
            [System.IO.File]::Move($source, $destination)
            $moved += $destination
        }
    } catch {
        foreach ($destination in $moved) {
            $source = Join-Path $dataDir (Split-Path $destination -Leaf)
            if ((Test-Path -LiteralPath $destination) -and -not (Test-Path -LiteralPath $source)) {
                try { [System.IO.File]::Move($destination, $source) } catch {}
            }
        }
        Remove-Item -LiteralPath $backupDir -Recurse -Force -ErrorAction SilentlyContinue
        throw "진행 기록 DB를 백업할 수 없습니다. Claude Code/Codex를 완전히 종료한 뒤 다시 실행하세요."
    }
    return $backupDir
}

function Restore-DowngradeJobDatabase {
    param([string]$BackupDir)
    if (-not $BackupDir -or -not (Test-Path -LiteralPath $BackupDir)) { return }
    $dataDir = Split-Path $BackupDir -Parent
    foreach ($item in @(Get-ChildItem -LiteralPath $BackupDir -File -ErrorAction SilentlyContinue)) {
        $destination = Join-Path $dataDir $item.Name
        if (-not (Test-Path -LiteralPath $destination)) {
            try { [System.IO.File]::Move($item.FullName, $destination) } catch {}
        }
    }
    if ((Get-ChildItem -LiteralPath $BackupDir -Force -ErrorAction SilentlyContinue).Count -eq 0) {
        Remove-Item -LiteralPath $BackupDir -Force -ErrorAction SilentlyContinue
    }
}

try {
    Write-Host ""
    Write-Host "=========================================="
    Write-Host "  EIASS MCP 설치"
    Write-Host "=========================================="
    Write-Host ""

    # 설치 폴더는 고정한다. -InstallRoot로 덮어쓸 수는 있게 두되(테스트/특수 상황용),
    # 평소에는 사용자가 위치를 고민할 일이 없어야 한다.
    if (-not $InstallRoot) { $InstallRoot = Join-Path $env:LOCALAPPDATA "Programs\EIASS MCP" }
    if ($DryRun) {
        $ExeDir = [System.IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($InstallRoot))
    } else {
        if (-not (Test-Path $InstallRoot)) { New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null }
        $ExeDir = (Resolve-Path $InstallRoot).Path
    }
    $AppDir = Join-Path $ExeDir "mcp_server"
    $ExePath = Join-Path $AppDir "mcp_server.exe"
    Write-Host "설치 위치: $ExeDir`n"

    # 예전 위치에 설치돼 있으면 VWorld 키를 먼저 가져온다. 사용자가 키를 다시 발급/입력하는
    # 일이 없어야 한다. 예전 설치본 정리는 새 배포본이 자리를 잡은 뒤에 한다.
    $legacyRoot = $null
    if (-not $DryRun) {
        $legacyRoot = Get-LegacyInstallRoot -CurrentRoot $ExeDir
        if ($legacyRoot) {
            Write-Host "기존 설치를 발견했습니다: $legacyRoot"
            $legacyEnv = Join-Path $legacyRoot ".env"
            $newEnv = Join-Path $ExeDir ".env"
            if ((Test-Path $legacyEnv) -and -not (Test-Path $newEnv)) {
                Copy-Item $legacyEnv $newEnv -Force
                Write-Host "  VWorld API 키(.env)를 새 위치로 옮겼습니다."
            }
            $legacyVersionFile = Join-Path $legacyRoot ".eiass_mcp_version"
            $newVersionFile = Join-Path $ExeDir ".eiass_mcp_version"
            if ((Test-Path $legacyVersionFile) -and -not (Test-Path $newVersionFile)) {
                Copy-Item $legacyVersionFile $newVersionFile -Force
            }
            Write-Host ""
        }

        # 옛 onefile 버전이 남긴 %TEMP%/_MEI 폴더 정리 — 업데이트 여부와 무관하게 매번 한다.
        Clear-OrphanMeiDirs

        # 이전 업데이트에서 물러난 구버전 폴더. 그때는 서버가 실행 중이라 못 지웠을 수 있으니
        # 매번 다시 시도한다(아직 쓰는 중이면 조용히 실패하고 다음 기회에 지운다).
        Get-ChildItem -LiteralPath $ExeDir -Directory -Filter "mcp_server.old*" -ErrorAction SilentlyContinue |
            ForEach-Object { Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
    }

    # 0) 버전 확인 + 자동 업데이트 — git 없이 GitHub Releases API만 사용한다.
    # 배포본(mcp_server_dist.zip)은 저장소에 커밋하지 않고 릴리스 자산으로 올린다. 41MB 바이너리를
    # 커밋하면 git 히스토리에 영구히 쌓여(실측 22회 커밋에 .git 907MB) clone이 갈수록 무거워진다.
    # 설치된 버전은 .eiass_mcp_version에 "태그<개행>버전"으로 남긴다. v1.11.0 이하가 남긴 파일에는
    # 1행에 커밋 SHA가 들어 있는데, 어떤 태그와도 일치하지 않으므로 자연히 업데이트가 걸린다.
    $RepoOwner = "M-SungJoon"
    $RepoName = "eiass-mcp"
    $versionFile = Join-Path $ExeDir ".eiass_mcp_version"
    $ghHeaders = @{ "User-Agent" = "eiass-mcp-install-script" }

    $localTag = $null
    $localVersion = "알 수 없음(최초 설치)"
    if (Test-Path $versionFile) {
        $versionLines = @(Get-Content $versionFile)
        if ($versionLines.Count -ge 1 -and $versionLines[0]) { $localTag = $versionLines[0].Trim() }
        if ($versionLines.Count -ge 2 -and $versionLines[1]) { $localVersion = $versionLines[1].Trim() }
    }
    # v1.11.0 이하의 1행 마커에는 태그 대신 커밋 SHA가 들어 있다. 그런 값은 GitHub 릴리스
    # 태그로 재설치할 수 없으므로 3번 선택지를 비활성화하고 최신 설치만 허용한다.
    $canReinstallCurrent = ($localTag -and $localTag -match '^v\d+\.\d+\.\d+$')
    Write-Host "현재 설치된 버전: $localVersion"

    if (-not $SkipUpdateCheck) {
        # -TargetVersion은 자동화/지원 상황에서 메뉴 없이 특정 버전을 선택하는 공개 진입점이다.
        if ($TargetVersion) { $InstallMode = "Specific" }

        if ($InstallMode -eq "Menu") {
            if (Test-InteractiveConsole) {
                while ($InstallMode -eq "Menu") {
                    Write-Host ""
                    Write-Host "설치 방법을 선택하세요:"
                    Write-Host "  [1] 최신 버전 설치/업데이트"
                    Write-Host "  [2] 특정 버전 선택"
                    if ($canReinstallCurrent) {
                        Write-Host "  [3] 현재 버전 재설치 ($localVersion)"
                    } else {
                        Write-Host "  [3] 현재 버전 재설치 (현재 설치 없음)"
                    }
                    $menuChoice = Read-Host "선택 (1~3)"
                    switch ($menuChoice.Trim()) {
                        "1" { $InstallMode = "Latest" }
                        "2" { $InstallMode = "Specific" }
                        "3" {
                            if ($canReinstallCurrent) { $InstallMode = "Reinstall" }
                            else { Write-Host "현재 설치된 버전이 없어 재설치할 수 없습니다." -ForegroundColor Yellow }
                        }
                        default { Write-Host "1, 2, 3 중 하나를 입력해 주세요." -ForegroundColor Yellow }
                    }
                }
            } else {
                # 파이프/자동화 실행은 메뉴 입력을 기다리면 영원히 멈추므로 기존 동작과 같은 최신 설치로 둔다.
                $InstallMode = "Latest"
                Write-Host "비대화식 실행: 최신 버전 설치/업데이트를 선택했습니다."
            }
        }

        $selectedRelease = $null
        if ($InstallMode -eq "Specific" -and -not $TargetVersion) {
            if (-not (Test-InteractiveConsole)) {
                throw "비대화식 특정 버전 설치에는 -TargetVersion을 지정해야 합니다."
            }
            Write-Host "`n설치 가능한 정식 릴리스를 확인하는 중..."
            $compatibleReleases = @(Get-CompatibleReleases -RepoOwner $RepoOwner -RepoName $RepoName -Headers $ghHeaders)
            if ($compatibleReleases.Count -eq 0) {
                throw "선택 설치가 가능한 정식 릴리스를 찾지 못했습니다."
            }
            Write-Host ""
            for ($index = 0; $index -lt $compatibleReleases.Count; $index++) {
                $marker = ""
                if ($compatibleReleases[$index].Tag -eq $localTag) { $marker = " (현재 설치)" }
                Write-Host ("  [{0}] {1}{2}" -f ($index + 1), $compatibleReleases[$index].Version, $marker)
            }
            while (-not $selectedRelease) {
                $versionChoice = (Read-Host "번호 또는 버전 입력 (예: 2 또는 1.16.1)").Trim()
                $choiceNumber = 0
                if ([int]::TryParse($versionChoice, [ref]$choiceNumber) -and
                    $choiceNumber -ge 1 -and $choiceNumber -le $compatibleReleases.Count) {
                    $selectedRelease = $compatibleReleases[$choiceNumber - 1]
                } elseif ($versionChoice) {
                    $TargetVersion = $versionChoice
                    break
                } else {
                    Write-Host "번호 또는 버전을 입력해 주세요." -ForegroundColor Yellow
                }
            }
        }

        $targetRelease = $null
        try {
            if ($selectedRelease) {
                $targetRelease = $selectedRelease
            } elseif ($InstallMode -eq "Latest") {
                $apiUrl = "https://api.github.com/repos/$RepoOwner/$RepoName/releases/latest"
                $release = Invoke-RestMethod -Uri $apiUrl -Headers $ghHeaders -TimeoutSec 15
                $targetRelease = ConvertTo-ReleaseDescriptor -Release $release
            } elseif ($InstallMode -eq "Specific") {
                $targetTagInput = Normalize-ReleaseTag -VersionOrTag $TargetVersion
                if (-not $targetTagInput) { throw "설치할 버전을 지정하지 않았습니다." }
                $targetRelease = Get-ReleaseByTag -RepoOwner $RepoOwner -RepoName $RepoName `
                    -Tag $targetTagInput -Headers $ghHeaders
            } elseif ($InstallMode -eq "Reinstall") {
                if (-not $canReinstallCurrent) { throw "현재 설치된 버전 태그가 없어 재설치할 수 없습니다." }
                $targetRelease = Get-ReleaseByTag -RepoOwner $RepoOwner -RepoName $RepoName `
                    -Tag $localTag -Headers $ghHeaders
            }
            if (-not $targetRelease) {
                throw "선택한 릴리스에 필수 배포 자산(mcp_server_dist.zip 및 SHA-256)이 없습니다."
            }
        } catch {
            if ($InstallMode -eq "Latest" -and (Test-Path $ExePath) -and -not $DryRun) {
                Write-Host "⚠ 최신 버전 확인 실패: $($_.Exception.Message)"
                Write-Host "   기존 배포본으로 계속 진행합니다.`n"
            } else {
                throw "릴리스 확인 실패: $($_.Exception.Message)"
            }
        }

        if ($targetRelease) {
            $targetTag = $targetRelease.Tag
            $targetReleaseVersion = $targetRelease.Version
            $downloadUrl = $targetRelease.DownloadUrl
            $manifestUrl = $targetRelease.ManifestUrl
            Write-Host "선택한 배포 버전: $targetReleaseVersion ($targetTag)"

            $currentSemantic = ConvertTo-SemanticVersion -Value $localVersion
            $targetSemantic = ConvertTo-SemanticVersion -Value $targetReleaseVersion
            $isDowngrade = ($null -ne $currentSemantic -and $null -ne $targetSemantic -and
                $targetSemantic.CompareTo($currentSemantic) -lt 0)
            $installAction = "설치"
            if ($localTag) {
                if ($isDowngrade) { $installAction = "다운그레이드" }
                elseif ($targetTag -eq $localTag) {
                    if ($InstallMode -eq "Latest" -and (Test-Path $ExePath)) { $installAction = "최신 유지" }
                    else { $installAction = "재설치" }
                }
                else { $installAction = "업그레이드" }
            }

            if ($isDowngrade -and -not $AllowDowngrade) {
                Write-Host ""
                Write-Host "⚠ 다운그레이드: $localVersion → $targetReleaseVersion" -ForegroundColor Yellow
                Write-Host "  새 버전의 진행 중 스캔 기록은 이전 버전과 호환되지 않을 수 있습니다."
                Write-Host "  계속하면 작업 DB를 별도 폴더에 백업한 뒤 초기화합니다."
                if (Test-InteractiveConsole) {
                    $downgradeAnswer = (Read-Host "다운그레이드를 계속하려면 Y를 입력하세요").Trim()
                    if ($downgradeAnswer -notmatch '(?i)^y(es)?$') {
                        throw "사용자가 다운그레이드를 취소했습니다."
                    }
                } else {
                    throw "비대화식 다운그레이드에는 -AllowDowngrade가 필요합니다."
                }
            }

            Write-Host "선택 결과: mode=$InstallMode action=$installAction target=$targetReleaseVersion tag=$targetTag"
            if ($DryRun) {
                Write-Host "DRY RUN — 릴리스 선택과 안전 조건을 확인했으며 파일은 변경하지 않았습니다."
                return
            }

            $shouldInstall = (($targetTag -ne $localTag) -or (-not (Test-Path $ExePath)) -or
                $InstallMode -eq "Specific" -or $InstallMode -eq "Reinstall")
            if ($shouldInstall) {
                Write-Host "$installAction 진행 — 배포본 다운로드 중... ($targetReleaseVersion)"
                # 확장자가 반드시 .zip이어야 한다 — Expand-Archive는 그 외 확장자를 아예 거부한다
                # (".zip.new"로 뒀다가 모든 업데이트가 실패했다).
                $tmpZip = Join-Path $ExeDir "mcp_server_dist.download.zip"
                $tmpManifestPath = "$tmpZip.sha256"
                $stageDir = Join-Path $ExeDir "mcp_server.new"
                # 물러날 폴더는 매번 새 이름을 쓴다. 이전 구버전이 아직 실행 중이라 삭제되지 않고
                # 남아 있으면, 고정 이름이면 rename 대상이 이미 존재해 교체가 통째로 막힌다.
                $retireDir = Join-Path $ExeDir ("mcp_server.old-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
                $jobDbBackupDir = $null
                $newAppInstalled = $false
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

                    # 새 버전이 기록한 작업 payload를 구버전이 읽지 않도록 다운그레이드 직전에
                    # 기본 작업 DB와 WAL/SHM을 함께 옮긴다. 교체가 실패하면 아래 catch에서 복구한다.
                    if ($isDowngrade) {
                        $jobDbBackupDir = Backup-DowngradeJobDatabase -TargetVersion $targetReleaseVersion
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
                        $newAppInstalled = $true
                    } catch {
                        # 새 배포본을 못 넣었으면 치워둔 구버전을 반드시 제자리로 되돌린다.
                        if ($retired) { [System.IO.Directory]::Move($retireDir, $AppDir) }
                        throw
                    }

                    Set-Content -Path $versionFile -Value @($targetTag, $targetReleaseVersion) -Encoding utf8
                    Write-Host "✅ $installAction 완료: $targetReleaseVersion"
                    if ($jobDbBackupDir) {
                        Write-Host "  이전 작업 기록 백업: $jobDbBackupDir"
                    }
                    Write-Host ""
                } catch {
                    if ($jobDbBackupDir -and -not $newAppInstalled) {
                        Restore-DowngradeJobDatabase -BackupDir $jobDbBackupDir
                    }
                    Write-Host "⚠ $installAction 다운로드/교체 실패: $($_.Exception.Message)"
                    Write-Host "   (Claude Code/Codex가 실행 중이면 파일이 잠겨 있을 수 있습니다 — 완전히 종료한 뒤 다시 실행해보세요.)"
                    # 실행 파일이 제자리에 없으면 아직 복구가 안 된 것이다. 폴더 존재 여부로 판단하면
                    # 안 된다 — 껍데기만 남은 폴더를 "복구 완료"로 오인해 그냥 지나친다(실제로 겪음).
                    if ((-not (Test-Path $ExePath)) -and (Test-Path $retireDir)) {
                        try { [System.IO.Directory]::Move($retireDir, $AppDir) } catch {}
                    }
                    if (-not (Test-Path $ExePath)) {
                        throw "배포본을 받지 못해 설치를 진행할 수 없습니다."
                    }
                    if ($InstallMode -ne "Latest") {
                        throw "선택한 버전 설치를 완료하지 못했습니다. 기존 배포본은 유지되었습니다."
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
        throw "배포본을 받지 못해 설치를 완료하지 못했습니다. 인터넷 연결을 확인한 뒤 다시 실행해 주세요."
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

    # 예전 위치의 배포본을 정리한다. 우리가 설치한 것(mcp_server 폴더 / 단일 exe)만 지우고
    # 나머지(.env, 사용자가 넣어둔 파일 등)는 건드리지 않는다 — 사용자 폴더를 통째로 지우면
    # 우리가 만들지 않은 파일까지 날아간다.
    if ($legacyRoot) {
        $removed = @()
        $kept = $false
        foreach ($item in @((Join-Path $legacyRoot "mcp_server"), (Join-Path $legacyRoot "mcp_server.exe"))) {
            if (Test-Path $item) {
                try {
                    Remove-Item $item -Recurse -Force -ErrorAction Stop
                    $removed += (Split-Path $item -Leaf)
                } catch { $kept = $true }
            }
        }
        if ($removed.Count -gt 0) {
            Write-Host "🧹 예전 위치의 배포본을 정리했습니다: $legacyRoot ($($removed -join ', '))"
        }
        if ($kept) {
            Write-Host "⚠ 예전 배포본 일부가 사용 중이라 남았습니다: $legacyRoot"
            Write-Host "   Claude를 완전히 종료한 뒤 이 설치를 한 번 더 실행하면 정리됩니다."
        }
        Write-Host "   (예전 폴더의 .env 등 나머지 파일은 그대로 두었습니다.)`n"
    }

    # 1) VWorld API 키 (.env) — exe 폴더가 아니라 설치 폴더에 둔다. 업데이트가 mcp_server
    # 폴더를 통째로 갈아끼우므로 그 안에 두면 키가 매번 날아간다(서버는 두 위치를 모두 찾는다).
    # 키를 얻는 순서: (1) 환경변수 EIASS_VWORLD_API_KEY → (2) 대화형 입력.
    # 배포자가 나눠주는 install.bat에 자기 키를 넣어두면, 그 키가 환경변수로 넘어와 사용자는
    # 아무것도 입력하지 않아도 된다. install.ps1은 공개 raw URL로 받아가므로 여기에 키를 절대
    # 넣지 않는다 — 키는 배포자가 직접 나눠주는 install.bat에만 담긴다.
    $envPath = Join-Path $ExeDir ".env"
    if (-not (Test-Path $envPath)) {
        $key = $null
        $bundledKey = $env:EIASS_VWORLD_API_KEY
        if ($bundledKey) { $bundledKey = $bundledKey.Trim() }
        if ($bundledKey) {
            $key = $bundledKey
            Write-Host "설치 파일에 포함된 VWorld API 키를 사용합니다."
        } elseif ([Environment]::UserInteractive -and -not ([Console]::IsInputRedirected)) {
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

    # 3) Codex CLI / ChatGPT Desktop의 Codex 환경. PATH에 CLI가 없어도 앱 패키지 내부의
    # codex.exe를 찾아 같은 사용자 설정(~/.codex/config.toml)에 등록하고 즉시 검증한다.
    $codexCommand = Get-CodexCommandPath
    if ($codexCommand) {
        $codexResult = Register-CodexClients -CodexCommand $codexCommand -ExePath $ExePath
        if ($codexResult -eq 'registered') {
            Write-Host "✅ Codex CLI / ChatGPT Desktop에 등록 및 검증 완료`n"
        } else {
            Write-Host "⚠ Codex CLI / ChatGPT Desktop 자동 등록 실패" -ForegroundColor Yellow
            Write-Host "   $($codexResult -replace '^failed:', '')`n"
        }
    } else {
        Write-Host "⚠ Codex CLI 또는 ChatGPT Desktop의 codex.exe를 찾지 못해 등록을 건너뜁니다.`n"
    }

    # 4) Claude Desktop — 설정 JSON에 직접 등록한다. 터미널도 어려워하는 사용자에게 JSON을
    # 손으로 고치게 할 수는 없다. 실패하면 기존 파일을 되돌리고 수동 안내로 넘어간다.
    # 설정 경로는 일반 설치와 Microsoft Store 설치(가상화된 패키지 경로)를 모두 찾는다.
    $desktopConfigPath = Get-DesktopConfigForWrite
    if ($desktopConfigPath) {
        $desktopResult = Register-ClaudeDesktop -ConfigPath $desktopConfigPath -ExePath $ExePath
        switch -Wildcard ($desktopResult) {
            'updated' { Write-Host "✅ Claude Desktop에 등록 완료`n" }
            'created' { Write-Host "✅ Claude Desktop 설정을 새로 만들어 등록했습니다`n" }
            default {
                if ($desktopResult -eq 'unparsable') {
                    Write-Host "⚠ Claude Desktop 설정 파일을 읽지 못해 자동 등록을 건너뜁니다(파일은 그대로 두었습니다)."
                } else {
                    Write-Host "⚠ Claude Desktop 자동 등록 실패 — 원래 설정으로 되돌렸습니다."
                    Write-Host "   ($($desktopResult -replace '^failed:', ''))"
                }
                Write-Host "   아래 항목을 $desktopConfigPath 의 `"mcpServers`" 안에 직접 추가하세요:"
                Write-Host "     `"eiass`": { `"command`": `"$($ExePath -replace '\\','\\')`" }`n"
            }
        }
        if (Get-Process 'Claude' -ErrorAction SilentlyContinue) {
            # 실행 중인 앱이 종료할 때 자기 상태를 이 파일에 다시 쓰면서 방금 넣은 등록을
            # 덮어쓸 수 있다. 그때는 앱을 끄고 한 번 더 실행하면 된다.
            Write-Host "⚠ Claude Desktop이 실행 중입니다. 완전히 종료했다가 다시 켜세요."
            Write-Host "   (종료 시 앱이 설정을 덮어써서 등록이 사라지면, 이 설치를 한 번 더 실행하면 됩니다.)`n"
        }
    } else {
        Write-Host "ℹ Claude Desktop이 설치되어 있지 않은 것으로 보여 건너뜁니다.`n"
    }

    # 5) 다음 업데이트용 실행 파일을 설치 폴더에 남긴다. 사용자가 처음 받은 .bat을 어디에 뒀는지
    # 잊어버려도, 설치 폴더의 이 파일을 더블클릭하면 최신 버전으로 갱신된다. 내용은 웹에서 최신
    # 스크립트를 받아 실행하는 것이라, 설치 로직이 고쳐지면 그것도 자동으로 반영된다.
    # 배치 파일 본문은 반드시 ASCII로만 쓴다. chcp 65001이 켜진 상태에서 비ASCII 문자가 있으면
    # cmd가 파일을 바이트 오프셋으로 되읽는 과정에서 위치가 어긋나 주석 조각을 명령으로 실행한다
    # (실제로 한글 주석 때문에 "'...'은(는) 내부 또는 외부 명령이 아닙니다" 오류가 났다).
    # 한글 안내는 전부 install.ps1이 출력한다. 파일 이름은 cmd가 파싱하지 않으므로 한글이어도 된다.
    $updaterPath = Join-Path $ExeDir "EIASS MCP 업데이트.bat"
    $updaterBody = @(
        '@echo off',
        'chcp 65001 >nul',
        'title EIASS MCP',
        # TrimStart로 BOM을 떼야 한다. 이 스크립트는 UTF-8 BOM 파일이고(PS 5.1이 한글을 제대로
        # 읽으려면 필요), irm은 BOM을 문자열 첫 글자로 그대로 넘겨준다. 그대로 iex에 넘기면
        # 첫 줄 주석이 "<BOM>#"으로 파싱돼 알 수 없는 명령 오류가 난다.
        ('powershell -NoProfile -ExecutionPolicy Bypass -Command "iex ((irm https://raw.githubusercontent.com/' +
         $RepoOwner + '/' + $RepoName + '/main/install.ps1).TrimStart([char]0xFEFF))"')
    )
    try {
        Set-Content -Path $updaterPath -Value $updaterBody -Encoding oem
    } catch {
        # 부가 기능이라 실패해도 설치 자체는 성공으로 둔다.
    }

    Write-Host "완료! Claude Code / Codex CLI / ChatGPT Desktop / Claude Desktop을 재시작하면 eiass 도구를 쓸 수 있습니다."
    Write-Host ""
    Write-Host "다음에 업데이트할 때는 아래 파일을 더블클릭하세요:"
    Write-Host "  $updaterPath"
} catch {
    Write-Host ""
    Write-Host "❌ 오류가 발생해 설치를 완료하지 못했습니다: $($_.Exception.Message)" -ForegroundColor Red
} finally {
    Wait-BeforeExit
}
