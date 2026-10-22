"""Комплексный набор тестов для клиентского окружения Factorio.

Покрывает чек-лист тест-плана T6.1 - T6.7:
- T6.1: Валидация ClientConfig и барьер портов Hysteria.
- T6.2: Автоопределение путей Factorio (Windows, macOS, Linux, Portable).
- T6.3: Split-Brain Shield (физическое скрытие и восстановление cloud_world.zip).
- T6.4: Управление никнеймом игрока в player-data.json и config.ini.
- T6.5: Тюнинг настроек мультиплеера и автосохранений (config.ini и player-data.json).
- T6.6: Извлечение манифеста активных модов из mod-list.json.
- T6.7: Буфер обмена и всплывающие баннеры ролей.
"""

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.client.config import ClientConfig
from src.client.factorio_env import FactorioEnvironment
from src.client.ui_notifier import copy_to_clipboard, show_role_banner


class TestClientConfig(unittest.TestCase):
    """T6.1: Тестирование валидации и параметров ClientConfig."""

    def test_default_config(self) -> None:
        """Проверка значений конфигурации по умолчанию."""
        cfg = ClientConfig()
        self.assertEqual(cfg.hub_http_url, "http://<YOUR_VPS_IP>:8088")
        self.assertEqual(cfg.hub_stun_host, "<YOUR_VPS_IP>")
        self.assertEqual(cfg.hub_stun_port, 14197)
        self.assertEqual(cfg.cloud_save_name, "cloud_world.zip")
        self.assertEqual(cfg.host_factorio_port, 34197)
        self.assertEqual(cfg.guest_proxy_port, 34198)

    def test_invalid_http_url(self) -> None:
        """URL должен начинаться с http:// или https://."""
        with self.assertRaises(ValueError):
            ClientConfig(hub_http_url="ftp://<YOUR_VPS_IP>:8088")

    def test_hysteria_port_barrier_rejection(self) -> None:
        """Защитный барьер: STUN порт не должен попадать в 20000:50000."""
        # Проверяем граничные и внутренние значения запрещенного диапазона Hysteria
        for forbidden_port in [20000, 25000, 34197, 44444, 50000]:
            with self.assertRaises(ValueError) as ctx:
                ClientConfig(hub_stun_port=forbidden_port)
            self.assertIn("КРИТИЧЕСКИЙ КОНФЛИКТ ПОРТОВ", str(ctx.exception))

        # Разрешенные порты за пределами диапазона
        cfg_low = ClientConfig(hub_stun_port=14197)
        self.assertEqual(cfg_low.hub_stun_port, 14197)
        cfg_high = ClientConfig(hub_stun_port=50001)
        self.assertEqual(cfg_high.hub_stun_port, 50001)

    def test_port_range_validation(self) -> None:
        """Порты должны быть в диапазоне 1..65535."""
        with self.assertRaises(ValueError):
            ClientConfig(hub_stun_port=0)
        with self.assertRaises(ValueError):
            ClientConfig(host_factorio_port=70000)
        with self.assertRaises(ValueError):
            ClientConfig(guest_proxy_port=-1)

    def test_host_and_guest_port_collision(self) -> None:
        """Порты хоста Factorio и прокси гостя не могут быть одинаковыми."""
        with self.assertRaises(ValueError):
            ClientConfig(host_factorio_port=34197, guest_proxy_port=34197)

    def test_invalid_save_extension(self) -> None:
        """Имя файла сохранения должно оканчиваться на .zip."""
        with self.assertRaises(ValueError):
            ClientConfig(cloud_save_name="cloud_world.tar")

    def test_from_env_loading(self) -> None:
        """Проверка загрузки параметров из переменных окружения."""
        env_vars = {
            "FACTORIO_HUB_HTTP_URL": "https://hub.example.com",
            "FACTORIO_HUB_STUN_HOST": "stun.example.com",
            "FACTORIO_HUB_STUN_PORT": "14200",
            "FACTORIO_CLOUD_SAVE_NAME": "custom_map.zip",
            "FACTORIO_HOST_PORT": "34200",
            "FACTORIO_GUEST_PROXY_PORT": "34201",
            "FACTORIO_PLAYER_NAME": "Alice",
        }
        with patch.dict(os.environ, env_vars, clear=False):
            cfg = ClientConfig.from_env()
            self.assertEqual(cfg.hub_http_url, "https://hub.example.com")
            self.assertEqual(cfg.hub_stun_host, "stun.example.com")
            self.assertEqual(cfg.hub_stun_port, 14200)
            self.assertEqual(cfg.cloud_save_name, "custom_map.zip")
            self.assertEqual(cfg.host_factorio_port, 34200)
            self.assertEqual(cfg.guest_proxy_port, 34201)
            self.assertEqual(cfg.player_name, "Alice")


