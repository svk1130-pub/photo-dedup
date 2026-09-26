"""Job-runner — исполнитель очереди jobs (Ф1, вариант A: in-process).

Отдельный сервис docker compose (`runner`) из ТОГО ЖЕ образа и с ТЕМИ ЖЕ
маунтами/env, что и engine. Вечный цикл: атомарно берёт задание из очереди
jobs (PostgreSQL: FOR UPDATE SKIP LOCKED + single-flight guard) и выполняет
соответствующую команду движка в собственном процессе — тот же код, что у
CLI, без subprocess и повторного argparse («тонкий слой» dispatch по имени).
docker-сокет runner'у не нужен; тонкий клиент UI не ослабляется: кнопка
«▶️ Запустить прогон» лишь вставляет строку в jobs (docs/WHY_NO_BUTTONS.md §5).

Пробуждение — LISTEN/NOTIFY (канал jobs, мгновенная реакция на INSERT из UI),
фоллбэк — опрос раз в RUNNER_POLL_SEC. Живость задания подтверждается
heartbeat'ом из отдельного потока; на старте runner'а зависшие running-задания
(погибший контейнер) помечаются stale — crash-recovery; Resume штатных команд
продолжит прерванную работу с места остановки.

Остановка: кнопка «🛑 Стоп» в UI работает как раньше (тот же флаг
status.stop_requested): движок доделывает текущий файл/батч, задание получает
state='stopped' и stop_reason «остановлено пользователем» (1.10.0; сигнал
SIGINT/SIGTERM даёт свой текст — причину видно в истории очереди). SIGTERM
контейнера — та же мягкая остановка; повторный сигнал — принудительный выход
(docker restart поднимет runner, задание станет stale).

Глобальная пауза очереди (1.10.0): флаг status.runner_paused (кнопка UI) —
claim_next молчит, задания (в т.ч. отложенные) ждут в очереди; выполняемое
задание дорабатает штатно. CLI-путь `docker compose run --rm engine run|scan|…`
не меняется.
"""
from __future__ import annotations

import contextlib
import logging
import os
import socket
import threading
from pathlib import Path
from typing import Any

import psycopg

from . import db, jobs
from .analyze import run_analyze
from .cli import EngineContext, _install_signal_handlers, _run_all, _run_undo
from .events import EventLog
from .move import MoveError, run_move
from .scan import run_scan
from .settings import SettingsError, check_paths, load_settings

logger = logging.getLogger("engine.runner")

POLL_SEC = float(os.environ.get("RUNNER_POLL_SEC", "2"))
HEARTBEAT_SEC = float(os.environ.get("RUNNER_HEARTBEAT_SEC", "5"))
STALE_SEC = float(os.environ.get("RUNNER_STALE_SEC", "30"))
RETAIN_JOBS = int(os.environ.get("RUNNER_RETAIN_JOBS", str(jobs.RETAIN_JOBS)))

# params задания → CLI-override (та же семантика, что у одноимённых флагов CLI)
_OVERRIDE_KEYS = {
    "threads": ("scan.threads", int),
    "threshold": ("analyze.threshold", int),
    "move_mode": ("move.mode", str),
    "keep_by": ("move.keep_by", str),
}


def _params_to_overrides(params: dict[str, Any]) -> dict[str, Any]:
    """{'threads': 4, 'dry_run': True} → {'scan.threads': 4, 'move.dry_run': True}."""
    ov: dict[str, Any] = {}
    for key, (setting, cast) in _OVERRIDE_KEYS.items():
        val = params.get(key)
        if val is not None:
            ov[setting] = cast(val)
    if params.get("dry_run"):
        ov["move.dry_run"] = True
    return ov


def _status_snapshot(conn_ctl: psycopg.Connection) -> dict[str, Any]:
    """Итоговое состояние status движка — краткая сводка в jobs.result."""
    try:
        row = db.get_status_row(conn_ctl)
    except psycopg.Error:  # undo/clean-db могли вычистить БД — статус не критичен
        return {}
    if not row:
        return {}
    return {
        "stage": row["stage"],
        "processed": int(row["processed"] or 0),
        "total": int(row["total"] or 0),
    }


