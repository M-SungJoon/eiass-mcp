@echo off
chcp 65001 >nul
title EIASS MCP 설치

echo.
echo  ==========================================
echo   EIASS MCP 설치
echo  ==========================================
echo.
echo  설치 파일을 내려받는 중입니다. 잠시만 기다려 주세요...
echo.

REM 이 파일 하나만 있으면 설치가 된다. 최신 install.ps1을 웹에서 받아 바로 실행하므로
REM 사용자가 파일 두 개를 같은 폴더에 두거나 터미널에 명령을 칠 필요가 없다.
REM 설치 로직에 버그가 있어도 실행할 때마다 최신 스크립트를 가져오므로 자동으로 고쳐진다
REM (예전에는 사용자 PC의 install.ps1이 낡은 채로 남아 수정이 전달되지 않았다).
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/M-SungJoon/eiass-mcp/main/install.ps1 | iex"

if errorlevel 1 (
    echo.
    echo  설치에 실패했습니다. 인터넷 연결을 확인한 뒤 다시 실행해 주세요.
    echo.
    pause
)
