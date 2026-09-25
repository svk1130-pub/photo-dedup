"""Перцептивный хэшинг: 8 вариантов D4 для каждого файла.

Инвариантность к поворотам/отражениям достигается вычислением imagehash.phash
для ВСЕХ 8 элементов группы D4: identity (исходное изображение), 3 поворота,
2 отражения и 2 диагональных (композиция отражения и поворота). В PIL.Image.Transpose
ровно 7 режимов — identity это сама картинка без transpose, поэтому вариант 0
хэширует изображение как есть.

Никакой min-канонизации: композиция поворота и отражения не входит в набор
из 5 трансформаций, из-за чего зеркальные копии терялись бы.

Здесь же extract_exif(): человекочитаемые EXIF-свойства для окна «Свойства»
в UI (scan.read_exif → files.exif jsonb) — только чтение заголовков APP1,
без декодирования пикселей.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
from PIL import Image, ImageOps
from imagehash import phash as _phash

try:  # опционально: pillow-heif (в requirements.txt закомментирован)
    import pillow_heif  # type: ignore

    pillow_heif.register_heif_opener()
except ImportError:
    pass

#: Полная группа D4 в фиксированном порядке (индекс варианта 0..7).
#: None = IDENTITY (Pillow не имеет такого режима в Image.Transpose —
#: единичный элемент группы это исходное изображение без трансформации).
D4_TRANSFORMS: tuple[Image.Transpose | None, ...] = (
    None,  # 0: IDENTITY
    Image.Transpose.ROTATE_90,        # 1
    Image.Transpose.ROTATE_180,       # 2
    Image.Transpose.ROTATE_270,       # 3
    Image.Transpose.FLIP_LEFT_RIGHT,  # 4
    Image.Transpose.FLIP_TOP_BOTTOM,  # 5
    Image.Transpose.TRANSPOSE,        # 6 (зеркало + поворот 90)
    Image.Transpose.TRANSVERSE,       # 7 (зеркало + поворот 270)
)

_PREVIEW = (512, 512)  # целевой размер img.draft() для JPEG (быстрое декодирование)

_EXIF_ORIENTATION = 0x0112  # тег 274
_SWAP_ORIENTATIONS = (5, 6, 7, 8)  # EXIF-ориентации, меняющие ширину и высоту местами


@dataclass(slots=True)
class FileHashes:
    """Успешно проиндексированный файл."""

    path: str
    size: int
    mtime: float
    width: int
    height: int
    phashes: list[bytes]  # 8 × (hash_size*hash_size/8) байт
    parts: list[str]      # 8 × hex-префикс для бакета
    old_id: int | None = None
    exif: dict | None = None  # свойства для окна «Свойства» в UI (см. extract_exif)


@dataclass(slots=True)
class CorruptFile:
    """Битый/нечитаемый файл (включая DecompressionBombError, UnidentifiedImageError)."""

    path: str
    size: int
    mtime: float
    error: str
    old_id: int | None = None


Entry = FileHashes | CorruptFile


def hamming_distance(a: bytes, b: bytes) -> int:
    """Расстояние Хэмминга между двумя равными по длине байтовыми хэшами."""
    return (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).bit_count()


def file_sha256(path: str, chunk: int = 1 << 20) -> bytes:
    """Точный sha256 содержимого файла (чанками, экономно по памяти).

    Используется на этапе move для верификации байт-в-байт копий:
    равенство sha256 гарантирует идентичность файлов независимо от phash
    и порога Хэмминга. Бросает OSError, если файл недоступен.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            h.update(blk)
    return h.digest()


def phash_to_bytes(h) -> bytes:
    """ImageHash → упакованные биты (hash_size=8 → 64 бита → 8 байт)."""
    return np.packbits(np.asarray(h.hash, dtype=bool).ravel()).tobytes()


def hex_part(b: bytes, part_len: int) -> str:
    """Префикс hex-представления хэша — ключ бакета."""
    return b.hex()[:part_len]


