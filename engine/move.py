"""Этап 3 — перенос: перемещение дубликатов в trash/group_{id}/ через shutil.move.

Режим: auto — все группы последнего run; manual — только confirmed=true
(кнопка в UI или move --group-id). Dry Run всегда главнее: физически ничего
не переносится, только фиксируются планируемые действия в events.

«Оригинал» внутри группы выбирается по keep_by (size|pixels) НА МОМЕНТ
переноса (роль из analyze — лишь предварительная подсказка для галереи).
Коллизии имён — суффикс _1, _2…; рядом с копиями пишется info.txt.

Отказоустойчивость: БД-транзакция коммитится ПОСЛЕ файловых операций, поэтому
при падении между ними повторный запуск восстанавливает состояние: если файла
нет в src, но он лежит в trash/group_{id} с тем же именем — moved_to доносится.
"""
from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from psycopg.types.json import Json
from psycopg.rows import dict_row

from .analyze import choose_kept
from .db import reset_status, set_stage, update_progress

if TYPE_CHECKING:
    from .cli import EngineContext

logger = logging.getLogger("engine.move")


class MoveError(RuntimeError):
    """Ошибка этапа переноса (например, группа не найдена)."""


def _unique_target(dest: Path, name: str) -> Path:
    """Коллизии имён внутри group_{id}: name.ext → name_1.ext → name_2.ext …"""
    base, ext = os.path.splitext(name)
    cand = dest / name
    i = 1
    while cand.exists():
        cand = dest / f"{base}_{i}{ext}"
        i += 1
    return cand


def _write_info(dest: Path, gid: int, run_id: int, kept: dict, moved_now: list[tuple[dict, str]],
                keep_by: str, threshold: int) -> None:
    lines = [
        "Photo Dedup — отчёт о переносе",
        f"Группа: {gid} (run {run_id})",
        f"Дата: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"Порог (расстояние Хэмминга): {threshold}",
        f"Критерий оригинала (keep_by): {keep_by}",
        f"Оставлен оригинал: {kept['path']} ({kept['size']} байт, "
        f"{kept['width']}x{kept['height']})",
        "",
        "Файлы, перемещённые в эту папку:",
    ]
    for m, to in moved_now:
        lines.append(f"  [dup] {m['path']} ({m['size']} байт) -> {os.path.basename(to)}")
    if not moved_now:
        lines.append("  (в этом проходе ничего не перемещалось)")
    (dest / "info.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _finalize_group_db(ctx: "EngineContext", conn, gid: int, members: list[dict],
                       dest: Path, keep_by: str, threshold: int,
                       *, kept: dict | None, moved_now: list[tuple[dict, str]]) -> None:
    """Одна транзакция: роли, moved_to/moved_at, удаление хэшей перенесённых/пропавших,
    обновление groups.kept_path/info. Вызывается после успешных файловых операций."""
    moved_ids = [m["file_id"] for m, _ in moved_now]
    missing_ids = [
        m["file_id"] for m in members
        if m["moved_to"] is None and not os.path.exists(m["path"])
    ]
    with conn.transaction():
        with conn.cursor() as cur:
            if moved_now:
                cur.executemany(
                    "UPDATE group_members SET role='dup', moved_to=%s, moved_at=now() "
                    "WHERE group_id=%s AND file_id=%s",
                    [(to, gid, m["file_id"]) for m, to in moved_now],
                )
            if kept is not None:
                cur.execute(
                    "UPDATE group_members SET role='kept', moved_to=NULL, moved_at=NULL "
                    "WHERE group_id=%s AND file_id=%s",
                    (gid, kept["file_id"]),
                )
            ids_del = moved_ids + missing_ids
            if ids_del:
                # перенесённые/исчезнувшие файлы больше не в src — их хэши не должны
                # участвовать в будущих run (иначе дубликаты находились бы повторно)
                cur.execute("DELETE FROM hashes WHERE file_id = ANY(%s)", (ids_del,))
            cur.execute("SELECT info FROM groups WHERE id=%s", (gid,))
            row = cur.fetchone()
            info = dict(row[0] or {})
            info.update({
                "moved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "moved_count": (info.get("moved_count") or 0) + len(moved_now),
                "keep_by": keep_by,
                "threshold": threshold,
                "dest": str(dest),
            })
            if kept is not None:
                cur.execute("UPDATE groups SET kept_path=%s, info=%s WHERE id=%s",
                            (kept["path"], Json(info), gid))
            else:
                cur.execute("UPDATE groups SET info=%s WHERE id=%s", (Json(info), gid))


