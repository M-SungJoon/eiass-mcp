@echo off
chcp 65001 >nul
title EIASS MCP

REM ---------------------------------------------------------------------------
REM  This file must stay PURE ASCII.
REM
REM  cmd.exe re-reads a batch file by byte offset as it executes. With a
REM  multi-byte codepage active (chcp 65001), any non-ASCII text in the file
REM  desyncs that offset, and cmd ends up executing a fragment of a comment as
REM  a command. It really happened here: a Korean REM line produced
REM  "'... is not recognized as an internal or external command".
REM
REM  So all Korean user-facing text lives in install.ps1, which is UTF-8 with a
REM  BOM and is read correctly by PowerShell.
REM
REM  This file is the entire installer from the user's point of view: it pulls
REM  the current install.ps1 from the web and runs it. One file, double-click,
REM  no terminal. Fetching the script each run also means fixes to the install
REM  logic reach everyone -- previously each user kept a stale local copy that
REM  never updated itself.
REM ---------------------------------------------------------------------------

REM  TrimStart is required, not cosmetic: install.ps1 is UTF-8 *with a BOM* (PowerShell
REM  5.1 needs that to read its Korean correctly), and irm hands the BOM through as the
REM  first character of the string. Piping that straight to iex makes the opening "#"
REM  comment parse as "<BOM>#", which PowerShell reports as an unknown command.
powershell -NoProfile -ExecutionPolicy Bypass -Command "iex ((irm https://raw.githubusercontent.com/M-SungJoon/eiass-mcp/main/install.ps1).TrimStart([char]0xFEFF))"

if errorlevel 1 (
    echo.
    echo  [!] Install failed. Check your internet connection and try again.
    echo.
    pause
)
