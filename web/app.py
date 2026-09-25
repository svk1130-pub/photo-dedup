"""Streamlit UI — тонкий монитор и пульт.

Запрещено by design: обходы файловой системы (кроме ТОЧЕЧНЫХ файловых
операций из engine/webops.py по путям из БД — ⭐/📋/↩️/🗑️ и undo),
запросы без LIMIT. Миниатюры — единственная обработка изображений
(web/thumbs.py, ленивый кэш ~400 px). Соединения — только из пула
(st.cache_resource, max 5). UI по-прежнему НЕ запускает процессы: он читает БД,
пишет флаги/команды и выполняет ручные файловые операции через webops (все они
запрещены, пока движок работает):
  * stop_requested (кнопка Стоп),
  * groups.confirmed (подтверждение переноса в manual-режиме),
  * settings.toml (через отдельный rw-маунт),
  * jobs.enqueue (кнопки запуска на Мониторе: ▶️/🔍/🧠/📦/↩️) — задания в очередь БД;
    выполняет их сервис runner (1.5.0+, вариант A — без docker-сокета),
  * webops: return_file / star_as_original / mark_as_duplicate /
    delete_file / undo_all / clean_db (журналируются в events).

Цель теста: реран UI < 300 мс при полностью загруженном движке — метрика
«UI rerun, ms» в сайдбаре; тяжёлые вкладки обновляются через st.fragment.
"""
from __future__ import annotations

import os
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

import streamlit as st

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:  # чтобы `import engine` работал при `streamlit run web/app.py`
    sys.path.insert(0, str(_ROOT))

from engine import webops  # noqa: E402
from engine import jobs as jobq  # noqa: E402  (очередь заданий runner'а, Ф1)
from engine.hashing import extract_exif  # noqa: E402  (ленивое чтение свойств в диалоге)
from engine.settings import (  # noqa: E402
    Settings,
    SettingsError,
    container_to_host,
    host_root,
    load_settings,
    settings_to_dict,
    update_settings_file,
)

DSN = os.environ.get("PG_DSN", "postgresql://photo:photo@db:5432/photo")
SETTINGS_PATH = os.environ.get("SETTINGS_PATH", "/data/settings.toml")
STALE_SEC = 15.0  # статус старше — движок считается незапущенным
REAP_STALE_SEC = float(os.environ.get("RUNNER_STALE_SEC", "30"))  # тот же порог, что у runner'а (Ф2: stale-детекция в UI)

st.set_page_config(page_title="Photo Dedup", page_icon="🖼", layout="wide")

from psycopg.types.json import Json  # noqa: E402
from psycopg import errors as pg_errors  # noqa: E402  (дубль задания в очереди)
from psycopg_pool import ConnectionPool  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402


@st.cache_resource(show_spinner="Подключение к базе данных…")
def get_pool() -> ConnectionPool:
    return ConnectionPool(
        conninfo=DSN,
        min_size=1,
        max_size=5,  # короткие запросы; больше не нужно
        open=True,
        name="web-pool",
        kwargs={"row_factory": dict_row},
        check=ConnectionPool.check_connection,
    )


def q(sql: str, params: tuple | list = (), *, fetch: bool = True) -> Any:
    """Короткий запрос из пула. Все запросы приложения — LIMIT/агрегаты/OFFSET."""
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if fetch else None


def _read_refresh_at_import() -> int:
    """refresh_sec читается один раз на процесс web (фрагменты создаются на import)."""
    try:
        with open(SETTINGS_PATH, "rb") as f:
            return int(tomllib.load(f).get("ui", {}).get("refresh_sec", 2) or 2)
    except Exception:
        return 2


MONITOR_REFRESH_SEC = max(1, _read_refresh_at_import())


# ----------------------------- вспомогательные -----------------------------

def human_size(n: float | int | None) -> str:
    if n is None:
        return "—"
    n = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def fmt_eta(sec: float | None) -> str:
    if not sec or sec <= 0 or sec > 10 ** 8:
        return "—"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def engine_row() -> dict | None:
    rows = q(
        """
        SELECT stage, processed, total, current_file, files_per_sec, started_at, updated_at,
               engine_pid, stop_requested, params,
               (updated_at > now() - make_interval(secs => %s)) AS alive
        FROM status WHERE id = 1
        """,
        (STALE_SEC,),
    )
    return rows[0] if rows else None


def last_run() -> dict | None:
    rows = q("SELECT id, created_at, threshold, params FROM analysis_runs ORDER BY id DESC LIMIT 1")
    return rows[0] if rows else None


def is_working(row: dict | None) -> bool:
    return bool(row and row["alive"] and row["stage"] in ("scan", "analyze", "move"))


def _load_settings_or_default() -> Settings:
    try:
        return load_settings(SETTINGS_PATH)
    except SettingsError as e:
        st.sidebar.warning(f"Настройки не применены (ошибка файла): {e}")
        return load_settings(None)


def _load_settings_quiet() -> Settings | None:
    try:
        return load_settings(SETTINGS_PATH)
    except SettingsError:
        return None


def _do_action(settings: Settings, fn, file_id: int, *, gid: int | None, toast: str) -> None:
    """Выполнить ручную операцию webops на соединении из пула + перерисовать."""
    try:
        with get_pool().connection() as conn:
            result = fn(conn, settings, file_id)
    except webops.OpError as e:
        st.error(f"❌ {e}")
        return
    except Exception as e:  # соединение/БД/диск — показываем, не роняем UI
        st.error(f"❌ {type(e).__name__}: {e}")
        return
    if gid is not None:  # группа остаётся раскрытой после операции
        st.session_state[f"grp_open_{gid}"] = True
    st.session_state.pop(f"delarm_{file_id}", None)
    st.session_state.pop(f"dlgarm_{file_id}", None)
    st.toast(f"{toast}: {str(result)[:120]}", icon="✅")
    st.rerun()


