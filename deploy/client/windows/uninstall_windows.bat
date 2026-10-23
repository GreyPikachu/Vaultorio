@echo off
chcp 65001 >nul
title Factorio P2P Cloud Sync - Удаление клиента (Windows)

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%..\..\..\"
set "REPO_ROOT=%CD%"

echo ========================================================
echo  Factorio P2P Cloud Sync — Удаление клиента (Windows)
echo ========================================================

set "PYTHON_CMD="
if exist "%REPO_ROOT%\.venv\Scripts\python.exe" (
    set "PYTHON_CMD=%REPO_ROOT%\.venv\Scripts\python.exe"
) else (
    where py >nul 2>&1
    if %ERRORLEVEL% equ 0 (
        set "PYTHON_CMD=py -3"
    ) else (
        where python >nul 2>&1
        if %ERRORLEVEL% equ 0 set "PYTHON_CMD=python"
    )
)

if "%PYTHON_CMD%"=="" (
    echo ОШИБКА: Python 3 не найден в системе.
    pause
    popd
    exit /b 1
)

"%PYTHON_CMD%" -m src.installer.installer --uninstall

echo.
echo Удаление завершено.
pause
popd
exit /b 0
