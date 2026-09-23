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

# 1i. дефолты 1.4.0: подтверждения UI включены; paths в settings_to_dict — host-форма
from engine.settings import settings_to_dict, host_to_container, container_to_host

s_def = load_settings(None)
assert s_def.ui.confirm_delete_files and s_def.ui.confirm_clean_all and s_def.ui.confirm_clean_log
d_def = settings_to_dict(s_def)
assert d_def["ui"]["confirm_clean_all"] is True
assert d_def["paths"]["src"] == s_def.paths.src_host, "в dict — host-форма путей"
print("1i. ui.confirm_* defaults + host-форма путей в settings_to_dict OK")

# 1j. маппинг host↔container: PATH_MAP_*; путь вне корня — ошибка валидации
import os

os.environ["PATH_MAP_HOST"] = "/home/me/myfotos"
os.environ["PATH_MAP_CONTAINER"] = "/photos"
try:
    load_settings(good)  # там src=/tmp/arch — вне корня /home/me/myfotos
    raise AssertionError("expected SettingsError: путь вне корня")
except SettingsError as e:
    assert "PATH_MAP_HOST" in str(e) and "/tmp/arch" in str(e), str(e)
with tempfile.NamedTemporaryFile("wb", suffix=".toml", delete=False) as f:
    tomli_w.dump({"paths": {"src": "/home/me/myfotos/src", "trash": "/home/me/myfotos/trash"}}, f)
    mapped = f.name
s_m = load_settings(mapped)
assert str(s_m.paths.src) == "/photos/src" and str(s_m.paths.trash) == "/photos/trash"
assert s_m.paths.src_host == "/home/me/myfotos/src", "host-форма сохраняется"
assert settings_to_dict(s_m)["paths"]["src"] == "/home/me/myfotos/src"
assert container_to_host("/photos/x/y.jpg") == "/home/me/myfotos/x/y.jpg"
assert container_to_host("/other/path") == "/other/path", "необратимый путь — как есть"
assert host_to_container("/home/me/myfotos") == (Path("/photos"), True)
try:
    host_to_container("/etc/passwd")
    raise AssertionError("expected SettingsError")
except SettingsError:
    pass
os.environ.pop("PATH_MAP_HOST")
os.environ.pop("PATH_MAP_CONTAINER")
s_id = load_settings(mapped)
assert str(s_id.paths.src) == "/home/me/myfotos/src", "без маппинга путь используется как есть"
print("1j. PATH_MAP host↔container (вне корня — ошибка; без env — identity) OK")

# 1k. EBUSY-fallback: os.replace на файловом bind-mount невозможен → запись НА МЕСТО
import errno
import engine.settings as eng_settings
from unittest.mock import patch


def _ebusy_replace(src, dst, **kw):
    raise OSError(errno.EBUSY, "Device or resource busy")


with patch.object(eng_settings.os, "replace", side_effect=_ebusy_replace):
    update_settings_file(good, {"ui": {"page_size": 44}})
s_e = load_settings(good)
assert s_e.ui.page_size == 44 and s_e.scan.threads == 3, "in-place запись сохраняет merge"
assert not list(Path(good).parent.glob(f".{Path(good).name}.tmp-*")), "tmp подчищен"
print("1k. EBUSY-fallback записи settings.toml (файловый bind-mount) OK")

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
for table in ("FILES", "HASHES", "ANALYSIS_RUNS", "GROUPS", "GROUP_MEMBERS", "STATUS", "EVENTS"):
    assert f"CREATE TABLE IF NOT EXISTS {table}" in sql, table
print("5. DDL contains all 7 tables OK")

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

print("\nALL SMOKE TESTS PASSED")
