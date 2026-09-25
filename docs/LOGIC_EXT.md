# Логика движка — расширенная версия (с функциями и фрагментами кода)

> Базовая версия — [LOGIC.md](LOGIC.md). Здесь те же темы, но с указанием
> конкретных функций `engine/*` и кратких фрагментов кода. Фрагменты слегка
> сокращены; полные версии — в исходниках (версия 1.4.0).

## 1. scan: батчи, resume, EXIF

**Обход и фильтры** — `engine/scan.py: iter_files(root, trash, extensions, recursive)`:
`os.walk(followlinks=False)`, пропуск `trash` (через `resolve().is_relative_to`),
скрытых каталогов и симлинков; сортировка — детерминизм порядка.

**Фоновый знаменатель прогресса** — `run_scan` + `_count_candidates`:
счёт кандидатов вынесен в поток-демон, готовое число забирается через
`total_holder` dict:

```python
total_holder: dict[str, int] = {}
def _count_candidates() -> None:
    total_holder["n"] = sum(1 for _ in iter_files(src, trash, exts, s.scan.recursive))
counter = threading.Thread(target=_count_candidates, name="scan-count", daemon=True)
counter.start()
def adopt_total() -> None:
    if stats.total == 0 and "n" in total_holder:
        stats.total = total_holder["n"]
```

Поток не трогает БД (psycopg-соединения не потокобезопасны) и не проверяет
stop-флаг — максимум, что он делает, это доподсчитывает знаменатель.

**Запись пачкой** — `_write_buffer(conn, buffer, existing, with_hashes)`:
одна транзакция на `scan.batch_size` файлов: `UPSERT_FILE` (ON CONFLICT (path)
DO UPDATE, включая `exif = EXCLUDED.exif`) → получение id → удаление старых
хэшей переиндексируемых → `UPSERT_HASH` × 8 вариантов.

**Resume-условие** — в `run_scan`:

```python
ex = existing.get(path)
if (ex is not None and not force
        and ex.size == size and ex.mtime == mtime
        and (ex.status == "corrupt" or ex.id in with_hashes)):
    stats.skipped += 1  # resume: без изменений
    continue
```

Файл со статусом `ok`, но без хэшей (например, после ручного ↩️) автоматически
попадает на переиндексацию — индекс самозаживляется.

**Извлечение EXIF** — `engine/hashing.py`:

* `extract_exif(img, *, width, height, mtime) -> dict` — теги читаются по
  числовым кодам EXIF (271 Make, 272 Model, 33434 ExposureTime, 33437 FNumber,
  34855 ISO, 34850 ExposureProgram, 37385 Flash, 37383 MeteringMode, 37386
  FocalLength, 36867 DateTimeOriginal из `get_ifd(0x8769)`); рационалы
  нормализуются `_exif_float` (IFDRational / tuple (num,den) / число);
  человекочитаемые форматы: `_fmt_exposure` («1/33 s»), `_fmt_flash`
  («No, auto»), `_fmt_ts` («2026:09:12 …» → «2026-09-12 …»);
* вызов из `compute_entry(..., read_exif=s.scan.read_exif)`, обёрнут в
  `try/except` — свойства не могут сломать индексацию;

```python
exif_dict = None
if read_exif:
    try:
        exif_dict = extract_exif(img, width=width, height=height, mtime=mtime)
    except Exception:
        exif_dict = None
```

## 2. analyze: бакеты, Хэмминг, union-find

**Потоковый перебор бакетов** — `run_analyze`: серверный курсор
`conn.cursor(name="analyze_buckets")` с `itersize=20000` по
`SELECT hash_part, file_id, phash FROM hashes ORDER BY hash_part`; строки
группируются по `hash_part` на лету, закрытие бакета triggers
`_process_bucket(uf, rows, threshold)`.

**2D-чанкинг** — `_bucket_pairs(fids, ints, threshold)`: сравнение — блоками
1024×1024, «столбцовые» блоки целиком левее строк пропускаются (j > i
невозможен), поэтому каждая пара считается ровно один раз:

```python
for start in range(0, n, block):
    for cstart in range(0, n, block):
        if cstop <= start:
            continue
        xored = arr[start:stop, None] ^ arr[None, cstart:cstop]
        d = np.bitwise_count(xored)
        ok = (d <= threshold) & (fa[None, cstart:cstop] != fa[start:stop, None])
        ok &= col_idx[None, cstart:cstop] > col_idx[start:stop][:, None]
        for i, jj in np.argwhere(ok):
            yield ...
```

Пик памяти ≈ блок 1024×1024 uint64 ≈ 8 МБ (плюс служебные), независимо от n.
Для хэшей > 64 бит — резерв `_bucket_pairs_python` (XOR + `int.bit_count()`).

**Смешанные длины** — `_process_bucket`: бакет делится на подгруппы по длине
байтового хэша, каждая сравнивается независимо (после смены `hash_size` без
`--force` старые 8-байтные и новые 16-байтные хэши несравнимы).

**Транзитивное замыкание** — `UnionFind` (path compression, `min_edge` для
`groups.info.min_distance`): `union(a, b, dist)` для каждой пары-кандидата.

**Персистенция групп** — финал `run_analyze`: компоненты ≥ 2 файлов,
сортировка по суммарному размеру, `INSERT INTO groups …` + `group_members`
(kept/dup по предварительному `choose_kept`); остановленный run удаляется.

## 3. Выбор оригинала (`engine/analyze.py: choose_kept`, `engine/originals.py`)

Ключ сортировки для `keep_by=capture` (минимальный выигрывает):

```python
def key(r):
    t = r.get("capture_time")
    if t is None:
        t = float(r.get("mtime") or 0.0)
    return (t, name_penalty(r["path"]), r.get("ctime") or 0.0, r["path"])
```

1. `capture_time` — `capture_time_of(path, mtime_db, st)`: json-sidecar
   (`sidecar_capture_time`: строгий матч `<путь>.json`, `photoTakenTime` →
   `creationTime`, окно валидности в `parse_takeout_ts`) → ФС
   (`fs_capture_time`: `min(birthtime, mtime)`; Windows — `st_ctime`) → БД.
2. `name_penalty` — циклическая срезка «копийных» хвостов
   (`_TRAILING_MARKERS`: `(Copy N)`, ` (1)`, `— копия`, `- Copy`, `-edited`…).
3. `ctime` — ранний ctime выигрывает: лечит кейс «копия с укороченным именем»
   (`2.jpg`): время снимка у копии совпадает с оригиналом (наследованный
   mtime), но ctime указывает момент создания копии. Поле заполняется на
   этапе move одним `os.stat` на файл; на analyze его нет — все None
   (→ 0.0) не меняют порядок.
4. путь — детерминизм.

```python
# engine/move.py: _process_group
for m in present:
    try:
        st = os.stat(m["path"])
    except OSError:
        st = None
    t, src_lbl = capture_time_of(m["path"], m.get("mtime"), st=st)
    m["capture_time"], m["capture_src"] = t, src_lbl
    m["ctime"] = float(st.st_ctime) if st is not None else None
kept = choose_kept(present, keep_by)
```

## 4. move: перенос, имена, recovery

**Имена папок** — `originals.group_dir_name(kept_path, prefix, gid)` =
`sanitize_name(prefix + basename)`; `sanitize_name` — NFC, Windows-запреты
(`<>:"/\|?*`, коды <32), зарезервированные имена (CON/NUL/COM1…), хвостовые
точки/пробелы, стем ≤ 100; `unique_name(candidate, used)` — коллизии
`__2, __3…` без учёта регистра; `used` сеется из листинга trash
(`collect_used_names`) — коллизии между прогонами.

**Внутрипапочные коллизии** — `move._unique_target(dest, name)`:
`name.ext → name_1.ext → name_2.ext…`.

