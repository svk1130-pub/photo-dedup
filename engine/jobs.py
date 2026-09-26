"""Очередь заданий jobs в PostgreSQL (Ф1) + операции runner'а.

Почему БД, а не Redis/брокер: задание — durable-данные (история прогонов),
как events, а не транзитное сообщение. PostgreSQL покрывает всё одной
сущностью: транзакционный INSERT, атомарный взбор FOR UPDATE SKIP LOCKED,
LISTEN/NOTIFY для мгновенного пробуждения. Минус компонент в стеке — минус
точка отказа (решение зафиксировано в docs/WHY_NO_BUTTONS.md §5, вариант A).

Схема использования:
  * UI/CLI:  enqueue(conn, "run", {...}, scheduled_at=None) → id + NOTIFY канала jobs
             (scheduled_at — Ф3, «не раньше»; None = как можно скорее)
  * UI:      reschedule(conn, job_id, scheduled_at|None) → перенести отсрочку
             cancel_queued(conn, job_id)                 → отменить не начатое
  * runner:  claim_next(conn, runner_id)              → dict | None (атомарно)
             heartbeat(conn, job_id, runner_id)       → метка живости ~раз в 5 с
             finish(conn, job_id, state=..., ...)     → итог в историю очереди
             reap_stale(conn)                         → crash-recovery на старте
  * UI:      snapshot(conn)                           → блок «Очередь runner» Монитора

Долгие команды — run/scan/analyze/move/undo (ставятся в очередь);
clean-db/undo-dry-run остаются прямыми операциями (CLI/webops) — они быстрые
и требуют явного подтверждения.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from . import db

CHANNEL = "jobs"          # NOTIFY-канал: runner спит в conn.notifies() и просыпается мгновенно
COMMANDS = ("run", "scan", "analyze", "move", "undo")
STATES = ("queued", "running", "done", "failed", "stopped", "stale")
GUARD_SEC = 15.0          # порог свежести status.updated_at — тот же, что в UI (STALE_SEC)
TERMINAL_STATES = ("done", "failed", "stopped", "stale")
RETAIN_JOBS = 200         # retention истории очереди: хранить последние N завершённых заданий


def enqueue(
    conn: psycopg.Connection,
    command: str,
    params: dict[str, Any] | None = None,
    *,
    scheduled_at: datetime | None = None,
) -> int:
    """Поставить задание в очередь и разбудить runner (pg_notify).

    Коммит — у вызывающего (в web — пул соединений, в runner — autocommit).
    Дубликат «живого» задания той же команды отсекает уникальный частичный
    индекс uniq_jobs_queued_command (UniqueViolation у вызывающего).
    scheduled_at (Ф3, 1.7.0) — «не раньше»: runner не возьмёт задание до срока
    (фильтр в claim_next); None = выполнить как можно скорее. Передаётся aware
    datetime — БД хранит абсолютный момент (TIMESTAMPTZ), пояс — дело вызывающего.
    """
    if command not in COMMANDS:
        raise ValueError(
            f"команда '{command}' не ставится в очередь (разрешено: {', '.join(COMMANDS)})"
        )
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO jobs (command, params, scheduled_at) VALUES (%s, %s, %s) RETURNING id",
            (command, Json(params or {}), scheduled_at),
        )
        # пул web отдаёт dict-строки (dict_row), движок — кортежи: первый
        # столбец извлекается через db.first_value, а не [0] (фикс 1.6.1)
        job_id: int = int(db.first_value(cur.fetchone()))
        cur.execute("SELECT pg_notify(%s, %s)", (CHANNEL, str(job_id)))
    return job_id


def claim_next(conn: psycopg.Connection, runner_id: str, *, guard_sec: float = GUARD_SEC) -> dict[str, Any] | None:
    """Атомарно взять следующее ДОСТУПНОЕ задание; None — брать нечего.

    «Недоступно» — это: (а) очередь пуста, (б) движок уже работает (single-flight
    guard: свежий рабочий этап в status — выполняется CLI-прогон или задание) и
    (в) Ф3: задание отложено на будущее (scheduled_at > now() — runner молчит
    до срока, точность старта ≈ RUNNER_POLL_SEC). FOR UPDATE SKIP LOCKED делает
    взбор безопасным даже при нескольких runner'ах: одна строка достаётся ровно
    одному. Транзакция короткая (миллисекунды).
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM status WHERE id = 1 AND stage = ANY(%s) "
                "AND updated_at > now() - make_interval(secs => %s)",
                (list(db.WORK_STAGES), guard_sec),
            )
            if cur.fetchone() is not None:
                return None  # движок уже работает — single-flight
            cur.execute(
                """
                UPDATE jobs j
                   SET state = 'running', taken_at = now(), heartbeat_at = now(),
                       runner_id = %s
                  FROM (SELECT id FROM jobs
                         WHERE state = 'queued'
                           AND (scheduled_at IS NULL OR scheduled_at <= now())
                         ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) q
                 WHERE j.id = q.id
                RETURNING j.id, j.command, j.params
                """,
                (runner_id,),
            )
            row = cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "command": row[1], "params": row[2] or {}}


