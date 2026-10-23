#!/bin/bash
# Factorio P2P Cloud Sync — Удаление клиента (macOS)
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
cd "$REPO_ROOT"

echo "========================================================"
echo " Factorio P2P Cloud Sync — Удаление клиента (macOS)"
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
    echo "ОШИБКА: Python 3 не найден в системе."
    echo ""
    read -p "Нажмите Enter для выхода..."
    exit 1
fi

"$PYTHON_CMD" -m src.installer.installer --uninstall

echo ""
echo "Удаление завершено."
read -p "Нажмите Enter для закрытия..."
