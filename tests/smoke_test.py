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
assert s.move.keep_by == "capture" and s.move.group_name_prefix == "_", "новые дефолты 1.2.0"
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

# 1h. атомарная запись: merge + os.replace не оставляет временных файлов
update_settings_file(good, {"move": {"dry_run": True}})
s3b = load_settings(good)
assert s3b.move.dry_run and s3b.ui.page_size == 30, "replace сохраняет другие секции"
assert not list(Path(good).parent.glob(f".{Path(good).name}.tmp-*")), "временный файл не остаётся"
print("1h. update_settings_file atomic (tmp + fsync + os.replace) OK")

# 1i. запись настроек при EBUSY (settings.toml — точка маунта одиночного файла):
# os.replace падает «Device or resource busy» → автоматический откат на запись на месте
import errno as _errno
import os as _os

import engine.settings as _settings_mod

_orig_replace = _os.replace


def _ebusy_replace(src, dst):
    raise OSError(_errno.EBUSY, "Device or resource busy")


_os.replace = _ebusy_replace
try:
    update_settings_file(good, {"ui": {"page_size": 44}})
finally:
    _os.replace = _orig_replace
s3c = load_settings(good)
assert s3c.ui.page_size == 44 and s3c.scan.threads == 3, "EBUSY-fallback: содержимое записано"
assert not list(Path(good).parent.glob(f".{Path(good).name}.tmp-*")), "tmp убран после fallback"
print("1i. update_settings_file EBUSY fallback (bind-mount одиночного файла) OK")

# 1j. новые дефолты 1.4.0: scan.read_exif и ui.confirm_*
s4 = load_settings(None)
assert s4.scan.read_exif is True
assert s4.ui.confirm_delete_files is True and s4.ui.confirm_clean_db is True \
    and s4.ui.confirm_clean_log is True
with tempfile.NamedTemporaryFile("wb", suffix=".toml", delete=False) as f:
    tomli_w.dump({"scan": {"read_exif": False}, "ui": {"confirm_clean_db": False}}, f)
    flags_toml = f.name
s5 = load_settings(flags_toml)
assert s5.scan.read_exif is False and s5.ui.confirm_clean_db is False \
    and s5.ui.confirm_clean_log is True and s5.ui.confirm_delete_files is True
print("1j. defaults + парсинг scan.read_exif / ui.confirm_* OK")

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

# точная (байт-в-байт) копия: одинаковый sha256 и одинаковые 8 phash
import shutil as _sh
from engine.hashing import file_sha256

copy_path = tmp / "base_copy.jpg"
_sh.copyfile(base_path, copy_path)
assert file_sha256(str(base_path)) == file_sha256(str(copy_path))
e_copy = compute_entry(str(copy_path), copy_path.stat().st_size, copy_path.stat().st_mtime)
assert set(e_copy.phashes) == set(ent.phashes), "идентичные файлы должны давать одинаковые phash"
assert file_sha256(str(p_exif)) != file_sha256(str(base_path))
print("2e. file_sha256 exact-copy verification OK")

# ---------- 3. analyze: бакетный поиск + union-find ----------
from engine.analyze import UnionFind, _bucket_pairs, _bucket_pairs_python, _process_bucket, choose_kept

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

# 3e. 2D-чанкинг _bucket_pairs: корректность на бакете больше блока (1200 > 1024)
n_half = 600
rng5 = np.random.default_rng(3)
base_h = int(rng5.integers(0, 2**63)) & ~0xFFFF
clusterA = [base_h | int(rng5.integers(0, 1 << 4)) for _ in range(n_half)]
clusterB = [(base_h ^ (0xFFFF << 32)) | int(rng5.integers(0, 1 << 4)) for _ in range(n_half)]
fids5 = list(range(2 * n_half))
ints5 = clusterA + clusterB
within = cross = 0
for a, b, d in _bucket_pairs(fids5, ints5, threshold=4):
    if (a < n_half) != (b < n_half):
        cross += 1
    else:
        within += 1