def reschedule(conn: psycopg.Connection, job_id: int, scheduled_at: datetime | None) -> bool:
    """Перенести отсрочку queued-задания (Ф3); None = запустить как можно скорее.

    False — задание уже не queued (взято runner'ом/отменено): менять поздно.
    running намеренно не трогается — остановка выполняемого задания это кнопка
    Стоп (status.stop_requested), а не правка очереди.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET scheduled_at = %s WHERE id = %s AND state = 'queued'",
            (scheduled_at, job_id),
        )
        return cur.rowcount == 1


def cancel_queued(conn: psycopg.Connection, job_id: int) -> bool:
    """Отменить ещё не начатое задание: queued → stopped («не начато», Ф3).

    Задание никогда не выполнялось — файлов это не касается, только строка
    очереди (в истории: state=stopped, error=«отменено пользователем…»).
    Слот uniq-индекса освобождается — ту же команду можно поставить снова.
    False — задание уже не queued (отменять поздно; выполняемое останавливается
    кнопкой Стоп).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE jobs
               SET state = 'stopped', finished_at = now(), exit_code = NULL,
                   error = coalesce(error, 'отменено пользователем (не начато)')
             WHERE id = %s AND state = 'queued'
            """,
            (job_id,),
        )
        return cur.rowcount == 1


def heartbeat(conn: psycopg.Connection, job_id: int, runner_id: str) -> bool:
    """Метка живости выполнения. False — задание больше не running (оборвано/reaped)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET heartbeat_at = now() "
            "WHERE id = %s AND state = 'running' AND runner_id = %s",
            (job_id, runner_id),
        )
        return cur.rowcount == 1


def finish(
    conn: psycopg.Connection,
    job_id: int,
    *,
    state: str,
    exit_code: int,
    error: str | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    """Записать итог задания в историю очереди (jobs.error обрезается до 2000)."""
    if state not in TERMINAL_STATES:
        raise ValueError(f"итоговое состояние задания должно быть одним из {TERMINAL_STATES}, got '{state}'")
    err_text = str(error)[:2000] if error else None
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE jobs
               SET state = %s, finished_at = now(), exit_code = %s,
                   error = %s, result = %s
             WHERE id = %s
            """,
            (state, int(exit_code), err_text, Json(result or {}), job_id),
        )


def reap_stale(conn: psycopg.Connection, *, stale_sec: float = 30.0) -> int:
    """Crash-recovery: running-задания с протухшим heartbeat → stale.

    Вызывается на старте runner'а: контейнер с restart=unless-stopped после
    гибели перезапускается и доклеивает историю очереди. Файлы не трогаются —
    штатный Resume команд (scan/move) продолжит работу с места остановки.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE jobs
               SET state = 'stale', finished_at = now(),
                   error = coalesce(error, 'прогон оборвался: runner перезапущен, heartbeat истёк')
             WHERE state = 'running'
               AND (heartbeat_at IS NULL OR heartbeat_at < now() - make_interval(secs => %s))
            """,
            (stale_sec,),
        )
        return cur.rowcount


def prune(conn: psycopg.Connection, keep: int = RETAIN_JOBS) -> int:
    """Retention истории очереди: оставить только последние `keep` завершённых заданий.

    Вызывается runner'ом после каждого finish() — история не растёт бесконечно,
    «💥 Очистить БД» при этом всё равно вычищает очередь целиком (TRUNCATE_ALL).
    queued/running не трогаются (у них нет finished_at).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM jobs
             WHERE state <> 'queued' AND state <> 'running'
               AND id NOT IN (
                   SELECT id FROM jobs
                    WHERE state <> 'queued' AND state <> 'running'
                    ORDER BY id DESC LIMIT %s
               )
            """,
            (int(keep),),
        )
        return cur.rowcount


def snapshot(conn: psycopg.Connection) -> dict[str, Any]:
    """Данные для блока «Очередь runner» на Мониторе: счётчики, текущее, история.

    Все запросы — LIMIT/агрегаты (правило тонкого клиента).
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT state, count(*) AS n FROM jobs GROUP BY state")
        counts = {r["state"]: int(r["n"]) for r in cur.fetchall()}
        cur.execute(
            "SELECT id, command, state, taken_at, runner_id FROM jobs "
            "WHERE state = 'running' ORDER BY id DESC LIMIT 1"
        )
        running = cur.fetchone()
        cur.execute(
            "SELECT id, command, state, exit_code, error, requested_at, taken_at, finished_at "
            "FROM jobs WHERE state <> 'queued' ORDER BY id DESC LIMIT 5"
        )
        recent = cur.fetchall()
        # Ф3: все queued-задания с деталями отсрочки (их ≤ 5 — uniq-индекс,
        # одна команда = одно queued) — блок «Очередь» Монитора с действиями.
        cur.execute(
            "SELECT id, command, scheduled_at, requested_at "
            "FROM jobs WHERE state = 'queued' ORDER BY id"
        )
        queued_jobs = cur.fetchall()
    return {
        "queued": counts.get("queued", 0),
        "counts": counts,
        "running": running,
        "queued_jobs": queued_jobs,
        "recent": recent,
    }