def _confirm_button(key: str, label: str, *, need_confirm: bool, scope: str = "app",
                    disabled: bool = False, help: str | None = None,
                    type_: str = "secondary") -> bool:
    """Кнопка с двухфазным подтверждением in-place (session_state, без dialog).

    Возвращает True ровно тогда, когда действие подтверждено и его нужно выполнить.
    """
    arm = f"arm_{key}"
    if st.button(label, key=f"btn_{key}", disabled=disabled, help=help,
                 use_container_width=True, type=type_):
        if not need_confirm:
            st.session_state.pop(arm, None)
            return True
        st.session_state[arm] = True
        st.rerun(scope=scope)
    if st.session_state.get(arm):
        st.caption("⚠️ Точно? Действие необратимо.")
        c1, c2 = st.columns(2)
        if c1.button("✅ Да", key=f"{key}_yes", use_container_width=True, type="primary"):
            st.session_state.pop(arm, None)
            return True
        if c2.button("❌ Отмена", key=f"{key}_no", use_container_width=True):
            st.session_state.pop(arm, None)
            st.rerun(scope=scope)
    return False


# ----------------------------- баннер и сайдбар -----------------------------

def _banner(row: dict | None) -> None:
    if row is None:
        st.error("Таблица status недоступна. Запустите движок один раз — схема создастся автоматически.")
        return
    if not is_working(row):
        stage = row["stage"]
        st.warning(
            f"Движок не запущен (последний этап: **{stage}**). "
            f"Просмотр прошлых результатов полностью доступен. Запуск — кнопки запуска "
            f"на вкладке «🖥 Монитор» (задание в очереди "
            f"jobs выполнит сервис runner) или команда:"
        )
        st.code("# в каталоге проекта\ndocker compose run --rm engine run", language="bash")
    elif row["stop_requested"]:
        st.info("🛑 Выставлен флаг остановки — движок завершит текущий файл/батч и остановится.")


def _sidebar(settings: Settings, row: dict | None) -> None:
    with st.sidebar:
        st.header("🖼 Photo Dedup")
        p = settings.paths
        hr = host_root()
        for label, path in (("src (архив)", p.src), ("trash (дубликаты)", p.trash)):
            exists = Path(str(path)).exists()
            line = f"**{label}:** `{path}`" + ("" if exists else " · ⚠️ не найден")
            if hr:
                line += f"\n\n· на хосте: `{container_to_host(path)}`"
            st.markdown(line)
        st.caption(
            "Пути в settings — КОНТЕЙНЕРНЫЕ (их видит движок). На хосте им соответствует "
            "**PHOTOS_ROOT** из `.env` (смонтировано в /data/src и /data/trash). "
            "Смена хост-папки: правьте `.env` → `docker compose up -d` — правка путей "
            "в settings.toml маунты не меняет."
        )
        if row and isinstance(row.get("params"), dict) and row["params"]:
            with st.expander("Параметры текущего/последнего запуска движка", expanded=False):
                st.json(row["params"], expanded=1)
        _settings_editor(settings)
        st.caption(
            "Движок перечитывает settings.toml на границе этапов "
            "(scan → analyze → move) и при каждом старте."
        )


def _settings_editor(settings: Settings) -> None:
    with st.sidebar.expander("⚙️ Эффективные настройки + редактор", expanded=True):
        st.json(settings_to_dict(settings), expanded=1)
        st.caption("Приоритет: CLI-флаги > settings.toml > дефолты. Сохранив форму, "
                   "вы правите settings.toml (те же значения увидит и CLI-движок).")
        with st.form("settings_form", border=True):
            scan, move, ui, analyze = settings.scan, settings.move, settings.ui, settings.analyze
            threads = st.number_input(
                "scan.threads — потоки хэширования", 1, 64, value=scan.threads,
                help="Применится: на этапе scan (старт движка или граница scan→analyze в `run`)",
            )
            read_exif = st.checkbox(
                "scan.read_exif — читать EXIF-свойства при прогоне", value=scan.read_exif,
                help="Сохраняет свойства (камера, выдержка, ISO…) в files.exif для окна "
                     "«ℹ️ Свойства» галереи. Чтение заголовков дёшево; можно отключить — "
                     "тогда свойства будут читаться лениво при открытии окна.",
            )
            threshold = st.number_input(
                "analyze.threshold — макс. расстояние Хэмминга", 0, 32, value=analyze.threshold,
                help="Применится: на этапе analyze (следующий запуск или граница этапа)",
            )
            mode = st.selectbox(
                "move.mode — режим переноса", ("auto", "manual"),
                index=("auto", "manual").index(move.mode),
                help="Применится: на этапе move. manual — только подтверждённые группы",
            )
            dry = st.checkbox(
                "move.dry_run — Dry Run (не переносить физически)", value=move.dry_run,
                help="Применится: на этапе move. Dry Run всегда главнее переноса",
            )
            keep_by = st.selectbox(
                "move.keep_by — что считать оригиналом", ("capture", "size", "pixels"),
                index=("capture", "size", "pixels").index(move.keep_by),
                help="capture: время снимка (json-Takeout → файловая система → тай-брейк по имени). "
                     "Применится: на этапе move (в т.ч. перевыбор оригинала в готовых группах)",
            )
            page = st.number_input(
                "ui.page_size — групп на странице галереи", 5, 100, value=ui.page_size,
                help="Применится: сразу",
            )
            refresh = st.number_input(
                "ui.refresh_sec — интервал обновления монитора, с", 1, 60, value=ui.refresh_sec,
                help="Применится: после перезапуска web (фрагменты создаются при старте процесса)",
            )
            c_del = st.checkbox(
                "ui.confirm_delete_files — подтверждать «🗑️ Удалить»", value=ui.confirm_delete_files,
                help="Диалог подтверждения при удалении файлов с диска из галереи",
            )
            c_db = st.checkbox(
                "ui.confirm_clean_db — подтверждать «💥 Очистить БД»", value=ui.confirm_clean_db,
                help="Диалог подтверждения при очистке результатов и журнала",
            )
            c_log = st.checkbox(
                "ui.confirm_clean_log — подтверждать «🧹 Очистить журнал»", value=ui.confirm_clean_log,
                help="Диалог подтверждения при очистке журнала",
            )
            submitted = st.form_submit_button("💾 Сохранить в settings.toml")
        if submitted:
            updates: dict[str, dict[str, Any]] = {
                "scan": {"threads": int(threads), "read_exif": bool(read_exif)},
                "analyze": {"threshold": int(threshold)},
                "move": {"mode": mode, "dry_run": bool(dry), "keep_by": keep_by},
                "ui": {
                    "page_size": int(page), "refresh_sec": int(refresh),
                    "confirm_delete_files": bool(c_del),
                    "confirm_clean_db": bool(c_db),
                    "confirm_clean_log": bool(c_log),
                },
            }
            try:
                update_settings_file(SETTINGS_PATH, updates)
                st.toast("Настройки сохранены в settings.toml", icon="💾")
                st.rerun()
            except (SettingsError, OSError) as e:
                st.error(f"Не удалось сохранить настройки: {e}")


