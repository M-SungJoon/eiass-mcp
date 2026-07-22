# -*- coding: utf-8 -*-
"""
빌드 스크립트: mcp_server.py -> mcp_server.exe (Python 설치 없는 PC 배포용)

PyQt5/WebEngine 의존성이 없는 독립 실행 파일이라 build.py보다 훨씬 단순하다.

주의: 이 스크립트는 반드시 requirements.lock만 설치된 "깨끗한" Python 3.12 venv의
python으로 실행해야 한다. torch/scipy/cv2/matplotlib 같은 무관한 패키지가 같이 깔린
환경에서 실행하면 --collect-all mcp가 그것들까지 정적 분석 대상으로 끌어들여 exe가
수백MB로 부풀고 실행이 느려진다(직접 겪은 문제).
  uv venv --python 3.12 .mcpbuild_venv
  uv pip install -r requirements.lock --python .mcpbuild_venv\\Scripts\\python.exe
  .mcpbuild_venv\\Scripts\\python.exe build_mcp.py

파이썬은 3.12여야 한다(CI의 setup-python과 같은 버전). exe는 빌드에 쓴 인터프리터를
그대로 품고 나가므로, 다른 버전으로 빌드하면 사용자가 실행하는 런타임이 조용히 바뀐다.
3.13은 컴파일 시점에 docstring 들여쓰기를 제거해서(3.12는 보존) FastMCP가 docstring으로
만드는 도구 설명이 통째로 달라지고, tools/parity_smoke.py가 소스와 exe 불일치로 잡아낸다.
아래 assert_build_python()이 애초에 막는다.

typer는 mcp 실행에 필요 없지만, mcp.cli.cli가 optional import로 typer를 참조해서
--collect-all mcp가 정적 분석 중 그 모듈을 import 시도하다 typer가 없으면 빌드가
실패한다. 그래서 빌드 시점에만 설치해둔다.
"""
import os
import argparse
import hashlib
import json
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE_DIR, 'mcp_server.py')
VERSION = open(os.path.join(BASE_DIR, 'VERSION'), encoding='utf-8-sig').read().strip()
VERSION_TAG = VERSION.replace('.', '_')
# 빌드 중간 산출물은 저장소 밖에 만든다. 이 저장소는 Google Drive 동기화 폴더 안에 있어서,
# onedir 배포본(약 100MB·수천 개 파일)을 저장소 안에 풀면 Drive가 그걸 업로드하는 몇 분 동안
# 파일을 잡고 있어 다음 빌드의 정리 단계가 WinError 5로 실패한다. 저장소에는 zip만 복사한다.
#
# %LOCALAPPDATA%를 쓰면 안 된다 — 이 빌드 venv는 Microsoft Store판 Python 기반이고, Store 앱은
# %LOCALAPPDATA% 쓰기를 Packages\...\LocalCache\Local\ 밑으로 조용히 리다이렉트해서 산출물이
# 엉뚱한 곳에 생긴다(실제로 겪음). %TEMP%는 이 리다이렉트 대상이 아닌 것을 실측으로 확인했다.
BUILD_ROOT = os.environ.get('EIASS_MCP_BUILD_ROOT') or os.path.join(tempfile.gettempdir(),
                                                                    'eiass-mcp-build')
DIST_DIR = os.path.join(BUILD_ROOT, 'release')
WORK_DIR = os.path.join(BUILD_ROOT, 'mcp_pyinstaller_' + VERSION_TAG)
SPEC_DIR = WORK_DIR
# 사람이 꺼내 쓰는 버전별 zip 사본은 저장소 안(gitignore 대상)에도 남긴다.
ARTIFACT_DIR = os.path.join(BASE_DIR, '#AI working', 'release')
# --onedir이라 배포 단위는 exe 하나가 아니라 폴더다. 폴더 이름과 그 안의 exe 이름에는
# 버전을 넣지 않는다 — 설치 경로(<설치폴더>/mcp_server/mcp_server.exe)가 버전마다 바뀌면
# 업데이트할 때 MCP 등록을 매번 다시 해야 하기 때문이다. 버전은 zip 파일명에만 붙인다.
OUTPUT_NAME = 'mcp_server'
PAYLOAD_ZIP_NAME = 'mcp_server_dist.zip'
# .github/workflows/windows-ci.yml의 setup-python과 반드시 같은 버전이어야 한다.
BUILD_PYTHON = (3, 12)


