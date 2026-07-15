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
import hashlib
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE_DIR, 'mcp_server.py')
VERSION = open(os.path.join(BASE_DIR, 'VERSION'), encoding='utf-8-sig').read().strip()
VERSION_TAG = VERSION.replace('.', '_')
DIST_DIR = os.path.join(BASE_DIR, '#AI working', 'release')
WORK_DIR = os.path.join(BASE_DIR, '#AI working', 'mcp_pyinstaller')
SPEC_DIR = WORK_DIR
OUTPUT_NAME = f'mcp_server_{VERSION_TAG}'


def configure_stdio():
    os.environ.setdefault('PYTHONUTF8', '1')
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(encoding='utf-8', errors='replace')
            except Exception:
                pass


def build():
    os.makedirs(DIST_DIR, exist_ok=True)
    os.makedirs(WORK_DIR, exist_ok=True)
    os.environ['PYTHONHASHSEED'] = '0'
    os.environ.setdefault('SOURCE_DATE_EPOCH', '0')
    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--noconfirm',
        '--onefile',
        '--console',
        '--name', OUTPUT_NAME,
        '--distpath', DIST_DIR,
        '--workpath', WORK_DIR,
        '--specpath', SPEC_DIR,
        '--collect-all', 'mcp',
        '--hidden-import', 'bs4',
        '--clean',
        SRC,
    ]
    print('=== 빌드 시작: mcp_server.exe ===')
    result = subprocess.run(cmd, cwd=BASE_DIR)
    if result.returncode != 0:
        print(f'\n❌ 빌드 실패 (exit code {result.returncode})')
        sys.exit(result.returncode)
    out_path = os.path.join(DIST_DIR, OUTPUT_NAME + '.exe')
    digest = hashlib.sha256()
    with open(out_path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    with open(out_path + '.sha256', 'w', encoding='utf-8', newline='\n') as manifest:
        manifest.write(digest.hexdigest().upper() + '  ' + os.path.basename(out_path) + '\n')
    print(f'\n✅ 빌드 완료: {out_path}')


if __name__ == '__main__':
    configure_stdio()
    build()
