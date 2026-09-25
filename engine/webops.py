"""Ручные файловые операции из UI и команды обслуживания: undo, очистка БД.

Здесь живёт ЕДИНСТВЕННАЯ реализация операций, меняющих файлы вне этапа move:

* ``return_file``        — ↩️ вернуть перенесённый дубликат на исходное место;
* ``star_as_original``   — ⭐ назначить файл оригиналом (вернуть его, а текущий
                           оригинал demote-нуть в trash — обмен ролями);
* ``mark_as_duplicate``  — 📋 перенести файл в trash как дубликат (если это был
                           оригинал — новый выбирается автоматически по keep_by);
* ``delete_file``        — 🗑️ удалить файл с диска (и из БД каскадом);
* ``undo_all``           — вернуть ВСЕ перенесённые файлы и очистить результаты;
* ``clean_db``           — очистить результаты и журнал, файлы не трогать.

Пользователи модуля: web/UI (rw-маунты src/trash, short-lived транзакции из
пула) и CLI-команды ``engine undo`` / ``engine clean-db``. Контейнерные пути
берутся из Settings ([paths] — путь, который виден ИЗНУТРИ контейнера).

Инварианты согласованы с engine/move.py:
* перенесённый файл: ``group_members.role='dup'``, ``moved_to`` заполнен,
  ``files.status='moved'``, строки hashes удалены;
* возвращённый/назначенный оригинал: ``moved_to=NULL``, ``status='ok'``,
  хэши появятся после следующего scan (resume видит «ok без хэшей» и
  переиндексирует файл сам);
* crash-recovery move-этапа ищет файлы по имени+размеру, поэтому info.txt
  после ручных операций ПЕРЕзаписывается под фактическое состояние.

Все функции пишут события в журнал (events), чтобы действия из UI были
видимы и воспроизводимы. Вызывающая сторона обязана НЕ запускать эти
операции параллельно с работающим движком (guard: db.engine_running).
"""
from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from psycopg.rows import dict_row

from .analyze import choose_kept
from .db import first_value
from .move import _unique_target, _write_info
from .originals import capture_time_of, collect_used_names, group_dir_name, unique_name

if TYPE_CHECKING:
    from .settings import Settings

logger = logging.getLogger("engine.webops")


class OpError(RuntimeError):
    """Понятная ошибка ручной операции (показывается пользователю UI как есть)."""


# ----------------------------- низкоуровневые помощники -----------------------------

def _event(conn, level: str, message: str) -> None:
    """Запись в журнал (events). Отдельное предложение — виден из UI сразу."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO events (level, message) VALUES (%s, %s)",
                    (level, message[:2000]))
    conn.commit()


def _fetch_member(conn, file_id: int) -> dict:
    """Член группы + строка files. Файл может входить в группы разных прогонов —
    берём самую свежую (макс. group_id: галерея и операции работают с последним run)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT gm.group_id, gm.role, gm.moved_to,
                   f.id AS file_id, f.path, f.size, f.mtime, f.width, f.height, f.status
            FROM group_members gm JOIN files f ON f.id = gm.file_id
            WHERE gm.file_id = %s
            ORDER BY gm.group_id DESC
            LIMIT 1
            """,
            (file_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise OpError(f"Файл #{file_id} не найден в БД (возможно, уже удалён)")
    return row


def _fetch_group(conn, gid: int) -> dict:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT id, run_id, kept_path, info FROM groups WHERE id=%s", (gid,))
        row = cur.fetchone()
    if row is None:
        raise OpError(f"Группа #{gid} не найдена в БД")
    return row


def _group_members(conn, gid: int) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT gm.group_id, gm.file_id, gm.role, gm.moved_to,
                   f.path, f.size, f.mtime, f.width, f.height, f.status
            FROM group_members gm JOIN files f ON f.id = gm.file_id
            WHERE gm.group_id = %s
            ORDER BY (gm.role='kept') DESC, f.size DESC, f.path
            """,
            (gid,),
        )
        return cur.fetchall()


