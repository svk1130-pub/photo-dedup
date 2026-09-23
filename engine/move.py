"""Этап 3 — перенос: перемещение дубликатов в trash/<папка группы>/ через shutil.move.

Режим: auto — все группы последнего run; manual — только confirmed=true
(кнопка в UI или move --group-id). Dry Run всегда главнее: физически ничего
не переносится и БД НЕ ИЗМЕНЯЕТСЯ (включая восстановление moved_to и
очистку хэшей отсутствующих файлов — они переносятся в план/events),
фиксируются только планируемые действия в events.

«Оригинал» внутри группы выбирается по keep_by (capture|size|pixels) НА
МОМЕНТ переноса (роль из analyze — лишь предварительная подсказка для
галереи). Политика capture (дефолт): время снимка — json-Takeout-sidecar
(photoTakenTime → creationTime) → файловая система (birthtime/mtime) →
тай-брейк по «копийным» суффиксам имени; см. engine/originals.py.

Папка группы называется по оригиналу: _IMG_20260914_123649.jpg (префикс —
move.group_name_prefix, старый нейминг group_{id} остаётся fallback-ом);
коллизии имён между группами — суффикс __2, __3… (без учёта регистра,
NFC); внутри папки коллизии имён файлов — суффикс _1, _2…; рядом с копиями
пишется info.txt.

Точная верификация: дубликаты группы хэшируются sha256 — равенство хэша
оригинала означает байт-в-байт копию независимо от phash и порога
Хэмминга (только чтение, допустимо в dry-run); результат — в info.txt
и groups.info.

Отказоустойчивость: БД-транзакция коммитится ПОСЛЕ файловых операций, поэтому
при падении между ними повторный запуск восстанавливает состояние: если файла
нет в src, но он лежит в папке этой группы (или прежней group_{id}) с тем же
именем и размером — moved_to доносится.
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
from .hashing import file_sha256
from .originals import capture_time_of, collect_used_names, fmt_capture, group_dir_name, unique_name

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
                keep_by: str, threshold: int, *, exact_moved: int | None = None,
                capture: tuple[float, str] | None = None) -> None:
    lines = [
        "Photo Dedup — отчёт о переносе",
        f"Группа: {gid} (run {run_id})",
        f"Дата: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"Порог (расстояние Хэмминга): {threshold}",
        f"Критерий оригинала (keep_by): {keep_by}",
    ]
    if capture is not None:
        lines.append(f"Время снимка оригинала: {fmt_capture(capture[0])} (источник: {capture[1]})")
    lines += [
        f"Папка группы: {dest.name}",
        f"Оставлен оригинал: {kept['path']} ({kept['size']} байт, "
        f"{kept['width']}x{kept['height']})",
    ]
    if exact_moved is not None:
        lines.append(f"Точных копий оригинала среди перемещённых (sha256): {exact_moved} из {len(moved_now)}")
    lines += [
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
                       *, kept: dict | None, moved_now: list[tuple[dict, str]],
                       exact: dict | None = None) -> None:
    """Одна транзакция: роли, moved_to/moved_at, удаление хэшей и пометка
    files.status='moved' для перенесённых/пропавших, обновление groups.kept_path/info.
    Вызывается после успешных файловых операций."""
    missing_ids = [
        m["file_id"] for m in members
        if m["moved_to"] is None and not os.path.exists(m["path"])
    ]
    # Все, кого нет в src: перенесённые сейчас, восстановленные после сбоя
    # (moved_to уже проставлен) и исчезнувшие — их хэши не должны участвовать
    # в будущих run (иначе дубликаты находились бы повторно).
    gone_ids = sorted({m["file_id"] for m in members if m["moved_to"] is not None}
                      | set(missing_ids))
    with conn.transaction():
        with conn.cursor() as cur:
            # moved_to пишется ВСЕМ членам с непустым moved_to: и перенесённым сейчас,
            # и восстановленным после сбоя (recovery проставляет его только в памяти —
            # без этой записи восстановленные строки оставались бы с moved_to=NULL).
            # Повторная запись тех же значений для ранее перенесённых — безвредна,
            # moved_at не перетирается (COALESCE).
            moved_all = [
                (m["moved_to"], m["file_id"]) for m in members if m["moved_to"] is not None
            ]
            if moved_all:
                cur.executemany(
                    "UPDATE group_members SET role='dup', moved_to=%s, "
                    "moved_at=COALESCE(moved_at, now()) "
                    "WHERE group_id=%s AND file_id=%s",
                    [(to, gid, fid) for to, fid in moved_all],
                )
            if kept is not None:
                cur.execute(
                    "UPDATE group_members SET role='kept', moved_to=NULL, moved_at=NULL "
                    "WHERE group_id=%s AND file_id=%s",
                    (gid, kept["file_id"]),
                )
            # moved_at для восстановленных после сбоя (recovery проставляет moved_to без времени)
            cur.execute(
                "UPDATE group_members SET moved_at=now() "
                "WHERE group_id=%s AND moved_to IS NOT NULL AND moved_at IS NULL",
                (gid,),
            )
            if gone_ids:
                cur.execute("DELETE FROM hashes WHERE file_id = ANY(%s)", (gone_ids,))
                # Помечаем строки files: файла больше нет в src. Счётчики статуса это
                # учитывают; вернувшийся на то же место файл переиндексируется в 'ok'.
                cur.executemany(
                    "UPDATE files SET status='moved', error=%s "
                    "WHERE id=%s AND status <> 'moved'",
                    [("перенесён в trash или отсутствует в src (отмечено этапом move)", fid)
                     for fid in gone_ids],
                )
            cur.execute("SELECT info FROM groups WHERE id=%s", (gid,))
            row = cur.fetchone()
            info = dict(row[0] or {})
            info.update({
                "moved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "moved_count": (info.get("moved_count") or 0) + len(moved_now),
                "keep_by": keep_by,
                "threshold": threshold,
                "dest": str(dest),
                "group_dir": dest.name,
            })
            if kept is not None and keep_by == "capture" and kept.get("capture_src"):
                info["kept_capture_time"] = kept.get("capture_time")
                info["kept_capture_source"] = kept["capture_src"]
            if exact is not None:
                info["exact_kept_copies"] = exact["kept_copies"]
                info["dups_sha_checked"] = exact["dups_checked"]
            if kept is not None:
                cur.execute("UPDATE groups SET kept_path=%s, info=%s WHERE id=%s",
                            (kept["path"], Json(info), gid))
            else:
                cur.execute("UPDATE groups SET info=%s WHERE id=%s", (Json(info), gid))


def _process_group(ctx: "EngineContext", conn, trash: Path, g: dict, run_id: int,
                   keep_by: str, dry: bool, *, threshold: int, prefix: str,
                   used_names: set[str]) -> str | None:
    """Обработка одной группы. Возвращает имя папки группы (для прогресса) или None."""
    gid = g["id"]
    legacy_dest = trash / f"group_{gid}"  # нейминг до 1.2.0 — кандидат crash-recovery
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT gm.file_id, gm.role, gm.moved_to, f.path, f.size, f.mtime, f.width, f.height
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
        return None

    # Оригинал и папка группы (имя по оригиналу) определяются ДО recovery:
    # в этой папке и ищутся файлы, «перенесённые» упавшим прошлым запуском.
    dest: Path = legacy_dest
    kept: dict | None = None
    if present:
        for m in present:  # время снимка: json-sidecar → ФС → БД (engine/originals.py)
            t, src_lbl = capture_time_of(m["path"], m.get("mtime"))
            m["capture_time"], m["capture_src"] = t, src_lbl
        kept = choose_kept(present, keep_by)
        dest = trash / unique_name(group_dir_name(kept["path"], prefix, gid), used_names)

    # Crash recovery: файла нет в src, но он лежит в trash → moved_to доносится.
    # Кандидаты: папка этой группы (нейминг 1.2.0+, выводится из имён ЧЛЕНОВ группы —
    # имя папки строится по оригиналу) и прежняя group_{id} (до 1.2.0).
    # Внутри папки имена при переносе аллоцируются детерминированно (_unique_target:
    # base, base_1, base_2…) — recovery повторяет аллокацию в порядке членов группы
    # и не переиспользует занятые слоты, поэтому одноимённые дубликаты не
    # «приклеиваются» к одному файлу. Проверка размера отсекает чужие файлы.
    if missing:
        cand_dirs = sorted(
            {legacy_dest, dest}
            | {trash / group_dir_name(m["path"], prefix, gid) for m in members if m["path"]},
            key=str,
        )
        placed: dict[tuple[str, str], int] = {}  # (папка, basename) → занятых слотов
        for m in already:
            if m["moved_to"] and m["path"]:
                key = (os.path.dirname(m["moved_to"]), os.path.basename(m["path"]))
                placed[key] = placed.get(key, 0) + 1
        for m in missing:
            base = os.path.basename(m["path"])
            stem, ext = os.path.splitext(base)
            for d in cand_dirs:
                k = placed.get((str(d), base), 0)
                name = base if k == 0 else f"{stem}_{k}{ext}"  # реплей _unique_target
                cand = d / name
                try:
                    found = cand.exists() and cand.stat().st_size == m["size"]
                except OSError:
                    found = False
                if found:
                    m["moved_to"] = str(cand)
                    placed[(str(d), base)] = k + 1
                    already.append(m)
                    if dry:
                        ctx.log.info(f"move: DRY RUN группа #{gid}: в реальном режиме moved_to "
                                     f"был бы восстановлен после сбоя: {cand}")
                    else:
                        ctx.log.info(f"move: группа #{gid}: восстановлен moved_to после сбоя: {cand}")
                    break
            else:
                ctx.log.warning(f"move: группа #{gid}: файл отсутствует, пропущен: {m['path']}")

    if not present:
        if dry:
            # Dry Run главнее: БД не меняется — в реальном режиме здесь были бы
            # восстановлены moved_to и удалены хэши отсутствующих файлов.
            ctx.log.info(f"move: DRY RUN группа #{gid}: файлы отсутствуют — БД не изменяется")
        else:
            _finalize_group_db(ctx, conn, gid, members, legacy_dest, keep_by, threshold,
                               kept=None, moved_now=[])
        return None

    assert kept is not None

    # Точная верификация дубликатов: равенство sha256 с оригиналом = байт-в-байт
    # копия независимо от phash и порога Хэмминга. Только чтение — допустимо в dry-run.
    kept_sha: bytes | None = None
    try:
        kept_sha = file_sha256(kept["path"])
    except OSError as e:
        ctx.log.warning(f"move: группа #{gid}: оригинал не прочитан для sha256: {e}")
    exact_ids: set[int] = set()
    if kept_sha is not None:
        for m in present:
            if m["file_id"] == kept["file_id"]:
                continue
            try:
                if file_sha256(m["path"]) == kept_sha:
                    exact_ids.add(m["file_id"])
            except OSError:
                continue  # файл исчез по ходу — верификация недоступна

    capture_note = ""
    if keep_by == "capture" and kept.get("capture_src"):
        capture_note = (f" (время снимка: {fmt_capture(kept['capture_time'])}, "
                        f"источник: {kept['capture_src']})")

    if dry:
        plan = [f"DRY RUN группа #{gid}: оставить {kept['path']}{capture_note}"]
        for m in present:
            if m["file_id"] != kept["file_id"]:
                mark = " · точная копия оригинала (sha256)" if m["file_id"] in exact_ids else ""
                plan.append(f"перенести {m['path']} ({m['size']} Б) -> "
                            f"{dest / os.path.basename(m['path'])}{mark}")
        if kept_sha is not None:
            plan.append(f"точных копий оригинала: {len(exact_ids)} из {len(present) - 1}")
        ctx.log.info(" | ".join(plan))
        return dest.name

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

    exact_moved = sum(1 for m, _ in moved_now if m["file_id"] in exact_ids)
    _write_info(dest, gid, run_id, kept, moved_now, keep_by, threshold,
                exact_moved=exact_moved if kept_sha is not None else None,
                capture=(kept["capture_time"], kept["capture_src"])
                if keep_by == "capture" and kept.get("capture_src") else None)
    _finalize_group_db(ctx, conn, gid, members, dest, keep_by, threshold,
                       kept=kept, moved_now=moved_now,
                       exact=None if kept_sha is None else
                       {"kept_copies": len(exact_ids), "dups_checked": len(present) - 1})
    ctx.log.info(
        f"move: группа #{gid}: папка «{dest.name}»: перенесено {len(moved_now)} файлов "
        f"(оставлен оригинал: {kept['path']}{capture_note}"
        + (f"; точных копий: {exact_moved})" if kept_sha is not None else ")")
    )
    return dest.name


def run_move(ctx: "EngineContext", *, group_id: int | None = None) -> tuple[int, int, bool]:
    """Возвращает (обработано, всего, stopped)."""
    s = ctx.settings
    conn, conn_ctl = ctx.conn, ctx.conn_ctl
    mode, keep_by, dry = s.move.mode, s.move.keep_by, s.move.dry_run
    trash = Path(s.paths.trash)
    prefix = s.move.group_name_prefix

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
    used_names = collect_used_names(trash)  # занятые имена в trash: коллизии между прогонами
    processed = 0
    stopped = False
    for g in groups:
        if ctx.should_stop():
            stopped = True
            ctx.log.warning(f"move: остановка по флагу после {processed}/{total} групп")
            break
        dir_name = _process_group(ctx, conn, trash, g, run_id, keep_by, dry,
                                  threshold=s.analyze.threshold, prefix=prefix,
                                  used_names=used_names)
        processed += 1
        update_progress(conn_ctl, stage="move", processed=processed, total=total,
                        current_file=dir_name or f"group_{g['id']}")
    if dry:
        ctx.log.info(f"move: DRY RUN завершён — физически ничего не перенесено ({processed}/{total} групп)")
    else:
        ctx.log.info(f"move: завершено: {processed}/{total} групп")
    set_stage(conn_ctl, "stopped" if stopped else "done")
    return processed, total, stopped
