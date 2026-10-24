"""Тесты для модуля установщика и деинсталлятора (src/installer/installer.py, build/build_client.py).

Покрывает чек-лист обязательного тест-плана T11.1 - T11.5:
- T11.1: Поиск и детекция установки Factorio (автопоиск, custom_path, обработка ошибок)
- T11.2: Первичная установка перехватчика (валидация ника, переименование в factorio_core, права chmod +x, настройки)
- T11.3: Защита от перезаписи при повторной установке / обновлении (is_update=True, неприкосновенность factorio_core)
- T11.4: Деинсталляция и полный откат к оригинальной игре (factorio_core -> factorio, удаление конфига, идемпотентность)
- T11.5: CLI-аргументы установщика и скрипт сборки PyInstaller (build/build_client.py)
"""

import json
import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from build.build_client import build_installer, build_wrapper, main as build_main
from src.installer.installer import FactorioWrapperInstaller, main as installer_main


@pytest.fixture
def win_game_dir(tmp_path: Path) -> Path:
    """Создает имитацию папки игры Factorio со структурой Windows (bin/x64/factorio.exe)."""
    game_root = tmp_path / "Factorio_Win"
    bin_dir = game_root / "bin" / "x64"
    bin_dir.mkdir(parents=True)
    exe_file = bin_dir / "factorio.exe"
    # Создаем фиктивный бинарник игры размером > 2048 байт
    exe_file.write_bytes(b"MZ" + b"\x00" * 4096)
    return game_root


@pytest.fixture
def macos_game_dir(tmp_path: Path) -> Path:
    """Создает имитацию папки игры Factorio со структурой macOS (.app бандл)."""
    game_root = tmp_path / "Factorio_Mac"
    app_dir = game_root / "Factorio.app" / "Contents" / "MacOS"
    app_dir.mkdir(parents=True)
    bin_file = app_dir / "factorio"
    bin_file.write_bytes(b"\xCF\xFA\xED\xFE" + b"\x00" * 4096)
    bin_file.chmod(0o755)
    return game_root


@pytest.fixture
def dummy_wrapper_bin(tmp_path: Path) -> Path:
    """Создает фиктивный скомпилированный бинарник перехватчика."""
    w_bin = tmp_path / "fake_factorio_wrapper"
    w_bin.write_bytes(b"WRAPPER_BINARY_DATA_TEST_PAYLOAD")
    w_bin.chmod(0o755)
    return w_bin


# ============================================================================
# T11.1: Поиск и детекция установки Factorio
# ============================================================================


def test_t11_1_1_detect_game_installation_windows(win_game_dir: Path):
    """T11.1.1: Автоопределение директории игры при наличии bin/x64/factorio.exe."""
    installer = FactorioWrapperInstaller()
    found = installer.find_game_installation(custom_path=win_game_dir, system_override="Windows")
    assert found == win_game_dir.resolve()

    paths = installer.get_binary_paths(win_game_dir, system_override="Windows")
    assert paths["bin_dir"] == win_game_dir / "bin" / "x64"
    assert paths["game_binary"] == win_game_dir / "bin" / "x64" / "factorio.exe"
    assert paths["core_binary"] == win_game_dir / "bin" / "x64" / "factorio_core.exe"


def test_t11_1_2_detect_game_installation_macos(macos_game_dir: Path):
    """T11.1.2: Автоопределение директории игры для macOS (.app бандл)."""
    installer = FactorioWrapperInstaller()
    found = installer.find_game_installation(custom_path=macos_game_dir, system_override="Darwin")
    assert found == macos_game_dir.resolve()

    paths = installer.get_binary_paths(macos_game_dir, system_override="Darwin")
    assert paths["bin_dir"] == macos_game_dir / "Factorio.app" / "Contents" / "MacOS"
    assert paths["game_binary"] == macos_game_dir / "Factorio.app" / "Contents" / "MacOS" / "factorio"
    assert paths["core_binary"] == macos_game_dir / "Factorio.app" / "Contents" / "MacOS" / "factorio_core"