def _stop_reason(result: dict[str, Any], conn_ctl: psycopg.Connection, stop_event: threading.Event) -> str | None:
    """Почему задание не доработало (1.10.0) — текст для jobs.stop_reason.

    Различаем по доступным признакам: флаг stop_requested в БД выставлен —
    «Стоп» (UI/CLI stop); иначе если локальный stop_event поднят — сигнал
    (SIGINT/SIGTERM: docker stop/рестарт контейнера); иначе остановка без
    уточнённой причины (встраивание/KeyboardInterrupt вне обработчика).
    Флаг в БД приоритетен: Стоп мог сопровождаться и сигналом рестарта.
    """
    if result.get("stage") != "stopped":
        return None  # задание доработало (done/failed) — причины остановки нет
    try:
        if db.get_stop_requested(conn_ctl):
            return "остановлено пользователем (кнопка Стоп)"
    except psycopg.Error:  # undo/clean-db могли вычистить БД — не критично
        pass
    if stop_event.is_set():
        return "остановлено сигналом (SIGINT/SIGTERM — контейнер runner остановлен)"
    return "остановлено (причина не уточнена)"


def execute_job(
    job: dict[str, Any],
    *,
    settings_path: str,
    stop_event: threading.Event,
    runner_id: str,
) -> tuple[int, str | None, dict[str, Any], str | None]:
    """Выполнить задание in-process: тот же код, что у CLI, без argparse.

    Возвращает (exit_code, error | None, result, stop_reason | None) —
    rc зеркалит cli.main (0 ок / 2 конфигурация / 3 нет БД / 1 ошибка этапа);
    stop_reason (1.10.0) непуст только для остановленных заданий (stage='stopped').
    """
    command, params = job["command"], dict(job.get("params") or {})
    overrides = _params_to_overrides(params)

    try:
        settings = load_settings(settings_path, overrides)
    except SettingsError as e:
        return 2, f"Ошибка настроек: {e}", {}
    try:
        check_paths(settings)
    except SettingsError as e:
        return 2, f"Ошибка конфигурации путей: {e}", {}
    try:
        conn = db.connect(db.dsn(), attempts=30, delay=1.0)
        conn.autocommit = True
        conn_ctl = db.connect(db.dsn(), attempts=30, delay=1.0)
        conn_ctl.autocommit = True
    except RuntimeError as e:
        return 3, str(e), {}
    db.init_db(conn)

    ctx = EngineContext(
        conn=conn, conn_ctl=conn_ctl, settings_path=settings_path, overrides=overrides,
        command=command, force=bool(params.get("force")), stop_event=stop_event,
    )
    ctx.log = EventLog(conn_ctl)

    if command != "undo":  # undo не требует существующего trash (как в cli.main)
        try:
            Path(settings.paths.trash).expanduser().mkdir(parents=True, exist_ok=True)
        except OSError as e:
            ctx.log.error(f"Не удалось создать папку trash {settings.paths.trash}: {e}")
            return 2, f"Не удалось создать папку trash {settings.paths.trash}: {e}", {}

    ctx.log.info(f"=== engine {command} (job #{job['id']}, runner {runner_id}, pid {os.getpid()}) ===")
    rc, error = 0, None
    try:
        if command == "scan":
            run_scan(ctx, force=ctx.force)
        elif command == "analyze":
            run_analyze(ctx)
        elif command == "move":
            run_move(ctx, group_id=params.get("group_id"))
        elif command == "run":
            _run_all(ctx, force=ctx.force)
        elif command == "undo":
            _run_undo(ctx, dry=bool(params.get("dry_run")))
        else:  # страховка; CHECK-констрейнт в DDL отсекает остальное
            rc, error = 2, f"неизвестная команда '{command}'"
    except KeyboardInterrupt:
        db.set_stage(conn_ctl, "stopped")
        ctx.log.warning("Прервано (KeyboardInterrupt) — статус: stopped")
    except (MoveError, RuntimeError, psycopg.Error) as e:
        logger.exception("Ошибка задания")
        db.set_stage(conn_ctl, "error")
        ctx.log.error(f"Ошибка этапа '{command}': {e}")
        rc, error = 1, f"{type(e).__name__}: {e}"
    except Exception as e:  # любая иная ошибка — статус error, не молчаливое падение
        logger.exception("Непредвиденная ошибка задания")
        db.set_stage(conn_ctl, "error")
        ctx.log.error(f"Непредвиденная ошибка в '{command}': {type(e).__name__}: {e}")
        rc, error = 1, f"{type(e).__name__}: {e}"
    finally:
        result = _status_snapshot(conn_ctl)
        stop_reason = _stop_reason(result, conn_ctl, stop_event)
        conn.close()
        conn_ctl.close()
    return rc, error, result, stop_reason


