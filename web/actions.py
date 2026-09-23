"""Файловые операции UI (1.4.0): «↩️ Вернуть» и «🗑️ Удалить» для дубликатов в trash.

ОСТОРОЖНОЕ ОТСТУПЛЕНИЕ от принципа «тонкого клиента» (по прямому запросу
пользователя, решение согласовано): web получает rw-доступ к src/trash и выполняет
ТОЧЕЧНЫЕ файловые операции над ОДНИМ файлом за клик. Полный перенос по-прежнему
делает только движок (этап move с crash-recovery); здесь нет групповых операций
и обходов дерева. Модуль НЕ зависит от streamlit — покрыт integ-тестами напрямую.

«↩️ Вернуть» — перемещает файл из trash на прежнее место (files.path), при
  коллизии — рядом с суффиксом « (restored)». Перезаписывать существующие файлы
  запрещено. Хэши пересчитываются (на этапе move они удалялись) → файл снова
  участвует в будущих run; files.status → 'ok', moved_to сбрасывается.
«🗑️ Удалить» — удаляет файл с диска (он должен лежать внутри trash — защита
  от испорченной строки БД) и вычищает его строки из БД (files → hashes,
  group_members каскадно), уменьшая счётчики групп.

Обе операции пишут событие в журнал (events) с префиксом «web:».
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from psycopg.rows import dict_row

from engine import db
from engine.events import EventLog
from engine.hashing import CorruptFile, FileHashes, compute_entry
from engine.scan import UPSERT_HASH
from engine.settings import Settings, load_settings


class FileOpError(RuntimeError):
    """Понятная ошибка файловой операции UI (без трейсбека — показывается в диалоге)."""


# ----------------------------- чистые функции (покрываются smoke-тестами) -----------------------------

def restore_dest(orig: Path) -> Path:
    """Куда возвращать файл: прежнее место; если занято — name (restored).ext,
    (restored 2), (restored 3)… Перезапись существующих файлов запрещена."""
    if not orig.exists():
        return orig
    stem, ext = os.path.splitext(orig.name)
    parent = orig.parent
    cand = parent / f"{stem} (restored){ext}"
    i = 2
    while cand.exists():
        cand = parent / f"{stem} (restored {i}){ext}"
        i += 1
    return cand


def is_inside(child: Path, parent: Path) -> bool:
    """True, если child == parent или лежит внутри parent (лексически, без symlinks)."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


# ----------------------------- операции -----------------------------