# ----------------------------- EXIF-свойства -----------------------------

# Теги base-IFD / EXIF-IFD (номера — стандарт EXIF; имена в PIL.ExifTags зависят
# от версии Pillow, поэтому используем числовые константы напрямую).
_TAG_MAKE = 271
_TAG_MODEL = 272
_TAG_DATETIME = 306
_TAG_EXIF_IFD = 0x8769
_TAG_DATETIME_ORIGINAL = 36867
_TAG_EXPOSURE_TIME = 33434
_TAG_FNUMBER = 33437
_TAG_EXPOSURE_PROGRAM = 34850
_TAG_ISO = 34855
_TAG_METERING_MODE = 37383
_TAG_FLASH = 37385
_TAG_FOCAL_LENGTH = 37386

_EXPOSURE_PROGRAMS = {
    0: "Auto", 1: "Manual", 2: "Auto", 3: "Aperture-priority AE",
    4: "Shutter speed priority AE", 5: "Creative (Slow speed)",
    6: "Action (High speed)", 7: "Portrait", 8: "Landscape",
}
_METERING_MODES = {
    1: "Average", 2: "Center weighted average", 3: "Spot",
    4: "Multi-spot", 5: "Multi-segment", 6: "Partial",
}


def _exif_float(v) -> float | None:
    """EXIF-рационал → float. Pillow возвращает IFDRational, tuple (num, den) или число."""
    try:
        if isinstance(v, tuple):
            num, den = v[0], (v[1] if len(v) > 1 else 1)
            den = float(den)
            return float(num) / den if den else None
        f = float(v)
        return f if f == f else None  # отсекаем NaN
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _fmt_exposure(sec: float) -> str:
    if sec <= 0:
        return "—"
    if sec >= 1:
        return f"{sec:g} s"
    return f"1/{round(1 / sec)} s"


def _fmt_flash(v: int) -> str:
    fired = "Yes" if v & 1 else "No"
    mode_bits = v & 0x18
    mode = "auto" if mode_bits == 0x18 else ("on" if v & 0x08 else ("off" if v & 0x10 else ""))
    if (not (v & 1)) and mode == "on":
        mode = ""  # 0x08 = «On, Did not fire» — противоречие, режим не показываем
    return f"{fired}, {mode}" if mode else fired


def _fmt_ts(s: str) -> str:
    """EXIF «2026:09:12 16:39:20» → «2026-09-12 16:39:20» (иначе как есть)."""
    parts = s.strip().split(" ", 1)
    if len(parts) == 2 and len(parts[0]) == 10 and parts[0].count(":") == 2:
        return f"{parts[0].replace(':', '-')} {parts[1]}"
    return s.strip()