def _group_dir(conn, s: "Settings", g: dict, wanted_name_from: str | None) -> Path:
    """Каталог группы в trash: существующий (info.group_dir) или новый.

    Существующее имя НЕ переименовывается (на него смотрят moved_to других
    членов и crash-recovery). Новый каталог называется по файлу-оригиналу
    (wanted_name_from) с уникализацией по всему trash.
    """
    trash = Path(s.paths.trash)
    info = g.get("info") if isinstance(g.get("info"), dict) else {}
    name = info.get("group_dir")
    if name:
        cand = trash / name
        try:
            if cand.is_dir():
                return cand
        except OSError:
            pass
    base = os.path.basename(wanted_name_from or "")
    name = group_dir_name(wanted_name_from if base else None, s.move.group_name_prefix, g["id"])
    used = collect_used_names(trash)
    return trash / unique_name(name, used)


def _restore_target(path: str) -> Path:
    """Куда возвращать файл: исходный путь, а если он занят — *_restored.*."""
    p = Path(path)
    if not p.exists():
        try:  # родительские папки могли исчезнуть после переноса
            p.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return p
    stem, ext = p.stem, p.suffix
    cand = p.with_name(f"{stem}_restored{ext}")
    i = 1
    while cand.exists():
        cand = p.with_name(f"{stem}_restored_{i}{ext}")
        i += 1
    try:
        cand.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return cand


def _rewrite_info(conn, s: "Settings", g: dict, dest: Path, members: list[dict]) -> None:
    """Перезаписать info.txt под фактическое состояние после ручных операций."""
    kept = next((m for m in members if m["role"] == "kept" and m["moved_to"] is None), None)
    moved_now = [(m, m["moved_to"]) for m in members
                 if m["moved_to"] and os.path.dirname(m["moved_to"]) == str(dest)]
    kept_stub = kept or {"path": "(оригинал не назначен)", "size": 0, "width": 0, "height": 0}
    _write_info(dest, g["id"], g["run_id"], kept_stub, moved_now,
                s.move.keep_by, s.analyze.threshold,
                capture=None)
    if kept is not None:  # в шапке отмечаем, что файл менялся вручную
        note = (f"\nПримечание: состав папки менялся вручную из UI "
                f"({datetime.now(timezone.utc).isoformat(timespec='seconds')}).\n")
        with open(dest / "info.txt", "a", encoding="utf-8") as f:
            f.write(note)


def _cleanup_dir(conn, s: "Settings", g: dict, dest: Path) -> str:
    """Папка группы после операции: переписать info.txt или удалить пустую."""
    try:
        leftover = [p.name for p in dest.iterdir()
                    if p.is_file() and p.name.lower() != "info.txt"]
    except OSError:
        return f"папка группы недоступна: {dest}"
    if leftover:
        try:
            _rewrite_info(conn, s, g, dest, _group_members(conn, g["id"]))
            return f"info.txt обновлён ({dest.name}: файлов {len(leftover)})"
        except OSError as e:
            return f"info.txt не переписан ({dest.name}): {e}"
    try:
        (dest / "info.txt").unlink(missing_ok=True)
        dest.rmdir()
        return f"пустая папка группы удалена: {dest.name}"
    except OSError as e:
        return f"папка группы {dest.name} не удалена: {e}"