def configure_stdio():
    os.environ.setdefault('PYTHONUTF8', '1')
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(encoding='utf-8', errors='replace')
            except Exception:
                pass


def git_output(*args):
    return subprocess.check_output(
        ['git', *args], cwd=BASE_DIR, text=True, encoding='utf-8',
        stderr=subprocess.DEVNULL,
    ).strip()


def assert_clean_release_source():
    """배포 자산이 태그 밖의 미커밋 소스에서 만들어지는 일을 막는다."""
    try:
        branch = git_output('branch', '--show-current')
        source_commit = git_output('rev-parse', 'HEAD')
        tracked_changes = git_output('status', '--porcelain', '--untracked-files=no')
    except (OSError, subprocess.SubprocessError) as exc:
        print(f'❌ Git 배포 출처를 확인하지 못했습니다: {exc}')
        sys.exit(8)
    if branch != 'main':
        print(f'❌ 배포 빌드는 main 브랜치에서만 허용됩니다(현재: {branch or "detached"}).')
        sys.exit(8)
    if tracked_changes:
        print('❌ 추적 파일에 미커밋 변경이 있어 재현 가능한 배포를 만들 수 없습니다.')
        print(tracked_changes)
        sys.exit(8)
    return source_commit


def write_build_info(source_commit):
    path = os.path.join(WORK_DIR, 'eiass_build_info.json')
    with open(path, 'w', encoding='utf-8', newline='\n') as stream:
        json.dump({'source_commit': source_commit,
                   'tls_verification': 'system_trust_store'}, stream, ensure_ascii=False)
        stream.write('\n')
    return path