def test_t11_1_3_find_game_installation_not_found(tmp_path: Path):
    """T11.1.3: Ошибка FileNotFoundError при отсутствии игры в указанном каталоге."""
    empty_dir = tmp_path / "empty_folder"
    empty_dir.mkdir()

    installer = FactorioWrapperInstaller()
    # При указании пустой папки, где нет бинарников
    with patch("pathlib.Path.cwd", return_value=empty_dir):
        with pytest.raises(FileNotFoundError, match="Не удалось автоматически найти установку"):
            installer.find_game_installation(system_override="Windows")


# ============================================================================
# T11.2: Первичная установка перехватчика
# ============================================================================


def test_t11_2_1_validate_player_name(win_game_dir: Path):
    """T11.2.1: Валидация никнейма: запрет пустых строк и дефолтного 'Player'."""
    installer = FactorioWrapperInstaller()

    with pytest.raises(ValueError, match="Никнейм игрока не может быть пустым"):
        installer.install(game_dir=win_game_dir, player_name="   ", system_override="Windows")

    with pytest.raises(ValueError, match="Недопустимый никнейм 'Player'"):
        installer.install(game_dir=win_game_dir, player_name="Player", system_override="Windows")

    with pytest.raises(ValueError, match="Недопустимый никнейм 'player'"):
        installer.install(game_dir=win_game_dir, player_name="player", system_override="Windows")


def test_t11_2_2_fresh_install_windows(win_game_dir: Path, dummy_wrapper_bin: Path):
    """T11.2.2: Первичная установка на Windows: factorio -> factorio_core, запись настроек и конфига."""
    installer = FactorioWrapperInstaller()
    paths = installer.get_binary_paths(win_game_dir, system_override="Windows")

    orig_content = paths["game_binary"].read_bytes()

    report = installer.install(
        game_dir=win_game_dir,
        player_name="AlexCoop",
        wrapper_binary_path=dummy_wrapper_bin,
        system_override="Windows",
    )

    assert report["status"] == "installed"
    assert report["is_update"] is False
    assert report["player_name"] == "AlexCoop"

    # Оригинал переименован в factorio_core.exe
    assert paths["core_binary"].is_file()
    assert paths["core_binary"].read_bytes() == orig_content

    # На месте factorio.exe теперь лежит перехватчик
    assert paths["game_binary"].is_file()
    assert paths["game_binary"].read_bytes() == dummy_wrapper_bin.read_bytes()

    # Проверка создания factorio_sync_config.json
    cfg_path = win_game_dir / "factorio_sync_config.json"
    assert cfg_path.is_file()
    cfg_data = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert cfg_data["player_name"] == "AlexCoop"
    assert cfg_data["hub_http_url"] == "http://127.0.0.1:8088"
    assert cfg_data["hub_stun_port"] == 14197


def test_t11_2_3_fresh_install_macos_chmod(macos_game_dir: Path, dummy_wrapper_bin: Path):
    """T11.2.3: Первичная установка на macOS: проверка прав исполнения chmod +x."""
    installer = FactorioWrapperInstaller()
    paths = installer.get_binary_paths(macos_game_dir, system_override="Darwin")

    installer.install(
        game_dir=macos_game_dir,
        player_name="MacUser",
        wrapper_binary_path=dummy_wrapper_bin,
        system_override="Darwin",
    )

    assert paths["core_binary"].is_file()
    assert paths["game_binary"].is_file()

    # Проверка, что файлы имеют права на исполнение
    game_mode = paths["game_binary"].stat().st_mode
    core_mode = paths["core_binary"].stat().st_mode
    assert bool(game_mode & stat.S_IXUSR)
    assert bool(core_mode & stat.S_IXUSR)


def test_t11_2_4_reject_too_small_binary(win_game_dir: Path, dummy_wrapper_bin: Path):
    """T11.2.4: Отказ в установке при подозрительно малом размере оригинального файла."""
    paths = FactorioWrapperInstaller().get_binary_paths(win_game_dir, system_override="Windows")
    # Перезаписываем файл пустышкой в 10 байт
    paths["game_binary"].write_bytes(b"small_stub")

    installer = FactorioWrapperInstaller()
    with pytest.raises(ValueError, match="подозрительно малый размер"):
        installer.install(
            game_dir=win_game_dir,
            player_name="ValidName",
            wrapper_binary_path=dummy_wrapper_bin,
            min_original_size_bytes=1000,
            system_override="Windows",
        )


# ============================================================================
# T11.3: Защита от перезаписи при повторной установке / обновлении
# ============================================================================