def _demote_to_trash(conn, s: "Settings", m: dict, dest: Path) -> str:
    """Перенести файл члена (в src) в папку группы + перевести в роль dup.

    Возвращает фактический путь в trash. Вызывается ВНУТРИ отработавшего
    файлового перемещения (файловая операция — до записи в БД, как в move.py).
    Папка группы создаётся на месте (1.4.3): до этого исправления первый
    перенос в ещё не существующую папку (штатный кейс ручного режима — до
    `engine move` папок в trash нет) падал с FileNotFoundError, как на
    этапе move (move.py: os.makedirs перед переносом).
    """
    target = _unique_target(dest, os.path.basename(m["path"]))
    try:
        dest.mkdir(parents=True, exist_ok=True)  # идемпотентно, как move.py
        shutil.move(m["path"], str(target))
    except (shutil.Error, OSError) as e:
        raise OpError(f"Не удалось перенести {m['path']} → {target}: {e}") from None
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE group_members SET role='dup', moved_to=%s, moved_at=now() "
                "WHERE group_id=%s AND file_id=%s",
                (str(target), _gid_of_member(m), m["file_id"]),
            )
            cur.execute("DELETE FROM hashes WHERE file_id=%s", (m["file_id"],))
            cur.execute(
                "UPDATE files SET status='moved', error='перенесён в trash вручную (UI)' "
                "WHERE id=%s",
                (m["file_id"],),
            )
    return str(target)


def _gid_of_member(m: dict) -> int:
    return m["group_id"]


# ----------------------------- операции над одним файлом -----------------------------

def return_file(conn, s: "Settings", file_id: int) -> str:
    """↩️ Вернуть перенесённый дубликат из trash на исходное место.

    Роль остаётся 'dup' (файл по-прежнему дубликат): следующий `move` перенесёт
    его снова, а ⭐ в галерее назначит оригиналом. Если исходный путь занят —
    файл возвращается рядом с суффиксом _restored и files.path обновляется.
    """
    m = _fetch_member(conn, file_id)
    if not m["moved_to"]:
        raise OpError(f"Файл не перенесён — возвращать нечего: {m['path']}")
    if not os.path.exists(m["moved_to"]):
        raise OpError(f"Файл в trash не найден: {m['moved_to']} (удалён или перемещён вручную?)")
    target = _restore_target(m["path"])
    try:
        shutil.move(m["moved_to"], str(target))
    except (shutil.Error, OSError) as e:
        raise OpError(f"Не удалось вернуть {m['moved_to']} → {target}: {e}") from None
    with conn.transaction():
        with conn.cursor() as cur:
            # moved_to гасится во ВСЕХ группах, куда входил файл (разные прогоны)
            cur.execute(
                "UPDATE group_members SET moved_to=NULL, moved_at=NULL "
                "WHERE file_id=%s",
                (file_id,),
            )
            cur.execute(
                "UPDATE files SET status='ok', error=NULL, path=%s WHERE id=%s",
                (str(target), file_id),
            )
    msg = f"↩️ #{file_id}: {m['moved_to']} → {target}"
    g = _fetch_group(conn, m["group_id"])
    _event(conn, "info", msg + " · " + _cleanup_dir(conn, s, g, Path(m["moved_to"]).parent))
    return str(target)