# ----------------------------- вкладка «Монитор» -----------------------------

def _what_next(row: dict | None, settings: Settings, lr: dict | None) -> None:
    """Информер «что делать дальше» (ручной режим: подтвердить → выполнить перенос)."""
    if is_working(row):
        return
    if lr is None:
        st.info("**Что дальше:** запустите полный цикл — он проиндексирует архив, найдёт "
                "дубликаты и (auto-режим) сразу перенесёт их в trash. Кнопка «▶️ Полный "
                "прогон» выше или команда:")
        st.code("docker compose run --rm engine run", language="bash")
        return
    g = q(
        "SELECT count(*) AS total, count(*) FILTER (WHERE confirmed) AS confirmed "
        "FROM groups WHERE run_id = %s",
        (lr["id"],),
    )[0]
    moved = q(
        """
        SELECT count(*) AS n FROM group_members gm JOIN groups g ON g.id = gm.group_id
        WHERE g.run_id = %s AND gm.moved_to IS NOT NULL
        """,
        (lr["id"],),
    )[0]["n"]
    if settings.move.mode == "manual":
        st.info(
            f"**Ручной режим — что делать дальше:**\n\n"
            f"1️⃣ Отметьте файлы кнопками ⭐/📋 в **Галерее** (и/или подтвердите группы: "
            f"сейчас подтверждено **{g['confirmed']} из {g['total']}**).\n\n"
            f"2️⃣ Выполните перенос подтверждённых групп — без шага 2 физического переноса "
            f"НЕ произойдёт (перенесено файлов: {moved}): кнопка «📦 Перенос» выше или команда:"
        )
        st.code("docker compose run --rm engine move", language="bash")
    else:
        st.info(
            f"**Auto-режим:** кнопка «📦 Перенос» выше (или команда ниже) перенесёт ВСЕ группы "
            f"последнего run (групп: {g['total']}, перенесено файлов: {moved}). "
            f"Хотите решать сами — переключите `move.mode` в **manual** в сайдбаре."
        )
        st.code("docker compose run --rm engine move", language="bash")


_QUICK_CMDS = """\
# полный цикл: индексация → поиск групп → перенос
#   (или кнопки запуска на Мониторе — очередь jobs → runner)
docker compose run --rm engine run
# поэтапно
docker compose run --rm engine scan
docker compose run --rm engine analyze
docker compose run --rm engine move            # перенос (manual: только подтверждённые)
docker compose run --rm engine move --dry-run  # только план
# обслуживание
docker compose run --rm engine undo            # вернуть ВСЁ из trash + очистить БД
docker compose run --rm engine undo --dry-run  # план отмены без изменений
docker compose run --rm engine clean-db        # очистить результаты и журнал
docker compose run --rm engine status          # статус
docker compose run --rm engine stop            # мягкая остановка"""


