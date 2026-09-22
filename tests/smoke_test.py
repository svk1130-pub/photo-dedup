"""Smoke-тесты для photo-dedup: settings, D4-инвариантность хэшей, bucket-поиск."""
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

import tempfile
import tomli_w

# ---------- 1. settings: загрузка, дефолты, приоритет CLI, ошибки с номером строки ----------
from engine.settings import SettingsError, check_paths, load_settings, update_settings_file

s = load_settings(None)
assert s.scan.threads == 8 and s.analyze.threshold == 4 and s.move.mode == "auto"
print("1a. defaults OK")

with tempfile.NamedTemporaryFile("wb", suffix=".toml", delete=False) as f:
    tomli_w.dump({
        "paths": {"src": "/tmp/arch", "trash": "/tmp/bin"},
        "scan": {"threads": 3, "extensions": [".jpg", ".PNG"]},
        "analyze": {"threshold": 6},
        "move": {"mode": "manual", "dry_run": True, "keep_by": "pixels"},
    }, f)
    good = f.name
s = load_settings(good, overrides={"scan.threads": 11, "move.mode": "auto"})
assert s.paths.src == Path("/tmp/arch")
assert s.scan.threads == 11, "CLI override must win"
assert s.scan.extensions == (".jpg", ".png"), "extensions lowercased"
assert s.analyze.threshold == 6 and s.move.dry_run and s.move.mode == "auto"
assert s.move.keep_by == "pixels"
print("1b. load + CLI-priority OK")

s2 = load_settings(good, overrides={"scan.threads": 11})
assert s2.scan.threads == 11
print("1c. reload at stage boundary OK")

bad = Path(tempfile.mkdtemp()) / "bad.toml"
bad.write_text('[scan]\nthreads = "oops"\n')
try:
    load_settings(bad)
    raise AssertionError("expected SettingsError")
except SettingsError as e:
    assert "scan" in str(e) and "threads" in str(e) and "строка 2" in str(e), str(e)
    print("1d. type error with line number OK:", str(e).splitlines()[0])

bad2 = Path(tempfile.mkdtemp()) / "bad2.toml"
bad2.write_text("[scan]\nthreads = 4\nbroken here ===\n")
try:
    load_settings(bad2)
    raise AssertionError("expected SettingsError")
except SettingsError as e:
    assert "Синтаксическая ошибка" in str(e), str(e)
    print("1e. syntax error OK:", str(e).splitlines()[0])

sc = load_settings(None)
for pair in (("/x", "/x"), ("/a", "/a/b"), ("/a/b", "/a")):
    sc.paths.src, sc.paths.trash = Path(pair[0]), Path(pair[1])
    try:
        check_paths(sc)
        raise AssertionError(f"expected failure for {pair}")
    except SettingsError:
        pass
sc.paths.src, sc.paths.trash = Path("/arch"), Path("/trash")
check_paths(sc)
print("1f. path validation OK")

update_settings_file(good, {"ui": {"page_size": 30}})
s3 = load_settings(good)
assert s3.ui.page_size == 30 and s3.scan.threads == 3, "merge must preserve other keys"
print("1g. update_settings_file merge OK")

# ---------- 2. hashing: полная D4-инвариантность ----------
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageOps
from engine.hashing import compute_entry, hamming_distance

tmp = Path(tempfile.mkdtemp())
rng = np.random.default_rng(7)


def make_photo(path: Path) -> Image.Image:
    img = Image.new("RGB", (640, 480), (30, 30, 40))
    dr = ImageDraw.Draw(img)
    for _ in range(30):
        x1, y1 = int(rng.integers(0, 639)), int(rng.integers(0, 479))
        x2, y2 = int(rng.integers(0, 639)), int(rng.integers(0, 479))
        if x2 < x1:
            x1, x2 = x2, x1
        if y2 < y1:
            y1, y2 = y2, y1
        color = tuple(int(c) for c in rng.integers(60, 255, 3))
        getattr(dr, rng.choice(["rectangle", "ellipse"]))([x1, y1, x2, y2], outline=color, width=4)
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    img.save(path, "JPEG", quality=90)
    return img


base_path = tmp / "base.jpg"
base = make_photo(base_path)

ent = compute_entry(str(base_path), base_path.stat().st_size, base_path.stat().st_mtime)
assert len(ent.phashes) == 8 and len(ent.parts) == 8 and all(len(p) == 8 for p in ent.phashes)
assert ent.width == 640 and ent.height == 480
assert all(len(part) == 4 for part in ent.parts)
print("2a. compute_entry: 8 variants x 64 bit OK")

TRANSFORMS = {
    "rot90": Image.Transpose.ROTATE_90,
    "rot180": Image.Transpose.ROTATE_180,
    "rot270": Image.Transpose.ROTATE_270,
    "flip_lr": Image.Transpose.FLIP_LEFT_RIGHT,
    "flip_tb": Image.Transpose.FLIP_TOP_BOTTOM,
    "transpose": Image.Transpose.TRANSPOSE,
    "transverse": Image.Transpose.TRANSVERSE,
}
base_hashes = set(ent.phashes)
for name, mode in TRANSFORMS.items():
    p = tmp / f"{name}.jpg"
    base.transpose(mode).save(p, "JPEG", quality=85)
    e2 = compute_entry(str(p), p.stat().st_size, p.stat().st_mtime)
    assert base_hashes & set(e2.phashes), f"{name}: ни один вариант не совпал с оригиналом!"
print("2b. D4-invariance (7 transforms, exact hash match) OK")