**Файловые операции → затем БД** — `_process_group`:

```python
os.makedirs(dest, exist_ok=True)
for m in present:
    if m["file_id"] == kept["file_id"]:
        continue
    target = _unique_target(dest, os.path.basename(m["path"]))
    shutil.move(m["path"], str(target))
    m["moved_to"] = str(target)
_write_info(dest, gid, run_id, kept, moved_now, keep_by, threshold, …)
_finalize_group_db(ctx, conn, gid, members, dest, keep_by, threshold, kept=kept, …)
```

**`_finalize_group_db`** — одна транзакция: `role='dup', moved_to, moved_at`
(с `COALESCE(moved_at, now())` — recovery не перетирает время), `role='kept'`
для оригинала, удаление хэшей «ушедших» (`gone_ids`), `files.status='moved'`,
обновление `groups.kept_path` и `info` (group_dir, moved_count, точные копии,
capture-время оригинала).

**Crash-recovery** — `_process_group`, ветка `if missing:`: кандидаты-папки —
именованная папка группы (выводится из имён членов), legacy `group_{gid}` и
папки других членов; аллокация имён повторяется детерминированно:

```python
placed: dict[tuple[str, str], int] = {}
for m in already:            # занятые слоты из уже зафиксированных moved_to
    key = (os.path.dirname(m["moved_to"]), os.path.basename(m["path"]))
    placed[key] = placed.get(key, 0) + 1
for m in missing:
    for d in cand_dirs:
        k = placed.get((str(d), base), 0)
        name = base if k == 0 else f"{stem}_{k}{ext}"   # реплей _unique_target
        cand = d / name
        if cand.exists() and cand.stat().st_size == m["size"]:
            m["moved_to"] = str(cand); placed[(str(d), base)] = k + 1
            break
```

Проверка размера отсекает чужие файлы; одноимённые дубликаты получают
`IMG.jpg`, `IMG_1.jpg` без «приклейки» к одному слоту.

**sha256-верификация** — `file_sha256` (чанковое чтение) оригинала и каждого
дубликата перед переносом; результат — в плане dry-run, `info.txt`
(`exact_moved`) и `groups.info` (`exact_kept_copies`, `dups_sha_checked`).

## 5. Ручные операции (`engine/webops.py`)

Общий каркас: `_fetch_member` (свежая группа по `max(group_id)` — галерея
работает с последним run), `_group_dir` (существующий `info.group_dir` не
переименовывается — на него смотрят `moved_to` и recovery; новый — по имени
назначаемого оригинала с уникализацией по всему trash), `_restore_target`
(исходный путь, при занятости — `*_restored.*`), `_demote_to_trash`
(файловое перемещение → затем транзакция: роль/`moved_to`/удаление хэшей/
`files.status='moved'`), `_cleanup_dir` (переписать info.txt или удалить
пустую папку), `_event` (журнал в events).

**⭐ обмен ролями** — `star_as_original(conn, s, file_id)`:

```python
# 1) демотировать текущего оригинала (если есть и другой файл)
cur_kept = next((x for x in members if x["role"] == "kept" and x["moved_to"] is None
                 and x["file_id"] != file_id), None)
if cur_kept is not None and os.path.exists(cur_kept["path"]):
    demoted_to = _demote_to_trash(conn, s, cur_kept, dest)
# 2) вернуть/назначить нового оригинала
if m["moved_to"]:
    target = _restore_target(m["path"])
    shutil.move(m["moved_to"], str(target))
    new_path = str(target)
# 3) одна транзакция: role='kept', moved_to=NULL, files.status='ok',
#    groups.kept_path = new_path
```

**📋 с автовыбором оригинала** — `mark_as_duplicate`: если демотировали kept,
новый выбирается `choose_kept` по текущему `keep_by` (с capture-временем
и ctime — тем же кодом, что на этапе move), и результат журналируется.