@st.fragment(run_every=f"{MONITOR_REFRESH_SEC}s")
def monitor_fragment(settings: Settings) -> None:
    tf = time.perf_counter()
    row = engine_row()
    if row is None:
        st.caption("Таблица status недоступна.")
        return
    working = is_working(row)
    locked = working
    total = int(row["total"] or 0)
    processed = int(row["processed"] or 0)
    pct = min(processed / total, 1.0) if total else 0.0
    speed = float(row["files_per_sec"] or 0.0)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Этап", str(row["stage"]))
    c2.metric("Обработано", f"{processed} из {total}", f"{pct * 100:.1f}%")
    speed_unit = "бакетов/с" if row["stage"] == "analyze" else "файлов/с"
    c3.metric("Скорость", f"{speed:.1f} {speed_unit}")
    eta = fmt_eta((total - processed) / speed) if (working and speed > 0 and total) else None
    c4.metric("ETA", eta)

    if working and total:
        st.progress(pct, text=f"{row['stage']}: {processed} из {total} ({pct * 100:.1f}%)")
    if working and row["current_file"]:
        st.caption("Текущий файл:")
        st.code(str(row["current_file"]), language=None)

    corrupt = q("SELECT count(*) AS n FROM files WHERE status='corrupt'")[0]["n"]
    lr = last_run()
    if lr:
        g = q(
            """
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE confirmed) AS confirmed,
                   coalesce(sum(member_count), 0) AS members
            FROM groups WHERE run_id = %s
            """,
            (lr["id"],),
        )[0]
        moved = q(
            """
            SELECT count(*) AS n
            FROM group_members gm JOIN groups g ON g.id = gm.group_id
            WHERE g.run_id = %s AND gm.moved_to IS NOT NULL
            """,
            (lr["id"],),
        )[0]["n"]
        st.caption(
            f"Битых файлов: {corrupt} · последний run #{lr['id']} (threshold {lr['threshold']}): "
            f"групп {g['total']} (подтверждено {g['confirmed']}), файлов в группах {g['members']}, "
            f"перенесено {moved}"
        )
    else:
        st.caption(f"Битых файлов: {corrupt} · анализ ещё не выполнялся")

    st.caption(
        f"фрагмент обновлён за {(time.perf_counter() - tf) * 1000:.0f} мс · "
        f"автообновление каждые {MONITOR_REFRESH_SEC} с"
    )

    # --- управление ---
    ca, cb = st.columns([1, 3])
    if ca.button("🛑 Стоп", type="primary" if working else "secondary",
                 disabled=not working or bool(row["stop_requested"]),
                 help="Мягкая (кооперативная) остановка: движок доделает текущий файл и батч"):
        q("UPDATE status SET stop_requested = true, updated_at = now() WHERE id = 1", fetch=False)
        st.toast("Флаг остановки выставлен — движок завершит текущий батч", icon="🛑")
        st.rerun(scope="fragment")
    if working and row["stop_requested"]:
        cb.caption("Ожидание остановки движка…")

    # --- очередь заданий runner (Ф2: кнопки всех длинных команд ставят INSERT в jobs) ---
    try:
        with get_pool().connection() as conn:
            # stale-детекция из UI (Ф2): runner мог погибнуть — reap по протухшему
            # heartbeat'у делаем и здесь (дешёвый UPDATE, обычно 0 строк)
            jobq.reap_stale(conn, stale_sec=REAP_STALE_SEC)
            snap = jobq.snapshot(conn)
    except Exception:            # таблицы jobs ещё нет (до первого старта движка/runner'а)
        snap = None
    queued = snap["queued"] if snap else 0
    running_job = snap["running"] if snap else None
    has_live_job = bool(queued or running_job)
    job_disabled = locked or has_live_job

    def _qjob(command: str) -> None:
        """INSERT задания в очередь + toast (дубликат отсекает uniq-индекс БД)."""
        try:
            with get_pool().connection() as conn:
                jid = jobq.enqueue(conn, command)
            st.toast(f"Задание #{jid} ({command}) в очереди — runner возьмёт его через пару секунд", icon="▶️")
        except pg_errors.UniqueViolation:
            st.toast(f"Задание `{command}` уже стоит в очереди", icon="⏳")
        except pg_errors.UndefinedTable:
            st.error("Таблица очереди `jobs` ещё не создана: схему создаёт сервис runner "
                     "(или любая команда движка) при старте. Проверьте: `docker compose up -d db web runner` "
                     "и `docker compose logs runner`.")

    b_run, b_scan, b_anl, b_move, b_undo = st.columns([1.3, 1, 1, 1, 1])
    if b_run.button(
            "▶️ Полный прогон", type="primary", disabled=job_disabled,
            help=("scan → analyze → move одним заданием (auto-режим переносит дубликаты в trash; "
                  "move.dry_run из сайдбара главнее). Выполнит сервис runner с текущим settings.toml. "
                  "Мягкая остановка — кнопка Стоп."),
    ):
        _qjob("run")
        st.rerun(scope="fragment")
    if b_scan.button(
            "🔍 Скан", disabled=job_disabled,
            help="Только этап scan: индексация src (новые/изменённые файлы). Resume штатный.",
    ):
        _qjob("scan")
        st.rerun(scope="fragment")
    if b_anl.button(
            "🧠 Анализ", disabled=job_disabled,
            help="Только этап analyze: поиск групп-дубликатов (threshold из сайдбара).",
    ):
        _qjob("analyze")
        st.rerun(scope="fragment")
    if b_move.button(
            "📦 Перенос", disabled=job_disabled,
            help=("Только этап move: перенос дубликатов в trash (auto — все группы последнего "
                  "run; manual — только подтверждённые; move.dry_run из сайдбара главнее)."),
    ):
        _qjob("move")
        st.rerun(scope="fragment")
    if b_undo.button(
            "↩️ Undo", disabled=job_disabled,
            help="Вернуть ВСЕ перенесённые в trash файлы и очистить результаты. Выполнит runner (с подтверждением).",
    ):
        st.session_state["arm_qundo"] = True
        st.rerun(scope="fragment")
    if job_disabled:
        st.session_state.pop("arm_qundo", None)
    if st.session_state.get("arm_qundo"):
        st.warning("↩️ **Undo вернёт все перенесённые файлы** из trash в src и очистит результаты прогонов. Точно?")
        u1, u2 = st.columns(2)
        if u1.button("✅ Да, вернуть файлы", key="qundo_yes", type="primary"):
            st.session_state.pop("arm_qundo", None)
            _qjob("undo")
            st.rerun(scope="fragment")
        if u2.button("❌ Отмена", key="qundo_no"):
            st.session_state.pop("arm_qundo", None)
            st.rerun(scope="fragment")

    if has_live_job:
        st.caption("Задание выполняет сервис **runner**; прогресс — метрики выше, история — ниже. "
                   "Одновременно в работе — одно задание (single-flight).")
    else:
        st.caption("Кнопки ставят задание в очередь jobs — его выполнит сервис runner "
                   "(тот же образ и код, что у CLI). Настройки — из settings.toml (сайдбар).")

    if snap:
        if running_job:
            taken = running_job["taken_at"].strftime("%H:%M:%S") if running_job["taken_at"] else ""
            st.info(f"🏃 Runner выполняет задание **#{running_job['id']}** `{running_job['command']}`"
                    + (f" (взял {taken})" if taken else "")
                    + " — мягкая остановка: кнопка 🛑 Стоп выше.")
        elif queued:
            st.info(f"⏳ В очереди: **{queued}** — runner заберёт задание в ближайшие секунды.")
        stale_n = int(snap["counts"].get("stale", 0))
        if stale_n:
            st.warning(
                f"⚠️ **Прогон оборвался** (заданий в состоянии stale: {stale_n}): runner был "
                "перезапущен/недоступен во время выполнения. Файлы не пострадали; штатный "
                "Resume: просто запустите нужный этап заново — scan/move продолжат с места остановки."
            )
        icons = {"done": "✅", "failed": "❌", "stopped": "🛑", "stale": "⚠️"}
        lines = []
        for r in snap["recent"]:
            dur = "—"
            if r["taken_at"] and r["finished_at"]:
                dur = fmt_eta((r["finished_at"] - r["taken_at"]).total_seconds())
            line = (f"{icons.get(r['state'], '·')} #{r['id']} {r['command']} → {r['state']} · "
                    f"exit {r['exit_code']} · {dur}")
            if r["error"]:
                line += f" · {str(r['error'])[:90]}"
            lines.append(line)
        if lines:
            st.caption("Последние задания runner:\n\n" + "\n\n".join(lines))

    _what_next(row, settings, lr)

    with st.expander("⌨️ Быстрые команды (запуск движка — из терминала хоста)"):
        st.caption("UI процессы не запускает: кнопки запуска на Мониторе ставят задания в "
                   "очередь jobs (их выполнит сервис runner), остальные команды — "
                   "в терминале, в каталоге проекта (кнопка копирования — справа в блоке).")
        st.code(_QUICK_CMDS, language="bash")

    # --- опасная зона ---
    st.divider()
    az1, az2 = st.columns(2)
    with az1:
        with st.expander("⚠️ Опасная зона: подтверждение переноса (manual)"):
            _confirm_all_block(lr)
    with az2:
        with st.expander("🧨 Опасная зона: обслуживание БД"):
            st.caption(
                "Операции выполняются прямо из UI (БД + маунты) и **запрещены, пока движок "
                "работает**. Файлы в src они не трогают (кроме Undo — он возвращает файлы). "
                "Для больших архивов предпочтительнее «↩️ Undo» в блоке очереди выше: задание "
                "выполнит runner (прогресс, история, Стоп), а не спиннер в этой странице."
            )
            if _confirm_button(
                    "clean_db", "💥 Очистить БД", need_confirm=settings.ui.confirm_clean_db,
                    scope="fragment", disabled=locked, type_="primary",
                    help="TRUNCATE результатов всех прогонов + журнала. Файлы на диске НЕ трогаются"):
                with get_pool().connection() as conn:
                    msg = webops.clean_db(conn)
                st.toast("БД очищена", icon="💥")
                st.session_state["op_result"] = msg
                st.rerun(scope="fragment")
            if _confirm_button(
                    "undo_all", "↩️ Отменить всё (undo)", need_confirm=True, scope="fragment",
                    disabled=locked,
                    help="Вернуть ВСЕ перенесённые файлы в src, затем очистить результаты "
                         "и журнал. При проблемах очистка откладывается"):
                with st.spinner("Возвращаю файлы из trash…"):
                    with get_pool().connection() as conn:
                        restored, problems, notes = webops.undo_all(conn, settings, dry=False)
                if problems:
                    st.session_state["op_result"] = (
                        f"Undo: возвращено {restored}, ПРОБЛЕМ: {len(problems)} "
                        f"(очистка БД отложена): " + " | ".join(problems[:5]))
                else:
                    st.session_state["op_result"] = (
                        f"Undo выполнен: возвращено {restored} файлов, БД очищена"
                        + ("; " + "; ".join(notes[:3]) if notes else ""))
                st.toast(f"Undo: возвращено {restored}", icon="↩️")
                st.rerun(scope="fragment")
    op_result = st.session_state.pop("op_result", None)
    if op_result:
        st.info(op_result)


