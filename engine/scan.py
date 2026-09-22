"""Этап 1 — сканирование: индексация файлов и вычисление 8 phash-вариантов.

Воркеры (ThreadPoolExecutor) ТОЛЬКО вычисляют хэши; все записи в БД делает
главный поток пачками batch_size в одной транзакции (executemany).
Status обновляется раз в батч/таймер, не на каждый файл.

Resume: файл пропускается, если в БД есть запись с тем же (path, size, mtime)
и не потеряны хэши. Изменившийся файл переиндексируется (сначала удаляются
старые строки хэшей). --force — принудительная переиндексация.

Остановка: флаг stop_requested проверяется перед постановкой каждого файла;
текущие in-flight файлы доделываются, батч коммитится, статус → stopped.
"""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from psycopg.rows import dict_row

from .db import reset_status, set_stage, update_progress
from .hashing import CorruptFile, Entry, FileHashes, compute_entry
from .settings import Settings

if TYPE_CHECKING:  # без циклического импорта в рантайме
    from .cli import EngineContext

logger = logging.getLogger("engine.scan")

UPSERT_FILE = """
    INSERT INTO files (path, size, mtime, width, height, status, error, indexed_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, now())
    ON CONFLICT (path) DO UPDATE SET
        size = EXCLUDED.size,
        mtime = EXCLUDED.mtime,
        width = EXCLUDED.width,
        height = EXCLUDED.height,
        status = EXCLUDED.status,
        error = EXCLUDED.error,
        indexed_at = now()
"""

UPSERT_HASH = """
    INSERT INTO hashes (file_id, variant, phash, hash_part)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (file_id, variant) DO UPDATE SET
        phash = EXCLUDED.phash,
        hash_part = EXCLUDED.hash_part
"""


@dataclass(slots=True)
class ExistingFile:
    id: int
    size: int
    mtime: float
    status: str


@dataclass(slots=True)
class ScanStats:
    total: int = 0       # предварительный подсчёт (для прогресс-бара)
    found: int = 0       # отобрано файлов во время работы (= processed)
    hashed: int = 0      # прогнали через хэширование (включая corrupt)
    skipped: int = 0     # resume: без изменений
    corrupt: int = 0
    stopped: bool = False


def iter_files(
    root: Path, trash: Path, extensions: frozenset[str], recursive: bool
) -> Iterator[str]:
    """Рекурсивный обход: пропускаем trash, скрытые директории и симлинки
    (os.walk с followlinks=False + явная проверка — защита от циклов)."""
    try:
        trash_res = trash.resolve()
    except OSError:
        trash_res = trash
    for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
        dp = Path(dirpath)
        keep: list[str] = []
        for d in sorted(dirnames):
            if d.startswith("."):
                continue
            full = dp / d
            if full.is_symlink():
                continue
            try:
                if full.resolve().is_relative_to(trash_res):
                    continue
            except OSError:
                continue
            keep.append(d)
        dirnames[:] = keep
        if not recursive:
            dirnames[:] = []
        for fname in sorted(filenames):
            if fname.startswith("."):
                continue
            if os.path.splitext(fname)[1].lower() not in extensions:
                continue
            fp = os.path.join(dirpath, fname)
            if os.path.islink(fp):
                continue
            yield fp


def count_files(root: Path, trash: Path, extensions: frozenset[str], recursive: bool) -> int:
    """Быстрый проход без хэширования — точный знаменатель прогресс-бара."""
    return sum(1 for _ in iter_files(root, trash, extensions, recursive))


def _load_existing(conn) -> dict[str, ExistingFile]:
    """Весь индекс файлов в память (путь → строка). Большие выборки — потоковым
    серверным курсором (itersize есть только у него в psycopg 3)."""
    out: dict[str, ExistingFile] = {}
    with conn.transaction():
        with conn.cursor(name="scan_existing", row_factory=dict_row) as cur:
            cur.itersize = 50000
            cur.execute("SELECT id, path, size, mtime, status FROM files")
            for r in cur:
                out[r["path"]] = ExistingFile(r["id"], r["size"], r["mtime"], r["status"])
    return out


