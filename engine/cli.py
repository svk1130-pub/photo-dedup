"""CLI движка: scan / analyze / move / run / status / stop.

Приложение запускается как `python -m engine.cli <command>` (или через
ENTRYPOINT контейнера engine). Никогда не стартует UI; UI никогда не стартует
движок. CLI-флаги имеют приоритет над settings.toml.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg

from . import db
from .analyze import run_analyze
from .events import EventLog
from .move import MoveError, run_move
from .scan import run_scan
from .settings import Settings, SettingsError, check_paths, load_settings

logger = logging.getLogger("engine.cli")


@dataclass
class EngineContext:
    """Среда выполнения одного запуска движка."""

    conn: psycopg.Connection            # данные (autocommit; транзакции — явные)
    conn_ctl: psycopg.Connection        # status/events (autocommit)
    settings_path: str
    overrides: dict[str, Any]
    command: str
    stop_event: threading.Event = field(default_factory=threading.Event)
    log: EventLog | None = None
    force: bool = False
    _settings: Settings | None = None
    _last_stop_check: float = 0.0
    _stop_cache: bool = False

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            self.reload_settings()
        return self._settings  # type: ignore[return-value]

    def reload_settings(self) -> None:
        """Перечитать settings.toml (на границе этапов run: scan → analyze → move)."""
        self._settings = load_settings(self.settings_path, self.overrides)
        if self.log is not None:
            self.log.info(
                f"настройки перечитаны: {self.settings_path} "
                f"(CLI-override: {', '.join(sorted(self.overrides)) or 'нет'})"
            )

    def should_stop(self) -> bool:
        """SIGINT/SIGTERM ИЛИ флаг stop_requested из БД (проверка не чаще 0.5 c)."""
        if self.stop_event.is_set():
            return True
        now = time.monotonic()
        if now - self._last_stop_check >= 0.5:
            self._last_stop_check = now
            self._stop_cache = db.get_stop_requested(self.conn_ctl)
        return self._stop_cache

    def params_payload(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Эффективная конфигурация текущего запуска (для status.params JSONB)."""
        s = self.settings
        payload: dict[str, Any] = {
            "command": self.command,
            "force": self.force,
            "threads": s.scan.threads,
            "threshold": s.analyze.threshold,
            "move_mode": s.move.mode,
            "dry_run": s.move.dry_run,
            "keep_by": s.move.keep_by,
            "batch_size": s.scan.batch_size,
            # пути в host-форме (как в settings.toml/на хосте) — понятнее пользователю
            "src": s.paths.src_host,
            "trash": s.paths.trash_host,
            "settings_file": self.settings_path,
        }
        if extra:
            payload.update(extra)
        return payload


# ----------------------------- парсер -----------------------------