def _confirm_all_block(lr: dict | None) -> None:
    if lr is None:
        st.caption("Анализ ещё не выполнялся.")
        return
    g = q(
        "SELECT count(*) AS total, count(*) FILTER (WHERE confirmed) AS confirmed "
        "FROM groups WHERE run_id = %s",
        (lr["id"],),
    )[0]
    st.write(f"Групп: {g['total']} · подтверждено: {g['confirmed']}")
    gate = st.checkbox("Я понимаю, что подтверждённые группы будут перемещены в trash")
    b1, b2 = st.columns(2)
    if b1.button("✅ Подтвердить все", disabled=not gate or g["total"] == g["confirmed"]):
        q("UPDATE groups SET confirmed = true WHERE run_id = %s", (lr["id"],), fetch=False)
        st.toast(f"Подтверждено групп: {g['total'] - g['confirmed']}", icon="✅")
        st.rerun(scope="fragment")
    if b2.button("↩️ Снять все подтверждения", disabled=g["confirmed"] == 0):
        q("UPDATE groups SET confirmed = false WHERE run_id = %s", (lr["id"],), fetch=False)
        st.rerun(scope="fragment")


# ----------------------------- вкладка «Журнал» -----------------------------

@st.fragment(run_every=f"{MONITOR_REFRESH_SEC}s")
def journal_fragment(settings: Settings) -> None:
    tf = time.perf_counter()
    rows = q("SELECT id, ts, level, message FROM events ORDER BY id DESC LIMIT 200")
    st.caption(f"последние {len(rows)} записей · автообновление {MONITOR_REFRESH_SEC} с · "
               f"фрагмент: {(time.perf_counter() - tf) * 1000:.0f} мс")
    if _confirm_button(
            "clean_log", "🧹 Очистить журнал", need_confirm=settings.ui.confirm_clean_log,
            scope="fragment", help="Удалить все записи журнала (events). Результаты прогонов не трогаются"):
        q("TRUNCATE events RESTART IDENTITY", fetch=False)
        st.toast("Журнал очищен", icon="🧹")
        st.rerun(scope="fragment")
    if not rows:
        st.text("Журнал пуст — движок ещё не запускался.")
        return
    icons = {"info": "ℹ️", "warning": "⚠️", "error": "❌", "debug": "🔍"}
    lines = []
    for r in reversed(rows):
        ts = r["ts"].strftime("%m-%d %H:%M:%S") if r["ts"] else ""
        lines.append(f"{icons.get(r['level'], '·')} {ts} [{r['level']:<7}] {r['message']}")
    st.code("\n".join(lines), language=None)


# ----------------------------- вкладка «Галерея» -----------------------------

