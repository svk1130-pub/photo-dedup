"""Streamlit UI — монитор и пульт.

Архитектурная линия: UI не делает тяжёлой работы — обработка изображений
(кроме ленивых миниатюр web/thumbs.py), обходы ФС и запросы без LIMIT запрещены;
соединения — только из пула (st.cache_resource, max 5).

ИСКЛЮЧЕНИЯ 1.4.0 (по прямому запросу пользователя, точечные):
  * кнопки «Монитора» запускают движок как subprocess ВНУТРИ web-контейнера
    (`python -m engine.cli run|scan`): буквально `docker compose run` из UI
    потребовал бы проброса docker.sock (root-доступ к хосту) и docker CLI в образе;
  * web/actions.py — возврат/удаление ОДНОГО файла из trash по кнопке в галерее
    (rw-доступ к src/trash; групповые операции по-прежнему только у движка).

Как и раньше, UI управляет движком флагами в БД: stop_requested (🛑 Стоп —
работает и для subprocess-движка: он опрашивает флаг), groups.confirmed
(подтверждение переноса в manual-режиме), settings.toml (rw-маунт).

Цель теста: реран UI < 300 мс при полностью загруженном движке — метрика
«UI rerun, ms» в сайдбаре; тяжёлые вкладки обновляются через st.fragment.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
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
    check_paths,
    container_to_host,
    load_settings,
    path_map,
    resolve_paths,
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


# ----------------------------- запуск движка из UI (1.4.0) -----------------------------

@st.cache_resource(show_spinner=False)
def _engine_procs() -> dict[str, subprocess.Popen]:
    """Реестр запущенных из UI процессов движка: {cmd: Popen}.

    ОБЯЗАТЕЛЬНО cache_resource, а не module-global: streamlit исполняет скрипт
    заново при каждом реране — обычная глобальная переменная сбрасывалась бы,
    и защита от двойного запуска не работала. cache_resource — один общий dict
    для всех сессий на всё время жизни web-процесса."""
    return {}


def _engine_log_path() -> Path:
    """Лог subprocess-движка: рядом с кэшем миниатюр (web-том), иначе — tmp."""
    base = Path(os.environ.get("THUMBS_DIR", "/data/cache/thumbs")).parent
    try:
        base.mkdir(parents=True, exist_ok=True)
        probe = base / ".probe"
        probe.write_text("")
        probe.unlink()
    except OSError:
        base = Path(tempfile.gettempdir())
    return base / "engine-sub.log"


def _engine_log_tail(limit_bytes: int = 6000) -> str:
    try:
        return _engine_log_path().read_bytes()[-limit_bytes:].decode("utf-8", errors="replace")
    except OSError:
        return ""


def _spawned_alive() -> list[tuple[str, subprocess.Popen]]:
    return [(cmd, p) for cmd, p in _engine_procs().items() if p.poll() is None]


def _spawn_engine(cmd: str, *, working: bool) -> None:
    """Запустить `python -m engine.cli <cmd>` как дочерний процесс web-контейнера.

    Эквивалент `docker compose run --rm engine <cmd>` по эффекту: тот же образ,
    env (PG_DSN, SETTINGS_PATH, PATH_MAP_*) и маунты — но без docker.sock в web.
    Статус/журнал/🛑 Стоп работают как обычно: движок пишет progress и флаги в БД.
    """
    if working:
        st.error("Движок уже работает (см. статус выше). Дождитесь завершения или нажмите 🛑 Стоп.")
        return
    if alive := _spawned_alive():
        st.error("Из UI уже запущено: " + ", ".join(f"engine {c} (pid {p.pid})" for c, p in alive))
        return
    log_path = _engine_log_path()
    try:
        fh = open(log_path, "wb")  # лог каждого запуска — с чистого листа
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "engine.cli", cmd],
                cwd=str(_ROOT),
                stdout=fh,
                stderr=subprocess.STDOUT,
            )
        finally:
            fh.close()
    except OSError as e:
        st.error(f"Не удалось запустить движок: {e}")
        return
    _engine_procs()[cmd] = proc
    st.toast(f"Движок запущен: engine {cmd} (pid {proc.pid}). Журнал — вкладка «📜 Журнал».", icon="🚀")
    st.rerun()


def _wipe_results() -> None:
    """«Очистить все»: результаты прогонов + журнал. Файлы на диске не затрагиваются."""
    from engine import db as engine_db  # лениво: тяжёлый импорт не нужен при старте UI

    try:
        with get_pool().connection() as conn:
            engine_db.wipe_results(conn)
        st.toast("БД очищена: индекс, группы, журнал. Файлы на диске не тронуты.", icon="💥")
    except Exception as e:
        st.error(f"Не удалось очистить БД: {type(e).__name__}: {e}")
    st.rerun()


def _clear_events() -> None:
    try:
        q("DELETE FROM events", fetch=False)
        st.toast("Журнал очищен", icon="🧹")
    except Exception as e:
        st.error(f"Не удалось очистить журнал: {type(e).__name__}: {e}")
    st.rerun(scope="fragment")


def _confirm_or(flag: str, enabled: bool, run: Any) -> None:
    """Двухшаговое подтверждение для фрагментов (внутри st.fragment диалоги
    ненадёжны): первый клик ставит флаг, повторный — выполняет действие."""
    if not enabled:
        run()
        return
    st.session_state[flag] = True
    st.rerun(scope="fragment")


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
        st.caption("…или кнопками «▶️ Запустить run» / «🔍 Запустить scan» на вкладке «🖥 Монитор».")
    elif row["stop_requested"]:
        st.info("🛑 Выставлен флаг остановки — движок завершит текущий файл/батч и остановится.")


def _sidebar(settings: Settings, row: dict | None) -> None:
    with st.sidebar:
        st.header("🖼 Photo Dedup")
        p = settings.paths
        # показываем HOST-пути (как в settings.toml/файловом менеджере хоста);
        # ⚠️ существование проверяем по контейнерному пути — web видит именно его
        for label, host, cont in (("src (архив)", p.src_host, p.src),
                                  ("trash (дубликаты)", p.trash_host, p.trash)):
            exists = Path(str(cont)).exists()
            st.markdown(f"**{label}:** `{host}`" + ("" if exists else " · ⚠️ не найден"))
        root_map, _ = path_map()
        if root_map:
            st.caption(f"Хост-корень маунта: `{root_map}` — задаётся один раз в .env "
                       f"(PHOTOS_ROOT). Подпапки (src/trash) правятся в редакторе ниже.")
        if row and isinstance(row.get("params"), dict) and row["params"]:
            with st.expander("Параметры текущего/последнего запуска движка", expanded=False):
                st.json(row["params"], expanded=1)
        _settings_editor(settings)
        st.caption(
            "Движок перечитывает settings.toml на границе этапов "
            "(scan → analyze → move) и при каждом старте."
        )


def _validate_paths_pair(src_h: str, trash_h: str) -> None:
    """Валидация путей ДО записи в settings.toml — те же правила, что у движка:
    непустые, внутри корня PATH_MAP_HOST (если задан), trash не внутри src."""
    if not src_h.strip() or not trash_h.strip():
        raise SettingsError("paths.src и paths.trash не могут быть пустыми")
    probe = load_settings(None)
    probe.paths.src_host, probe.paths.trash_host = src_h.strip(), trash_h.strip()
    resolve_paths(probe.paths)
    check_paths(probe)


def _settings_editor(settings: Settings) -> None:
    with st.sidebar.expander("⚙️ Эффективные настройки + редактор", expanded=True):
        st.json(settings_to_dict(settings), expanded=1)
        st.caption("Приоритет: CLI-флаги > settings.toml > дефолты. Сохранив форму, "
                   "вы правите settings.toml (те же значения увидит и CLI-движок).")
        with st.form("settings_form", border=True):
            scan, move, ui, analyze = settings.scan, settings.move, settings.ui, settings.analyze
            src_h = st.text_input(
                "paths.src — папка архива (путь на хосте)", value=settings.paths.src_host,
                help="Путь КАК НА ХОСТЕ (редактируется отсюда). Должен лежать внутри корня "
                     "PHOTOS_ROOT из .env — маунты docker на лету менять нельзя. "
                     "Применится при следующем запуске движка/этапе.",
            )
            trash_h = st.text_input(
                "paths.trash — папка дубликатов (путь на хосте)", value=settings.paths.trash_host,
                help="Аналогично src: host-путь внутри PHOTOS_ROOT; не должен быть внутри src.",
            )
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
            st.markdown("**Подтверждения опасных действий:**")
            cdf = st.checkbox(
                "Подтверждать удаление файлов (галерея, 🗑️)", value=ui.confirm_delete_files,
                help="Диалог «удалить безвозвратно?» перед удалением файла из trash",
            )
            cca = st.checkbox(
                "Подтверждать «Очистить все» (БД)", value=ui.confirm_clean_all,
                help="Диалог перед очисткой результатов прогонов и журнала",
            )
            ccl = st.checkbox(
                "Подтверждать очистку журнала", value=ui.confirm_clean_log,
                help="Диалог перед очисткой журнала (events)",
            )
            submitted = st.form_submit_button("💾 Сохранить в settings.toml")
        if submitted:
            updates: dict[str, dict[str, Any]] = {
                "paths": {"src": src_h.strip(), "trash": trash_h.strip()},
                "scan": {"threads": int(threads)},
                "analyze": {"threshold": int(threshold)},
                "move": {"mode": mode, "dry_run": bool(dry), "keep_by": keep_by},
                "ui": {
                    "page_size": int(page),
                    "refresh_sec": int(refresh),
                    "confirm_delete_files": bool(cdf),
                    "confirm_clean_all": bool(cca),
                    "confirm_clean_log": bool(ccl),
                },
            }
            try:
                _validate_paths_pair(src_h, trash_h)
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

    # --- запуск движка из UI (1.4.0) ---
    settings_now = _load_settings_quiet()
    spawned = _spawned_alive()
    st.caption("**Запуск движка** (subprocess внутри web-контейнера — эквивалент "
               "`docker compose run --rm engine …`):")
    b1, b2, b3, _ = st.columns([1.1, 1.1, 1.2, 3.5])
    busy = working or bool(spawned)
    if b1.button("▶️ Запустить run", disabled=busy,
                 help="Полный цикл scan → analyze → move (как `docker compose run --rm engine run`):"):
        _spawn_engine("run", working=working)
    if b2.button("🔍 Запустить scan", disabled=busy,
                 help="Только индексация и хэширование (как `docker compose run --rm engine scan`):"):
        _spawn_engine("scan", working=working)
    if b3.button("💥 Очистить все", type="primary", disabled=busy,
                 help="Очистить БД: результаты прогонов и журнал. ФАЙЛЫ НА ДИСКЕ НЕ ЗАТРАГИВАЮТСЯ"):
        _confirm_or("confirm_wipe", settings_now is None or settings_now.ui.confirm_clean_all,
                    _wipe_results)
    if st.session_state.get("confirm_wipe"):
        st.warning(
            "⚠️ Будут очищены **результаты прежних прогонов и журнал**: "
            "индекс файлов и хэши, все runs/группы, события (events). "
            "Сами файлы на диске (src и trash) не затрагиваются."
        )
        wy, wn, _ = st.columns([1, 1, 5])
        if wy.button("✅ Да, очистить", type="primary", key="wipe_yes"):
            st.session_state["confirm_wipe"] = False
            _wipe_results()
        if wn.button("Отмена", key="wipe_no"):
            st.session_state["confirm_wipe"] = False
            st.rerun(scope="fragment")
    if spawned:
        st.caption("🟢 Запущено из UI: " + ", ".join(
            f"`engine {c}` (pid {p.pid})" for c, p in spawned))
        with st.expander("stdout/stderr запуска из UI"):
            st.code(_engine_log_tail() or "(пока пусто)", language=None)

    if settings_now is not None and settings_now.move.mode == "manual":
        with st.expander("⚠️ Опасная зона (manual-режим): подтверждение переноса"):
            st.caption(
                "Подтверждённые группы будут физически перенесены в trash командой "
                "`docker compose run --rm engine move` (или кнопками запуска выше)."
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
    jc1, jc2 = st.columns([1, 5])
    if jc1.button("🧹 Очистить", disabled=not rows,
                  help="Удалить все записи журнала (events)"):
        s_now = _load_settings_quiet()
        _confirm_or("confirm_log", s_now is None or s_now.ui.confirm_clean_log, _clear_events)
    if st.session_state.get("confirm_log"):
        st.warning("⚠️ Очистить журнал — будут удалены все записи (events)?")
        ly, ln, _ = st.columns([1, 1, 5])
        if ly.button("✅ Да, очистить", type="primary", key="log_yes"):
            st.session_state["confirm_log"] = False
            _clear_events()
        if ln.button("Отмена", key="log_no"):
            st.session_state["confirm_log"] = False
            st.rerun(scope="fragment")
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
                "или кнопкой «▶️ Запустить run» на вкладке «🖥 Монитор».")
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
    # Члены ВСЕХ групп страницы одним батч-запросом (ANY по ids страницы) —
    # вместо N+1 запроса на каждую группу. Объём ограничен page_size группами.
    ids = [g["id"] for g in page_rows]
    member_rows = q(
        """
        SELECT gm.group_id, gm.file_id, gm.role, gm.moved_to,
               f.path, f.size, f.width, f.height
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
        _render_group(g, by_group.get(g["id"], []), settings)