def extract_exif(img: Image.Image, *, width: int, height: int, mtime: float,
                 fmt: str | None = None) -> dict:
    """Словарь человекочитаемых свойств для окна «Свойства» в UI (files.exif jsonb).

    Читает только заголовки (APP1) — декодирование пикселей не требуется,
    поэтому стоимость на этапе scan ничтожна на фоне phash-хэширования.
    fmt — исходный формат файла (Pillow теряет img.format после
    exif_transpose, который возвращает новое изображение); если не задан —
    берётся img.format. Отсутствующие теги просто не попадают в словарь;
    createdOn есть всегда (DateTimeOriginal → DateTime → mtime файла).
    """
    fmt = fmt or img.format or "?"
    out: dict[str, str] = {
        "imageType": f"{fmt.lower()} ({fmt})",
        "width": f"{width} pixels",
        "height": f"{height} pixels",
    }
    try:
        exif = img.getexif()
        ifd = exif.get_ifd(_TAG_EXIF_IFD) if exif else {}
    except Exception:  # битый/экзотический APP1 — свойства не причина падать
        exif, ifd = {}, {}
    make = exif.get(_TAG_MAKE)
    model = exif.get(_TAG_MODEL)
    if make:
        out["cameraBrand"] = str(make).strip()
    if model:
        out["cameraModel"] = str(model).strip()
    et = _exif_float(ifd.get(_TAG_EXPOSURE_TIME))
    if et is not None:
        out["exposureTime"] = _fmt_exposure(et)
    ep = ifd.get(_TAG_EXPOSURE_PROGRAM)
    if ep is not None and _exif_float(ep) is not None:
        out["exposureProgram"] = _EXPOSURE_PROGRAMS.get(int(_exif_float(ep) or 0), str(ep))
    fn = _exif_float(ifd.get(_TAG_FNUMBER))
    if fn:
        out["apertureValue"] = f"F{fn:g}"
    iso = ifd.get(_TAG_ISO)
    if iso is not None:
        try:
            out["isoSpeedRating"] = str(int(iso))
        except (TypeError, ValueError):
            pass
    fl = ifd.get(_TAG_FLASH)
    if fl is not None and _exif_float(fl) is not None:
        out["flashFired"] = _fmt_flash(int(_exif_float(fl) or 0))
    mm = ifd.get(_TAG_METERING_MODE)
    if mm is not None and _exif_float(mm) is not None:
        out["meteringMode"] = _METERING_MODES.get(int(_exif_float(mm) or 0), str(mm))
    focal = _exif_float(ifd.get(_TAG_FOCAL_LENGTH))
    if focal:
        out["focalLength"] = f"{focal:g} mm"
    created = None
    for v in (ifd.get(_TAG_DATETIME_ORIGINAL), exif.get(_TAG_DATETIME)):
        if isinstance(v, str) and v.strip():
            created = _fmt_ts(v)
            break
    out["createdOn"] = created or datetime.fromtimestamp(mtime, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    return out


def compute_entry(
    path: str,
    size: int,
    mtime: float,
    *,
    hash_size: int = 8,
    hash_part_len: int = 4,
    old_id: int | None = None,
    read_exif: bool = False,
) -> Entry:
    """Полная обработка одного файла. Никогда не бросает исключений:
    любые ошибки декодирования → CorruptFile (чтобы не ретраить каждый запуск).
    read_exif=True → словарь свойств в FileHashes.exif (см. extract_exif).
    """
    try:
        with Image.open(path) as img:
            w0, h0 = img.size
            fmt0 = img.format  # после exif_transpose теряется
            try:
                orientation = int(img.getexif().get(_EXIF_ORIENTATION, 1) or 1)
            except Exception:
                orientation = 1
            # JPEG/MPO: черновое декодирование до ~512 px до любого pixel-доступа —
            # многократное ускорение на больших архивах.
            if (img.format or "").upper() in ("JPEG", "MPO"):
                img.draft("RGB", _PREVIEW)
            # Pillow не применяет EXIF-поворот сам — применяем явно.
            img = ImageOps.exif_transpose(img)
            if img.mode != "RGB":
                img = img.convert("RGB")
            width, height = (h0, w0) if orientation in _SWAP_ORIENTATIONS else (w0, h0)

            exif_dict: dict | None = None
            if read_exif:
                try:  # свойства не должны ломать индексацию
                    exif_dict = extract_exif(img, width=width, height=height,
                                             mtime=mtime, fmt=fmt0)
                except Exception:
                    exif_dict = None

            phashes: list[bytes] = []
            parts: list[str] = []
            for mode in D4_TRANSFORMS:
                variant = img if mode is None else img.transpose(mode)
                hb = phash_to_bytes(_phash(variant, hash_size=hash_size))
                phashes.append(hb)
                parts.append(hex_part(hb, hash_part_len))
        return FileHashes(path, size, mtime, width, height, phashes, parts, old_id, exif_dict)
    except Image.DecompressionBombError as e:
        return CorruptFile(path, size, mtime, f"DecompressionBombError: {e}", old_id)
    except Exception as e:  # UnidentifiedImageError, OSError, ValueError, MemoryError...
        return CorruptFile(path, size, mtime, f"{type(e).__name__}: {e}", old_id)