def job_state(rc: int, error: str | None, result: dict[str, Any]) -> str:
    """Итоговое состояние очереди: failed | stopped | done (по rc и финальному этапу)."""
    if rc != 0 or error is not None:
        return "failed"
    if result.get("stage") == "stopped":
        return "stopped"
    return "done"


@contextlib.contextmanager
def _heartbeat(job_id: int, runner_id: str, interval: float = HEARTBEAT_SEC):
    """Отдельный поток: каждые interval сек ставит метку живости задания в БД.

    Своё соединение (psycopg не потокобезопасен); при недоступности БД метки
    просто прекращаются — задание со временем будет помечено stale.
    """
    stop = threading.Event()

    def loop() -> None:
        try:
            hb_conn = db.connect(db.dsn(), attempts=5, delay=1.0)
            hb_conn.autocommit = True
        except RuntimeError:
            logger.warning("heartbeat: нет соединения с БД — метки живости не ставятся")
            return
        try:
            while not stop.wait(interval):
                try:
                    if not jobs.heartbeat(hb_conn, job_id, runner_id):
                        logger.warning("heartbeat: задание #%d больше не running", job_id)
                        return
                except psycopg.Error:
                    logger.warning("heartbeat не прошёл", exc_info=True)
        finally:
            hb_conn.close()

    thread = threading.Thread(target=loop, name=f"hb-job{job_id}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=3)


def _wait_notify(conn: psycopg.Connection, timeout: float) -> bool:
    """Спать до NOTIFY (канал jobs) или timeout; True — проснулись по уведомлению.

    Непрочитанные уведомления остаются в очереди соединения и будут получены
    при следующем вызове — ничего не теряется.
    """
    for _ in conn.notifies(timeout=timeout):
        return True
    return False


def _listen(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(f"LISTEN {jobs.CHANNEL}")  # jobs.CHANNEL — константа-идентификатор


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    runner_id = f"runner-{socket.gethostname()}-{os.getpid()}"
    settings_path = os.environ.get("SETTINGS_PATH", "settings.toml")
    stop_event = threading.Event()
    _install_signal_handlers(stop_event)  # 1-й SIGTERM — мягкая остановка задания

    conn = db.connect(db.dsn(), attempts=30, delay=1.0)
    conn.autocommit = True
    db.init_db(conn)
    reaped = jobs.reap_stale(conn, stale_sec=STALE_SEC)
    if reaped:
        logger.warning("crash-recovery: %d зависших заданий помечено stale", reaped)
    _listen(conn)
    logger.info(
        "runner %s: слушаю очередь jobs (poll %.1f с, heartbeat %.1f с, stale %.0f с), "
        "settings: %s", runner_id, POLL_SEC, HEARTBEAT_SEC, STALE_SEC, settings_path,
    )

    while True:
        try:
            job = jobs.claim_next(conn, runner_id)
            if job is None:
                _wait_notify(conn, POLL_SEC)
                continue
            stop_event.clear()
            logger.info("задание #%d: %s %s", job["id"], job["command"], job["params"] or "")
            with _heartbeat(job["id"], runner_id):
                rc, error, result, stop_reason = execute_job(
                    job, settings_path=settings_path, stop_event=stop_event, runner_id=runner_id,
                )
            state = job_state(rc, error, result)
            jobs.finish(conn, job["id"], state=state, exit_code=rc, error=error,
                        result=result, stop_reason=stop_reason)
            pruned = jobs.prune(conn, keep=RETAIN_JOBS)  # retention: история очереди не растёт бесконечно
            logger.info(
                "задание #%d завершено: %s (exit %d)%s",
                job["id"], state, rc,
                f" — {error}" if error else (f" — {stop_reason}" if stop_reason else ""),
            )
            if pruned:
                logger.info("retention: из истории очереди удалено %d старых заданий", pruned)
        except KeyboardInterrupt:
            logger.warning("runner остановлен (Ctrl+C)")
            return 0
        except psycopg.Error:
            logger.exception("ошибка БД в цикле runner — переподключение")
            try:
                conn.close()
            except Exception:  # noqa: BLE001 — соединение уже мертво
                pass
            try:
                conn = db.connect(db.dsn(), attempts=10, delay=2.0)
                conn.autocommit = True
                _listen(conn)
            except RuntimeError:
                logger.exception("не удалось переподключиться к БД — выход")
                return 3


if __name__ == "__main__":
    raise SystemExit(main())
