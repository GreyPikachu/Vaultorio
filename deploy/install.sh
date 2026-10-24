#!/usr/bin/env bash
# Vaultorio — Factorio P2P Cloud Sync One-Line Installer for macOS
set -e

echo "============================================================"
echo "      Vaultorio — Factorio P2P Cloud Sync (macOS)          "
echo "============================================================"

# 1. Поиск установки Factorio.app
CANDIDATES=(
    "/Applications/Factorio.app"
    "$HOME/Applications/Factorio.app"
    "$HOME/Library/Application Support/Steam/steamapps/common/Factorio/Factorio.app"
)

FOUND_GAME=""
for candidate in "${CANDIDATES[@]}"; do
    if [ -d "$candidate/Contents/MacOS" ]; then
        FOUND_GAME="$candidate"
        break
    fi
done

if [ -z "$FOUND_GAME" ]; then
    echo "Игра не найдена в стандартных каталогах."
    read -p "Введите путь к Factorio.app: " USER_INPUT
    if [ -d "$USER_INPUT/Contents/MacOS" ]; then
        FOUND_GAME="$USER_INPUT"
    else
        echo "ОШИБКА: Factorio.app не найден по указанному пути."
        exit 1
    fi
fi

echo "Найдена игра: $FOUND_GAME"

# 2. Проверка Python 3
PYTHON_BIN=""
if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
fi

if [ -z "$PYTHON_BIN" ]; then
    echo "ОШИБКА: Python 3 не обнаружен на вашем Mac."
    echo "Установите Python 3 (через https://www.python.org или 'brew install python3') и запустите снова."
    exit 1
fi

echo "Используется Python: $($PYTHON_BIN --version) ($PYTHON_BIN)"

# 3. Загрузка исходников с GitHub
INSTALL_DIR="$HOME/.vaultorio"
mkdir -p "$INSTALL_DIR"

echo "Загрузка компонентов Vaultorio с GitHub..."
ZIP_TMP="$(mktemp /tmp/vaultorio.XXXXXX.zip)"
EXTRACT_TMP="$(mktemp -d /tmp/vaultorio_ext.XXXXXX)"

curl -sSL "https://github.com/GreyPikachu/Vaultorio/archive/refs/heads/main.zip" -o "$ZIP_TMP"
unzip -q "$ZIP_TMP" -d "$EXTRACT_TMP"
cp -R "$EXTRACT_TMP/Vaultorio-main/"* "$INSTALL_DIR/"
rm -rf "$ZIP_TMP" "$EXTRACT_TMP"

# 4. Виртуальное окружение и зависимости
if [ ! -d "$INSTALL_DIR/.venv" ]; then
    "$PYTHON_BIN" -m venv "$INSTALL_DIR/.venv"
fi

VENV_PY="$INSTALL_DIR/.venv/bin/python3"
"$VENV_PY" -m pip install -q -r "$INSTALL_DIR/deploy/client/requirements.txt"

# 5. Внедрение перехватчика
export PYTHONPATH="$INSTALL_DIR:$PYTHONPATH"
"$VENV_PY" -m src.installer.installer --install --game-dir "$FOUND_GAME" --hub-url "http://45.91.248.78:8088"

echo ""
echo "============================================================"
echo "      УСТАНОВКА VAULTORIO УСПЕШНО ЗАВЕРШЕНА!               "
echo "============================================================"
echo "Теперь вы можете просто запустить Factorio как обычно!"
echo "При первом запуске откроется окно привязки Кода Дружбы."
echo "============================================================"