def star_as_original(conn, s: "Settings", file_id: int) -> str:
    """⭐ Назначить файл оригиналом группы.

    Файл возвращается на исходное место (если был в trash), текущий оригинал
    переносится в trash группы (обмен ролями). groups.kept_path обновляется.
    """
    m = _fetch_member(conn, file_id)
    if m["role"] == "kept" and not m["moved_to"] and m["status"] == "ok":
        raise OpError(f"Файл уже оригинал: {m['path']}")
    g = _fetch_group(conn, m["group_id"])
    dest = _group_dir(conn, s, g, m["path"])

    # 1) демотируем текущего оригинала (если есть и это другой файл)
    members = _group_members(conn, m["group_id"])
    cur_kept = next((x for x in members
                     if x["role"] == "kept" and x["moved_to"] is None
                     and x["file_id"] != file_id), None)
    demoted_to = None
    if cur_kept is not None:
        if os.path.exists(cur_kept["path"]):
            demoted_to = _demote_to_trash(conn, s, cur_kept, dest)
        else:  # файла нет на диске — только чиним БД
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE group_members SET role='dup', moved_at=COALESCE(moved_at, now()) "
                        "WHERE group_id=%s AND file_id=%s",
                        (m["group_id"], cur_kept["file_id"]),
                    )
                    cur.execute("DELETE FROM hashes WHERE file_id=%s", (cur_kept["file_id"],))
                    cur.execute(
                        "UPDATE files SET status='moved', error='отсутствует в src (отмечено UI)' "
                        "WHERE id=%s",
                        (cur_kept["file_id"],),
                    )

    # 2) возвращаем/назначаем нового оригинала
    new_path = m["path"]
    if m["moved_to"]:
        if not os.path.exists(m["moved_to"]):
            raise OpError(f"Файл в trash не найден: {m['moved_to']}")
        target = _restore_target(m["path"])
        try:
            shutil.move(m["moved_to"], str(target))
        except (shutil.Error, OSError) as e:
            raise OpError(f"Не удалось вернуть {m['moved_to']} → {target}: {e}") from None
        new_path = str(target)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE group_members SET role='kept', moved_to=NULL, moved_at=NULL "
                "WHERE group_id=%s AND file_id=%s",
                (m["group_id"], file_id),
            )
            cur.execute(
                "UPDATE group_members SET moved_to=NULL, moved_at=NULL WHERE file_id=%s",
                (file_id,),
            )
            cur.execute(
                "UPDATE files SET status='ok', error=NULL, path=%s WHERE id=%s",
                (new_path, file_id),
            )
            cur.execute("UPDATE groups SET kept_path=%s WHERE id=%s", (new_path, m["group_id"]))
    msg = f"⭐ #{file_id}: оригинал группы #{m['group_id']} — {new_path}"
    if demoted_to:
        msg += f" · прежний оригинал перенесён в {demoted_to}"
    _event(conn, "info", msg + " · " + _cleanup_dir(conn, s, g, dest))
    return new_path


def mark_as_duplicate(conn, s: "Settings", file_id: int) -> str:
    """📋 Перенести файл в trash как дубликат.

    Если файл был оригиналом группы — новый оригинал выбирается автоматически
    по текущему keep_by (пользователь может затем переназначить его кнопкой ⭐).
    Папка группы называется по оригиналу (конвенция move.py): если переносим
    сам оригинал — новый выбирается ДО вычисления папки, иначе имя папки не
    совпало бы с той, что позже создаст этап move (группа дробилась бы между
    двумя папками в trash; 1.4.3).
    """
    m = _fetch_member(conn, file_id)
    if m["moved_to"]:
        raise OpError(f"Файл уже в trash: {m['moved_to']}")
    g = _fetch_group(conn, m["group_id"])
    was_kept = m["role"] == "kept"

    # Кандидаты в новый оригинал — ДО демотирования: файл file_id исключён,
    # остальные члены не меняются (записи в БД — после файлового переноса).
    new_kept: dict | None = None
    if was_kept:
        rest = [x for x in _group_members(conn, m["group_id"])
                if x["moved_to"] is None and x["file_id"] != file_id
                and os.path.exists(x["path"])]
        if rest:
            for x in rest:
                try:
                    st = os.stat(x["path"])
                except OSError:
                    st = None
                t, src_lbl = capture_time_of(x["path"], x.get("mtime"), st=st)
                x["capture_time"], x["capture_src"] = t, src_lbl
                x["ctime"] = float(st.st_ctime) if st is not None else None
            new_kept = choose_kept(rest, s.move.keep_by)

    # Имя папки: по новому оригиналу (переносили старый), по текущему оригиналу
    # группы (переносили дубликат), в fallback — по самому файлу.
    donor = (new_kept or {}).get("path") \
        or (None if was_kept else g.get("kept_path")) or m["path"]
    dest = _group_dir(conn, s, g, donor)

    moved_to: str
    if os.path.exists(m["path"]):
        moved_to = _demote_to_trash(conn, s, m, dest)
    else:  # файла нет на диске — только чиним БД
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE group_members SET role='dup', moved_at=COALESCE(moved_at, now()) "
                    "WHERE group_id=%s AND file_id=%s",
                    (m["group_id"], file_id),
                )
                cur.execute("DELETE FROM hashes WHERE file_id=%s", (file_id,))
                cur.execute(
                    "UPDATE files SET status='moved', error='отсутствует в src (отмечено UI)' "
                    "WHERE id=%s",
                    (file_id,),
                )
        moved_to = "(файл отсутствовал на диске)"

    msg = f"📋 #{file_id}: {m['path']} → {moved_to}"
    if was_kept:
        # Новый оригинал уже выбран выше (до вычисления папки); здесь — только
        # записи в БД (тот же критерий выбора, что на этапе move).
        if new_kept is not None:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE group_members SET role='kept' WHERE group_id=%s AND file_id=%s",
                        (m["group_id"], new_kept["file_id"]),
                    )
                    cur.execute("UPDATE groups SET kept_path=%s WHERE id=%s",
                                (new_kept["path"], m["group_id"]))
            msg += f" · новый оригинал автоматически: {new_kept['path']}"
        else:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("UPDATE groups SET kept_path=NULL WHERE id=%s", (m["group_id"],))
            msg += " · в группе не осталось файлов в src"
    _event(conn, "info", msg + " · " + _cleanup_dir(conn, s, g, dest))
    return moved_to