class TestFactorioEnvironmentPaths(unittest.TestCase):
    """T6.2: Тестирование автоопределения путей и каталогов."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.root_path = Path(self.temp_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_portable_detection(self) -> None:
        """Portable режим: config-path.cfg содержит use-system-read-write-data-directories=false."""
        cfg_path = self.root_path / "config-path.cfg"
        cfg_path.write_text("use-system-read-write-data-directories=false\n", encoding="utf-8")

        # Создаем псевдо-бинарник в bin/x64
        bin_dir = self.root_path / "bin" / "x64"
        bin_dir.mkdir(parents=True)

        paths = FactorioEnvironment.detect_paths(executable_dir=bin_dir)
        self.assertEqual(paths["game_root"], self.root_path.resolve())
        self.assertEqual(paths["user_data_dir"], self.root_path.resolve())

    def test_windows_system_paths(self) -> None:
        """Определение пути Windows (%APPDATA%\\Factorio)."""
        fake_appdata = self.root_path / "AppData" / "Roaming"
        fake_appdata.mkdir(parents=True)

        with patch.dict(os.environ, {"APPDATA": str(fake_appdata)}):
            paths = FactorioEnvironment.detect_paths(system_override="Windows")
            expected = fake_appdata / "Factorio"
            self.assertEqual(paths["user_data_dir"], expected.resolve())

    def test_macos_system_paths(self) -> None:
        """Определение пути macOS (~/Library/Application Support/factorio)."""
        paths = FactorioEnvironment.detect_paths(system_override="Darwin")
        expected = Path.home() / "Library" / "Application Support" / "factorio"
        self.assertEqual(paths["user_data_dir"], expected.resolve())

    def test_linux_system_paths(self) -> None:
        """Определение пути Linux (~/.factorio)."""
        paths = FactorioEnvironment.detect_paths(system_override="Linux")
        expected = Path.home() / ".factorio"
        self.assertEqual(paths["user_data_dir"], expected.resolve())

    def test_ensure_dirs_creates_structure(self) -> None:
        """ensure_dirs создает saves, .cloud_stash, mods, config."""
        env = FactorioEnvironment(custom_user_data_dir=self.root_path)
        env.ensure_dirs()
        self.assertTrue(env.saves_dir.is_dir())
        self.assertTrue(env.cloud_stash_dir.is_dir())
        self.assertTrue(env.mods_dir.is_dir())
        self.assertTrue(env.config_dir.is_dir())


class TestSplitBrainShield(unittest.TestCase):
    """T6.3: Тестирование физического скрытия и восстановления cloud_world.zip."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.env = FactorioEnvironment(custom_user_data_dir=self.temp_dir)
        self.env.ensure_dirs()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_hide_cloud_save_when_present(self) -> None:
        """Файл cloud_world.zip перемещается из saves/ в saves/.cloud_stash/."""
        save_file = self.env.saves_dir / "cloud_world.zip"
        save_file.write_bytes(b"PK001-SAVE-DATA")

        hidden_path = self.env.hide_cloud_save_for_guest()
        self.assertIsNotNone(hidden_path)
        self.assertTrue(hidden_path.is_file())
        self.assertEqual(hidden_path, self.env.cloud_stash_dir / "cloud_world.zip")

        # В корне saves/ файла быть не должно
        self.assertFalse(save_file.exists())
        self.assertEqual(hidden_path.read_bytes(), b"PK001-SAVE-DATA")

    def test_hide_cloud_save_idempotent(self) -> None:
        """Повторный вызов hide, когда файл уже скрыт, не падает и возвращает путь."""
        stash_file = self.env.cloud_stash_dir / "cloud_world.zip"
        stash_file.write_bytes(b"ALREADY-HIDDEN")

        result = self.env.hide_cloud_save_for_guest()
        self.assertEqual(result, stash_file)
        self.assertTrue(stash_file.is_file())

    def test_hide_cloud_save_when_no_save_exists(self) -> None:
        """Если сейва нет ни в saves/, ни в stash, возвращается None."""
        result = self.env.hide_cloud_save_for_guest()
        self.assertIsNone(result)

    def test_restore_hidden_cloud_save_basic(self) -> None:
        """Восстановление файла из .cloud_stash/ обратно в корень saves/."""
        stash_file = self.env.cloud_stash_dir / "cloud_world.zip"
        stash_file.write_bytes(b"RESTORE-DATA")

        restored_path = self.env.restore_hidden_cloud_save()
        self.assertIsNotNone(restored_path)
        self.assertEqual(restored_path, self.env.saves_dir / "cloud_world.zip")
        self.assertTrue(restored_path.is_file())
        self.assertEqual(restored_path.read_bytes(), b"RESTORE-DATA")
        self.assertFalse(stash_file.exists())

    def test_restore_resolves_conflict_by_mtime(self) -> None:
        """При наличии обоих файлов побеждает тот, у которого новее mtime."""
        root_file = self.env.saves_dir / "cloud_world.zip"
        stash_file = self.env.cloud_stash_dir / "cloud_world.zip"

        root_file.write_bytes(b"OLD-ROOT-DATA")
        time.sleep(0.05)
        stash_file.write_bytes(b"NEWER-STASH-DATA")

        # stash новее -> должен перетереть root
        restored = self.env.restore_hidden_cloud_save()
        self.assertEqual(restored.read_bytes(), b"NEWER-STASH-DATA")
        self.assertFalse(stash_file.exists())

    def test_restore_when_root_is_newer_cleans_stash(self) -> None:
        """Если root новее stash, root сохраняется, а устаревший stash очищается."""
        stash_file = self.env.cloud_stash_dir / "cloud_world.zip"
        root_file = self.env.saves_dir / "cloud_world.zip"

        stash_file.write_bytes(b"OLD-STASH-DATA")
        time.sleep(0.05)
        root_file.write_bytes(b"NEWER-ROOT-DATA")

        restored = self.env.restore_hidden_cloud_save()
        self.assertEqual(restored.read_bytes(), b"NEWER-ROOT-DATA")
        self.assertFalse(stash_file.exists())

    def test_restore_when_nothing_exists(self) -> None:
        """Если файлов нет, возвращается None."""
        self.assertIsNone(self.env.restore_hidden_cloud_save())