def _process_group(ctx: "EngineContext", conn, trash: Path, g: dict, run_id: int,
                   keep_by: str, dry: bool, *, threshold: int) -> None:
    gid = g["id"]
    dest = trash / f"group_{gid}"
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT gm.file_id, gm.role, gm.moved_to, f.path, f.size, f.width, f.height
            FROM group_members gm JOIN files f ON f.id = gm.file_id
            WHERE gm.group_id = %s
            ORDER BY f.size DESC, f.path
            """,
            (gid,),
        )
        members = cur.fetchall()

    already = [m for m in members if m["moved_to"] is not None]
    present = [m for m in members if m["moved_to"] is None and os.path.exists(m["path"])]
    missing = [m for m in members if m["moved_to"] is None and not os.path.exists(m["path"])]

    if not present and not missing and already:
        ctx.log.info(f"move: группа #{gid} уже перенесена ранее — пропуск")
        return

    # Crash recovery: файл исчез из src, но лежит в trash/group_{id} → доносим moved_to
    for m in missing:
        cand = dest / os.path.basename(m["path"])
        if cand.exists():
            m["moved_to"] = str(cand)
            already.append(m)
            ctx.log.info(f"move: группа #{gid}: восстановлен moved_to после сбоя: {cand}")
        else:
            ctx.log.warning(f"move: группа #{gid}: файл отсутствует, пропущен: {m['path']}")

    if not present:
        _finalize_group_db(ctx, conn, gid, members, dest, keep_by, threshold,
                           kept=None, moved_now=[])
        return

    kept = choose_kept(present, keep_by)

    if dry:
        plan = [f"DRY RUN группа #{gid}: оставить {kept['path']}"]
        for m in present:
            if m["file_id"] != kept["file_id"]:
                plan.append(f"перенести {m['path']} ({m['size']} Б) -> {dest / os.path.basename(m['path'])}")
        ctx.log.info(" | ".join(plan))
        return

    os.makedirs(dest, exist_ok=True)
    moved_now: list[tuple[dict, str]] = []
    for m in present:
        if m["file_id"] == kept["file_id"]:
            continue
        target = _unique_target(dest, os.path.basename(m["path"]))
        try:
            shutil.move(m["path"], str(target))  # src и trash могут быть на разных ФС
        except (shutil.Error, OSError) as e:
            ctx.log.error(f"move: группа #{gid}: не удалось перенести {m['path']}: {e}")
            continue
        m["moved_to"] = str(target)
        moved_now.append((m, str(target)))

    _write_info(dest, gid, run_id, kept, moved_now, keep_by, threshold)
    _finalize_group_db(ctx, conn, gid, members, dest, keep_by, threshold,
                       kept=kept, moved_now=moved_now)
    ctx.log.info(
        f"move: группа #{gid}: перенесено {len(moved_now)} файлов в {dest} "
        f"(оставлен оригинал: {kept['path']})"
    )


def run_move(ctx: "EngineContext", *, group_id: int | None = None) -> tuple[int, int, bool]:
    """Возвращает (обработано, всего, stopped)."""
    s = ctx.settings
    conn, conn_ctl = ctx.conn, ctx.conn_ctl
    mode, keep_by, dry = s.move.mode, s.move.keep_by, s.move.dry_run
    trash = Path(s.paths.trash)

    reset_status(conn_ctl, stage="move",
                 params=ctx.params_payload({"group_id": group_id, "dry_run": dry}),
                 pid=os.getpid())
    ctx.log.info(f"move: старт (mode={mode}, keep_by={keep_by}, dry_run={dry})")

    with conn.cursor(row_factory=dict_row) as cur:
        if group_id is not None:
            cur.execute("SELECT id, run_id FROM groups WHERE id=%s", (group_id,))
            g = cur.fetchone()
            if g is None:
                raise MoveError(f"Группа #{group_id} не найдена")
            run_id: int = g["run_id"]
            sel_sql = ("SELECT id, member_count, total_size, confirmed FROM groups "
                       "WHERE run_id=%s AND id=%s ORDER BY id")
            sel_args: tuple = (run_id, group_id)
        else:
            run_id = conn.execute("SELECT max(id) AS run_id FROM analysis_runs").fetchone()[0]
            if run_id is None:
                ctx.log.warning("move: нет ни одного analysis_runs — сначала выполните `analyze`")
                set_stage(conn_ctl, "done")
                return 0, 0, False
            sel_sql = "SELECT id, member_count, total_size, confirmed FROM groups WHERE run_id=%s"
            if mode == "manual":
                sel_sql += " AND confirmed"
            sel_sql += " ORDER BY id"
            sel_args = (run_id,)
        cur.execute(sel_sql, sel_args)
        groups = cur.fetchall()

    total = len(groups)
    if total == 0:
        extra = (" (режим manual: нет подтверждённых групп — подтвердите их в UI "
                 "или укажите --group-id)") if mode == "manual" and group_id is None else ""
        ctx.log.info(f"move: нет групп для переноса{extra}")
        set_stage(conn_ctl, "done")
        return 0, 0, False

    ctx.log.info(f"move: к обработке {total} групп (run #{run_id})")
    processed = 0
    stopped = False
    for g in groups:
        if ctx.should_stop():
            stopped = True
            ctx.log.warning(f"move: остановка по флагу после {processed}/{total} групп")
            break
        _process_group(ctx, conn, trash, g, run_id, keep_by, dry, threshold=s.analyze.threshold)
        processed += 1
        update_progress(conn_ctl, stage="move", processed=processed, total=total,
                        current_file=f"group_{g['id']}")
    if dry:
        ctx.log.info(f"move: DRY RUN завершён — физически ничего не перенесено ({processed}/{total} групп)")
    else:
        ctx.log.info(f"move: завершено: {processed}/{total} групп")
    set_stage(conn_ctl, "stopped" if stopped else "done")
    return processed, total, stopped
