#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
"$DIR/.venv/bin/python3" -m src.installer.installer --install --player-name "CliPlayer"
echo ""
echo "Готово! Модификация перехватчика установлена."
read -p "Нажмите Enter для закрытия..."