_EXIF_LABELS = {
    "imageType": "Тип",
    "width": "Ширина",
    "height": "Высота",
    "cameraBrand": "Камера (бренд)",
    "cameraModel": "Камера (модель)",
    "exposureTime": "Выдержка",
    "exposureProgram": "Программа экспозиции",
    "apertureValue": "Диафрагма",
    "isoSpeedRating": "ISO",
    "flashFired": "Вспышка",
    "meteringMode": "Замер",
    "focalLength": "Фокусное расстояние",
    "createdOn": "Дата съёмки",
}


def _exif_of(m: dict) -> dict:
    """EXIF-словарь из БД; если пусто — ленивое чтение с диска + кэш в files.exif.

    Ленивый путь нужен, когда файл проиндексирован до 1.4.0 или scan.read_exif
    был выключен: чтение заголовков происходит ТОЛЬКО при открытии окна
    «Свойства», на скорость галереи не влияет.
    """
    rows = q("SELECT exif FROM files WHERE id=%s", (m["file_id"],))
    if rows and rows[0]["exif"]:
        return dict(rows[0]["exif"])
    src = m["moved_to"] or m["path"]
    try:
        from PIL import Image as PILImage

        mtime = m.get("mtime")
        if not mtime and Path(src).exists():
            mtime = Path(src).stat().st_mtime
        with PILImage.open(src) as img:
            exif = extract_exif(
                img, width=m["width"] or img.size[0], height=m["height"] or img.size[1],
                mtime=float(mtime or 0.0),
            )
        q("UPDATE files SET exif=%s WHERE id=%s", (Json(exif), m["file_id"]), fetch=False)
        return exif
    except Exception:
        return {}


@st.dialog("ℹ️ Свойства файла", width="large")
def _props_dialog(m: dict, g: dict, settings: Settings, locked: bool) -> None:
    """Модальное окно: свойства (EXIF) + дублирующий набор кнопок ⭐📋↩️🗑️.

    Кнопки ℹ️ в окне НЕТ (1.4.3): окно уже открыто. Подтверждение 🗑️
    тоже живёт внутри окна (in_dialog=True): полный st.rerun закрыл бы
    диалог, и подтверждение уезжало под картинку в сетку (баг 1.4.0).
    """
    c_img, c_info = st.columns([1, 2])
    with c_img:
        from web import thumbs  # локальный импорт — не тормозит старт приложения

        data = thumbs.get_thumbnail(m["moved_to"] or m["path"])
        if data is None:
            st.caption("🚫 миниатюра недоступна")
        else:
            st.image(data, use_container_width=True)
    with c_info:
        st.caption(f"`{m['moved_to'] or m['path']}`")
        exif = _exif_of(m)
        if not exif:
            st.caption("EXIF-свойства недоступны (файл удалён/битый).")
        st.markdown("\n".join(
            f"- **{_EXIF_LABELS.get(k, k)}:** {v}" for k, v in exif.items()
        ) or "")
    st.divider()
    _member_action_buttons(m, g, settings, locked, cols=st.columns(5),
                           key_prefix="dlg_", in_dialog=True)


def gallery_tab(settings: Settings, row: dict | None) -> None:
    locked = is_working(row)
    lr = last_run()
    if lr is None:
        st.info("Анализ ещё не выполнялся. Запустите `docker compose run --rm engine run` "
                "(или отдельно `scan` + `analyze`).")
        return
    nav1, nav2, nav3, nav4, _x = st.columns([1, 1, 2, 1, 5])
    if nav4.button("🔄 Обновить", use_container_width=True,
                   help="Перечитать данные из БД. Автообновление галереи намеренно не "
                        "включено: при больших архивах (до 3 ТБ) перерисовка миниатюр "
                        "может быть тяжёлой для браузера."):
        st.rerun()

    st.caption(
        f"Run **#{lr['id']}** от {lr['created_at']:%Y-%m-%d %H:%M} · threshold {lr['threshold']} · "
        f"галерея всегда показывает последний run; история хранится в analysis_runs"
    )
    if locked:
        st.warning("Движок работает — кнопки действий над файлами временно отключены.",
                   icon="⏳")

    page_size = settings.ui.page_size
    total = q("SELECT count(*) AS n FROM groups WHERE run_id=%s", (lr["id"],))[0]["n"]
    if total == 0:
        st.success("В последнем run группы не найдены — дубликатов нет (или хэшей нет).")
        return
    pages = (total + page_size - 1) // page_size
    st.session_state.setdefault("gallery_page", 0)
    st.session_state.gallery_page = min(st.session_state.gallery_page, pages - 1)

    if nav1.button("◀", disabled=st.session_state.gallery_page == 0):
        st.session_state.gallery_page -= 1
        st.rerun()
    if nav2.button("▶", disabled=st.session_state.gallery_page >= pages - 1):
        st.session_state.gallery_page += 1
        st.rerun()
    nav3.markdown(f"**Страница {st.session_state.gallery_page + 1} / {pages}** · всего {total} групп")

    if settings.move.mode == "manual":
        g_cnt = q(
            "SELECT count(*) AS total, count(*) FILTER (WHERE confirmed) AS confirmed "
            "FROM groups WHERE run_id=%s", (lr["id"],),
        )[0]
        st.info(
            f"Режим **manual**: после разметки (⭐/📋 и/или подтверждение групп — сейчас "
            f"{g_cnt['confirmed']} из {g_cnt['total']}) выполните в терминале "
            f"`docker compose run --rm engine move` — без этой команды перенос не выполняется.",
            icon="👆",
        )
    else:
        st.info("Режим **auto**: `docker compose run --rm engine move` перенесёт все группы "
                "последнего run.", icon="👆")

    page_rows = q(
        """
        SELECT id, member_count, total_size, kept_path, confirmed, info
        FROM groups WHERE run_id = %s
        ORDER BY total_size DESC, id
        LIMIT %s OFFSET %s
        """,
        (lr["id"], page_size, st.session_state.gallery_page * page_size),
    )
    # Члены ВСЕХ групп страницы одним батч-запросом (ANY по ids страницы) —
    # вместо N+1 запроса на каждую группу. Объём ограничен page_size группами.
    ids = [g["id"] for g in page_rows]
    member_rows = q(
        """
        SELECT gm.group_id, gm.file_id, gm.role, gm.moved_to,
               f.path, f.size, f.mtime, f.width, f.height
        FROM group_members gm JOIN files f ON f.id = gm.file_id
        WHERE gm.group_id = ANY(%s)
        ORDER BY (gm.role = 'kept') DESC, f.size DESC, f.path
        """,
        (ids,),
    ) if ids else []
    by_group: dict[int, list[dict]] = {}
    for r in member_rows:
        by_group.setdefault(r["group_id"], []).append(r)
    for g in page_rows:
        _render_group(g, by_group.get(g["id"], []), settings, locked)


