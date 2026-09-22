"""Streamlit UI — тонкий монитор и пульт.

Запрещено by design: обработка изображений (кроме ленивых миниатюр из
web/thumbs.py), обходы файловой системы, запросы без LIMIT. Соединения —
только из пула (st.cache_resource, max 5). UI никогда не запускает движок
как subprocess — только читает БД и пишет флаги/команды:
  * stop_requested (кнопка Стоп),
  * groups.confirmed (подтверждение переноса в manual-режиме),
  * settings.toml (через отдельный rw-маунт).

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

from engine.settings import (  # noqa: E402
    Settings,
    SettingsError,
    load_settings,
    settings_to_dict,
    update_settings_file,
)

DSN = os.environ.get("PG_DSN", "postgresql://photo:photo@db:5432/photo")
SETTINGS_PATH = os.environ.get("SETTINGS_PATH", "/data/settings.toml")
STALE_SEC = 15.0  # статус старше — движок считается незапущенным

st.set_page_config(page_title="Photo Dedup", page_icon="🖼", layout="wide")

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


# ----------------------------- баннер и сайдбар -----------------------------

def _banner(row: dict | None) -> None:
    if row is None:
        st.error("Таблица status недоступна. Запустите движок один раз — схема создастся автоматически.")
        return
    if not is_working(row):
        stage = row["stage"]
        st.warning(
            f"Движок не запущен (последний этап: **{stage}**). "
            f"Просмотр прошлых результатов полностью доступен. Запуск:"
        )
        st.code("# в каталоге проекта\ndocker compose run --rm engine run", language="bash")
    elif row["stop_requested"]:
        st.info("🛑 Выставлен флаг остановки — движок завершит текущий файл/батч и остановится.")


def _sidebar(settings: Settings, row: dict | None) -> None:
    with st.sidebar:
        st.header("🖼 Photo Dedup")
        p = settings.paths
        for label, path in (("src (архив)", p.src), ("trash (дубликаты)", p.trash)):
            exists = Path(str(path)).exists()
            st.markdown(f"**{label}:** `{path}`" + ("" if exists else " · ⚠️ не найден"))
        st.caption("web читает src/trash **только для просмотра** (ro-маунты).")
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
                "move.keep_by — что считать оригиналом", ("size", "pixels"),
                index=("size", "pixels").index(move.keep_by),
                help="Применится: на этапе move (в т.ч. перевыбор оригинала в готовых группах)",
            )
            page = st.number_input(
                "ui.page_size — групп на странице галереи", 5, 100, value=ui.page_size,
                help="Применится: сразу",
            )
            refresh = st.number_input(
                "ui.refresh_sec — интервал обновления монитора, с", 1, 60, value=ui.refresh_sec,
                help="Применится: после перезапуска web (фрагменты создаются при старте процесса)",
            )
            submitted = st.form_submit_button("💾 Сохранить в settings.toml")
        if submitted:
            updates: dict[str, dict[str, Any]] = {
                "scan": {"threads": int(threads)},
                "analyze": {"threshold": int(threshold)},
                "move": {"mode": mode, "dry_run": bool(dry), "keep_by": keep_by},
                "ui": {"page_size": int(page), "refresh_sec": int(refresh)},
            }
            try:
                update_settings_file(SETTINGS_PATH, updates)
                st.toast("Настройки сохранены в settings.toml", icon="💾")
                st.rerun()
            except (SettingsError, OSError) as e:
                st.error(f"Не удалось сохранить настройки: {e}")


# ----------------------------- вкладка «Монитор» -----------------------------

@st.fragment(run_every=f"{MONITOR_REFRESH_SEC}s")
def monitor_fragment() -> None:
    tf = time.perf_counter()
    row = engine_row()
    if row is None:
        st.caption("Таблица status недоступна.")
        return
    working = is_working(row)
    total = int(row["total"] or 0)
    processed = int(row["processed"] or 0)
    pct = min(processed / total, 1.0) if total else 0.0
    speed = float(row["files_per_sec"] or 0.0)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Этап", str(row["stage"]))
    c2.metric("Обработано", f"{processed} из {total}", f"{pct * 100:.1f}%")
    c3.metric("Скорость", f"{speed:.1f} файлов/с")
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

    settings_now = _load_settings_quiet()
    if settings_now is not None and settings_now.move.mode == "manual":
        with st.expander("⚠️ Опасная зона (manual-режим): подтверждение переноса"):
            st.caption(
                "Подтверждённые группы будут физически перенесены в trash командой "
                "`docker compose run --rm engine move`."
            )
            _confirm_all_block(lr)


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
def journal_fragment() -> None:
    tf = time.perf_counter()
    rows = q("SELECT id, ts, level, message FROM events ORDER BY id DESC LIMIT 200")
    st.caption(f"последние {len(rows)} записей · автообновление {MONITOR_REFRESH_SEC} с · "
               f"фрагмент: {(time.perf_counter() - tf) * 1000:.0f} мс")
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

def gallery_tab(settings: Settings) -> None:
    lr = last_run()
    if lr is None:
        st.info("Анализ ещё не выполнялся. Запустите `docker compose run --rm engine run` "
                "(или отдельно `scan` + `analyze`).")
        return
    st.caption(
        f"Run **#{lr['id']}** от {lr['created_at']:%Y-%m-%d %H:%M} · threshold {lr['threshold']} · "
        f"галерея всегда показывает последний run; история хранится в analysis_runs"
    )

    page_size = settings.ui.page_size
    total = q("SELECT count(*) AS n FROM groups WHERE run_id=%s", (lr["id"],))[0]["n"]
    if total == 0:
        st.success("В последнем run группы не найдены — дубликатов нет (или хэшей нет).")
        return
    pages = (total + page_size - 1) // page_size
    st.session_state.setdefault("gallery_page", 0)
    st.session_state.gallery_page = min(st.session_state.gallery_page, pages - 1)

    nav1, nav2, nav3, _ = st.columns([1, 1, 2, 6])
    if nav1.button("◀", disabled=st.session_state.gallery_page == 0):
        st.session_state.gallery_page -= 1
        st.rerun()
    if nav2.button("▶", disabled=st.session_state.gallery_page >= pages - 1):
        st.session_state.gallery_page += 1
        st.rerun()
    nav3.markdown(f"**Страница {st.session_state.gallery_page + 1} / {pages}** · всего {total} групп")

    if settings.move.mode == "manual":
        st.info("Режим **manual**: `engine move` перенесёт только подтверждённые группы.")
    else:
        st.info("Режим **auto**: `engine move` перенесёт все группы последнего run.")

    page_rows = q(
        """
        SELECT id, member_count, total_size, kept_path, confirmed, info
        FROM groups WHERE run_id = %s
        ORDER BY total_size DESC, id
        LIMIT %s OFFSET %s
        """,
        (lr["id"], page_size, st.session_state.gallery_page * page_size),
    )
    for g in page_rows:
        _render_group(g, settings)


def _render_group(g: dict, settings: Settings) -> None:
    members = q(
        """
        SELECT gm.file_id, gm.role, gm.moved_to, f.path, f.size, f.width, f.height
        FROM group_members gm JOIN files f ON f.id = gm.file_id
        WHERE gm.group_id = %s
        ORDER BY (gm.role = 'kept') DESC, f.size DESC, f.path
        """,
        (g["id"],),
    )
    if not members:
        return
    info = g.get("info") if isinstance(g.get("info"), dict) else {}
    title = (f"Группа #{g['id']} · {g['member_count']} файлов · {human_size(g['total_size'])}"
             + (f" · мин. дистанция {info.get('min_distance')}" if info.get("min_distance") is not None else "")
             + (" · ✅ подтверждена" if g["confirmed"] else ""))
    with st.expander(title):
        lines = []
        for m in members:
            icon = "⭐" if m["role"] == "kept" else "🗑"
            dims = f"{m['width']}×{m['height']}" if m["width"] and m["height"] else "?"
            note = f" → перенесён: `{m['moved_to']}`" if m["moved_to"] else ""
            lines.append(f"{icon} `{m['path']}` — {human_size(m['size'])}, {dims}{note}")
        st.markdown("\n\n".join(lines))

        c1, c2, _ = st.columns([1, 1, 4])
        show = c1.toggle("🖼 Показать изображения", key=f"toggle_{g['id']}")
        already_all = all(m["moved_to"] for m in members)
        if settings.move.mode == "manual" and not already_all:
            if g["confirmed"]:
                if c2.button("↩️ Снять подтверждение", key=f"unc_{g['id']}"):
                    q("UPDATE groups SET confirmed = false WHERE id=%s", (g["id"],), fetch=False)
                    st.rerun()
            else:
                if c2.button("✅ Подтвердить для переноса", key=f"cf_{g['id']}"):
                    q("UPDATE groups SET confirmed = true WHERE id=%s", (g["id"],), fetch=False)
                    st.toast(f"Группа #{g['id']} подтверждена", icon="✅")
                    st.rerun()
        if show:
            _render_thumbs(members)


def _render_thumbs(members: list[dict]) -> None:
    from web import thumbs  # локальный импорт — не тормозит старт приложения

    per_row = 4
    for start in range(0, len(members), per_row):
        chunk = members[start:start + per_row]
        cols = st.columns(per_row)
        for col, m in zip(cols, chunk):
            src_path = m["moved_to"] or m["path"]  # галерея показывает актуальный путь
            data = thumbs.get_thumbnail(src_path)
            label = ("⭐ оригинал" if m["role"] == "kept" else "дубликат") \
                + f"\n{Path(src_path).name}\n{human_size(m['size'])}"
            if data is None:
                col.caption(f"🚫 не удалось показать:\n`{src_path}`")
            else:
                col.image(data, caption=label, use_container_width=True)


# ----------------------------- сборка страницы -----------------------------

def main() -> None:
    t0 = time.perf_counter()  # замер рерана UI — начало скрипта

    try:
        row = engine_row()  # первый запрос: заодно проверка доступности БД
    except Exception as e:
        st.error(f"PostgreSQL недоступен: {type(e).__name__}: {e}")
        st.caption("Проверьте контейнер db (`docker compose up -d db`). "
                   "При первом запуске таблицы создаёт движок: `docker compose run --rm engine status`.")
        return

    settings = _load_settings_or_default()
    _banner(row)
    _sidebar(settings, row)

    tab_monitor, tab_log, tab_gallery = st.tabs(["🖥 Монитор", "📜 Журнал", "🖼 Галерея"])
    with tab_monitor:
        monitor_fragment()
    with tab_log:
        journal_fragment()
    with tab_gallery:
        gallery_tab(settings)

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