def _moved_row(conn, file_id: int) -> dict:
    """Строка перенесённого файла: moved_to + files.*. Ошибка, если файл не в trash."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT gm.moved_to, f.id AS file_id, f.path, f.size, f.status
            FROM group_members gm JOIN files f ON f.id = gm.file_id
            WHERE gm.file_id = %s AND gm.moved_to IS NOT NULL
            ORDER BY gm.moved_at DESC NULLS LAST LIMIT 1
            """,
            (file_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise FileOpError(
            "Файл не найден среди перенесённых (moved_to пуст). "
            "Кнопки «Вернуть»/«Удалить» действуют только для файлов, уже лежащих в trash."
        )
    return row


def _check_in_trash(moved_to: str, s: Settings) -> None:
    """Защита от испорченной строки БД: физически трогаем только файлы внутри trash."""
    trash_root = Path(s.paths.trash)
    p = Path(moved_to)
    if not is_inside(p, trash_root):
        raise FileOpError(
            f"Отказ: путь {p} вне trash ({trash_root}). Удаление/возврат разрешены "
            f"только для файлов внутри папки дубликатов."
        )


def restore_file(settings_path: str | Path, file_id: int,
                 *, settings: Settings | None = None) -> str:
    """Перенести дубликат из trash на прежнее место (files.path) и вернуть его
    в рабочий индекс (status='ok', хэши пересчитаны). Возвращает текст для UI."""
    s = settings or load_settings(settings_path)
    conn = db.connect(db.dsn())
    try:
        conn.autocommit = True
        row = _moved_row(conn, file_id)
        moved_to, orig = Path(row["moved_to"]), Path(row["path"])
        _check_in_trash(str(moved_to), s)
        if not moved_to.is_file():
            raise FileOpError(f"Файл в trash не найден: {moved_to}")

        dest = restore_dest(orig)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)  # прежняя папка могла быть удалена
            shutil.move(str(moved_to), str(dest))
        except OSError as e:
            raise FileOpError(f"Не удалось переместить {moved_to} → {dest}: {e}") from None

        # Хэши пересчитываются: на этапе move они были удалены, чтобы перенесённые
        # файлы не находились повторно. После возврата файл снова полноправный
        # участник будущих run. Коллизия (restored) меняет путь — проверяем это:
        st = dest.stat()
        entry = compute_entry(
            str(dest), st.st_size, st.st_mtime,
            hash_size=s.hash.hash_size, hash_part_len=s.hash.hash_part_len,
        )
        with conn.transaction():
            with conn.cursor() as cur:
                if isinstance(entry, FileHashes):
                    cur.executemany(
                        UPSERT_HASH,
                        [(row["file_id"], v, ph, part)
                         for v, (ph, part) in enumerate(zip(entry.phashes, entry.parts))],
                    )
                    status, error = "ok", None
                    w, h = entry.width, entry.height
                else:  # CorruptFile — возвращён, но декодировать не удалось
                    assert isinstance(entry, CorruptFile)
                    status, error = "corrupt", entry.error[:500]
                    w = h = None
                cur.execute(
                    """
                    UPDATE files SET size=%s, mtime=%s, width=%s, height=%s,
                                     status=%s, error=%s, indexed_at=now()
                     WHERE id=%s
                    """,
                    (st.st_size, st.st_mtime, w, h, status, error, row["file_id"]),
                )
                # сброс moved_to ТОЛЬКО у той строки группы, что указывала на этот файл
                cur.execute(
                    "UPDATE group_members SET moved_to=NULL, moved_at=NULL "
                    "WHERE file_id=%s AND moved_to=%s",
                    (row["file_id"], str(moved_to)),
                )
        note = "" if dest == orig else f" (прежнее место занято — возвращён как {dest.name})"
        msg = f"Файл возвращён из trash: {moved_to} → {dest}{note}"
        EventLog(conn).info(f"web: {msg}")
        return msg
    finally:
        conn.close()


def delete_file(settings_path: str | Path, file_id: int,
                *, settings: Settings | None = None) -> str:
    """Удалить дубликат с диска (из trash) и вычистить его строки из БД.
    Возвращает текст для UI."""
    s = settings or load_settings(settings_path)
    conn = db.connect(db.dsn())
    try:
        conn.autocommit = True
        row = _moved_row(conn, file_id)
        moved_to = Path(row["moved_to"])
        _check_in_trash(str(moved_to), s)

        missing = not moved_to.is_file()
        if not missing:
            try:
                moved_to.unlink()
            except OSError as e:
                raise FileOpError(f"Не удалось удалить {moved_to}: {e}") from None

        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT group_id FROM group_members WHERE file_id=%s", (file_id,))
                gids = [r[0] for r in cur.fetchall()]
                # files → hashes и group_members удаляются каскадно
                cur.execute("DELETE FROM files WHERE id=%s", (file_id,))
                for gid in gids:
                    cur.execute(
                        "UPDATE groups SET member_count=GREATEST(member_count-1, 0), "
                        "total_size=GREATEST(total_size-%s, 0) WHERE id=%s",
                        (row["size"], gid),
                    )
                if gids:
                    # страховка: группа без членов не должна оставаться в галерее
                    cur.execute("DELETE FROM groups WHERE id=ANY(%s) AND member_count=0", (gids,))
        warn = " (файла уже не было на диске — вычищены только строки БД)" if missing else ""
        msg = f"Файл удалён с диска: {moved_to}{warn}"
        EventLog(conn).warning(f"web: {msg}")
        return msg
    finally:
        conn.close()
