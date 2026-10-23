#!/bin/bash
# Factorio P2P Cloud Sync — Установщик клиента (macOS)
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
cd "$REPO_ROOT"

echo "========================================================"
echo " Factorio P2P Cloud Sync — Установка клиента (macOS)"
echo "========================================================"

# Поиск доступного интерпретатора Python 3
PYTHON_CMD=""
if [ -f "$REPO_ROOT/.venv/bin/python3" ]; then
    PYTHON_CMD="$REPO_ROOT/.venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD="python3"
elif command -v python >/dev/null 2>&1 && python -c 'import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)' 2>/dev/null; then
    PYTHON_CMD="python"
fi

if [ -z "$PYTHON_CMD" ]; then
    echo ""
    echo "ОШИБКА: Python 3 не обнаружен на вашем компьютере."
    echo "Пожалуйста, установите Python 3 с официального сайта:"
    echo "https://www.python.org/downloads/ (или через Homebrew: brew install python3)"
    echo "и запустите данный скрипт снова."
    echo ""
    read -p "Нажмите Enter для выхода..."
    exit 1
fi

# Проверка и создание виртуального окружения
VENV_DIR="$REPO_ROOT/.venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "Создание виртуального окружения .venv..."
    "$PYTHON_CMD" -m venv "$VENV_DIR"
fi

VENV_PYTHON="$VENV_DIR/bin/python3"

# Проверка и установка клиентских зависимостей (httpx)
if ! "$VENV_PYTHON" -c "import httpx" >/dev/null 2>&1; then
    echo "Установка необходимых библиотек..."
    "$VENV_PYTHON" -m pip install -q -r "$REPO_ROOT/deploy/client/requirements.txt"
fi

# Запуск установщика (GUI или интерактивное меню при отсутствии дисплея)
echo "Запуск установщика..."
"$VENV_PYTHON" -m src.installer.installer

echo ""
echo "Работа установщика завершена."
read -p "Нажмите Enter для закрытия..."