class TestPlayerUsernameManagement(unittest.TestCase):
    """T6.4: Тестирование чтения и записи ника игрока."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.env = FactorioEnvironment(custom_user_data_dir=self.temp_dir)
        self.env.ensure_dirs()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_get_username_empty(self) -> None:
        """При отсутствии файла или пустых данных возвращается None."""
        self.assertIsNone(self.env.get_player_username())

    def test_set_and_get_username(self) -> None:
        """Запись ника и последующее чтение."""
        self.env.set_player_username("EngineerBob")
        name = self.env.get_player_username()
        self.assertEqual(name, "EngineerBob")

        # Проверка структуры файла player-data.json
        data = json.loads(self.env.player_data_path.read_text(encoding="utf-8"))
        self.assertEqual(data["service-username"], "EngineerBob")
        self.assertEqual(data["username"], "EngineerBob")

    def test_set_empty_username_raises(self) -> None:
        """Попытка записать пустой ник вызывает ValueError."""
        with self.assertRaises(ValueError):
            self.env.set_player_username("   ")

    def test_get_username_fallback_config_ini(self) -> None:
        """Fallback чтение ника из config.ini, если player-data.json отсутствует."""
        ini_content = "[other]\nservice-username=FallbackPlayer\n"
        self.env.config_ini_path.write_text(ini_content, encoding="utf-8")
        self.assertEqual(self.env.get_player_username(), "FallbackPlayer")


class TestMultiplayerSettingsTuning(unittest.TestCase):
    """T6.5: Тестирование настройки config.ini и player-data.json."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.env = FactorioEnvironment(custom_user_data_dir=self.temp_dir)
        self.env.ensure_dirs()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_prepare_multiplayer_creates_settings(self) -> None:
        """Создание настроек с нуля: verify-user-identity, autosave-interval, proxy."""
        self.env.prepare_multiplayer_settings(is_host=False, proxy_address="127.0.0.1:34198")

        # Проверка player-data.json
        player_data = json.loads(self.env.player_data_path.read_text(encoding="utf-8"))
        self.assertFalse(player_data["verify-user-identity"])
        self.assertFalse(player_data["public-visible"])
        self.assertEqual(player_data["latest-multiplayer-connections"][0], "127.0.0.1:34198")

        # Проверка config.ini
        ini_text = self.env.config_ini_path.read_text(encoding="utf-8")
        self.assertIn("autosave-interval=2", ini_text)
        self.assertIn("non-blocking-saving=true", ini_text)

    def test_prepare_multiplayer_preserves_ini_comments_and_sections(self) -> None:
        """Модификация существующего config.ini с сохранением комментариев."""
        initial_ini = (
            "; Важный комментарий пользователя\n"
            "[sound]\n"
            "master-volume=0.8\n"
            "\n"
            "[other]\n"
            "; Комментарий в секции other\n"
            "autosave-interval=5\n"
            "log-saving-statistics=false\n"
            "\n"
            "[graphics]\n"
            "show-fps=true\n"
        )
        self.env.config_ini_path.write_text(initial_ini, encoding="utf-8")

        self.env.prepare_multiplayer_settings(is_host=True, autosave_interval_minutes=2)

        # Проверяем бэкап
        backup_path = self.env.config_ini_path.with_suffix(".ini.bak")
        self.assertTrue(backup_path.is_file())
        self.assertEqual(backup_path.read_text(encoding="utf-8"), initial_ini)

        # Проверяем модифицированный ini
        modified = self.env.config_ini_path.read_text(encoding="utf-8")
        self.assertIn("; Важный комментарий пользователя", modified)
        self.assertIn("; Комментарий в секции other", modified)
        self.assertIn("master-volume=0.8", modified)
        self.assertIn("show-fps=true", modified)
        self.assertIn("autosave-interval=2", modified)
        self.assertNotIn("autosave-interval=5", modified)
        self.assertIn("non-blocking-saving=true", modified)

    def test_proxy_address_prepended_without_duplicates(self) -> None:
        """Адрес прокси всегда ставится первым и не дублируется."""
        initial_player_data = {
            "latest-multiplayer-connections": ["192.168.1.50:34197", "127.0.0.1:34198", "10.0.0.1:34197"]
        }
        self.env.player_data_path.write_text(json.dumps(initial_player_data), encoding="utf-8")

        self.env.prepare_multiplayer_settings(is_host=False, proxy_address="127.0.0.1:34198")

        data = json.loads(self.env.player_data_path.read_text(encoding="utf-8"))
        conns = data["latest-multiplayer-connections"]
        self.assertEqual(conns[0], "127.0.0.1:34198")
        self.assertEqual(conns.count("127.0.0.1:34198"), 1)
        self.assertEqual(len(conns), 3)


