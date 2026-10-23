@echo off
chcp 65001 >nul
title Factorio P2P Cloud Sync - Установка клиента (Windows)

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%..\..\..\"
set "REPO_ROOT=%CD%"

echo ========================================================
echo  Factorio P2P Cloud Sync — Установка клиента (Windows)
echo ========================================================

:: 1. Поиск интерпретатора Python 3
set "PYTHON_CMD="
if exist "%REPO_ROOT%\.venv\Scripts\python.exe" (
    set "PYTHON_CMD=%REPO_ROOT%\.venv\Scripts\python.exe"
    goto :python_found
)

where py >nul 2>&1
if %ERRORLEVEL% equ 0 (
    set "PYTHON_CMD=py -3"
    goto :python_found
)

where python >nul 2>&1
if %ERRORLEVEL% equ 0 (
    set "PYTHON_CMD=python"
    goto :python_found
)

echo.
echo ОШИБКА: Python 3 не найден на вашем компьютере.
echo Пожалуйста, установите Python 3 с официального сайта:
echo https://www.python.org/downloads/
echo (Обязательно включите пункт "Add Python to PATH" при установке!)
echo.
pause
popd
exit /b 1

:python_found
:: 2. Проверка и создание виртуального окружения
if not exist "%REPO_ROOT%\.venv\Scripts\python.exe" (
    echo Создание виртуального окружения .venv...
    %PYTHON_CMD% -m venv "%REPO_ROOT%\.venv"
)

set "VENV_PYTHON=%REPO_ROOT%\.venv\Scripts\python.exe"

:: 3. Проверка зависимостей
"%VENV_PYTHON%" -c "import httpx" >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo Установка необходимых библиотек...
    "%VENV_PYTHON%" -m pip install -q -r "%REPO_ROOT%\deploy\client\requirements.txt"
)

:: 4. Запуск установщика
echo Запуск установщика...
"%VENV_PYTHON%" -m src.installer.installer

echo.
echo Работа установщика завершена.
pause
popd
exit /b 0