def write_manifest(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    manifest_path = path + '.sha256'
    with open(manifest_path, 'w', encoding='utf-8', newline='\n') as manifest:
        manifest.write(digest.hexdigest().upper() + '  ' + os.path.basename(path) + '\n')
    return manifest_path


def rmtree_resilient(path, attempts=6):
    """Google Drive 동기화/백신이 방금 만들어진 폴더를 잠깐 잡고 있어 rmtree가 WinError 5로
    실패하는 일이 잦다(이 저장소가 동기화 폴더 안에 있다). 짧게 기다렸다 다시 시도한다.
    읽기 전용 속성 때문에 막히는 경우도 있어 그때는 속성을 풀고 재시도한다.
    """
    def on_error(func, target, exc_info):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    for attempt in range(attempts):
        if not os.path.isdir(path):
            return
        try:
            shutil.rmtree(path, onerror=on_error)
            if not os.path.isdir(path):
                return
        except OSError:
            pass
        time.sleep(0.5 * (attempt + 1))
    if os.path.isdir(path):
        print(f'❌ 이전 빌드 폴더를 지우지 못했습니다: {path}')
        print('   Google Drive 동기화나 백신이 잠그고 있을 수 있습니다. 잠시 후 다시 시도하세요.')
        sys.exit(5)


def assert_build_python():
    """배포 exe의 런타임 파이썬이 CI가 검증하는 버전과 어긋나는 것을 막는다.

    exe는 빌드에 쓴 인터프리터를 그대로 품고 나간다. 3.13 venv로 빌드했다가 사용자 런타임이
    3.12에서 3.13으로 조용히 바뀐 적이 있다(도구 설명이 전부 달라져 CI parity가 잡았다).
    """
    if sys.version_info[:2] != BUILD_PYTHON:
        current = '.'.join(str(part) for part in sys.version_info[:3])
        expected = '.'.join(str(part) for part in BUILD_PYTHON)
        print(f'❌ 빌드 파이썬이 {current}입니다. 배포본은 {expected}로 빌드해야 합니다'
              f'(CI의 setup-python과 동일해야 함).')
        print('   uv venv --python ' + expected + ' .mcpbuild_venv')
        print('   uv pip install -r requirements.lock --python .mcpbuild_venv\\Scripts\\python.exe')
        sys.exit(6)


def assert_version_in_sync():
    """VERSION 파일과 eiass_core.__version__이 어긋난 채로 빌드되는 것을 막는다.

    버전이 두 곳에 수동 중복돼 있어서 실제로 한쪽만 올린 채 배포한 적이 있다(v1.10.0 빌드에서
    eiass_version이 1.9.0을 반환). 사람이 기억하는 대신 빌드가 잡게 한다.
    """
    source = open(os.path.join(BASE_DIR, 'eiass_core.py'), encoding='utf-8').read()
    match = re.search(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", source, re.MULTILINE)
    if not match:
        print('❌ eiass_core.py에서 __version__을 찾지 못했습니다.')
        sys.exit(2)
    if match.group(1) != VERSION:
        print(f'❌ 버전 불일치: VERSION={VERSION} 인데 eiass_core.__version__={match.group(1)} 입니다.')
        print('   두 값을 맞춘 뒤 다시 빌드하세요.')
        sys.exit(2)


def build():
    assert_build_python()
    assert_version_in_sync()
    source_commit = assert_clean_release_source()
    os.makedirs(DIST_DIR, exist_ok=True)
    os.makedirs(WORK_DIR, exist_ok=True)
    os.environ['PYTHONHASHSEED'] = '0'
    os.environ.setdefault('SOURCE_DATE_EPOCH', '0')
    build_info_path = write_build_info(source_commit)
    app_dir = os.path.join(DIST_DIR, OUTPUT_NAME)
    rmtree_resilient(app_dir)  # 이전 빌드의 잔여 파일이 새 배포본에 섞여 들어가지 않게 한다
    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--noconfirm',
        # --onefile이 아니라 --onedir인 이유: onefile 부트로더는 실행할 때마다 exe 전체를
        # %TEMP%/_MEIxxxxxx에 풀고 정상 종료 시에만 지운다. MCP 서버는 클라이언트가 끝낼 때
        # 강제 종료되는 일이 잦아 이 폴더가 계속 쌓였다(실측 280개 20.7GB). onedir는 추출
        # 단계 자체가 없어 임시파일이 0이고 기동도 빠르다.
        '--onedir',
        '--console',
        '--name', OUTPUT_NAME,
        '--distpath', DIST_DIR,
        '--workpath', WORK_DIR,
        '--specpath', SPEC_DIR,
        '--hidden-import', 'bs4',
        '--add-data', build_info_path + os.pathsep + '.',
        '--clean',
        SRC,
    ]
    print('=== 빌드 시작: mcp_server (onedir) ===')
    result = subprocess.run(cmd, cwd=BASE_DIR)
    if result.returncode != 0:
        print(f'\n❌ 빌드 실패 (exit code {result.returncode})')
        sys.exit(result.returncode)
    exe_path = os.path.join(app_dir, OUTPUT_NAME + '.exe')
    if not os.path.exists(exe_path):
        print(f'\n❌ 빌드 산출물을 찾을 수 없습니다: {exe_path}')
        sys.exit(4)

    # install.ps1은 git 없이 raw URL로 파일 하나만 받는다. 폴더 배포물은 zip 한 개로 묶어야
    # 그 방식을 그대로 쓸 수 있다. zip 안에는 mcp_server/ 폴더가 통째로 들어간다.
    versioned_zip = os.path.join(DIST_DIR, f'mcp_server_dist_{VERSION_TAG}.zip')
    if os.path.exists(versioned_zip):
        os.remove(versioned_zip)
    shutil.make_archive(versioned_zip[:-4], 'zip', root_dir=DIST_DIR, base_dir=OUTPUT_NAME)
    write_manifest(versioned_zip)
    # 릴리스 자산은 이름이 고정돼야 한다 — install.ps1이 mcp_server_dist.zip / .sha256 두 개를
    # 이름으로 찾는다. 업로드용 사본은 반드시 저장소 밖(DIST_DIR)에 둔다: 저장소 폴더 이름이
    # '#AI working'인데 gh는 자산 경로의 '#'을 "파일#라벨" 구분자로 해석해서 경로가 쪼개진다
    # (실제로 파일명이 'eiass-mcp'로 잘못 잡혀 업로드가 실패했다).
    asset_zip = os.path.join(DIST_DIR, PAYLOAD_ZIP_NAME)
    shutil.copyfile(versioned_zip, asset_zip)
    write_manifest(asset_zip)
    # 사람이 어느 빌드인지 확인할 수 있게 버전 붙은 사본도 저장소 작업 폴더에 남긴다.
    os.makedirs(ARTIFACT_DIR, exist_ok=True)
    shutil.copyfile(versioned_zip, os.path.join(ARTIFACT_DIR, os.path.basename(versioned_zip)))
    write_manifest(os.path.join(ARTIFACT_DIR, os.path.basename(versioned_zip)))

    size_mb = os.path.getsize(versioned_zip) / (1024 * 1024)
    print(f'\n✅ 빌드 완료: {app_dir}')
    print(f'   릴리스 자산: {asset_zip} ({size_mb:.1f} MB)')
    return asset_zip, source_commit


def publish_release(asset_zip, source_commit):
    """빌드 산출물을 GitHub 릴리스로 올린다.

    저장소에 41MB zip을 커밋하면 git 히스토리에 영구히 쌓인다(실측: 22회 커밋에 .git 907MB).
    릴리스 자산은 히스토리 밖에 있어서 저장소가 커지지 않는다.
    """
    tag = 'v' + VERSION
    exists = subprocess.run(['gh', 'release', 'view', tag], cwd=BASE_DIR,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    assets = [asset_zip, asset_zip + '.sha256']
    if exists:
        try:
            tag_commit = git_output('rev-list', '-n', '1', tag)
        except (OSError, subprocess.SubprocessError):
            tag_commit = ''
        if tag_commit != source_commit:
            print(f'❌ 기존 {tag} 태그({tag_commit or "unknown"})가 빌드 커밋({source_commit})과 다릅니다.')
            sys.exit(8)
        print(f'릴리스 {tag}가 이미 있어 자산만 갱신합니다.')
        cmd = ['gh', 'release', 'upload', tag] + assets + ['--clobber']
    else:
        cmd = ['gh', 'release', 'create', tag] + assets + [
            '--target', source_commit,
            '--title', f'EIASS MCP {VERSION}',
            '--notes', (f'EIASS MCP {VERSION}\n\nSource commit: `{source_commit}`\n\n'
                        '설치/업데이트: `install.bat`을 더블클릭하세요.'),
        ]
    result = subprocess.run(cmd, cwd=BASE_DIR)
    if result.returncode != 0:
        print(f'\n❌ 릴리스 발행 실패 (exit code {result.returncode})')
        sys.exit(7)
    print(f'\n✅ 릴리스 발행 완료: {tag}')


if __name__ == '__main__':
    configure_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument('--publish-release', action='store_true',
                        help='빌드 후 GitHub 릴리스(v<VERSION>)에 배포본을 올린다')
    args = parser.parse_args()
    built_asset, source_commit = build()
    if args.publish_release:
        publish_release(built_asset, source_commit)