def _render_group(g: dict, members: list[dict], settings: Settings, locked: bool) -> None:
    if not members:
        return
    info = g.get("info") if isinstance(g.get("info"), dict) else {}
    orig_name = Path(g["kept_path"]).name if g.get("kept_path") else None
    title = (
        f"Группа #{g['id']}"
        + (f" · {orig_name}" if orig_name else "")
        + f" · {g['member_count']} файлов · {human_size(g['total_size'])}"
        + (f" · мин. дистанция {info.get('min_distance')}" if info.get("min_distance") is not None else "")
        + (" · ✅ подтверждена" if g["confirmed"] else "")
    )
    # Группа остаётся раскрытой после действий: состояние хранится в session_state
    # (стрымлитовские expander-ы теряют состояние при изменении дерева выше).
    open_key = f"grp_open_{g['id']}"
    st.session_state.setdefault(open_key, False)
    with st.expander(title, expanded=bool(st.session_state[open_key])):
        lines = []
        for m in members:
            icon = "⭐" if m["role"] == "kept" else "📋"
            dims = f"{m['width']}×{m['height']}" if m["width"] and m["height"] else "?"
            note = f" → перенесён: `{m['moved_to']}`" if m["moved_to"] else ""
            lines.append(f"{icon} `{m['path']}` — {human_size(m['size'])}, {dims}{note}")
        st.markdown("\n\n".join(lines))

        c1, c2, _ = st.columns([1, 1, 4])
        show = c1.toggle("🖼 Показать изображения", key=f"toggle_{g['id']}")
        already_all = all(m["moved_to"] for m in members)
        if settings.move.mode == "manual" and not already_all and not locked:
            if g["confirmed"]:
                if c2.button("↩️ Снять подтверждение", key=f"unc_{g['id']}"):
                    q("UPDATE groups SET confirmed = false WHERE id=%s", (g["id"],), fetch=False)
                    st.session_state[open_key] = True
                    st.rerun()
            else:
                if c2.button("✅ Подтвердить для переноса", key=f"cf_{g['id']}"):
                    q("UPDATE groups SET confirmed = true WHERE id=%s", (g["id"],), fetch=False)
                    st.toast(f"Группа #{g['id']} подтверждена", icon="✅")
                    st.session_state[open_key] = True
                    st.rerun()
        if show:
            _render_members_grid(g, members, settings, locked)


def _render_members_grid(g: dict, members: list[dict], settings: Settings, locked: bool) -> None:
    from web import thumbs  # локальный импорт — не тормозит старт приложения

    per_row = 4
    for start in range(0, len(members), per_row):
        chunk = members[start:start + per_row]
        cols = st.columns(per_row)
        for col, m in zip(cols, chunk):
            with col:
                src_path = m["moved_to"] or m["path"]  # галерея показывает актуальный путь
                data = thumbs.get_thumbnail(src_path)
                label = ("⭐ оригинал" if m["role"] == "kept" else "📋 дубликат") \
                    + f"\n{Path(src_path).name}\n{human_size(m['size'])}"
                if data is None:
                    st.caption(f"🚫 не удалось показать:\n`{src_path}`")
                else:
                    st.image(data, caption=label, use_container_width=True)
                _member_action_buttons(m, g, settings, locked, cols=st.columns(5))


