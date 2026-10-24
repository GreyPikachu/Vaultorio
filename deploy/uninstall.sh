#!/usr/bin/env bash
# Vaultorio — Factorio P2P Cloud Sync One-Line Uninstaller for macOS
set -e

echo "============================================================"
echo "      Vaultorio — Удаление перехватчика (macOS)            "
echo "============================================================"

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

if [ -n "$FOUND_GAME" ]; then
    CORE_BIN="$FOUND_GAME/Contents/MacOS/factorio_core"
    GAME_BIN="$FOUND_GAME/Contents/MacOS/factorio"
    if [ -f "$CORE_BIN" ]; then
        echo "Восстановление оригинального бинарника игры..."
        rm -f "$GAME_BIN"
        mv -f "$CORE_BIN" "$GAME_BIN"
        chmod +x "$GAME_BIN"
        if command -v codesign >/dev/null 2>&1; then
            codesign -s - --force "$GAME_BIN" >/dev/null 2>&1 || true
        fi
    fi
    rm -f "$FOUND_GAME/factorio_sync_config.json"
    rm -f "$HOME/Library/Application Support/factorio/factorio_sync_config.json"
fi

if [ -d "$HOME/.vaultorio" ]; then
    rm -rf "$HOME/.vaultorio"
    echo "Каталог $HOME/.vaultorio удален."
fi

echo ""
echo "============================================================"
echo "  ОЧИСТКА ЗАВЕРШЕНА! Игра возвращена в исходное состояние. "
echo "============================================================"