assert cross == 0, cross
assert within == 2 * (n_half * (n_half - 1) // 2), within
print(f"3e. 2D-chunked bucket pairs across blocks OK ({within} pairs, 0 cross-cluster)")

# 3f. смешанная длина хэшей в бакете (смена hash_size без --force) — без OverflowError
h64a = 0xFF00FF00FF00FF00
h128 = (0x1234567890ABCDEF << 64) | 0xFEDCBA0987654321
uf_m = UnionFind()
_process_bucket(uf_m, [
    (1, h64a, 8),
    (2, h64a ^ 0b11, 8),      # 8-байтные сравниваются между собой
    (3, h128, 16),            # другая длина — с 8-байтными НЕ смешивается
    (4, h128 ^ 0b11, 16),     # 16-байтные сравниваются между собой (fallback)
], threshold=4)
comps_m = sorted(sorted(c) for c in uf_m.components().values() if len(c) >= 2)
assert comps_m == [[1, 2], [3, 4]], comps_m
print("3f. mixed-length bucket split OK:", comps_m)

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
for table in ("FILES", "HASHES", "ANALYSIS_RUNS", "GROUPS", "GROUP_MEMBERS", "STATUS", "EVENTS",
              "JOBS"):
    assert f"CREATE TABLE IF NOT EXISTS {table}" in sql, table
print("5. DDL contains all 8 tables OK")

# ---------- 6. originals: capture-время, name-penalty, нейминг папок групп ----------
import json as _json
import os as _os

from engine.originals import (
    capture_time_of,
    fs_capture_time,
    group_dir_name,
    name_penalty,
    parse_takeout_ts,
    sanitize_name,
    sidecar_capture_time,
    unique_name,
)
from datetime import datetime as _dt, timezone as _tz

# 6a. parse_takeout_ts: dict/число/строка/ISO/мусор/мс
TS = 1789305560.0
assert parse_takeout_ts({"timestamp": "1789305560", "formatted": "12 сент. 2026 г."}) == TS
assert parse_takeout_ts({"timestamp": 1789305560}) == TS
assert parse_takeout_ts(1789305560) == TS and parse_takeout_ts("1789305560") == TS
assert parse_takeout_ts("2026-09-12T16:39:20Z") == _dt(2026, 9, 12, 16, 39, 20, tzinfo=_tz.utc).timestamp()
assert parse_takeout_ts("1789305560123") == TS + 0.123, "мс-epoch → секунды"
assert parse_takeout_ts("not a time") is None
assert parse_takeout_ts({"formatted": "без timestamp"}) is None
assert parse_takeout_ts(True) is None and parse_takeout_ts(None) is None
assert parse_takeout_ts("9999999999999999") is None, "вне окна валидности"
print("6a. parse_takeout_ts OK")

# 6b. sidecar_capture_time: Takeout-sidecar <файл>.json (photoTakenTime → creationTime)
d6 = Path(tempfile.mkdtemp())
img6 = d6 / "IMG_20260912_163920.jpg"
img6.write_bytes(b"\xff\xd8fakejpeg")
(d6 / "IMG_20260912_163920.jpg.json").write_text(_json.dumps({
    "title": "IMG_20260912_163920.jpg",
    "photoTakenTime": {"timestamp": "1789305560", "formatted": "12 сент. 2026 г., 16:39:20 UTC"},
    "creationTime": {"timestamp": "1789405560", "formatted": "…"},
}), encoding="utf-8")
ts6, src6 = sidecar_capture_time(str(img6))
assert ts6 == TS and src6 == "json photoTakenTime", (ts6, src6)

(d6 / "only_ct.jpg").write_bytes(b"x")
(d6 / "only_ct.jpg.json").write_text(_json.dumps({"creationTime": {"timestamp": "1789405560"}}), encoding="utf-8")
ts6b, src6b = sidecar_capture_time(str(d6 / "only_ct.jpg"))
assert ts6b == 1789405560.0 and src6b == "json creationTime"

(d6 / "broken.jpg").write_bytes(b"x")
(d6 / "broken.jpg.json").write_text("{oops", encoding="utf-8")
assert sidecar_capture_time(str(d6 / "broken.jpg")) is None, "битый json → None"
assert sidecar_capture_time(str(d6 / "missing.jpg")) is None, "нет json → None"
print("6b. sidecar_capture_time OK")

# 6c. name_penalty: «копийные» суффиксы
assert name_penalty("IMG_20260912_163920.jpg") == 0
assert name_penalty("IMG_20260912_163920 (Copy 2).jpg") == 1
assert name_penalty("IMG_20260912_163920 (Copy 3) (1).jpg") == 2
assert name_penalty("PXL_20260912-edited.jpg") == 1
assert name_penalty("фото — копия (2).jpg") == 1
assert name_penalty("photo - Copy.jpg") == 1
assert name_penalty("note_2026.jpg") == 0, "дата в имени — не маркер копии"
print("6c. name_penalty OK")

# 6d. choose_kept(capture): раннее время → имя-тай-брейк → путь
from engine.analyze import choose_kept as _ck

r_json = {"id": 1, "path": "IMG_20260912_163920.jpg", "size": 100,
          "mtime": 1900000000.0, "capture_time": TS, "capture_src": "json photoTakenTime"}
rc2 = {"id": 2, "path": "IMG_20260912_163920 (Copy 2).jpg", "size": 900, "mtime": 1900000000.0}
rc3 = {"id": 3, "path": "IMG_20260912_163920 (Copy 3).jpg", "size": 900, "mtime": 1890000000.0}
assert _ck([r_json, rc2, rc3], "capture")["id"] == 1, "json-время выигрывает даже у более раннего fs"
assert _ck([rc2, rc3], "capture")["id"] == 3, "раннее fs-время выигрывает"
r_base = {"id": 1, "path": "DSC_0001.jpg", "size": 100, "mtime": 100.0}
r_cop = {"id": 2, "path": "DSC_0001 (Copy 2).jpg", "size": 900, "mtime": 100.0}
assert _ck([r_cop, r_base], "capture")["id"] == 1, "при равном времени чистое имя бьёт (Copy)"
print("6d. choose_kept capture OK")

# 6e. fs_capture_time / capture_time_of: иерархия json → ФС → БД
p6 = d6 / "fs.jpg"
p6.write_bytes(b"y")
_os.utime(p6, (1789300000, 1789300000))
t6, s6 = fs_capture_time(str(p6))
assert s6 == "fs mtime" and t6 == 1789300000.0, (s6, t6)  # здесь ФС без birthtime
t6b, s6b = capture_time_of(str(p6), 1.0)
assert s6b == "fs mtime" and t6b == 1789300000.0
# файл с json: json главнее ФС
p6j = d6 / "with_json.jpg"
p6j.write_bytes(b"z")
_os.utime(p6j, (1789300000, 1789300000))
(d6 / "with_json.jpg.json").write_text(_json.dumps({"photoTakenTime": {"timestamp": "1590000000"}}), encoding="utf-8")
t6c, s6c = capture_time_of(str(p6j), 1.0)
assert s6c == "json photoTakenTime" and t6c == 1590000000.0
# недоступный файл → fallback на mtime из БД
t6d, s6d = capture_time_of(str(d6 / "nope.jpg"), 5.0)
assert s6d == "db mtime" and t6d == 5.0
print("6e. fs_capture_time / capture_time_of OK")

# 6f. sanitize_name / unique_name / group_dir_name: Windows-безопасность и коллизии
assert sanitize_name('a<b>c"d.jpg') == "a_b_c_d.jpg"
assert sanitize_name("e\\f|g?h*i.jpg") == "e_f_g_h_i.jpg"
assert sanitize_name("CON.jpg") == "_CON.jpg" and sanitize_name("com1.dat") == "_com1.dat"
assert sanitize_name("name. ") == "name", "хвостовые точки/пробелы (Windows)"
assert group_dir_name("src/x/IMG_20260914_123649.jpg", "_", 7) == "_IMG_20260914_123649.jpg"
assert len(group_dir_name("src/x/long" + "n" * 300 + ".jpg", "_", 7)) <= 104, "обрезка длинных имён"
assert group_dir_name(None, "_", 7) == "group_7"
used = {"_a.jpg"}
assert unique_name("_a.jpg", used) == "_a__2.jpg", "суффикс перед расширением (как _1/_2 у файлов)"
assert unique_name("_A.jpg", set()) == "_A.jpg", "уникальность без учёта регистра"
assert unique_name("_A.jpg", {"_a.jpg"}) == "_A__2.jpg", "IMG.jpg vs img.jpg — одно имя на macOS/Win"
print("6f. sanitize/unique/group_dir_name OK")

# 6g. choose_kept capture: тай-брейк по РАННЕМУ ctime (кейс «копия → 2.jpg»)
T6G = 1789305560.0
r_orig6g = {"id": 1, "path": "IMG_20260912_163920.jpg", "size": 100, "mtime": T6G,
            "capture_time": T6G, "ctime": 1000.0}
r_short6g = {"id": 2, "path": "2.jpg", "size": 100, "mtime": T6G,
             "capture_time": T6G, "ctime": 2000.0}  # копия создана позже
assert _ck([r_short6g, r_orig6g], "capture")["id"] == 1, \
    "ранний ctime бьёт короткое имя (копия создана позже)"
# без ctime (как на этапе analyze) — прежнее поведение: путь решает детерминированно
r_nc1 = {"id": 1, "path": "IMG_20260912_163920.jpg", "size": 100, "mtime": T6G, "capture_time": T6G}
r_nc2 = {"id": 2, "path": "2.jpg", "size": 100, "mtime": T6G, "capture_time": T6G}
assert _ck([r_nc1, r_nc2], "capture")["id"] == 2
print("6g. choose_kept ctime tie-break OK")

# ---------- 7. EXIF-свойства (scan.read_exif → files.exif) ----------
from engine.hashing import extract_exif

p_ex = tmp / "exif_props.jpg"
im7 = Image.new("RGB", (64, 48), (10, 200, 10))
ex7 = Image.Exif()
ex7[271] = "Xiaomi"                 # Make
ex7[272] = "Redmi Note 13"          # Model
ex7[306] = "2026:09:12 16:39:20"    # DateTime
sub7 = ex7.get_ifd(0x8769)
sub7[36867] = "2026:09:12 16:39:20"  # DateTimeOriginal
sub7[33434] = (1, 33)                # ExposureTime 1/33
sub7[33437] = (17, 10)               # FNumber 1.7
sub7[34855] = 160                    # ISO
sub7[34850] = 2                      # ExposureProgram = Auto
sub7[37383] = 2                      # MeteringMode = Center weighted average
sub7[37385] = 24                     # Flash = auto, did not fire
sub7[37386] = (53, 10)               # FocalLength 5.3 mm
im7.save(p_ex, "JPEG", exif=ex7)

ent7 = compute_entry(str(p_ex), p_ex.stat().st_size, p_ex.stat().st_mtime, read_exif=True)
x = ent7.exif
assert x is not None
assert x["imageType"] == "jpeg (JPEG)", x["imageType"]
assert x["width"] == "64 pixels" and x["height"] == "48 pixels"
assert x["cameraBrand"] == "Xiaomi" and x["cameraModel"] == "Redmi Note 13"
assert x["exposureTime"] == "1/33 s", x["exposureTime"]
assert x["exposureProgram"] == "Auto"
assert x["apertureValue"] == "F1.7", x["apertureValue"]
assert x["isoSpeedRating"] == "160"
assert x["flashFired"] == "No, auto", x["flashFired"]
assert x["meteringMode"] == "Center weighted average"
assert x["focalLength"] == "5.3 mm"
assert x["createdOn"] == "2026-09-12 16:39:20", x["createdOn"]
print("7a. extract_exif: полный набор свойств (схема окна «Свойства») OK")

# read_exif=False → свойства не читаются; файл без EXIF → createdOn из mtime
ent7b = compute_entry(str(p_ex), p_ex.stat().st_size, p_ex.stat().st_mtime)
assert ent7b.exif is None
plain = tmp / "plain_props.jpg"
Image.new("RGB", (32, 16), (5, 5, 5)).save(plain, "JPEG")
plain_mtime = 1700000000.0
_os.utime(plain, (plain_mtime, plain_mtime))
ent7c = compute_entry(str(plain), plain.stat().st_size, plain_mtime, read_exif=True)
xc = ent7c.exif
assert "cameraBrand" not in xc and "exposureTime" not in xc
from datetime import datetime as _dt7, timezone as _tz7
assert xc["createdOn"] == _dt7.fromtimestamp(plain_mtime, tz=_tz7.utc).strftime("%Y-%m-%d %H:%M:%S")
assert xc["imageType"] == "jpeg (JPEG)"
print("7b. extract_exif: отключение по флагу + fallback createdOn=mtime OK")

# ---------- 8. webops: папка группы создаётся до переноса (регресс 1.4.3) ----------
# Кейс из багрепорта: в ручном режиме ⭐/📋 падали с FileNotFoundError, если
# папки группы в trash ещё нет (штатно: до `engine move` папок не существует).
# Функциональный тест без БД: fake-conn (транзакции/курсоры — no-op).
from engine.webops import _demote_to_trash  # noqa: E402


class _FakeCur:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        pass


class _FakeTx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def transaction(self):
        return _FakeTx()

    def cursor(self):
        return _FakeCur()


w8 = Path(tempfile.mkdtemp())
src8, tr8 = w8 / "src", w8 / "trash"
src8.mkdir()
missing_dir = tr8 / "_IMG_20260912_163920.jpg"
assert not missing_dir.exists(), "предусловие: папки группы ещё нет (кейс багрепорта)"
f_a = src8 / "2.jpg"
f_a.write_bytes(b"a" * 10)
target8 = _demote_to_trash(_FakeConn(), s, {"path": str(f_a), "file_id": 1, "group_id": 5},
                           missing_dir)
assert missing_dir.is_dir(), "папка группы должна создаваться автоматически (фикс 1.4.3)"
assert Path(target8) == missing_dir / "2.jpg" and Path(target8).exists()
assert not f_a.exists()
# коллизия имени внутри freshly-created папки (_unique_target по-прежнему работает)
f_b = src8 / "2.jpg"
f_b.write_bytes(b"b" * 10)
target8b = _demote_to_trash(_FakeConn(), s, {"path": str(f_b), "file_id": 2, "group_id": 5},
                            missing_dir)
assert Path(target8b).name == "2_1.jpg" and Path(target8b).exists(), target8b
print("8. webops._demote_to_trash: mkdir папки группы до переноса + коллизии имён OK")

# ---------- 9. пины фиксов 1.4.3 в исходниках (webops + web/app.py) ----------
# Файловые операции этих путей требуют БД (проверяются integ-тестами на живом
# PostgreSQL); здесь фиксируем саму структуру исправлений.
webops_src = (PROJ / "engine" / "webops.py").read_text(encoding="utf-8")
assert "dest.mkdir(parents=True, exist_ok=True)" in webops_src, \
    "баг 1: _demote_to_trash обязан создавать папку группы"
assert 'donor = (new_kept or {}).get("path")' in webops_src, \
    "баг 1: имя папки — по оригиналу (конвенция move.py), не по переносимому файлу"
app_src = (PROJ / "web" / "app.py").read_text(encoding="utf-8")
assert "in_dialog: bool = False" in app_src and 'in_dialog=True' in app_src, \
    "баги 2+3: режим диалога должен передаваться в _member_action_buttons"
assert 'arm_key = f"dlgarm_{fid}" if in_dialog else f"delarm_{fid}"' in app_src, \
    "баг 3: у подтверждения удаления в диалоге — своё состояние (dlgarm_)"
assert "if not in_dialog and b_info.button" in app_src, \
    "баг 2: кнопка ℹ️ не рендерится внутри модального окна"
assert 'return  # подтверждение остаётся ВНУТРИ окна' in app_src, \
    "баг 3: вооружение 🗑️ в диалоге без полного st.rerun (иначе окно закрывается)"
assert 'return  # диалог перерисуется сам (семантика фрагмента); rerun закрыл бы окно' in app_src, \
    "баг 3: отмена ❌ в диалоге без полного st.rerun"
print("9. source-пины фиксов 1.4.3 (webops mkdir/donor, dialog ℹ️/🗑️) OK")

# ---------- 10. Ф1 job-runner (1.5.0): пины структуры (compose/DDL/runner/UI) ----------
# Живой цикл очереди проверяется integ-тестом (§18, реальный PostgreSQL);
# здесь фиксируем структуру: сервис runner, DDL, атомарный взбор, кнопка UI.
import py_compile

for rel in ("engine/db.py", "engine/jobs.py", "engine/runner.py", "web/app.py"):
    py_compile.compile(str(PROJ / rel), doraise=True)
compose_src = (PROJ / "docker-compose.yml").read_text(encoding="utf-8")
assert 'entrypoint: ["python", "-m", "engine.runner"]' in compose_src, \
    "Ф1: в compose должен быть сервис runner (entrypoint engine.runner)"
assert compose_src.count("photo-dedup:1.9.0") == 3, "теги образа engine/web/runner = 1.9.0"
assert "docker.sock" not in compose_src, "вариант A: docker-сокет нигде не используется"
db_src15 = (PROJ / "engine" / "db.py").read_text(encoding="utf-8")
assert "CREATE TABLE IF NOT EXISTS jobs" in db_src15, "Ф1: DDL очереди jobs"
assert "uniq_jobs_queued_command" in db_src15, "одно queued-задание на команду"
jobs_src = (PROJ / "engine" / "jobs.py").read_text(encoding="utf-8")
assert "FOR UPDATE SKIP LOCKED" in jobs_src and "pg_notify" in jobs_src, \
    "атомарный взбор + мгновенное пробуждение"
assert "stage = ANY" in jobs_src, "single-flight guard внутри claim_next"
runner_src = (PROJ / "engine" / "runner.py").read_text(encoding="utf-8")
assert "LISTEN" in runner_src and "reap_stale" in runner_src \
    and "_install_signal_handlers" in runner_src, "LISTEN/NOTIFY + crash-recovery + сигналы"
assert "▶️ Полный прогон" in app_src and "jobq.enqueue" in app_src, \
    "Ф1/Ф2: кнопки запуска через очередь в Мониторе"
assert "1.9.0" in (PROJ / "engine" / "__init__.py").read_text(encoding="utf-8")
print("10. Ф1 job-runner: пины структуры (compose/DDL/jobs/runner/UI) OK")

# ---------- 11. Ф2 (1.6.0): все длинные команды в очереди + stale-детекция + retention ----------
assert '"🔍 Скан"' in app_src and '"🧠 Анализ"' in app_src and '"📦 Перенос"' in app_src \
    and '"↩️ Undo"' in app_src, "Ф2: кнопки scan/analyze/move/undo на Мониторе"
assert '"arm_qundo"' in app_src and "qundo_yes" in app_src, \
    "Ф2: undo из очереди — с двухфазным подтверждением"
assert "reap_stale" in app_src and "RUNNER_STALE_SEC" in app_src, \
    "Ф2: stale-детекция из UI (reap по протухшему heartbeat, порог RUNNER_STALE_SEC)"
assert "Прогон оборвался" in app_src, "Ф2: подпись «прогон оборвался» + Resume-подсказка"
assert "def prune" in jobs_src and "RETAIN_JOBS" in jobs_src, "Ф2: retention истории очереди (prune)"
assert "jobs.prune" in runner_src and "RUNNER_RETAIN_JOBS" in runner_src, \
    "Ф2: runner ужимает историю после finish (env RUNNER_RETAIN_JOBS)"
assert "RUNNER_RETAIN_JOBS" in compose_src and "RUNNER_RETAIN_JOBS" in \
    (PROJ / ".env.example").read_text(encoding="utf-8"), "Ф2: RUNNER_RETAIN_JOBS в compose/.env.example"
assert "TRUNCATE events, jobs," in webops_src, "Ф2: «Очистить БД» вычищает и историю очереди jobs"
assert "RUNNER_STALE_SEC" in compose_src.split("  web:")[1], \
    "Ф2: web-контейнер получает тот же порог stale, что и runner"
print("11. Ф2: кнопки всех команд + stale-детекция в UI + retention OK")

# ---------- 12. хотфикс 1.6.1: row-factory-агностичные извлечения (пул web — dict_row) ----------
# Регресс: enqueue делал cur.fetchone()[0]; соединения пула web отдают dict-строки
# → KeyError: 0 на любой кнопке запуска (INSERT откатывался пулом).
db_src = (PROJ / "engine" / "db.py").read_text(encoding="utf-8")
assert "def first_value" in db_src and "isinstance(row, dict)" in db_src, \
    "1.6.1: db.first_value — извлечение первого столбца для tuple И dict строк"
assert "return first_value(cur.fetchone())" in db_src, "1.6.1: db.scalar через first_value"
assert "db.first_value(cur.fetchone())" in jobs_src, "1.6.1: jobs.enqueue без fetchone()[0]"
assert "first_value(cur.fetchone())" in webops_src, "1.6.1: webops.clean_db без fetchone()[0]"
assert "pg_errors.UndefinedTable" in app_src, \
    "1.6.1: _qjob объясняет отсутствие таблицы jobs (runner не стартовал)"
print("12. хотфикс 1.6.1: first_value (dict_row-пул) + UndefinedTable-подсказка OK")

# ---------- 13. Ф3 (1.7.0): отложенный запуск (scheduled_at) + автообновление галереи ----------
# Живое поведение очереди с scheduled_at — integ §18j (реальный PostgreSQL);
# здесь фиксируем структуру: миграция DDL, фильтр срока в claim_next,
# операции reschedule/cancel_queued, UI-селект и edge-детекция для галереи.
assert "ADD COLUMN IF NOT EXISTS scheduled_at TIMESTAMPTZ" in db_src, \
    "Ф3: миграция jobs.scheduled_at (идемпотентная)"
assert "scheduled_at: datetime | None = None" in jobs_src, "Ф3: параметр enqueue"
assert "AND (scheduled_at IS NULL OR scheduled_at <= now())" in jobs_src, \
    "Ф3: claim_next не берёт задание из будущего"
assert "def reschedule" in jobs_src and "def cancel_queued" in jobs_src, \
    "Ф3: перенос/отмена queued-задания в jobs.py"
assert "queued_jobs" in jobs_src, "Ф3: snapshot отдаёт queued с scheduled_at"
assert "_WHEN_OPTIONS" in app_src and "Когда запускать" in app_src, \
    "Ф3: селект «Когда запускать» над кнопками Монитора"
assert "jobq.reschedule" in app_src and "jobq.cancel_queued" in app_src, \
    "Ф3: действия ▶️ Сейчас / ⏰ +1 ч / ✖ Отменить над queued-заданием"
assert "prev_engine_live" in app_src and 'st.rerun(scope="app")' in app_src, \
    "автообновление галереи: edge «занят → свободен» → один полный rerun"
assert "ZoneInfo" in app_src and "Europe/Moscow" in app_src, \
    "Ф3: пояс пресетов из env TZ (по умолчанию Europe/Moscow)"
assert "TZ" in compose_src, "Ф3: TZ пробрасывается в web/runner из .env"
runner_src = (PROJ / "engine" / "runner.py").read_text(encoding="utf-8")
assert "claim_next" in runner_src, "Ф3: runner не менялся — берёт только доступные задания"
# Ф3-хвост (1.8.0): «Своё время…» = календарная дата + время, а не только ЧЧ:ММ
# на сегодня/завтра; прошедший момент валидируется в UI (предупреждение +
# блокировка кнопок), а не молчаливым переносом на завтра.
assert "def _scheduled_dt(when: str, custom_date: date | None = None," in app_src, \
    "1.8.0: «Своё время…» принимает точные дату и время"
assert "datetime.combine(custom_date, custom_time, tzinfo=_local_tz())" in app_src, \
    "1.8.0: custom-момент строится aware в поясе TZ (интерпретация — как у пресетов)"
assert "min_value=now_local.date()" in app_src, \
    "1.8.0: календарь ограничен сегодняшним днём (min_value у date_input)"
assert "launch_disabled = job_disabled or sched_past" in app_src, \
    "1.8.0: прошедший custom-момент блокирует запуск (без молчаливого сдвига)"
assert app_src.count("disabled=launch_disabled") == 5, \
    "1.8.0: все 5 кнопок запуска учитывают валидацию срока"
assert "_fmt_moment" in app_src, \
    "1.8.0: год в отображении отложенного момента, если он не в текущем году"
assert "from datetime import date, datetime, time as dt_time, timedelta" in app_src, \
    "1.8.0: импорт date в web/app.py"
print("13. Ф3: scheduled_at (DDL/claim/reschedule/cancel) + селект UI + автообновление галереи + выбор даты (1.8.0) OK")

# ---------- 14. Хотфикс 1.7.1: скрытие manual-блока в auto + edge-детект до дельт ----------
# Баги: (1) блок «Опасная зона: подтверждение переноса (manual)» отображался и в
# auto-режиме (в auto move берёт ВСЕ группы, confirmed не учитывается — блок
# бессмысленный); (2) после Прогона/Скан задваивались «Опасные зоны»: полный
# st.rerun(scope="app") выполнялся в СЕРЕДИНЕ фрагмент-рана, и клиентский рендер
# Streamlit оставлял дублированный хвост фрагмента. Фикс: edge-детект первым
# делом в фрагменте — до генерации каких-либо дельт.
assert 'if settings.move.mode == "manual":\n        az1, az2 = st.columns(2)' in app_src, \
    "1.7.1: блок подтверждения переноса рендерится ТОЛЬКО в manual-режиме"
assert "def _db_ops_block(" in app_src and "_db_ops_block(settings, locked)" in app_src, \
    "1.7.1: БД-блок опасной зоны выделен в общий помощник (оба режима)"
assert app_src.index("prev_engine_live") < app_src.index('c1.metric("Этап"'), \
    "1.7.1: edge-детект «занят → свободен» выполняется ДО отрисовки элементов фрагмента"
assert app_src.count('st.rerun(scope="app")') == 1, \
    "1.7.1: полный rerun приложения — ровно один вызов (в начале monitor_fragment)"
print("14. хотфикс 1.7.1: manual-блок по режиму + edge-детект до дельт (без задвоения) OK")

# ---------- 15. 1.9.0: CLI schedule (--at/--delay) — клиент очереди jobs ----------
# Живой путь (INSERT + отложенный claim) — integ §18k; здесь фиксируем структуру
# (подкоманда, взаимное исключение --at/--delay, uniq-дубликат, TZ) и парсеры.
cli_src = (PROJ / "engine" / "cli.py").read_text(encoding="utf-8")
assert '"schedule", parents=[common]' in cli_src, "1.9.0: подкоманда schedule"
assert "choices=jobs.COMMANDS" in cli_src, "1.9.0: в очередь ставятся только очередь-команды"
assert "psycopg.errors.UniqueViolation" in cli_src, \
    "1.9.0: дубликат queued-команды отсекается uniq-индексом с понятным сообщением (rc 1)"
assert "уже в прошлом" in cli_src, \
    "1.9.0: прошедший --at — ошибка без молчаливого сдвига (семантика «Своё время…» UI)"
assert "ZoneInfo" in cli_src and '"Europe/Moscow"' in cli_src, \
    "1.9.0: пояс --at из env TZ (по умолчанию Europe/Moscow — как у пресетов UI)"
assert 'args.command not in ("undo", "schedule")' in cli_src, \
    "1.9.0: schedule не создаёт trash — это только INSERT в очередь"
assert "TZ: ${TZ:-Europe/Moscow}" in compose_src.split("  engine:")[1].split("  runner:")[0], \
    "1.9.0: engine-сервис получает TZ (иначе --at посчитается в UTC контейнера)"
from datetime import datetime as _dt19, timedelta as _td19, timezone as _tz19  # noqa: E402
from types import SimpleNamespace as _NS19  # noqa: E402
from engine.cli import _parse_at, _parse_delay, _schedule_moment  # noqa: E402
now19 = _dt19(2026, 9, 26, 12, 0, tzinfo=_tz19.utc)
assert _parse_delay("90", now19) == now19 + _td19(minutes=90), "без суффикса — минуты"
assert _parse_delay("2h", now19) == now19 + _td19(hours=2)
assert _parse_delay("1d12h", now19) == now19 + _td19(hours=36), "сегменты суммируются"
for _bad in ("", "abc", "1x", "-5m", "h", "0"):
    try:
        _parse_delay(_bad, now19)
        raise AssertionError(f"ожидали ValueError на --delay {_bad!r}")
    except ValueError:
        pass
iso19 = _parse_at("2027-03-15 09:30", now19)
assert iso19 == _parse_at("15.03.2027 09:30", now19), "ISO и ДД.ММ.ГГГГ — одно и то же"
assert iso19 == _parse_at("2027-03-15T09:30", now19), "ISO с T тоже принимается"
assert iso19.utcoffset() == now19.utcoffset(), "момент наследует пояс now (TZ)"
assert _parse_at("15:30", now19) == now19.replace(hour=15, minute=30), \
    "ЧЧ:ММ = СЕГОДНЯ (strptime даёт 1900-01-01 — день обязан браться из now)"
assert _parse_at("2030-01-02", now19) == _dt19(2030, 1, 2, 0, 0, tzinfo=_tz19.utc), "дата = 00:00"
for _bad in ("2000-01-01 00:00", "11:00"):  # 11:00 уже прошло для now19=12:00
    try:
        _parse_at(_bad, now19)
        raise AssertionError(f"ожидали ValueError на прошедший --at {_bad!r}")
    except ValueError:
        pass
for _args in (_NS19(at="x", delay="y"), _NS19(at=None, delay=None)):
    try:
        _schedule_moment(_args)
        raise AssertionError("ожидали ValueError: ровно один из --at/--delay")
    except ValueError:
        pass
print("15. 1.9.0 CLI schedule: подкоманда + парсеры --at/--delay + TZ + uniq-дубликат OK")

print("\nALL SMOKE TESTS PASSED")
