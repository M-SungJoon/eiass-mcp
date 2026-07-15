# -*- coding: utf-8 -*-
"""
빌드 스크립트: mcp_server.py -> mcp_server.exe (Python 설치 없는 PC 배포용)

PyQt5/WebEngine 의존성이 없는 독립 실행 파일이라 build.py보다 훨씬 단순하다.

주의: 이 스크립트는 반드시 mcp/requests/beautifulsoup4/PyMuPDF/typer/pyinstaller만
설치된 "깨끗한" venv의 python으로 실행해야 한다. torch/scipy/cv2/matplotlib 같은
무관한 패키지가 같이 깔린 환경에서 실행하면 --collect-all mcp가 그것들까지
정적 분석 대상으로 끌어들여 exe가 수백MB로 부풀고 실행이 느려진다(직접 겪은 문제).
  python -m venv .mcpbuild_venv
  .mcpbuild_venv\\Scripts\\python.exe -m pip install mcp requests beautifulsoup4 urllib3 PyMuPDF typer pyinstaller
  .mcpbuild_venv\\Scripts\\python.exe build_mcp.py

typer는 mcp 실행에 필요 없지만, mcp.cli.cli가 optional import로 typer를 참조해서
--collect-all mcp가 정적 분석 중 그 모듈을 import 시도하다 typer가 없으면 빌드가
실패한다. 그래서 빌드 시점에만 설치해둔다.
"""
import os
import argparse
import hashlib
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


def configure_stdio():
    os.environ.setdefault('PYTHONUTF8', '1')
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(encoding='utf-8', errors='replace')
            except Exception:
                pass


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


def build(publish_root=False):
    assert_version_in_sync()
    os.makedirs(DIST_DIR, exist_ok=True)
    os.makedirs(WORK_DIR, exist_ok=True)
    os.environ['PYTHONHASHSEED'] = '0'
    os.environ.setdefault('SOURCE_DATE_EPOCH', '0')
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
    os.makedirs(ARTIFACT_DIR, exist_ok=True)
    artifact_zip = os.path.join(ARTIFACT_DIR, os.path.basename(versioned_zip))
    shutil.copyfile(versioned_zip, artifact_zip)
    write_manifest(artifact_zip)
    if publish_root:
        stable_path = os.path.join(BASE_DIR, PAYLOAD_ZIP_NAME)
        shutil.copyfile(versioned_zip, stable_path)
        write_manifest(stable_path)
        print(f'✅ 저장소 배포본 갱신: {stable_path}')
    size_mb = os.path.getsize(versioned_zip) / (1024 * 1024)
    print(f'\n✅ 빌드 완료: {app_dir}')
    print(f'   배포 zip: {artifact_zip} ({size_mb:.1f} MB)')


if __name__ == '__main__':
    configure_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument('--publish-root', action='store_true',
                        help='검증용 버전 실행 파일을 저장소의 mcp_server.exe에도 반영')
    build(parser.parse_args().publish_root)