# EXIF-поворот: ориентация 6 = поворот на 90; размеры после transpose — 640x480
p_exif = tmp / "exif.jpg"
im = Image.new("RGB", (480, 640), (200, 180, 160))
ImageDraw.Draw(im).rectangle([50, 50, 430, 590], outline=(10, 10, 10), width=10)
exif = Image.Exif()
exif[274] = 6
im.save(p_exif, "JPEG", exif=exif)
e3 = compute_entry(str(p_exif), p_exif.stat().st_size, p_exif.stat().st_mtime)
assert (e3.width, e3.height) == (640, 480), (e3.width, e3.height)
ref = tmp / "exif_ref.jpg"
ImageOps.exif_transpose(Image.open(p_exif)).save(ref, "JPEG", quality=95)
e_ref = compute_entry(str(ref), ref.stat().st_size, ref.stat().st_mtime)
assert set(e3.phashes) & set(e_ref.phashes), "exif-transposed variant mismatch"
print("2c. EXIF-orientation dims + hash match OK")

badf = tmp / "corrupt.jpg"
badf.write_bytes(b"\xff\xd8\xe0garbage" + b"x" * 100)
bad_ent = compute_entry(str(badf), badf.stat().st_size, 1.0)
assert hasattr(bad_ent, "error") and bad_ent.error != ""
print("2d. corrupt file -> CorruptFile OK:", bad_ent.error[:40])

# ---------- 3. analyze: бакетный поиск + union-find ----------
from engine.analyze import UnionFind, _bucket_pairs, _bucket_pairs_python, choose_kept

hA = 0b1111_0000_1111_0000_1111_0000_1111_0000_1111_0000_1111_0000_1111_0000_1111_0000
hB = hA ^ 0b11
hC = 0xFFFF_FFFF_FFFF_FFFF ^ (hA & 0xFFFF)
hD = hB ^ (0b11 << 8)
entries = [(101, hA), (102, hB), (103, hC), (102, hD)]
pairs = list(_bucket_pairs([f for f, _ in entries], [h for _, h in entries], threshold=4))
flat = {(a, b) for a, b, d in pairs}
assert (101, 102) in flat, flat
assert all(103 not in (a, b) for a, b in flat)
print("3a. vectorized bucket pairs OK:", sorted(flat))

uf = UnionFind()
for a, b, d in pairs:
    uf.add(a)
    uf.add(b)
    uf.union(a, b, d)
comps = [c for c in uf.components().values() if len(c) >= 2]
assert sorted(comps[0]) == [101, 102] and uf.min_edge[uf.find(101)] == 2
print("3b. union-find transitive closure + min_edge OK")

rows = [
    {"id": 1, "path": "a.jpg", "size": 100, "width": 800, "height": 600},
    {"id": 2, "path": "b.jpg", "size": 500, "width": 400, "height": 300},
    {"id": 3, "path": "c.jpg", "size": 500, "width": 1600, "height": 1200},
]
assert choose_kept(rows, "size")["path"] == "b.jpg"
assert choose_kept(rows, "pixels")["id"] == 3
print("3c. choose_kept (size/pixels + tie-break) OK")

long_ints = [(hA << 64) | hA, ((hA << 64) | hA) ^ 0b111]
py_pairs = list(_bucket_pairs_python([1, 2], long_ints, 4))
assert py_pairs and py_pairs[0][2] == 3
print("3d. python fallback for >64-bit hashes OK")

# ---------- 4. make_testset + end-to-end hashing ----------
sys.path.insert(0, str(PROJ / "scripts"))
import make_testset

tset = tmp / "testset"
sys.argv = ["make_testset.py", str(tset), "3"]
assert make_testset.main() == 0
files = sorted(tset.rglob("*.jpg"))
assert len(files) == 3 * (1 + 8) + 1
entries2 = {}
corrupt_seen = False
for p in files:
    e = compute_entry(str(p), p.stat().st_size, p.stat().st_mtime)
    if hasattr(e, "phashes"):
        for i, (h, part) in enumerate(zip(e.phashes, e.parts)):
            entries2.setdefault(part, []).append((p.name, i, h))
    else:
        assert "corrupt_000" in p.name, p
        corrupt_seen = True
assert corrupt_seen
print(f"4a. testset generated: {len(files)} files (incl. 1 corrupt), hashes computed OK")

orig_name = "original_000.jpg"
all_rows = [(n, h) for v in entries2.values() for (n, i, h) in v]
orig_hashes = [h for (n, h) in all_rows if n == orig_name]
copy_names = {n for (n, _h) in all_rows if n != orig_name and n.startswith(("rot", "flip", "trans"))}
matched, unmatched = 0, []
for cname in sorted(copy_names):
    chashes = [h for (n, h) in all_rows if n == cname]
    best = min(hamming_distance(ch, oh) for ch in chashes for oh in orig_hashes)
    if best <= 4:
        matched += 1
    else:
        unmatched.append((cname, best))
assert matched == len(copy_names), f"unmatched: {unmatched}"
print(f"4b. all {len(copy_names)} transformed copies matched original (any-variant hamming <= 4)")

# ---------- 5. db DDL sanity ----------
import engine.db as dbmod

assert len(dbmod.DDL) >= 12
sql = " ".join(dbmod.DDL).upper()
for table in ("FILES", "HASHES", "ANALYSIS_RUNS", "GROUPS", "GROUP_MEMBERS", "STATUS", "EVENTS"):
    assert f"CREATE TABLE IF NOT EXISTS {table}" in sql, table
print("5. DDL contains all 7 tables OK")

print("\nALL SMOKE TESTS PASSED")
