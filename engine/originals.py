"""Определение «оригинала» по времени снимка (keep_by=capture) и нейминг групп.

Иерархия времени снимка одного файла (первый успешный источник выигрывает):

1. Google Takeout sidecar ``<файл>.json`` рядом с файлом: ``photoTakenTime``
   → ``creationTime``. Оба в формате ``{"timestamp": "<unix-сек>",
   "formatted": "<локализованная дата>"}`` — парсится ТОЛЬКО timestamp,
   поле formatted локализовано и не парсится принципиально. Если оба поля
   есть и расходятся, приоритет у photoTakenTime: это момент съёмки, тогда
   как creationTime — момент добавления в Google Photos (у большинства
   файлов они совпадают). Защита от мусора: timestamps вне окна
   (0, now + 1 сутки) игнорируются; миллисекундныеepoch делятся на 1000.
2. Файловая система: min(birthtime, mtime). Копии файлов наследуют mtime
   источника, но получают НОВЫЙ birthtime — поэтому минимум двух времён
   ≈ время появления контента, и оригинал (созданный раньше) выигрывает.
   birthtime берётся из st_birthtime (macOS/BSD, часть Linux); на Windows
   st_ctime — это время создания, поэтому там он и используется. Если ФС
   не хранит birthtime — остаётся mtime.
3. mtime из БД (файл недоступен на диске — редкий edge на этапе move).

Тай-брейки при равном времени: name-penalty (число «копийных» маркеров в
имени: " (Copy 2)", " (1)", " — копия", "-edited"…) → лексикографический
путь. Это чинит кейс, когда IMG_20260912_163920.jpg идёт после своих
«(Copy N)» по сортировке и без метаданных считался бы дубликатом: при
равной информации о времени чистое имя выигрывает у копий.

Нейминг папок групп в trash: имя = префикс (move.group_name_prefix) +
имя файла-оригинала, с санитизацией под Windows/Linux/macOS и
гарантированной уникальностью внутри прогона.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("engine.originals")

#: Окно валидности unix-времени из sidecar-JSON: не в будущем (с запасом сутки).
_TS_MAX_FUTURE_SEC = 86400

#: Максимальная длина стема имени папки группы (защита от длинных путей Windows).
_MAX_STEM = 100


# ----------------------------- время снимка -----------------------------

def parse_takeout_ts(v) -> float | None:
    """photoTakenTime/creationTime из Takeout-JSON → unix-секунды (float) или None.

    Допустимые формы: {"timestamp": "1622812496", ...}, {"timestamp": 1622812496},
    число, строка цифр или ISO-строка ("2026-09-12T16:39:20Z"). Строка
    «formatted» не парсится (локализована). Мусор/вне окна → None.
    """
    if isinstance(v, dict):
        v = v.get("timestamp")
    if isinstance(v, bool):  # bool — подкласс int, отсекаем явно
        return None
    ts: float | None = None
    if isinstance(v, (int, float)):
        ts = float(v)
    elif isinstance(v, str):
        s = v.strip()
        if re.fullmatch(r"-?\d+", s):
            ts = float(s)
        else:
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ts = dt.timestamp()
    if ts is None:
        return None
    if ts > 1e12:  # защита: миллисекундная epoch → секунды
        ts /= 1000.0
    if not 0 < ts <= time.time() + _TS_MAX_FUTURE_SEC:
        return None
    return ts


def sidecar_capture_time(path: str) -> tuple[float, str] | None:
    """Время снимка из Takeout-sidecar ``<файл>.json``: photoTakenTime → creationTime.

    Строгий матчинг: sidecar ищется по точному имени ``<полный путь>.json``
    в той же папке (Takeout именует json по экспортированному файлу).
    Любая ошибка чтения/разбора → None (файл просто не имеет метаданных).
    """
    jp = path + ".json"
    try:
        if not os.path.isfile(jp):
            return None
        with open(jp, "rb") as f:
            data = json.load(f)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("photoTakenTime", "creationTime"):
        ts = parse_takeout_ts(data.get(key))
        if ts is not None:
            return ts, f"json {key}"
    return None


def fs_capture_time(path: str, st: os.stat_result | None = None) -> tuple[float, str] | None:
    """min(birthtime, mtime) файловой системы (см. докстринг модуля).

    Возвращает (время, метка источника) или None, если файл не читается.
    """
    st = os.stat(path) if st is None else st
    mtime = float(st.st_mtime)
    btime = getattr(st, "st_birthtime", None)
    if (btime is None or btime <= 0) and os.name == "nt":
        # Windows: st_ctime хранит время создания файла
        btime = float(st.st_ctime)
    if not btime or btime <= 0:
        return mtime, "fs mtime"
    btime = float(btime)
    if btime <= mtime:
        return btime, "fs birthtime"
    return mtime, "fs mtime"


def capture_time_of(path: str, mtime_db: float | None = None) -> tuple[float, str]:
    """Полная иерархия: json-sidecar → файловая система → mtime из БД."""
    sc = sidecar_capture_time(path)
    if sc is not None:
        return sc
    try:
        fsc = fs_capture_time(path)
    except OSError:
        fsc = None
    if fsc is not None:
        return fsc
    return (float(mtime_db) if mtime_db is not None else 0.0), "db mtime"


def fmt_capture(t: float) -> str:
    """Единый формат времени снимка для плана dry-run, info.txt и журнала."""
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ----------------------------- name-penalty -----------------------------

#: Маркеры «это копия» В КОНЦЕ стема (применяются циклически:
#: "IMG (Copy 3) (1)" → два маркера). Регистр не важен, локаль RU/EN.
_TRAILING_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\s*\((?:copy|копия|another copy|new copy)(?:\s+\d+)?\)$", re.IGNORECASE),
    re.compile(r"\s*[—–-]\s*копия(?:\s*\(\d+\))?$", re.IGNORECASE),   # Win RU: « — копия (2)»
    re.compile(r"\s*-\s*copy(?:\s*\(\d+\))?$", re.IGNORECASE),        # Win EN: " - Copy (2)"
    re.compile(r"\s*\(\d{1,4}\)$"),                                   # " (1)" — браузеры/Takeout
    re.compile(r"-edited$", re.IGNORECASE),                           # Google-версия "-edited"
    re.compile(r"\s*\(edited\)$", re.IGNORECASE),
)


def name_penalty(path: str) -> int:
    """Сколько «копийных» маркеров в конце имени файла (0 для чистого имени)."""
    stem = os.path.splitext(os.path.basename(path))[0]
    pen = 0
    for _ in range(10):
        for rx in _TRAILING_MARKERS:
            m = rx.search(stem)
            if m:
                stem = stem[: m.start()]
                pen += 1
                break
        else:
            break
    return pen


# ----------------------------- нейминг папок групп -----------------------------

_WIN_FORBIDDEN = set('<>:"/\\|?*')
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL"} \
    | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def sanitize_name(name: str, *, max_stem: int = _MAX_STEM) -> str:
    """Имя папки, безопасное для Windows/Linux/macOS.

    NFC-нормализация; запрещённые/управляющие символы → «_» (Windows:
    < > : " / \\ | ? * и коды < 32); хвостовые точки/пробелы срезаются
    (Windows не позволяет); зарезервированные имена (CON, NUL, COM1…)
    получают префикс «_»; стем длиннее max_stem обрезается. Пустое имя
    (после санитизации) → «group».
    """
    name = unicodedata.normalize("NFC", name)
    out = "".join(
        "_" if (c in _WIN_FORBIDDEN or ord(c) < 32 or ord(c) == 127) else c for c in name
    )
    stem, ext = os.path.splitext(out)
    stem = stem.rstrip(". ")
    ext = ext.rstrip(". ")  # «name. » → ext=". " → срезаем: Windows запрещает хвостовые точки
    if len(stem) > max_stem:
        stem = stem[:max_stem].rstrip(". ")
    if stem.upper() in _WIN_RESERVED:
        stem = "_" + stem
    if not stem:
        stem = "group"
    return stem + ext


def group_dir_name(kept_path: str | None, prefix: str, gid: int) -> str:
    """Имя папки группы в trash: префикс + имя файла-оригинала.

    Пример: kept="src/IMG_20260914_123649.jpg", prefix="_" → "_IMG_20260914_123649.jpg".
    Fallback — прежний нейминг "group_{gid}", если путь пуст.
    """
    base = os.path.basename(kept_path or "")
    if not base:
        return f"group_{gid}"
    return sanitize_name(f"{prefix}{base}")


def _nfc_casefold(s: str) -> str:
    return unicodedata.normalize("NFC", s).casefold()


def unique_name(candidate: str, used: set[str]) -> str:
    """Уникальное имя папки/файла: candidate → candidate__2 → candidate__3…

    Уникальность БЕЗ учёта регистра и с NFC-нормализацией — на macOS/Windows
    «IMG.jpg» и «img.jpg» одно и то же имя. used — множество занятых ключей,
    пополняется выбранным именем (вызов мутирует used).
    """
    key = _nfc_casefold(candidate)
    if key not in used:
        used.add(key)
        return candidate
    stem, ext = os.path.splitext(candidate)
    i = 2
    while True:
        cand = f"{stem}__{i}{ext}"
        k = _nfc_casefold(cand)
        if k not in used:
            used.add(k)
            return cand
        i += 1


def collect_used_names(trash: Path) -> set[str]:
    """Ключи уже занятых имён в trash (для коллизий между прогонами)."""
    try:
        entries = os.listdir(trash)
    except OSError:
        return set()
    return {_nfc_casefold(e) for e in entries}
