"""Фоновый клиентский наблюдатель и отправщик хроники Factorio (ChronicleWatcher).

Отслеживает:
1. Новые игровые события из script-output/coop_chronicle/events.jsonl.
2. Свежие скриншоты базы и мест ключевых событий (гибель, запуск ракеты, реакторы).
3. Резервный парсинг гибели игроков из factorio-current.log.

Триггеры отправки на сервер (POST /chronicle/report):
- important_event: при важных событиях (гибель, ракета, реактор) с кулдауном 5 минут.
- hourly_summary: каждые 60 минут активной игры.
- session_summary: при завершении сессии Хоста (flush_on_exit).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from src.client.hub_client import HubClient

logger = logging.getLogger("factorio_client.chronicle_watcher")


class ChronicleWatcher:
    """Фоновый поток отслеживания и отправки игровой хроники и скриншотов."""

    def __init__(
        self,
        script_output_dir: Path | str,
        hub_client: HubClient,
        session_token: Optional[str] = None,
        log_path: Optional[Path | str] = None,
        poll_interval_sec: float = 2.0,
        important_cooldown_sec: float = 300.0,
        hourly_interval_sec: float = 3600.0,
        wait_screenshot_sec: float = 2.0,
        is_confirmed_getter: Optional[Callable[[], bool]] = None,
    ) -> None:
        """Инициализирует наблюдатель хроники.

        Args:
            script_output_dir: Путь к папке script-output Factorio.
            hub_client: Экземпляр клиента связи с сервером.
            session_token: Активный токен сессии хоста.
            log_path: Опциональный путь к логу factorio-current.log для резервного парсинга.
            poll_interval_sec: Интервал проверки новых строк и скриншотов (секунды).
            important_cooldown_sec: Кулдаун между экстренными сводками важных событий (секунды).
            hourly_interval_sec: Интервал между периодическими часовыми сводками (секунды).
            wait_screenshot_sec: Задержка для завершения записи скриншота на диск игрой.
            is_confirmed_getter: Функция-предикат, возвращающая True, если сетевое лобби активно.
        """
        self.script_output_dir = Path(script_output_dir).resolve()
        self.chronicle_dir = self.script_output_dir / "coop_chronicle"
        self.events_file = self.chronicle_dir / "events.jsonl"
        self.hub_client = hub_client
        self.session_token = session_token
        self.log_path = Path(log_path).resolve() if log_path else None
        self.poll_interval_sec = max(0.5, float(poll_interval_sec))
        self.important_cooldown_sec = max(10.0, float(important_cooldown_sec))
        self.hourly_interval_sec = max(60.0, float(hourly_interval_sec))
        self.wait_screenshot_sec = max(0.1, float(wait_screenshot_sec))
        self.is_confirmed_getter = is_confirmed_getter

        self._pending_events: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._events_file_offset: int = 0
        self._log_file_offset: int = 0
        self._last_important_sent_time: float = 0.0
        self._last_hourly_sent_time: float = time.time()
        self._seen_signatures: set[str] = set()

    @property
    def is_running(self) -> bool:
        """Возвращает True, если фоновый поток активен."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Запускает фоновый поток наблюдателя."""
        with self._lock:
            if self.is_running:
                logger.debug("ChronicleWatcher уже запущен.")
                return

            self._stop_event.clear()
            # Пропускаем чтение старого содержимого файла при первичном старте,
            # чтобы не отправлять события прошлых сессий
            try:
                if self.events_file.is_file():
                    self._events_file_offset = self.events_file.stat().st_size
                if self.log_path and self.log_path.is_file():
                    self._log_file_offset = self.log_path.stat().st_size
            except Exception as err:
                logger.warning(f"Ошибка при инициализации смещений логов: {err}")

            self._last_hourly_sent_time = time.time()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="ChronicleWatcher",
                daemon=True,
            )
            self._thread.start()
            logger.info("ChronicleWatcher успешно запущен.")

    def stop(self, timeout: float = 3.0) -> None:
        """Останавливает фоновый поток наблюдателя."""
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        logger.info("ChronicleWatcher остановлен.")

    def _run_loop(self) -> None:
        """Основной цикл фонового потока."""
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception as loop_err:
                logger.warning(f"Ошибка в цикле ChronicleWatcher: {loop_err}")

            self._stop_event.wait(self.poll_interval_sec)

    def _poll_once(self) -> None:
        """Выполняет одну итерацию опроса файлов и проверки триггеров."""
        # 1. Чтение новых строк из events.jsonl
        self._read_new_events_from_file()

        # 2. Резервное чтение factorio-current.log (если задан)
        if self.log_path:
            self._read_new_events_from_log()

        # 3. Если лобби еще не подтверждено, события накапливаются в буфер, но не отправляются
        if self.is_confirmed_getter and not self.is_confirmed_getter():
            return

        # 4. Проверка триггеров отправки
        self._check_triggers()

    def _read_new_events_from_file(self) -> None:
        """Инкрементально читает новые JSON-строки из events.jsonl."""
        try:
            if not self.events_file.is_file():
                return

            size = self.events_file.stat().st_size
            if size < self._events_file_offset:
                # Файл был усечен или перезаписан
                self._events_file_offset = 0

            if size == self._events_file_offset:
                return

            with self.events_file.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(self._events_file_offset)
                new_text = f.read()
                self._events_file_offset = f.tell()

            if not new_text:
                return

            for raw_line in new_text.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    if isinstance(event, dict):
                        sig = f"{event.get('type')}:{event.get('tick')}:{event.get('player')}:{event.get('tech')}:{event.get('entity')}"
                        if sig not in self._seen_signatures:
                            self._seen_signatures.add(sig)
                            with self._lock:
                                self._pending_events.append(event)
                            logger.debug(f"Новое игровое событие из events.jsonl: {event.get('type')}")
                except Exception as parse_err:
                    logger.debug(f"Пропуск некорректной строки events.jsonl: {parse_err}")
        except Exception as err:
            logger.warning(f"Ошибка чтения events.jsonl: {err}")

    def _read_new_events_from_log(self) -> None:
        """Резервный парсинг строк гибели игроков из factorio-current.log."""
        try:
            if not self.log_path or not self.log_path.is_file():
                return

            size = self.log_path.stat().st_size
            if size < self._log_file_offset:
                self._log_file_offset = 0

            if size == self._log_file_offset:
                return

            with self.log_path.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(self._log_file_offset)
                new_text = f.read()
                self._log_file_offset = f.tell()

            if not new_text:
                return

            # Ищем строки вида: PlayerName was killed by biter-spitter
            # или [CHAT] PlayerName was killed by locomotive
            death_pattern = re.compile(
                r"(?:\[CHAT\]\s+)?([A-Za-z0-9_ -]+)\s+(?:was killed by|был убит)\s+([A-Za-z0-9_ -]+)",
                re.IGNORECASE,
            )

            for line in new_text.splitlines():
                match = death_pattern.search(line)
                if match:
                    player_name = match.group(1).strip()
                    cause_name = match.group(2).strip()
                    sig = f"log_player_died:{player_name}:{cause_name}"
                    if sig not in self._seen_signatures:
                        self._seen_signatures.add(sig)
                        event = {
                            "type": "player_died",
                            "important": True,
                            "tick": 0,
                            "player": player_name,
                            "cause": cause_name,
                            "source": "log",
                        }
                        with self._lock:
                            self._pending_events.append(event)
                        logger.info(f"Обнаружена гибель игрока из лога: {player_name} убит {cause_name}")
        except Exception as err:
            logger.warning(f"Ошибка резервного парсинга factorio-current.log: {err}")

    def _check_triggers(self) -> None:
        """Проверяет условия триггеров important_event и hourly_summary."""
        now = time.time()

        # Триггер 1: Важное событие (гибель, ракета, знаковая постройка)
        has_important = False
        target_screenshot_name: Optional[str] = None

        with self._lock:
            for ev in self._pending_events:
                if ev.get("important") is True:
                    has_important = True
                    if ev.get("screenshot"):
                        target_screenshot_name = ev.get("screenshot")
                    break

        if has_important and (now - self._last_important_sent_time >= self.important_cooldown_sec):
            # Краткая пауза для гарантии завершения записи файла скриншота на диск игрой
            self._stop_event.wait(self.wait_screenshot_sec)
            shot_path = self._resolve_screenshot_path(target_screenshot_name)
            res = self._send_report(trigger_type="important_event", screenshot_path=shot_path)
            self._last_important_sent_time = time.time()
            logger.info(f"Отправлена экстренная сводка важного события: {res}")
            return

        # Триггер 2: Ежечасная сводка (каждые 60 минут)
        if now - self._last_hourly_sent_time >= self.hourly_interval_sec:
            shot_path = self._find_latest_screenshot()
            with self._lock:
                has_data = bool(self._pending_events) or (shot_path is not None)

            if has_data:
                res = self._send_report(trigger_type="hourly_summary", screenshot_path=shot_path)
                self._last_hourly_sent_time = time.time()
                logger.info(f"Отправлена ежечасная сводка базы: {res}")
            else:
                self._last_hourly_sent_time = time.time()

    def _resolve_screenshot_path(self, screenshot_name: Optional[str]) -> Optional[Path]:
        """Находит путь к файлу скриншота по имени или ищет самый свежий."""
        if screenshot_name:
            # 1. Проверяем в script-output/coop_chronicle/
            cand1 = self.chronicle_dir / screenshot_name
            if cand1.is_file():
                return cand1
            # 2. Проверяем в script-output/
            cand2 = self.script_output_dir / screenshot_name
            if cand2.is_file():
                return cand2

        # 3. Иначе берем самый свежий скриншот
        return self._find_latest_screenshot()

    def _find_latest_screenshot(self) -> Optional[Path]:
        """Ищет самый свежий файл изображения в coop_chronicle или script-output."""
        candidates: list[Path] = []
        for d in (self.chronicle_dir, self.script_output_dir):
            if d.is_dir():
                try:
                    for p in d.iterdir():
                        if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png"):
                            candidates.append(p)
                except Exception:
                    pass

        if not candidates:
            return None

        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return candidates[0]

    def _send_report(
        self,
        trigger_type: str,
        screenshot_path: Optional[Path] = None,
    ) -> dict[str, Any]:
        """Отправляет накопленные события и скриншот на сервер."""
        with self._lock:
            events_to_send = list(self._pending_events)
            self._pending_events.clear()

        if not events_to_send and screenshot_path is None:
            return {"skipped": True, "reason": "no_events_or_screenshot"}

        try:
            return self.hub_client.send_chronicle_report(
                events=events_to_send,
                trigger_type=trigger_type,
                screenshot_path=screenshot_path,
                session_token=self.session_token,
            )
        except Exception as err:
            logger.warning(f"Ошибка вызова HubClient.send_chronicle_report: {err}")
            return {"success": False, "error": str(err)}

    def flush_on_exit(self) -> dict[str, Any]:
        """Сбрасывает все оставшиеся за сессию события и последний скриншот при выходе."""
        logger.info("ChronicleWatcher.flush_on_exit: сброс финальной хроники сессии...")
        try:
            # Считываем финальные строки перед закрытием
            self._read_new_events_from_file()
            if self.log_path:
                self._read_new_events_from_log()

            shot_path = self._find_latest_screenshot()
            with self._lock:
                has_data = bool(self._pending_events) or (shot_path is not None)

            if not has_data:
                logger.info("Нет накопленных событий или скриншотов для отправки при выходе.")
                return {"action": "flushed_empty", "success": True}

            res = self._send_report(trigger_type="session_summary", screenshot_path=shot_path)
            logger.info(f"Финальная хроника сессии отправлена: {res}")
            return res
        except Exception as err:
            logger.warning(f"Ошибка при flush_on_exit: {err}")
            return {"success": False, "error": str(err)}