def _load_ids_with_hashes(conn) -> set[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT file_id FROM hashes")
        return {r[0] for r in cur}


def _write_buffer(conn, buffer: list[Entry], existing: dict[str, ExistingFile],
                  with_hashes: set[int]) -> None:
    """Одна транзакция: upsert files → маппинг id → очистка старых хэшей → вставка хэшей."""
    file_rows: list[tuple] = []
    clear_ids: list[int] = []
    for e in buffer:
        if isinstance(e, FileHashes):
            file_rows.append((e.path, e.size, e.mtime, e.width, e.height, "ok", None))
        else:
            file_rows.append((e.path, e.size, e.mtime, None, None, "corrupt", e.error[:500]))
        if e.old_id is not None:
            clear_ids.append(e.old_id)

    with conn.cursor() as cur:
        cur.executemany(UPSERT_FILE, file_rows)
        cur.execute("SELECT id, path FROM files WHERE path = ANY(%s)", ([e.path for e in buffer],))
        id_map = {path: fid for fid, path in cur.fetchall()}
        if clear_ids:
            cur.execute("DELETE FROM hashes WHERE file_id = ANY(%s)", (clear_ids,))
        hash_rows = [
            (id_map[e.path], v, ph, part)
            for e in buffer
            if isinstance(e, FileHashes)
            for v, (ph, part) in enumerate(zip(e.phashes, e.parts))
        ]
        if hash_rows:
            cur.executemany(UPSERT_HASH, hash_rows)
        for e in buffer:  # обновляем in-memory кэш существующих
            fid = id_map[e.path]
            status = "ok" if isinstance(e, FileHashes) else "corrupt"
            existing[e.path] = ExistingFile(fid, e.size, e.mtime, status)
            if status == "ok":
                with_hashes.add(fid)


def _report(conn_ctl, stats: ScanStats, speed: float | None, last_path: str | None) -> None:
    update_progress(
        conn_ctl,
        stage="scan",
        processed=stats.found,
        total=stats.total,
        current_file=last_path,
        files_per_sec=float(speed or 0.0),
    )


def run_scan(ctx: "EngineContext", *, force: bool = False) -> ScanStats:
    s: Settings = ctx.settings
    conn, conn_ctl = ctx.conn, ctx.conn_ctl
    exts = frozenset(e.lower() for e in s.scan.extensions)
    src = Path(s.paths.src)
    trash = Path(s.paths.trash)

    reset_status(conn_ctl, stage="scan",
                 params=ctx.params_payload({"force": force}), pid=os.getpid())

    stats = ScanStats()
    stats.total = count_files(src, trash, exts, s.scan.recursive)
    ctx.log.info(
        f"scan: старт; найдено {stats.total} файлов-кандидатов "
        f"(threads={s.scan.threads}, batch={s.scan.batch_size}, force={force})"
    )

    existing = _load_existing(conn)
    with_hashes = _load_ids_with_hashes(conn)

    ema_speed: float | None = None
    last_flush_t = time.monotonic()
    last_status_t = 0.0
    last_path: str | None = None
    buffer: list[Entry] = []

    def flush() -> None:
        nonlocal ema_speed, last_flush_t
        if not buffer:
            return
        n = len(buffer)
        with conn.transaction():
            _write_buffer(conn, buffer, existing, with_hashes)
        now = time.monotonic()
        dt = now - last_flush_t
        if dt > 0:
            inst = n / dt
            ema_speed = inst if ema_speed is None else 0.7 * ema_speed + 0.3 * inst
        last_flush_t = now
        buffer.clear()

    pool_size = max(1, s.scan.threads)
    inflight: set[Future] = set()
    exhausted = False
    it = iter_files(src, trash, exts, s.scan.recursive)

    with ThreadPoolExecutor(max_workers=pool_size, thread_name_prefix="hash") as pool:
        while True:
            # дозаполняем очередь; перед КАЖДЫМ файлом — проверка флага остановки
            while not exhausted and len(inflight) < pool_size * 4:
                if ctx.should_stop():
                    break
                try:
                    path = next(it)
                except StopIteration:
                    exhausted = True
                    break
                stats.found += 1
                try:
                    st = os.stat(path)
                    size, mtime = st.st_size, st.st_mtime
                except OSError as e:
                    buffer.append(CorruptFile(path, 0, 0.0, f"stat: {e}"))
                    stats.hashed += 1
                    stats.corrupt += 1
                    last_path = path
                    if len(buffer) >= s.scan.batch_size:
                        flush()
                    continue

                ex = existing.get(path)
                if (ex is not None and not force
                        and ex.size == size and ex.mtime == mtime
                        and (ex.status == "corrupt" or ex.id in with_hashes)):
                    stats.skipped += 1  # resume: без изменений
                    continue
                old_id = None
                if ex is not None and (
                    force or ex.size != size or ex.mtime != mtime or ex.id not in with_hashes
                ):
                    old_id = ex.id  # изменился → сначала удалить старые хэши
                inflight.add(
                    pool.submit(
                        compute_entry, path, size, mtime,
                        hash_size=s.hash.hash_size, hash_part_len=s.hash.hash_part_len,
                        old_id=old_id,
                    )
                )

            if not inflight:
                if exhausted or ctx.should_stop():
                    break
                continue

            done, inflight = wait(inflight, timeout=0.5, return_when=FIRST_COMPLETED)
            for fut in done:
                e: Entry = fut.result()  # воркер не бросает исключений
                buffer.append(e)
                last_path = e.path
                stats.hashed += 1
                if isinstance(e, CorruptFile):
                    stats.corrupt += 1
                if len(buffer) >= s.scan.batch_size:
                    flush()

            now = time.monotonic()
            if now - last_status_t >= 0.5:
                last_status_t = now
                _report(conn_ctl, stats, ema_speed, last_path)

        # --- мягкая остановка: отменяем не начатые, доделываем in-flight ---
        if ctx.should_stop():
            stats.stopped = True
            for f in list(inflight):
                f.cancel()
            while inflight:
                done, inflight = wait(inflight, timeout=1.0, return_when=FIRST_COMPLETED)
                for fut in done:
                    e = fut.result()
                    buffer.append(e)
                    last_path = e.path
                    stats.hashed += 1
                    if isinstance(e, CorruptFile):
                        stats.corrupt += 1
                    if len(buffer) >= s.scan.batch_size:
                        flush()
            ctx.log.warning(
                f"scan: остановка по флагу; обработано {stats.found}, закоммичено {stats.hashed}"
            )

    flush()
    _report(conn_ctl, stats, ema_speed, last_path)
    set_stage(conn_ctl, "stopped" if stats.stopped else "done")
    ctx.log.info(
        f"scan: завершено: проиндексировано {stats.hashed}, "
        f"пропущено (resume) {stats.skipped}, битых {stats.corrupt}"
    )
    return stats