def build_parser() -> argparse.ArgumentParser:
    # --settings принимается И до, И после подкоманды; SUPPRESS не затирает значение
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--settings", default=argparse.SUPPRESS,
        help="путь к settings.toml (по умолчанию: $SETTINGS_PATH или ./settings.toml)",
    )
    p = argparse.ArgumentParser(
        prog="engine",
        description="CLI-движок поиска и переноса изменённых/повёрнутых/зеркальных дубликатов фото",
    )
    p.add_argument("--settings", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", parents=[common], help="индексация файлов и хэширование (с Resume)")
    scan.add_argument("--force", action="store_true", help="переиндексировать даже неизменённые файлы")
    scan.add_argument("--threads", type=int, metavar="N", help="потоки хэширования (приоритет над settings)")

    an = sub.add_parser("analyze", parents=[common], help="поиск групп дубликатов по хэшам")
    an.add_argument("--threshold", type=int, metavar="N", help="макс. расстояние Хэмминга 0..64")

    mv = sub.add_parser("move", parents=[common], help="перенос дубликатов в trash")
    mv.add_argument("--dry-run", action="store_true", help="только план, без переноса")
    mv.add_argument("--move-mode", choices=("auto", "manual"), help="auto | manual")
    mv.add_argument("--keep-by", choices=("capture", "size", "pixels"),
                    help="критерий выбора оригинала (capture: время снимка — json-Takeout/ФС)")
    mv.add_argument("--group-id", type=int, help="обработать только эту группу (даже без подтверждения)")

    run = sub.add_parser("run", parents=[common], help="scan → analyze → move с Resume")
    run.add_argument("--force", action="store_true")
    run.add_argument("--threads", type=int, metavar="N")
    run.add_argument("--threshold", type=int, metavar="N")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--move-mode", choices=("auto", "manual"))
    run.add_argument("--keep-by", choices=("capture", "size", "pixels"))

    sub.add_parser("status", parents=[common], help="вывести текущий статус в консоль")
    sub.add_parser("stop", parents=[common], help="выставить флаг мягкой остановки")
    return p


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    ov: dict[str, Any] = {}
    if getattr(args, "threads", None) is not None:
        ov["scan.threads"] = args.threads
    if getattr(args, "threshold", None) is not None:
        ov["analyze.threshold"] = args.threshold
    if getattr(args, "move_mode", None) is not None:
        ov["move.mode"] = args.move_mode
    if getattr(args, "keep_by", None) is not None:
        ov["move.keep_by"] = args.keep_by
    if getattr(args, "dry_run", False):
        ov["move.dry_run"] = True
    return ov


def _settings_path(args: argparse.Namespace) -> str:
    """--settings (в любой позиции) > $SETTINGS_PATH > ./settings.toml"""
    return getattr(args, "settings", None) or os.environ.get("SETTINGS_PATH", "settings.toml")


# ----------------------------- сигналы -----------------------------

def _install_signal_handlers(stop_event: threading.Event) -> None:
    """Первый SIGINT/SIGTERM — мягкая остановка; второй — принудительный выход.
    docker stop не портит данные: батчи атомарны, in-flight доделывается."""

    def handler(sig, _frame):  # noqa: ANN001
        if stop_event.is_set():
            os._exit(130)
        stop_event.set()
        logger.warning("Получен сигнал %s: мягкая остановка после текущего файла/батча "
                       "(повторный сигнал — принудительный выход)", sig)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except ValueError:
            # signal.signal доступен только в главном потоке; при запуске из
            # другого потока (тесты, embedding) мягкая остановка через stop-флаг
            # продолжает работать.
            logger.debug("Сигналы %s не установлены (не главный поток)", sig)


# ----------------------------- лёгкие команды -----------------------------

def _fmt_duration(sec: float) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _cmd_light(args: argparse.Namespace) -> int:
    """status/stop: не требуют валидных настроек, но создают схему при первом запуске."""
    try:
        conn = db.connect(db.dsn(), attempts=3, delay=1.0)
        conn.autocommit = True
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 3
    try:
        db.init_db(conn)
        if args.command == "stop":
            db.request_stop(conn)
            alive = db.engine_running(conn, stale_sec=15.0)
            print("Флаг stop_requested=true выставлен.")
            if alive:
                print("Движок работает — завершит текущий файл/батч и остановится.")
            else:
                print("Движок не запущен: флаг актуален только для работающего движка "
                      "и будет сброшен при следующем старте рабочей команды.")
            return 0

        row = db.get_status_row(conn)
        if row is None:
            print("Статус недоступен.")
            return 1
        total, processed = int(row["total"] or 0), int(row["processed"] or 0)
        pct = f"{100.0 * processed / total:.1f}%%" if total else "—"
        speed = float(row["files_per_sec"] or 0.0)
        speed_unit = "бакетов/с" if row["stage"] == "analyze" else "файлов/с"
        eta = ""
        if speed > 0 and row["stage"] in db.WORK_STAGES and total > processed:
            eta = f" | ETA {_fmt_duration((total - processed) / speed)}"
        print(f"Этап:    {row['stage']}")
        print(f"Прогресс: {processed} / {total} ({pct.replace('%%', '%')}){eta}")
        print(f"Скорость: {speed:.1f} {speed_unit}")
        if row["current_file"]:
            print(f"Файл:    {row['current_file']}")
        print(f"PID:     {row['engine_pid']} | stop_requested: {row['stop_requested']}")
        if row["started_at"]:
            print(f"Запуск:  {row['started_at']:%Y-%m-%d %H:%M:%S} | обновлён: {row['updated_at']:%H:%M:%S}")

        files_total = db.scalar(conn, "SELECT count(*) FROM files WHERE status <> 'moved'")
        moved_files = db.scalar(conn, "SELECT count(*) FROM files WHERE status = 'moved'")
        corrupt = db.scalar(conn, "SELECT count(*) FROM files WHERE status='corrupt'")
        extra = f", перенесено/отсутствует: {moved_files}" if moved_files else ""
        print(f"Файлов в индексе: {files_total} (битых: {corrupt}{extra})")
        lr = conn.execute(
            "SELECT id, created_at, threshold FROM analysis_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if lr:
            groups_total = db.scalar(conn, "SELECT count(*) FROM groups WHERE run_id=%s", (lr[0],))
            confirmed = db.scalar(
                conn, "SELECT count(*) FROM groups WHERE run_id=%s AND confirmed", (lr[0],)
            )
            moved = db.scalar(
                conn,
                "SELECT count(*) FROM group_members gm JOIN groups g ON g.id=gm.group_id "
                "WHERE g.run_id=%s AND gm.moved_to IS NOT NULL",
                (lr[0],),
            )
            print(f"Последний run #{lr[0]} ({lr[1]:%Y-%m-%d %H:%M}, threshold={lr[2]}): "
                  f"групп {groups_total} (подтверждено {confirmed}), перенесено файлов {moved}")
        else:
            print("Анализ ещё не выполнялся.")
        return 0
    finally:
        conn.close()


# ----------------------------- рабочие команды -----------------------------

def _run_all(ctx: EngineContext, *, force: bool) -> None:
    stats = run_scan(ctx, force=force)
    if stats.stopped or ctx.should_stop():
        ctx.log.warning("run: прервано на этапе scan — повторный `run` продолжит с места остановки")
        return
    ctx.reload_settings()          # граница этапов scan → analyze
    _run_id, stopped = run_analyze(ctx)
    if stopped:
        return
    ctx.reload_settings()          # граница этапов analyze → move
    run_move(ctx)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    args = build_parser().parse_args(argv)
    overrides = _overrides(args)

    if args.command in ("status", "stop"):
        return _cmd_light(args)

    # Валидация конфигурации ДО подключения к БД: аварийный выход с понятной ошибкой
    settings_file = _settings_path(args)
    try:
        settings = load_settings(settings_file, overrides)
    except SettingsError as e:
        print(f"Ошибка настроек:\n  {e}", file=sys.stderr)
        return 2
    try:
        check_paths(settings)
    except SettingsError as e:
        print(f"Ошибка конфигурации путей:\n  {e}", file=sys.stderr)
        print("Исправьте секцию [paths] в settings.toml.", file=sys.stderr)
        return 2

    try:
        conn = db.connect(db.dsn(), attempts=30, delay=1.0)
        conn.autocommit = True
        conn_ctl = db.connect(db.dsn(), attempts=30, delay=1.0)
        conn_ctl.autocommit = True
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 3
    db.init_db(conn)

    ctx = EngineContext(
        conn=conn, conn_ctl=conn_ctl, settings_path=settings_file, overrides=overrides,
        command=args.command, force=getattr(args, "force", False),
    )
    ctx.log = EventLog(conn_ctl)
    _install_signal_handlers(ctx.stop_event)

    try:
        Path(settings.paths.trash).expanduser().mkdir(parents=True, exist_ok=True)
    except OSError as e:
        ctx.log.error(f"Не удалось создать папку trash {settings.paths.trash}: {e}")
        return 2

    ctx.log.info(f"=== engine {args.command} (pid {os.getpid()}) ===")
    try:
        if args.command == "scan":
            run_scan(ctx, force=args.force)
        elif args.command == "analyze":
            run_analyze(ctx)
        elif args.command == "move":
            run_move(ctx, group_id=args.group_id)
        elif args.command == "run":
            _run_all(ctx, force=args.force)
    except KeyboardInterrupt:
        db.set_stage(conn_ctl, "stopped")
        ctx.log.warning("Прервано (KeyboardInterrupt) — статус: stopped")
    except (MoveError, RuntimeError, psycopg.Error) as e:
        logger.exception("Ошибка этапа")
        db.set_stage(conn_ctl, "error")
        ctx.log.error(f"Ошибка этапа '{args.command}': {e}")
        return 1
    except Exception as e:  # любая иная ошибка — статус error, не молчаливое падение
        logger.exception("Непредвиденная ошибка")
        db.set_stage(conn_ctl, "error")
        ctx.log.error(f"Непредвиденная ошибка в '{args.command}': {type(e).__name__}: {e}")
        return 1
    finally:
        conn.close()
        conn_ctl.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