def test_t11_3_1_repeat_install_does_not_corrupt_core(win_game_dir: Path, dummy_wrapper_bin: Path, tmp_path: Path):
    """T11.3.1: Повторная установка поверх перехватчика НЕ перезаписывает factorio_core."""
    installer = FactorioWrapperInstaller()
    paths = installer.get_binary_paths(win_game_dir, system_override="Windows")

    original_engine_bytes = paths["game_binary"].read_bytes()

    # Первая установка
    installer.install(
        game_dir=win_game_dir,
        player_name="FirstInstall",
        wrapper_binary_path=dummy_wrapper_bin,
        system_override="Windows",
    )

    assert paths["core_binary"].read_bytes() == original_engine_bytes

    # Создаем новую версию перехватчика
    new_wrapper_bin = tmp_path / "wrapper_v2"
    new_wrapper_bin.write_bytes(b"NEW_WRAPPER_V2_PAYLOAD")

    # Вторая установка (обновление)
    report2 = installer.install(
        game_dir=win_game_dir,
        player_name="SecondInstall",
        wrapper_binary_path=new_wrapper_bin,
        system_override="Windows",
    )

    assert report2["is_update"] is True
    # КРИТИЧЕСКАЯ ПРОВЕРКА: ядро игры НЕ повреждено и содержит оригинальные байты игры!
    assert paths["core_binary"].read_bytes() == original_engine_bytes
    # А перехватчик успешно обновился до v2
    assert paths["game_binary"].read_bytes() == b"NEW_WRAPPER_V2_PAYLOAD"


# ============================================================================
# T11.4: Деинсталляция и полный откат к оригинальной игре
# ============================================================================


def test_t11_4_1_uninstall_restores_original_game(win_game_dir: Path, dummy_wrapper_bin: Path):
    """T11.4.1: Деинсталляция: удаление перехватчика, возврат factorio_core -> factorio."""
    installer = FactorioWrapperInstaller()
    paths = installer.get_binary_paths(win_game_dir, system_override="Windows")

    original_engine_bytes = paths["game_binary"].read_bytes()

    # Установка
    installer.install(
        game_dir=win_game_dir,
        player_name="UninstallTest",
        wrapper_binary_path=dummy_wrapper_bin,
        system_override="Windows",
    )
    assert paths["core_binary"].is_file()
    assert (win_game_dir / "factorio_sync_config.json").is_file()

    # Деинсталляция
    uninst_report = installer.uninstall(game_dir=win_game_dir, system_override="Windows")

    assert uninst_report["status"] == "uninstalled"
    # factorio_core.exe удален, так как он перемещен обратно в factorio.exe
    assert not paths["core_binary"].exists()
    assert paths["game_binary"].is_file()
    assert paths["game_binary"].read_bytes() == original_engine_bytes

    # Конфигурационный файл удален
    assert not (win_game_dir / "factorio_sync_config.json").exists()


def test_t11_4_2_uninstall_idempotent_when_already_clean(win_game_dir: Path):
    """T11.4.2: Идемпотентность: повторный вызов uninstall на чистой игре возвращает already_uninstalled."""
    installer = FactorioWrapperInstaller()
    paths = installer.get_binary_paths(win_game_dir, system_override="Windows")
    assert paths["game_binary"].is_file()
    assert not paths["core_binary"].exists()

    # Попытка деинсталляции чистой игры
    res = installer.uninstall(game_dir=win_game_dir, system_override="Windows")
    assert res["status"] == "already_uninstalled"


