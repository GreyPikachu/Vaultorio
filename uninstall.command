#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
"$DIR/.venv/bin/python3" -m src.installer.installer --uninstall
echo ""
echo "Готово! Оригинальная Factorio полностью восстановлена."
read -p "Нажмите Enter для закрытия..."