class TestModsManifestExtraction(unittest.TestCase):
    """T6.6: Тестирование извлечения манифеста модов."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.env = FactorioEnvironment(custom_user_data_dir=self.temp_dir)
        self.env.ensure_dirs()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_manifest_missing_file(self) -> None:
        """Если mod-list.json нет, возвращается пустой список."""
        self.assertEqual(self.env.get_active_mods_manifest(), [])

    def test_manifest_corrupted_json(self) -> None:
        """Если mod-list.json поврежден, возвращается пустой список без падения."""
        (self.env.mods_dir / "mod-list.json").write_text("{corrupted: json", encoding="utf-8")
        self.assertEqual(self.env.get_active_mods_manifest(), [])

    def test_manifest_filters_enabled_mods_only(self) -> None:
        """Возвращаются только моды с enabled == True."""
        mods_data = {
            "mods": [
                {"name": "base", "enabled": True},
                {"name": "space-age", "enabled": True, "version": "2.0.0"},
                {"name": "quality", "enabled": True},
                {"name": "deprecated-mod", "enabled": False},
                {"name": "test-mod", "enabled": False},
            ]
        }
        (self.env.mods_dir / "mod-list.json").write_text(json.dumps(mods_data), encoding="utf-8")

        manifest = self.env.get_active_mods_manifest()
        self.assertEqual(len(manifest), 3)
        names = [m["name"] for m in manifest]
        self.assertIn("base", names)
        self.assertIn("space-age", names)
        self.assertIn("quality", names)
        self.assertNotIn("deprecated-mod", names)


class TestClipboardAndNotifier(unittest.TestCase):
    """T6.7: Тестирование буфера обмена и всплывающих баннеров."""

    def test_copy_to_clipboard_invalid_input(self) -> None:
        """Передача нестрокового значения не вызывает исключение и возвращает False."""
        self.assertFalse(copy_to_clipboard(None))  # type: ignore
        self.assertFalse(copy_to_clipboard(12345))  # type: ignore

    def test_copy_to_clipboard_string_does_not_crash(self) -> None:
        """Копирование строки отрабатывает стабильно."""
        result = copy_to_clipboard("127.0.0.1:34198")
        # На headless/CI может быть False или True, главное - отсутствие исключений
        self.assertIsInstance(result, bool)

    def test_show_role_banner_host_format(self) -> None:
        """Форматирование баннера для Хоста."""
        body = show_role_banner("host", background=False)
        self.assertIn("Вы — Хост", body)
        self.assertIn("Запустить сохранённую игру -> cloud_world", body)

    def test_show_role_banner_guest_format(self) -> None:
        """Форматирование баннера для Гостя с именем Хоста."""
        body = show_role_banner("guest", host_name="Alex", background=False)
        self.assertIn("Сейчас хостит Alex", body)
        self.assertIn("Сейв скрыт", body)
        self.assertIn("Прямое подключение", body)

    def test_show_role_banner_guest_default_name(self) -> None:
        """Если имя Хоста не передано, используется значение по умолчанию."""
        body = show_role_banner("guest", host_name=None, background=False)
        self.assertIn("Сейчас хостит Друг", body)

    def test_show_role_banner_with_extra_message(self) -> None:
        """Баннер с дополнительным поясняющим сообщением."""
        body = show_role_banner("host", extra_message="Сейв синхронизирован с облаком", background=False)
        self.assertIn("Сейв синхронизирован с облаком", body)


if __name__ == "__main__":
    unittest.main()