def _render_group(g: dict, members: list[dict], settings: Settings) -> None:
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
    with st.expander(title):
        lines, moved = [], []
        for m in members:
            icon = "⭐" if m["role"] == "kept" else "📋"
            dims = f"{m['width']}×{m['height']}" if m["width"] and m["height"] else "?"
            base = f"{icon} `{container_to_host(m['path'])}` — {human_size(m['size'])}, {dims}"
            if m["moved_to"]:
                moved.append(m)
                lines.append(base + f" → в trash: `{container_to_host(m['moved_to'])}`")
            elif m["role"] == "kept":
                lines.append(base)
            else:
                lines.append(base + " · ещё в архиве (не перенесён)")
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

        # Кнопки над ПЕРЕНЕСЁННЫМИ дубликатами (файл физически лежит в trash):
        # «↩️ Вернуть» — на прежнее место (files.path), «🗑️ Удалить» — с диска.
        if moved:
            st.markdown("**Действия с перенесёнными дубликатами:**")
            for m in moved:
                r1, r2, r3, _ = st.columns([6, 1, 1, 2])
                r1.markdown(
                    f"📋 `{container_to_host(m['moved_to'])}` — {human_size(m['size'])}")
                if r2.button("↩️ Вернуть", key=f"ret_{m['file_id']}",
                             help="Вернуть файл из trash на прежнее место в архиве"):
                    _restore_member(m)
                if r3.button("🗑️ Удалить", key=f"del_{m['file_id']}",
                             help="Удалить файл с диска (безвозвратно)"):
                    _delete_member(m, settings)

        if show:
            _render_thumbs(members)


