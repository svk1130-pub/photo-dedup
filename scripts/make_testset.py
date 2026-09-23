"""Генератор тестового набора для проверки приёмки (критерий 2):
оригиналы + повёрнутые на 90/180/270 копии, отражённые, диагональные
(зеркало+поворот), уменьшенные и перекодированные, плюс один битый файл.

Запуск:
    docker compose run --rm --entrypoint python engine scripts/make_testset.py /data/src/testset 5
    python scripts/make_testset.py ./src/testset 5     # локально, нужен Pillow
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

VARIANTS = {
    "rot90": Image.Transpose.ROTATE_90,
    "rot180": Image.Transpose.ROTATE_180,
    "rot270": Image.Transpose.ROTATE_270,
    "flip_lr": Image.Transpose.FLIP_LEFT_RIGHT,
    "flip_tb": Image.Transpose.FLIP_TOP_BOTTOM,
    "transpose": Image.Transpose.TRANSPOSE,     # зеркало + поворот на 90
    "transverse": Image.Transpose.TRANSVERSE,   # зеркало + поворот на 270
}


def make_base(idx: int, rng: random.Random) -> Image.Image:
    """Синтетическое «фото»: цветной фон + фигуры + лёгкое размытие (контент для phash)."""
    w = 900 + rng.randint(-100, 100)
    h = 700 + rng.randint(-80, 80)
    img = Image.new("RGB", (w, h), (rng.randint(0, 60), rng.randint(0, 60), rng.randint(0, 60)))
    dr = ImageDraw.Draw(img)
    for _ in range(24 + idx % 7):
        x1, y1 = rng.randint(0, w - 1), rng.randint(0, h - 1)
        x2, y2 = rng.randint(0, w - 1), rng.randint(0, h - 1)
        if x2 < x1:
            x1, x2 = x2, x1
        if y2 < y1:
            y1, y2 = y2, y1
        color = (rng.randint(60, 255), rng.randint(60, 255), rng.randint(60, 255))
        shape = rng.choice(["rect", "ellipse", "line"])
        width = rng.randint(1, 8)
        if shape == "rect":
            dr.rectangle([x1, y1, x2, y2], outline=color, width=width)
        elif shape == "ellipse":
            dr.ellipse([x1, y1, x2, y2], outline=color, width=width)
        else:
            dr.line([x1, y1, x2, y2], fill=color, width=width)
    return img.filter(ImageFilter.GaussianBlur(0.6))


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "./src/testset")
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    rng = random.Random(42)
    out.mkdir(parents=True, exist_ok=True)

    made = 0
    for i in range(n):
        base = make_base(i, rng)
        base.save(out / f"original_{i:03d}.jpg", "JPEG", quality=92)
        made += 1
        sub = out / "copies" / f"set_{i:03d}"
        sub.mkdir(parents=True, exist_ok=True)
        for name, mode in VARIANTS.items():
            base.transpose(mode).save(sub / f"{name}.jpg", "JPEG", quality=rng.choice((70, 80, 90)))
            made += 1
        small = base.resize((base.width // 2, base.height // 2))
        small.save(sub / "resized_q60.jpg", "JPEG", quality=60)
        made += 1

    (out / "corrupt_000.jpg").write_bytes(
        b"\xff\xd8\xff\xe0not-really-jpeg" + os.urandom(2048)
    )
    print(f"Создано: {made} изображений + 1 битый файл в {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