def delete_file(conn, s: "Settings", file_id: int) -> str:
    """🗑️ Удалить файл с диска (trash или src) и вычистить его строки из БД.

    files удаляется каскадом (hashes, group_members), счётчики группы
    пересчитываются; опустевшая группа удаляется, пустая папка группы — тоже.
    """
    m = _fetch_member(conn, file_id)
    disk_path = m["moved_to"] or m["path"]
    if os.path.exists(disk_path):
        try:
            os.unlink(disk_path)
        except OSError as e:
            raise OpError(f"Не удалось удалить {disk_path}: {e}") from None
    gids = [m["group_id"]]
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("DELETE FROM files WHERE id=%s", (file_id,))
            for gid in gids:
                cur.execute(
                    """
                    UPDATE groups SET
                        member_count = (SELECT count(*) FROM group_members WHERE group_id=%s),
                        total_size = COALESCE((
                            SELECT sum(f.size) FROM group_members gm
                            JOIN files f ON f.id = gm.file_id WHERE gm.group_id=%s
                        ), 0)
                    WHERE id=%s
                    """,
                    (gid, gid, gid),
                )
            cur.execute("DELETE FROM groups WHERE id=%s AND member_count=0", (m["group_id"],))
    gone = "файл отсутствовал на диске, " if not os.path.exists(disk_path) else ""
    msg = f"🗑️ #{file_id}: удалён {gone}{disk_path}"
    g = _fetch_group(conn, m["group_id"])
    _event(conn, "warning", msg + " · " + _cleanup_dir(conn, s, g, Path(disk_path).parent))
    return str(disk_path)


# ----------------------------- обслуживание: undo / очистка -----------------------------

TRUNCATE_ALL = """
    TRUNCATE events, jobs, group_members, groups, analysis_runs, hashes, files RESTART IDENTITY
"""

_RESET_STATUS = """
    UPDATE status SET stage='idle', processed=0, total=0, current_file=NULL,
           files_per_sec=0, started_at=NULL, updated_at=now(), engine_pid=NULL,
           stop_requested=false, params='{}'::jsonb
    WHERE id = 1
"""


def truncate_results(conn) -> None:
    """Очистить результаты прогонов и журнал (файлы на диске не трогаются)."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(TRUNCATE_ALL)
            cur.execute(_RESET_STATUS)


def clean_db(conn) -> str:
    """💥 Очистить БД: результаты прогонов + журнал; файлы на диске не трогаются."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM files")
        n_files = int(first_value(cur.fetchone()))
        cur.execute("SELECT count(*) FROM group_members WHERE moved_to IS NOT NULL")
        n_moved = int(first_value(cur.fetchone()))
    truncate_results(conn)
    msg = (f"💥 БД очищена: удалены результаты ({n_files} строк индекса, "
           f"{n_moved} перенесённых), история очереди заданий и журнал сброшены. "
           "Файлы на диске не тронуты.")
    if n_moved:
        msg += (" ВНИМАНИЕ: перенесённые файлы остались в trash — вернуть их можно "
                "только вручную (Undo был сброшен очисткой БД).")
    _event(conn, "warning", msg)
    return msg


