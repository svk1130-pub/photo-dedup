"""Структурированный лог движка: таблица events в БД + stdout через logging.

Никаких разделяемых лог-файлов между контейнерами: UI читает хвост events.
Старые записи периодически подчищаются (хранятся последние ~1000).
"""
from __future__ import annotations

import logging

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

_KEEP_LAST = 1000      # сколько последних событий храним
_CLEANUP_EVERY = 200   # чистка раз в N записей


class EventLog:
    """Пишет событие в БД (autocommit-соединение) + дублирует в stdout.

    Не потокобезопасен и не должен быть: пишет только главный поток движка.
    Использует отдельное управляющее соединение, поэтому события видны в UI
    даже между батчами основной записи.
    """

    def __init__(self, conn, keep: int = _KEEP_LAST) -> None:
        self._conn = conn
        self._keep = keep
        self._count = 0

    def log(self, level: str, message: str) -> None:
        logging.getLogger("engine").log(_LEVELS.get(level, logging.INFO), message)
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO events (level, message) VALUES (%s, %s)",
                (level, message[:2000]),
            )
        self._count += 1
        if self._count % _CLEANUP_EVERY == 0:
            self._cleanup()

    def _cleanup(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM events WHERE id NOT IN "
                "(SELECT id FROM events ORDER BY id DESC LIMIT %s)",
                (self._keep,),
            )

    def debug(self, message: str) -> None:
        self.log("debug", message)

    def info(self, message: str) -> None:
        self.log("info", message)

    def warning(self, message: str) -> None:
        self.log("warning", message)

    def error(self, message: str) -> None:
        self.log("error", message)
