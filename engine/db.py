"""DDL и вспомогательные запросы. Единственная точка доступа движка к PostgreSQL.

DDL идемпотентный (CREATE TABLE IF NOT EXISTS), выполняется при старте движка
(а также командами status/stop). Драйвер — psycopg 3.
"""
from __future__ import annotations

import os
import time
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

DEFAULT_DSN = "postgresql://photo:photo@db:5432/photo"

WORK_STAGES = ("scan", "analyze", "move")


def dsn() -> str:
    """DSN из env PG_DSN (по умолчанию — локальная БД docker compose)."""
    return os.environ.get("PG_DSN", DEFAULT_DSN)


DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS files (
        id          BIGSERIAL PRIMARY KEY,
        path        TEXT UNIQUE NOT NULL,
        size        BIGINT NOT NULL,
        mtime       DOUBLE PRECISION NOT NULL,
        width       INTEGER,
        height      INTEGER,
        indexed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        status      TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok','corrupt','moved')),
        error       TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hashes (
        file_id    BIGINT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        variant    SMALLINT NOT NULL CHECK (variant BETWEEN 0 AND 7),
        phash      BYTEA NOT NULL,
        hash_part  TEXT NOT NULL,
        PRIMARY KEY (file_id, variant)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hashes_hash_part ON hashes (hash_part)",
    "CREATE INDEX IF NOT EXISTS idx_files_status ON files (status)",
    # Миграция 1.3.0: статус 'moved' (файл перенесён в trash или исчез из src).
    # Снимаем любые CHECK-ограничения на status (в старых БД — двухзначный вариант)
    # и ставим каноническое трёхзначное. Идемпотентно: выполняется при каждом старте.
    """
    DO $$ DECLARE r record; BEGIN
      FOR r IN SELECT conname FROM pg_constraint
               WHERE conrelid = 'files'::regclass AND contype = 'c'
                 AND lower(pg_get_constraintdef(oid)) LIKE '%status%' LOOP
        EXECUTE format('ALTER TABLE files DROP CONSTRAINT %I', r.conname);
      END LOOP;
    END $$;
    """,
    "ALTER TABLE files ADD CONSTRAINT files_status_check "
    "CHECK (status IN ('ok','corrupt','moved'))",
    # Миграция 1.4.0: EXIF-свойства файла (словарь строк) для окна «Свойства»
    # в галерее. Заполняется на этапе scan (scan.read_exif) либо лениво из UI.
    "ALTER TABLE files ADD COLUMN IF NOT EXISTS exif JSONB",
    """
    CREATE TABLE IF NOT EXISTS analysis_runs (
        id          BIGSERIAL PRIMARY KEY,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        threshold   INTEGER NOT NULL,
        params      JSONB NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS groups (
        id            BIGSERIAL PRIMARY KEY,
        run_id        BIGINT NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
        member_count  INTEGER NOT NULL,
        total_size    BIGINT NOT NULL DEFAULT 0,
        kept_path     TEXT,
        confirmed     BOOLEAN NOT NULL DEFAULT false,
        info          JSONB NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_groups_run ON groups (run_id)",
    """
    CREATE TABLE IF NOT EXISTS group_members (
        group_id   BIGINT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
        file_id    BIGINT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        role       TEXT NOT NULL CHECK (role IN ('kept','dup')),
        moved_to   TEXT,
        moved_at   TIMESTAMPTZ,
        PRIMARY KEY (group_id, file_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_group_members_file ON group_members (file_id)",
    """
    CREATE TABLE IF NOT EXISTS status (
        id             INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
        stage          TEXT NOT NULL DEFAULT 'idle',
        processed      BIGINT NOT NULL DEFAULT 0,
        total          BIGINT NOT NULL DEFAULT 0,
        current_file   TEXT,
        files_per_sec  DOUBLE PRECISION NOT NULL DEFAULT 0,
        started_at     TIMESTAMPTZ,
        updated_at     TIMESTAMPTZ,
        engine_pid     BIGINT,
        stop_requested BOOLEAN NOT NULL DEFAULT false,
        params         JSONB NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    "INSERT INTO status (id) VALUES (1) ON CONFLICT (id) DO NOTHING",
    """
    CREATE TABLE IF NOT EXISTS events (
        id       BIGSERIAL PRIMARY KEY,
        ts       TIMESTAMPTZ NOT NULL DEFAULT now(),
        level    TEXT NOT NULL,
        message  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts DESC)",
    # Миграция 1.5.0 (Ф1): очередь заданий runner'а. UI/CLI ставят задания
    # (INSERT + pg_notify), сервис runner забирает их атомарно (FOR UPDATE
    # SKIP LOCKED) и выполняет in-process — docs/WHY_NO_BUTTONS.md §5 (вариант A).
    # clean-db в очередь не входит: это прямая операция с подтверждением.
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id            BIGSERIAL PRIMARY KEY,
        command       TEXT NOT NULL CHECK (command IN ('run','scan','analyze','move','undo')),
        params        JSONB NOT NULL DEFAULT '{}'::jsonb,
        state         TEXT NOT NULL DEFAULT 'queued'
                      CHECK (state IN ('queued','running','done','failed','stopped','stale')),
        requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        taken_at      TIMESTAMPTZ,
        finished_at   TIMESTAMPTZ,
        heartbeat_at  TIMESTAMPTZ,
        runner_id     TEXT,
        exit_code     INTEGER,
        error         TEXT,
        result        JSONB
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs (state, id)",
    # Одно «живое» (queued) задание на команду: двойной клик/гонка вкладок
    # отсекаются на уровне БД, а не только в UI.
    "CREATE UNIQUE INDEX IF NOT EXISTS uniq_jobs_queued_command "
    "ON jobs (command) WHERE state = 'queued'",
)


def connect(dsn_str: str | None = None, *, attempts: int = 1, delay: float = 1.0) -> psycopg.Connection:
    """Соединение с повторными попытками (ожидание старта db в docker compose)."""
    d = dsn_str or dsn()
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return psycopg.connect(d, connect_timeout=5)
        except psycopg.OperationalError as e:
            last = e
            if attempt + 1 < attempts:
                time.sleep(delay)
    raise RuntimeError(f"Не удалось подключиться к PostgreSQL ({d}): {last}") from last


def init_db(conn: psycopg.Connection) -> None:
    """Идемпотентное создание схемы."""
    with conn.cursor() as cur:
        for stmt in DDL:
            cur.execute(stmt)
    conn.commit()


# ----------------------------- status (singleton) -----------------------------

def reset_status(conn: psycopg.Connection, *, stage: str, params: dict[str, Any], pid: int) -> None:
    """Сброс статуса в начале команды/этапа."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE status
               SET stage = %s, processed = 0, total = 0, current_file = NULL,
                   files_per_sec = 0, started_at = now(), updated_at = now(),
                   engine_pid = %s, stop_requested = false, params = %s
             WHERE id = 1
            """,
            (stage, pid, Json(params)),
        )


def update_progress(
    conn: psycopg.Connection,
    *,
    stage: str | None = None,
    processed: int | None = None,
    total: int | None = None,
    current_file: str | None = None,
    files_per_sec: float | None = None,
) -> None:
    """Частичное обновление прогресса; None = не менять. updated_at обновляется всегда."""
    sets = ["updated_at = now()"]
    args: list[Any] = []
    for col, val in (
        ("stage", stage),
        ("processed", processed),
        ("total", total),
        ("current_file", current_file),
        ("files_per_sec", files_per_sec),
    ):
        if val is not None:
            sets.append(f"{col} = %s")
            args.append(val)
    args.append(1)
    with conn.cursor() as cur:
        cur.execute(f"UPDATE status SET {', '.join(sets)} WHERE id = %s", args)


def set_stage(conn: psycopg.Connection, stage: str) -> None:
    """Терминальный/переходный статус: done | stopped | error | ..."""
    with conn.cursor() as cur:
        cur.execute("UPDATE status SET stage = %s, updated_at = now() WHERE id = 1", (stage,))


def request_stop(conn: psycopg.Connection) -> bool:
    """Выставить флаг мягкой остановки. Используется и командой stop, и UI."""
    with conn.cursor() as cur:
        cur.execute("UPDATE status SET stop_requested = true, updated_at = now() WHERE id = 1")
        return cur.rowcount == 1


def get_stop_requested(conn: psycopg.Connection) -> bool:
    return bool(scalar(conn, "SELECT stop_requested FROM status WHERE id = 1"))


def get_status_row(conn: psycopg.Connection) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM status WHERE id = 1")
        return cur.fetchone()


def engine_running(conn: psycopg.Connection, stale_sec: float = 15.0) -> bool:
    """Движок считается работающим, если статус свежий и этап — рабочий."""
    return bool(
        scalar(
            conn,
            "SELECT (updated_at > now() - make_interval(secs => %s)) "
            "AND stage = ANY(%s) FROM status WHERE id = 1",
            (stale_sec, list(WORK_STAGES)),
        )
    )


def scalar(conn: psycopg.Connection, sql: str, params: tuple = ()) -> Any:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return first_value(cur.fetchone())


def first_value(row: Any) -> Any:
    """Первое значение строки результата независимо от row_factory.

    Соединения движка возвращают кортежи, пул web — словари (dict_row);
    наивный fetchone()[0] на dict-строке даёт KeyError: 0 (регресс 1.6.1).
    """
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()))
    return row[0]