def test_t11_4_3_uninstall_windows_locked_file_safe_restore(win_game_dir: Path, dummy_wrapper_bin: Path):
    """T11.4.3: Безопасное восстановление при блокировке файла Windows (WinError 5 / PermissionError)."""
    installer = FactorioWrapperInstaller()
    paths = installer.get_binary_paths(win_game_dir, system_override="Windows")

    original_engine_bytes = paths["game_binary"].read_bytes()

    # Установка
    installer.install(
        game_dir=win_game_dir,
        player_name="LockedFileTest",
        wrapper_binary_path=dummy_wrapper_bin,
        system_override="Windows",
    )
    assert paths["core_binary"].is_file()

    # Симулируем PermissionError при попытке unlink() исполняемого файла, который сейчас запущен
    original_unlink = Path.unlink

    def mock_unlink(self, missing_ok=False):
        if self.name == "factorio.exe":
            raise PermissionError("[WinError 5] Отказано в доступе: 'factorio.exe'")
        return original_unlink(self, missing_ok=missing_ok)

    with patch.object(Path, "unlink", mock_unlink):
        with patch.object(installer, "_schedule_deferred_deletion") as mock_sched_del:
            uninst_report = installer.uninstall(game_dir=win_game_dir, system_override="Windows")

            assert uninst_report["status"] == "uninstalled"
            # Оригинальный бинарник возвращен на место
            assert paths["game_binary"].is_file()
            assert paths["game_binary"].read_bytes() == original_engine_bytes
            # factorio_core.exe перемещен
            assert not paths["core_binary"].exists()
            # Отложенное удаление временного переименованного файла запланировано
            assert mock_sched_del.called


# ============================================================================
# T11.5: CLI-аргументы установщика и скрипт сборки PyInstaller
# ============================================================================


def test_t11_5_1_installer_cli_install_and_uninstall(macos_game_dir: Path, dummy_wrapper_bin: Path):
    """T11.5.1: Вызов точки входа installer main() с флагами командной строки на текущей платформе."""
    # 1. Установка через CLI
    code_install = installer_main([
        "--install",
        "--game-dir", str(macos_game_dir),
        "--player-name", "CliPlayer",
        "--wrapper-bin", str(dummy_wrapper_bin),
    ])
    assert code_install == 0
    paths = FactorioWrapperInstaller().get_binary_paths(macos_game_dir)
    assert paths["core_binary"].is_file()

    # 2. Деинсталляция через CLI
    code_uninstall = installer_main([
        "--uninstall",
        "--game-dir", str(macos_game_dir),
    ])
    assert code_uninstall == 0
    assert not paths["core_binary"].exists()


def test_t11_5_2_build_client_script_functions(tmp_path: Path):
    """T11.5.2: Проверка функций скрипта сборки build/build_client.py."""
    dist_dir = tmp_path / "dist"
    work_dir = tmp_path / "build_temp"

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)

        # 1. Сборка wrapper
        wrapper_bin = build_wrapper(dist_dir=dist_dir, work_dir=work_dir, clean=False)
        assert mock_run.called
        cmd_wrapper = mock_run.call_args[0][0]
        assert "factorio_wrapper" in cmd_wrapper

        mock_run.reset_mock()

        # 2. Сборка installer
        installer_bin = build_installer(
            dist_dir=dist_dir,
            work_dir=work_dir,
            wrapper_binary=wrapper_bin,
            clean=False,
        )
        assert mock_run.called
        cmd_installer = mock_run.call_args[0][0]
        assert "Factorio Coop Setup" in cmd_installer

        mock_run.reset_mock()

        # 3. Вызов main()
        res = build_main(["--target", "all", "--dist-dir", str(dist_dir), "--work-dir", str(work_dir)])
        assert res == 0


def test_t11_5_3_build_client_with_custom_icon(tmp_path: Path):
    """T11.5.3: Передача параметра --icon в сборщик перехватчика и установщика."""
    dist_dir = tmp_path / "dist"
    work_dir = tmp_path / "build_temp"
    dummy_icon = tmp_path / "custom_icon.ico"
    dummy_icon.write_bytes(b"\x00\x00\x01\x00" + b"\x00" * 100)

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)

        # Сборка wrapper с явной иконкой
        build_wrapper(dist_dir=dist_dir, work_dir=work_dir, clean=False, icon_path=dummy_icon)
        assert mock_run.called
        cmd = mock_run.call_args[0][0]
        assert "--icon" in cmd
        icon_idx = cmd.index("--icon")
        assert cmd[icon_idx + 1] == str(dummy_icon)

        mock_run.reset_mock()

        # Сборка через CLI main()
        res = build_main([
            "--target", "wrapper",
            "--dist-dir", str(dist_dir),
            "--work-dir", str(work_dir),
            "--icon", str(dummy_icon),
        ])
        assert res == 0
        cmd_main = mock_run.call_args[0][0]
        assert "--icon" in cmd_main
        assert cmd_main[cmd_main.index("--icon") + 1] == str(dummy_icon)