**↩️ возврат** — `return_file`: `shutil.move(moved_to → target)`, затем
`UPDATE group_members SET moved_to=NULL, moved_at=NULL WHERE file_id=%s`
(во ВСЕХ группах, куда входил файл) + `files.status='ok', path=target`.
Роль остаётся `dup` — следующий `move` перенесёт снова; `⭐` назначит оригиналом.

**🗑️ удаление** — `delete_file`: `os.unlink(moved_to or path)`, затем
`DELETE FROM files WHERE id=%s` (каскад в hashes/group_members), пересчёт
`member_count/total_size` групп, удаление опустевшей группы.

**Guard параллельности**: функции не проверяют статус движка сами —
это делает вызывающая сторона (UI — `is_working(row)`; CLI — `_guard_idle`
в `engine/cli.py`), чтобы guard был одинаково строг и не обходился.

## 6. undo и очистка БД

```python
TRUNCATE_ALL = """
    TRUNCATE events, group_members, groups, analysis_runs, hashes, files RESTART IDENTITY
"""
```

`undo_all(conn, s, *, dry=False)`:

1. выборка всех `moved_to IS NOT NULL` (дедупликация по file_id — файл мог
   входить в группы разных прогонов);
2. `dry=True` — только план (используется `engine undo --dry-run`);
3. возврат каждого файла (`shutil.move`, занятые пути → `_restored`), после
   каждого — транзакция с гашением `moved_to` и `status='ok'` (при частичном
   успехе БД остаётся консистентной);
4. `_cleanup_all_dirs` — удаление пустых папок групп;
5. **если проблем нет** — `truncate_results` (TRUNCATE + сброс singleton
   `status`) и запись в свежий журнал; **если есть** — очистка отложена,
   проблемы перечислены, повторный undo пропустит уже возвращённые
   (`moved_to=NULL`).

`clean_db(conn)` — то же TRUNCATE без файловых операций, с предупреждением
в журнале, если в trash оставались перенесённые файлы.

## 7. Настройки: чтение/запись (`engine/settings.py`)

* `load_settings(path, overrides)` — `tomllib` → dataclass-конфиги с построчной
  валидацией (`_err` добавляет `[секция] ключ (строка N)`), cross-валидация
  (`hash_part_len ≤ hash_size*8`, threshold ≤ разрядность), CLI-overrides
  (`scan.threads`, `move.keep_by`, …) применяются последними.
* `update_settings_file(path, updates)` — атомарная запись с fallback:

```python
payload = tomli_w.dumps(data).encode("utf-8")
with open(tmp, "wb") as f: f.write(payload); f.flush(); os.fsync(f.fileno())
try:
    os.replace(tmp, p)          # атомарно: движок видит целостный файл
except OSError:
    with open(p, "wb") as f:    # settings.toml — точка маунта одиночного файла:
        f.write(payload)        # rename поверх неё даёт EBUSY (Errno 16) —
        f.flush(); os.fsync(f.fileno())   # пишем на месте
    tmp.unlink(missing_ok=True)
```

Именно это чинит ошибку UI «Device or resource busy» при любом способе
маунта settings.toml.
* `container_to_host(path)` — отображение контейнерных путей в хост-вид
  для UI (`/data/src/x.jpg` → `$PHOTOS_ROOT/src/x.jpg`); сами `[paths]` —
  контейнерные и от места архива не зависят.

## 8. Журнал и статусы

* `events.EventLog` (`engine/events.py`) — INSERT в `events` + дублирование в
  stdout (`logging`); чистка хвоста: раз в 200 записей удаляется всё, кроме
  последних 1000. Отдельное управляющее соединение — события видны в UI даже
  между батчами основной записи.
* `status` singleton (`engine/db.py: reset_status/update_progress/set_stage`)
  — по нему UI и `engine status` видят этап/прогресс/скорость; `engine_running`
  (свежесть `updated_at` ≤ 15 c + рабочий этап) — guard для ручных операций.