def _restore_member(m: dict) -> None:
    """Кнопка «↩️ Вернуть»: файл из trash — на прежнее место (web/actions.py)."""
    from web import actions  # лениво: PIL/numpy/imagehash не нужны при старте UI
    try:
        msg = actions.restore_file(SETTINGS_PATH, m["file_id"])
        st.toast(msg, icon="↩️")
    except Exception as e:
        st.error(f"Не удалось вернуть файл: {e}")
    st.rerun()


@st.dialog("🗑️ Удалить дубликат?", width="small")
def _delete_dialog(m: dict) -> None:
    st.markdown(f"Удалить файл **с диска безвозвратно**?\n\n`{container_to_host(m['moved_to'])}`")
    st.caption("Файл сейчас лежит в trash. После удаления восстановить его будет невозможно.")
    c1, c2, _ = st.columns([1, 1, 3])
    if c1.button("🗑️ Да, удалить", type="primary"):
        from web import actions
        try:
            actions.delete_file(SETTINGS_PATH, m["file_id"])
            st.toast("Файл удалён", icon="🗑️")
            st.rerun()
        except Exception as e:
            st.error(f"Не удалось удалить: {e}")  # диалог остаётся открытым
    if c2.button("Отмена"):
        st.rerun()


def _delete_member(m: dict, settings: Settings) -> None:
    """Кнопка «🗑️ Удалить»: диалог подтверждения (или сразу — если отключён в настройках)."""
    if settings.ui.confirm_delete_files:
        _delete_dialog(m)
        return
    from web import actions
    try:
        actions.delete_file(SETTINGS_PATH, m["file_id"])
        st.toast("Файл удалён", icon="🗑️")
    except Exception as e:
        st.error(f"Не удалось удалить: {e}")
    st.rerun()


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
