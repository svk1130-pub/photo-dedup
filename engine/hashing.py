"""Перцептивный хэшинг: 8 вариантов D4 для каждого файла.

Инвариантность к поворотам/отражениям достигается вычислением imagehash.phash
для ВСЕХ 8 элементов группы D4: identity (исходное изображение), 3 поворота,
2 отражения и 2 диагональных (композиция отражения и поворота). В PIL.Image.Transpose
ровно 7 режимов — identity это сама картинка без transpose, поэтому вариант 0
хэширует изображение как есть.

Никакой min-канонизации: композиция поворота и отражения не входит в набор
из 5 трансформаций, из-за чего зеркальные копии терялись бы.
"""
from __future__ import annotations

from dataclasses import dataclass

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


def phash_to_bytes(h) -> bytes:
    """ImageHash → упакованные биты (hash_size=8 → 64 бита → 8 байт)."""
    return np.packbits(np.asarray(h.hash, dtype=bool).ravel()).tobytes()


def hex_part(b: bytes, part_len: int) -> str:
    """Префикс hex-представления хэша — ключ бакета."""
    return b.hex()[:part_len]


def compute_entry(
    path: str,
    size: int,
    mtime: float,
    *,
    hash_size: int = 8,
    hash_part_len: int = 4,
    old_id: int | None = None,
) -> Entry:
    """Полная обработка одного файла. Никогда не бросает исключений:
    любые ошибки декодирования → CorruptFile (чтобы не ретраить каждый запуск).
    """
    try:
        with Image.open(path) as img:
            w0, h0 = img.size
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

            phashes: list[bytes] = []
            parts: list[str] = []
            for mode in D4_TRANSFORMS:
                variant = img if mode is None else img.transpose(mode)
                hb = phash_to_bytes(_phash(variant, hash_size=hash_size))
                phashes.append(hb)
                parts.append(hex_part(hb, hash_part_len))
        return FileHashes(path, size, mtime, width, height, phashes, parts, old_id)
    except Image.DecompressionBombError as e:
        return CorruptFile(path, size, mtime, f"DecompressionBombError: {e}", old_id)
    except Exception as e:  # UnidentifiedImageError, OSError, ValueError, MemoryError...
        return CorruptFile(path, size, mtime, f"{type(e).__name__}: {e}", old_id)
