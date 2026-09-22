"""Ленивая генерация миниатюр с дисковым кэшем.

Единственное место в UI, где допустима работа с изображениями: исходник
никогда не уходит в st.image целиком — только даунскейл ~400px в JPEG.
Имя файла кэша = sha256(path); генерация — при первом просмотре.
"""
from __future__ import annotations

import hashlib
import io
import os
import uuid
from pathlib import Path

from PIL import Image, ImageOps

CACHE_DIR = Path(os.environ.get("THUMBS_DIR", "/data/cache/thumbs"))
THUMB_SIZE = 400


def get_thumbnail(path_str: str, size: int = THUMB_SIZE) -> bytes | None:
    """Байты JPEG-миниатюры из кэша или свежесгенерированные; None — не удалось."""
    p = Path(path_str)
    if not p.is_file():
        return None
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    key = hashlib.sha256(str(p).encode("utf-8")).hexdigest()[:32]
    cache_file = CACHE_DIR / f"{key}_{size}.jpg"
    if cache_file.is_file():
        try:
            return cache_file.read_bytes()
        except OSError:
            pass
    try:
        with Image.open(p) as img:
            if (img.format or "").upper() in ("JPEG", "MPO"):
                img.draft("RGB", (size, size))  # черновое декодирование — дёшево
            img = ImageOps.exif_transpose(img)
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((size, size))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
    except Exception:  # битый файл, DecompressionBomb, исчез по ходу — не роняем UI
        return None
    try:
        tmp = CACHE_DIR / f".{key}_{size}.{uuid.uuid4().hex}.tmp"
        tmp.write_bytes(buf.getvalue())
        tmp.replace(cache_file)  # атомарная замена — безопасно при параллельных сессиях
    except OSError:
        pass
    return buf.getvalue()