def _member_action_buttons(m: dict, g: dict, settings: Settings, locked: bool,
                           *, cols, key_prefix: str = "", in_dialog: bool = False) -> None:
    """Ряд кнопок-иконок под изображением: ⭐ 📋 ↩️ 🗑️(❗/❌) ℹ️.

    key_prefix различает экземпляры (сетка галереи и модальное окно), иначе —
    DuplicateWidgetID: диалог рендерится параллельно с основной страницей.
    in_dialog=True — ряд внутри модального окна «ℹ️ Свойства» (1.4.3):
      кнопка ℹ️ скрывается (окно уже открыто), а подтверждение 🗑️ остаётся
      ВНУТРИ окна: диалог работает как фрагмент (перерисовывается при клике
      по виджету без полного rerun), а полный st.rerun закрыл бы окно —
      подтверждение уезжало под картинку в сетку (баг 1.4.0).
    Состояния:
      ⭐ — назначить оригиналом (вернуть из trash, прежний оригинал → в trash);
      📋 — перенести в trash как дубликат;
      ↩️ — вернуть на исходное место (только для перенесённых);
      🗑️ — удалить с диска (с подтверждением, если ui.confirm_delete_files);
      ℹ️ — модальное окно свойств (только в сетке галереи).
    """
    fid = m["file_id"]
    gid = g["id"]
    kp = key_prefix
    arm_key = f"dlgarm_{fid}" if in_dialog else f"delarm_{fid}"
    armed_del = bool(st.session_state.get(arm_key))
    b_star, b_dup, b_ret, b_del, b_info = cols

    if b_star.button("⭐", key=f"{kp}act_{gid}_{fid}_star", use_container_width=True,
                     disabled=locked or (m["role"] == "kept" and not m["moved_to"]),
                     help="Это оригинал — пометить как оригинал и вернуть в исходное место "
                          "(прежний оригинал будет перенесён в trash)"):
        _do_action(settings, webops.star_as_original, fid, gid=gid,
                   toast=f"⭐ Оригинал группы #{gid}")
    if b_dup.button("📋", key=f"{kp}act_{gid}_{fid}_dup", use_container_width=True,
                    disabled=locked or bool(m["moved_to"]),
                    help="Это дубликат — пометить как дубликат и перенести в trash"):
        _do_action(settings, webops.mark_as_duplicate, fid, gid=gid,
                   toast=f"📋 Перенесён в trash (группа #{gid})")
    if b_ret.button("↩️", key=f"{kp}act_{gid}_{fid}_ret", use_container_width=True,
                    disabled=locked or not m["moved_to"],
                    help="Вернуть файл из trash на исходное место (останется дубликатом — "
                         "следующий move перенесёт снова, ⭐ назначит оригиналом)"):
        _do_action(settings, webops.return_file, fid, gid=gid,
                   toast="↩️ Возвращён на исходное место")
    if armed_del:
        if b_del.button("❗", key=f"{kp}act_{gid}_{fid}_del_go", use_container_width=True,
                        type="primary", disabled=locked,
                        help="Точно удалить файл с диска (необратимо)"):
            _do_action(settings, webops.delete_file, fid, gid=gid,
                       toast=f"🗑️ Удалён с диска (группа #{gid})")
        if b_info.button("❌", key=f"{kp}act_{gid}_{fid}_del_no", use_container_width=True,
                         help="Отменить удаление"):
            st.session_state.pop(arm_key, None)
            if in_dialog:
                return  # диалог перерисуется сам (семантика фрагмента); rerun закрыл бы окно
            st.session_state[f"grp_open_{gid}"] = True
            st.rerun()
    else:
        if b_del.button("🗑️", key=f"{kp}act_{gid}_{fid}_del", use_container_width=True,
                        disabled=locked,
                        help="Удалить файл с диска (необратимо)"
                             + (" — потребуется подтверждение" if settings.ui.confirm_delete_files else "")):
            if settings.ui.confirm_delete_files:
                st.session_state[arm_key] = True
                if in_dialog:
                    return  # подтверждение остаётся ВНУТРИ окна (1.4.3)
                st.session_state[f"grp_open_{gid}"] = True
                st.rerun()
            _do_action(settings, webops.delete_file, fid, gid=gid,
                       toast=f"🗑️ Удалён с диска (группа #{gid})")
        # ℹ️ только в сетке галереи: внутри окна свойств она бессмысленна (1.4.3)
        if not in_dialog and b_info.button("ℹ️", key=f"{kp}act_{gid}_{fid}_info",
                                           use_container_width=True, help="Свойства"):
            st.session_state.pop(f"dlgarm_{fid}", None)  # свежее открытие окна — без прежнего подтверждения
            _props_dialog(m, g, settings, locked)


# ----------------------------- вкладка «Логика» -----------------------------

def _logic_tab() -> None:
    docs_dir = Path(os.environ.get("DOCS_DIR", str(_ROOT / "docs")))
    f = docs_dir / "LOGIC.md"
    if not f.exists():
        st.info(
            f"Файл {f} не найден. В docker он монтируется как `./docs:/app/docs:ro`; "
            "при локальном запуске он ищется рядом с проектом в `docs/`."
        )
        return
    try:
        content = f.read_text(encoding="utf-8")
    except OSError as e:
        st.error(f"Не удалось прочитать {f}: {e}")
        return
    st.caption(f"источник: `{f}` · {len(content):,} символов · отображается «как есть» "
               f"(изменения файла подхватываются при следующем обновлении страницы)"
               .replace(",", " "))
    st.markdown(content)


# ----------------------------- сборка страницы -----------------------------

def main() -> None:
    t0 = time.perf_counter()  # замер рерана UI — начало скрипта

    try:
        row = engine_row()  # первый запрос: заодно проверка доступности БД
    except Exception as e:
        if getattr(e, "sqlstate", None) == "42P01":  # UndefinedTable: БД жива, схемы ещё нет
            st.warning("🆕 База данных доступна, но таблицы ещё не созданы: "
                       "схему создаёт движок при первом запуске (идемпотентный DDL).")
            st.markdown("Выполните **один раз** в терминале хоста из каталога проекта:")
            st.code("docker compose run --rm engine status", language="bash")
            st.caption("Команда ничего не индексирует и не переносит — только создаёт таблицы "
                       "и показывает статус. Затем обновите страницу (F5).")
        else:
            st.error(f"PostgreSQL недоступен: {type(e).__name__}: {e}")
            st.caption("Проверьте контейнер db (`docker compose up -d db`). "
                       "При первом запуске таблицы создаёт движок: `docker compose run --rm engine status`.")
        return

    settings = _load_settings_or_default()
    _banner(row)
    _sidebar(settings, row)

    tab_monitor, tab_log, tab_gallery, tab_logic = st.tabs(
        ["🖥 Монитор", "📜 Журнал", "🖼 Галерея", "🧠 Логика"]
    )
    with tab_monitor:
        monitor_fragment(settings)
    with tab_log:
        journal_fragment(settings)
    with tab_gallery:
        gallery_tab(settings, row)
    with tab_logic:
        _logic_tab()

    # Измерение производительности UI (ориентир < 300 мс при полной загрузке движка)
    dt_ms = (time.perf_counter() - t0) * 1000
    history: list[float] = st.session_state.setdefault("rerun_ms", [])
    history.append(dt_ms)
    st.session_state["rerun_ms"] = history[-30:]
    avg10 = sum(history[-10:]) / min(len(history), 10)
    with st.sidebar:
        st.metric(
            "UI rerun, мс", f"{dt_ms:.0f}",
            help="Полный реран скрипта UI (wall-time). Цель < 300 мс даже при индексации "
                 "движком на полной скорости. Автообновляемые вкладки (фрагменты) в замер не входят.",
        )
        st.caption(f"avg(10): {avg10:.0f} мс · max(30): {max(history):.0f} мс · "
                   f"замер встроен в приложение (time.perf_counter)")


main()