def _cleanup_all_dirs(conn, s: "Settings") -> list[str]:
    """Удалить пустые папки групп и legacy group_N после undo."""
    notes: list[str] = []
    trash = Path(s.paths.trash)
    try:
        dirs = [p for p in trash.iterdir() if p.is_dir()]
    except OSError:
        return notes
    for d in sorted(dirs):
        try:
            leftover = [p.name for p in d.iterdir() if p.is_file() and p.name.lower() != "info.txt"]
            subdirs = [p for p in d.iterdir() if p.is_dir()]
        except OSError:
            continue
        if leftover or subdirs:
            continue
        try:
            (d / "info.txt").unlink(missing_ok=True)
            d.rmdir()
            notes.append(f"пустая папка удалена: {d.name}")
        except OSError:
            pass
    return notes


def undo_all(conn, s: "Settings", *, dry: bool = False) -> tuple[int, list[str], list[str]]:
    """Отмена всех переносов: каждый файл из trash возвращается на исходное место.

    Возвращает (возвращено файлов, ПРОБЛЕМЫ, заметки об очистке папок).
    Если проблем НЕТ — результаты и журнал очищаются (TRUNCATE), как и
    задумано в сценарии «Отменить всё». При проблемах очистка НЕ выполняется:
    исправьте их и повторите undo — уже возвращённые файлы при повторном
    запуске пропускаются (moved_to=NULL).

    dry=True — только план: ничего не переносится и не очищается.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT gm.file_id, gm.group_id, gm.moved_to, f.path, f.size
            FROM group_members gm JOIN files f ON f.id = gm.file_id
            WHERE gm.moved_to IS NOT NULL
            ORDER BY gm.group_id, f.path
            """
        )
        rows = cur.fetchall()

    if dry:
        return len(rows), [], [f"вернуть {r['moved_to']} → {r['path']}" for r in rows[:20]]

    restored, problems = 0, []
    seen: set[int] = set()
    for r in rows:
        if r["file_id"] in seen:  # файл мог входить в группы разных прогонов
            continue
        seen.add(r["file_id"])
        if not os.path.exists(r["moved_to"]):
            problems.append(f"нет в trash: {r['moved_to']} (файл #{r['file_id']})")
            continue
        target = _restore_target(r["path"])
        try:
            shutil.move(r["moved_to"], str(target))
        except (shutil.Error, OSError) as e:
            problems.append(f"{r['moved_to']} → {target}: {e}")
            continue
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE group_members SET moved_to=NULL, moved_at=NULL WHERE file_id=%s",
                    (r["file_id"],),
                )
                cur.execute(
                    "UPDATE files SET status='ok', error=NULL, path=%s WHERE id=%s",
                    (str(target), r["file_id"]),
                )
        restored += 1

    notes = _cleanup_all_dirs(conn, s)
    if problems:
        _event(conn, "warning",
               f"undo: возвращено {restored}, проблем {len(problems)} — очистка БД отложена. "
               + "; ".join(problems[:5]))
        logger.warning("undo: возвращено %d, проблем %d — очистка отложена", restored, len(problems))
        return restored, problems, notes

    truncate_results(conn)
    _event(conn, "warning",
           f"undo: все переносы отменены ({restored} файлов возвращены в src); "
           f"результаты и журнал очищены. " + "; ".join(notes[:5]))
    logger.warning("undo: возвращено %d файлов, БД очищена", restored)
    return restored, [], notes
