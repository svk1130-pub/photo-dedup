"""Интеграционный тест photo-dedup НА РЕАЛЬНОМ PostgreSQL (pgserver).

Проверяет: init_db (DDL), scan (батчи, upsert, corrupt), analyze (потоковый
курсор, union-find, groups), move (auto, info.txt, moved_to), resume (skip),
status, события в events, галерейные выборки.
Запуск: python3 integ_test.py  (python3.12, deps установлены)
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

import pgserver  # встроенный PostgreSQL

work = Path(tempfile.mkdtemp(prefix="pd-integ-"))
srv = pgserver.get_server(str(work / "pgdata"))
uri = srv.get_uri()
print("pgserver uri:", uri)

import psycopg

# --- создаём роль/БД как в docker compose (photo/photo) ---
adm = psycopg.connect(uri, autocommit=True)
adm.execute("CREATE ROLE photo LOGIN PASSWORD 'photo'")
adm.execute("CREATE DATABASE photo OWNER photo")
port = adm.execute("SHOW port").fetchone()[0]
sockdir = uri.split("host=")[-1].split("&")[0]
adm.close()
PG_DSN = f"host={sockdir} port={port} dbname=photo user=photo password=photo"
os.environ["PG_DSN"] = PG_DSN
print("PG_DSN:", PG_DSN)
_ = psycopg.connect(PG_DSN).close()  # проверка DSN сразу

# --- тестовый набор в src ---
src = work / "src"
trash = work / "trash"
src.mkdir()
sys.path.insert(0, str(PROJ / "scripts"))
import make_testset

sys.argv = ["make_testset.py", str(src / "testset"), "4"]
assert make_testset.main() == 0

# --- settings.toml (пути во временный каталог) ---
import tomli_w

settings_path = work / "settings.toml"
with open(settings_path, "wb") as f:
    tomli_w.dump({
        "paths": {"src": str(src), "trash": str(trash)},
        "scan": {"threads": 4, "batch_size": 10},
        "analyze": {"threshold": 4},
        "move": {"mode": "auto", "keep_by": "size", "conflict": "suffix", "dry_run": False},
    }, f)

# --- 1) поэтапно: scan → проверка → analyze → проверка → move ---
from engine.cli import main as cli_main

rc = cli_main(["--settings", str(settings_path), "scan"])
assert rc == 0, f"engine scan rc={rc}"
conn = psycopg.connect(PG_DSN, autocommit=True)
n_files = conn.execute("SELECT count(*) FROM files").fetchone()[0]
n_ok = conn.execute("SELECT count(*) FROM files WHERE status='ok'").fetchone()[0]
n_corrupt = conn.execute("SELECT count(*) FROM files WHERE status='corrupt'").fetchone()[0]
n_hashes = conn.execute("SELECT count(*) FROM hashes").fetchone()[0]
print(f"   после scan: files={n_files} (ok={n_ok}, corrupt={n_corrupt}), hashes={n_hashes}")
assert n_files == 37 and n_corrupt == 1 and n_hashes == 8 * n_ok, "8 вариантов на каждый ok-файл"
print("1a. scan OK (батчи, upsert, corrupt, 8×D4)")

rc = cli_main(["--settings", str(settings_path), "analyze"])
assert rc == 0, f"engine analyze rc={rc}"
n_runs = conn.execute("SELECT count(*) FROM analysis_runs").fetchone()[0]
n_groups = conn.execute("SELECT count(*) FROM groups").fetchone()[0]
n_members = conn.execute("SELECT count(*) FROM group_members").fetchone()[0]
print(f"   после analyze: runs={n_runs}, groups={n_groups}, members={n_members}")
assert n_runs == 1 and n_groups >= 4 and n_members == n_ok, "все ok-файлы сгруппированы"
min_d = conn.execute("SELECT min((info->>'min_distance')::int) FROM groups").fetchone()[0]
assert min_d == 0, "точные D4-копии должны иметь дистанцию 0"
print("1b. analyze OK (4 группы, min_distance=0)")

rc = cli_main(["--settings", str(settings_path), "move"])
assert rc == 0, f"engine move rc={rc}"
n_moved = conn.execute("SELECT count(*) FROM group_members WHERE moved_to IS NOT NULL").fetchone()[0]
n_events = conn.execute("SELECT count(*) FROM events").fetchone()[0]
n_hashes_after = conn.execute("SELECT count(*) FROM hashes").fetchone()[0]
print(f"   после move: moved={n_moved}, events={n_events}, hashes(остаток)={n_hashes_after}")
assert n_moved == n_members - n_groups, "все дубли перенесены"
assert n_hashes_after == 8 * n_groups, "хэши перенесённых удалены, оригиналы остались"
print("1c. move OK (auto, хэши дубликатов очищены)")

# галерея: moved_to указывает в папку группы в trash, файл существует
rows = conn.execute(
    "SELECT gm.moved_to FROM group_members gm WHERE gm.moved_to IS NOT NULL LIMIT 50"
).fetchall()
for (mv,) in rows:
    assert mv.startswith(str(trash)) and Path(mv).exists(), mv
kept_rows = conn.execute(
    "SELECT kept_path FROM groups WHERE kept_path IS NOT NULL"
).fetchall()
for (kp,) in kept_rows:
    assert Path(kp).exists() and str(kp).startswith(str(src)), kp
print("2. DB state consistent; moved_to -> именованная папка группы в trash exists on disk OK")

n_events = conn.execute("SELECT count(*) FROM events").fetchone()[0]
assert n_events > 0

# папки групп названы по оригиналу (префикс «_»), у каждой — info.txt
gdirs = sorted(p for p in trash.iterdir() if p.is_dir())
assert len(gdirs) == n_groups, f"{len(gdirs)} папок != {n_groups} групп"
for gd in gdirs:
    assert gd.name.startswith("_") and (gd / "info.txt").exists(), gd
print("3. именованные папки групп с info.txt OK:", len(gdirs), "dirs:",
      ", ".join(gd.name for gd in gdirs[:4]), "…")

# --- 4) resume: повторный scan пропускает всё ---
from engine.cli import main  # noqa: E402  (уже импортирован)

rc = cli_main(["--settings", str(settings_path), "scan"])
assert rc == 0
n_files2 = conn.execute("SELECT count(*) FROM files").fetchone()[0]
assert n_files2 == n_files, "resume не должен дублировать строки"
print("4. resume: повторный scan не дублирует индекс OK")

# --- 5) кооперативная остановка: флаг ставится ВО ВРЕМЯ работы scan ---
import threading
import time

from engine import db as enginedb

sys.argv = ["make_testset.py", str(src / "bulk"), "40"]  # ~440 файлов — окно для остановки
assert make_testset.main() == 0

result = {}

def _run_scan():
    result["rc"] = cli_main(["--settings", str(settings_path), "scan"])

th = threading.Thread(target=_run_scan)
th.start()
deadline = time.time() + 120
while time.time() < deadline:
    row = conn.execute("SELECT stage, processed FROM status WHERE id=1").fetchone()
    if row[0] == "scan" and row[1] > 0:
        enginedb.request_stop(conn)
        break
    time.sleep(0.02)
th.join(180)
stage = conn.execute("SELECT stage FROM status WHERE id=1").fetchone()[0]
processed_at_stop = conn.execute("SELECT processed FROM status WHERE id=1").fetchone()[0]
assert result.get("rc") == 0, result
assert stage == "stopped", stage
assert processed_at_stop > 0
print(f"5. кооперативная остановка OK (остановлен на processed={processed_at_stop}, stage=stopped)")

# --- 6) resume: повторный run дорабатывает и сбрасывает флаг ---
rc = cli_main(["--settings", str(settings_path), "run"])
assert rc == 0
stop_flag = conn.execute("SELECT stop_requested FROM status WHERE id=1").fetchone()[0]
assert not stop_flag
n_files_total = conn.execute("SELECT count(*) FROM files").fetchone()[0]
assert n_files_total == 37 + (40 * 9 + 1)  # 37 из testset; bulk: 40 наборов × (оригинал+7 копий+resize) + 1 битый
print(f"6. повторный run доработал: files={n_files_total}, флаг сброшен OK")
print("   (resume продолжил с места остановки, полный цикл завершён)")

# --- 7) статус-команда (отдельным процессом) ---
env = dict(os.environ, PG_DSN=PG_DSN, SETTINGS_PATH=str(settings_path))
out = subprocess.run(
    [sys.executable, "-m", "engine.cli", "status"], cwd=str(PROJ), env=env,
    capture_output=True, text=True,
)
assert out.returncode == 0 and "Этап" in out.stdout, out.stdout + out.stderr
print("7. engine status OK:")
for line in out.stdout.strip().splitlines():
    print("   ", line)

# --- 8) dry-run: план фиксируется, физически ничего не переносится ---
# свежие дубликаты, чтобы dry-run было с чем работать
sys.argv = ["make_testset.py", str(src / "fresh"), "2"]
assert make_testset.main() == 0

moved_before = conn.execute("SELECT count(*) FROM group_members WHERE moved_to IS NOT NULL").fetchone()[0]
rc = cli_main(["--settings", str(settings_path), "run", "--dry-run"])
assert rc == 0
dry_events = conn.execute("SELECT count(*) FROM events WHERE message LIKE '%DRY RUN%'").fetchone()[0]
assert dry_events >= 1, "план должен фиксироваться в events"
moved_after_dry = conn.execute("SELECT count(*) FROM group_members WHERE moved_to IS NOT NULL").fetchone()[0]
assert moved_after_dry == moved_before, "dry-run не должен переносить"
# физически: оригиналы свежего набора всё ещё в src
assert (src / "fresh" / "original_000.jpg").exists(), "dry-run: файл не должен покидать src"
run_dry = conn.execute("SELECT max(id) FROM analysis_runs").fetchone()[0]
dry_groups = conn.execute("SELECT count(*) FROM groups WHERE run_id=%s", (run_dry,)).fetchone()[0]
assert dry_groups >= 2
print(f"8. dry-run OK: план в журнале ({dry_events} событий), физически ничего не перенесено")

# --- 9) manual-режим: без подтверждения перенос не идёт; --group-id и confirmed работают ---
rc = cli_main(["--settings", str(settings_path), "move", "--move-mode", "manual"])
assert rc == 0
moved_manual = conn.execute("SELECT count(*) FROM group_members WHERE moved_to IS NOT NULL").fetchone()[0]
assert moved_manual == moved_before, "manual без подтверждений не должен переносить"
print("9a. manual без подтверждений: 0 переносов OK")

# подтверждаем одну группу (как это делает UI) и переносим
first_group = conn.execute(
    "SELECT id FROM groups WHERE run_id=%s AND confirmed=false ORDER BY id LIMIT 1", (run_dry,)
).fetchone()[0]
conn.execute("UPDATE groups SET confirmed=true WHERE id=%s", (first_group,))
rc = cli_main(["--settings", str(settings_path), "move", "--move-mode", "manual"])
assert rc == 0
moved_confirmed = conn.execute("SELECT count(*) FROM group_members WHERE moved_to IS NOT NULL").fetchone()[0]
members_of_first = conn.execute("SELECT member_count FROM groups WHERE id=%s", (first_group,)).fetchone()[0]
assert moved_confirmed == moved_before + (members_of_first - 1), "перенесена ровно подтверждённая группа"
gdir1 = conn.execute(
    "SELECT info->>'group_dir' FROM groups WHERE id=%s", (first_group,)
).fetchone()[0]
gd = trash / gdir1
assert gd.exists() and (gd / "info.txt").exists()
print(f"9b. manual: подтверждённая группа #{first_group} перенесена в «{gdir1}» ({members_of_first - 1} файлов) OK")

# ещё одна группа — через --group-id без подтверждения
second_group = conn.execute(
    "SELECT id FROM groups WHERE run_id=%s AND confirmed=false ORDER BY id LIMIT 1", (run_dry,)
).fetchone()[0]
rc = cli_main(["--settings", str(settings_path), "move", "--group-id", str(second_group)])
assert rc == 0
gdir2 = conn.execute(
    "SELECT info->>'group_dir' FROM groups WHERE id=%s", (second_group,)
).fetchone()[0]
assert (trash / gdir2).exists()
print(f"9c. move --group-id #{second_group}: принудительный перенос в «{gdir2}» OK")

# --- 10) галерейные запросы UI (лимитированные; члены страницы ОДНИМ батч-запросом) ---
page = conn.execute(
    "SELECT id, member_count, total_size, kept_path, confirmed, info FROM groups "
    "WHERE run_id=%s ORDER BY total_size DESC, id LIMIT %s OFFSET %s",
    (run_dry, 15, 0),
).fetchall()
assert len(page) >= 2
ids10 = [r[0] for r in page]
members_all = conn.execute(
    "SELECT gm.group_id, gm.file_id, gm.role, gm.moved_to, f.path, f.size, f.width, f.height "
    "FROM group_members gm JOIN files f ON f.id=gm.file_id WHERE gm.group_id = ANY(%s) "
    "ORDER BY (gm.role='kept') DESC, f.size DESC, f.path",
    (ids10,),
).fetchall()
assert len(members_all) == sum(r[1] for r in page), "батч-запрос покрывает все группы страницы"
first_members = [m for m in members_all if m[0] == page[0][0]]
assert len(first_members) == page[0][1]
# перенесённые показывают moved_to (актуальный путь), оригинал — путь в src
moved_rows = [m for m in first_members if m[3] is not None]
assert all(str(m[3]).startswith(str(trash)) for m in moved_rows)
print("10. gallery queries OK (page LIMIT/OFFSET, батч-запрос членов ANY(), актуальные moved_to)")

# --- 11) точные (байт-в-байт) копии: sha256-верификация на этапе move + dry-run read-only ---
import random as _random
sys.path.insert(0, str(PROJ / "scripts"))
import make_testset as _mts
from PIL import Image as PILImage

exact_dir = src / "exact"
exact_dir.mkdir()
# имена подобраны так, чтобы оригинал детерминированно выигрывал tie-break по пути;
# текстурированное изображение — чтобы повёрнутая копия гарантированно сгруппировалась;
# паддинг после EOI (безвреден для JPEG-декодера) делает оригинал строго крупнейшим,
# чтобы keep_by=size детерминированно оставлял именно его
img_x = _mts.make_base(99, _random.Random(7))
orig_f = exact_dir / "a_original.jpg"
img_x.save(orig_f, "JPEG", quality=92)
with open(orig_f, "ab") as f:
    f.write(b"\x00" * 65536)
shutil.copyfile(orig_f, exact_dir / "z_exact_copy.jpg")  # байт-в-байт (с паддингом)
img_x.transpose(PILImage.Transpose.ROTATE_90).save(exact_dir / "z_rot90.jpg", "JPEG", quality=95)

assert cli_main(["--settings", str(settings_path), "scan"]) == 0
assert cli_main(["--settings", str(settings_path), "analyze"]) == 0
run_x = conn.execute("SELECT max(id) FROM analysis_runs").fetchone()[0]
g_exact = conn.execute(
    "SELECT gm.group_id FROM group_members gm JOIN files f ON f.id=gm.file_id WHERE f.path=%s",
    (str(exact_dir / "a_original.jpg"),),
).fetchone()[0]
member_count_exact = conn.execute(
    "SELECT member_count FROM groups WHERE id=%s", (g_exact,)
).fetchone()[0]
assert member_count_exact == 3, member_count_exact

hashes_before_dry = conn.execute("SELECT count(*) FROM hashes").fetchone()[0]
info_before_dry = conn.execute("SELECT info FROM groups WHERE id=%s", (g_exact,)).fetchone()[0]

rc = cli_main(["--settings", str(settings_path), "move", "--dry-run"])
assert rc == 0
msg = conn.execute(
    "SELECT message FROM events WHERE message LIKE %s ORDER BY id DESC LIMIT 1",
    (f"DRY RUN группа #{g_exact}:%",),
).fetchone()[0]
assert msg.count("точная копия оригинала (sha256)") == 1, msg
assert "точных копий оригинала: 1 из 2" in msg, msg
assert conn.execute("SELECT count(*) FROM hashes").fetchone()[0] == hashes_before_dry, \
    "dry-run не должен менять hashes"
assert conn.execute("SELECT info FROM groups WHERE id=%s", (g_exact,)).fetchone()[0] == info_before_dry, \
    "dry-run не должен менять groups.info"
assert (exact_dir / "z_exact_copy.jpg").exists(), "dry-run: файл не должен покидать src"
print("11a. sha256-верификация в dry-run (маркеры точных копий) + БД не тронута OK")

rc = cli_main(["--settings", str(settings_path), "move"])
assert rc == 0
gdir_exact = conn.execute(
    "SELECT info->>'group_dir' FROM groups WHERE id=%s", (g_exact,)
).fetchone()[0]
info_txt = (trash / gdir_exact / "info.txt").read_text(encoding="utf-8")
assert "Точных копий оригинала среди перемещённых (sha256): 1 из 2" in info_txt, info_txt
info_after = dict(conn.execute("SELECT info FROM groups WHERE id=%s", (g_exact,)).fetchone()[0])
assert info_after.get("exact_kept_copies") == 1, info_after
assert info_after.get("dups_sha_checked") == 2, info_after
assert (exact_dir / "a_original.jpg").exists(), "оригинал должен остаться в src"
assert not (exact_dir / "z_exact_copy.jpg").exists(), "точная копия должна быть перенесена"
assert (trash / gdir_exact / "z_exact_copy.jpg").exists()
print("11b. точная копия перенесена, info.txt и groups.info отмечают sha256 OK")

# --- 12) keep_by=capture: оригинал по json-Takeout/ФС + папки групп по имени оригинала ---
import io as _io
import json as _json12

tk = src / "takeout"
tk.mkdir()
# Группа A: байт-в-байт копии «IMG_20260912_163920.jpg»; у оригинала — Takeout-sidecar,
# время снимка (2020) заведомо раньше любых файловых времён — json обязан выиграть.
img12 = _mts.make_base(555, _random.Random(99))
buf12 = _io.BytesIO()
img12.save(buf12, "JPEG", quality=92)
blob12 = buf12.getvalue()
orig12 = tk / "IMG_20260912_163920.jpg"
orig12.write_bytes(blob12)
(tk / "IMG_20260912_163920.jpg.json").write_text(_json12.dumps({
    "title": "IMG_20260912_163920.jpg",
    "photoTakenTime": {"timestamp": "1590000000", "formatted": "20 мая 2020 г., 18:40:00 UTC"},
    "creationTime": {"timestamp": "1590100000", "formatted": "…"},
}), encoding="utf-8")
for i in (2, 3):
    (tk / f"IMG_20260912_163920 (Copy {i}).jpg").write_bytes(blob12)
# Группа B: без json — при равных файловых временах чистое имя бьёт «(Copy N)».
dsc12 = tk / "dsc"
dsc12.mkdir()
img12b = _mts.make_base(556, _random.Random(100))
buf12b = _io.BytesIO()
img12b.save(buf12b, "JPEG", quality=92)
blob12b = buf12b.getvalue()
(dsc12 / "DSC_0001.jpg").write_bytes(blob12b)
for i in (2, 3):
    (dsc12 / f"DSC_0001 (Copy {i}).jpg").write_bytes(blob12b)
# одинаковые (прошлые) файловые времена у всех: ФС не должна решать спор
t12 = 1700000000
for p in list(tk.glob("*.jpg")) + list(dsc12.glob("*.jpg")):
    os.utime(p, (t12, t12))

assert cli_main(["--settings", str(settings_path), "scan"]) == 0
assert cli_main(["--settings", str(settings_path), "analyze"]) == 0
g_tk = conn.execute(
    "SELECT gm.group_id FROM group_members gm JOIN files f ON f.id=gm.file_id WHERE f.path=%s",
    (str(orig12),),
).fetchone()[0]
g_dsc = conn.execute(
    "SELECT gm.group_id FROM group_members gm JOIN files f ON f.id=gm.file_id WHERE f.path=%s",
    (str(dsc12 / "DSC_0001.jpg"),),
).fetchone()[0]
assert g_tk != g_dsc, "разные текстуры не должны сгруппироваться вместе"

rc = cli_main(["--settings", str(settings_path), "move", "--dry-run", "--keep-by", "capture"])
assert rc == 0
msg_tk = conn.execute(
    "SELECT message FROM events WHERE message LIKE %s ORDER BY id DESC LIMIT 1",
    (f"DRY RUN группа #{g_tk}:%",),
).fetchone()[0]
assert f"оставить {orig12}" in msg_tk, msg_tk
assert "(Copy" not in msg_tk.split("перенести")[0], "json-файл должен быть оригиналом"
assert "источник: json photoTakenTime" in msg_tk, msg_tk
assert "_IMG_20260912_163920.jpg" in msg_tk, "в плане фигурирует именованная папка"
msg_dsc = conn.execute(
    "SELECT message FROM events WHERE message LIKE %s ORDER BY id DESC LIMIT 1",
    (f"DRY RUN группа #{g_dsc}:%",),
).fetchone()[0]
assert f"оставить {dsc12 / 'DSC_0001.jpg'}" in msg_dsc, msg_dsc
print("12a. dry-run capture: json-файл и чистое имя выбраны оригиналами OK")

rc = cli_main(["--settings", str(settings_path), "move", "--keep-by", "capture"])
assert rc == 0
dir12 = trash / "_IMG_20260912_163920.jpg"
assert dir12.is_dir(), sorted(p.name for p in trash.iterdir())
assert (dir12 / "IMG_20260912_163920 (Copy 2).jpg").exists()
assert (dir12 / "IMG_20260912_163920 (Copy 3).jpg").exists()
assert orig12.exists(), "оригинал (по json) остаётся в src"
assert not (tk / "IMG_20260912_163920 (Copy 2).jpg").exists()
info12 = dict(conn.execute("SELECT info FROM groups WHERE id=%s", (g_tk,)).fetchone()[0])
assert info12.get("group_dir") == "_IMG_20260912_163920.jpg", info12
assert info12.get("kept_capture_source") == "json photoTakenTime", info12
assert abs(info12.get("kept_capture_time", 0) - 1590000000.0) < 1e-6
info12txt = (dir12 / "info.txt").read_text(encoding="utf-8")
assert "keep_by): capture" in info12txt and "источник: json photoTakenTime" in info12txt
# группа B: папка по чистому имени, оригинал остался
dir12b = trash / "_DSC_0001.jpg"
assert dir12b.is_dir() and (dir12b / "DSC_0001 (Copy 2).jpg").exists()
assert (dsc12 / "DSC_0001.jpg").exists(), "оригинал DSC (чистое имя) остаётся в src"
print("12b. move capture: папки _IMG_20260912_163920.jpg / _DSC_0001.jpg, json в info.txt OK")

# 12c) коллизии: два оригинала с одинаковым basename в разных папках → __2
coll = src / "coll"
for sub, seed in (("a", 601), ("b", 602)):
    d = coll / sub
    d.mkdir(parents=True)
    imc = _mts.make_base(seed, _random.Random(seed))
    imc.save(d / "IMG_X.jpg", "JPEG", quality=92)
    imc.transpose(PILImage.Transpose.ROTATE_90).save(d / "IMG_X_rot90.jpg", "JPEG", quality=95)
    os.utime(d / "IMG_X.jpg", (t12, t12))
    os.utime(d / "IMG_X_rot90.jpg", (t12, t12))
assert cli_main(["--settings", str(settings_path), "scan"]) == 0
assert cli_main(["--settings", str(settings_path), "analyze"]) == 0
rc = cli_main(["--settings", str(settings_path), "move", "--keep-by", "capture"])
assert rc == 0
assert (trash / "_IMG_X.jpg").is_dir() and (trash / "_IMG_X__2.jpg").is_dir(), \
    sorted(p.name for p in trash.iterdir())
kept_coll = conn.execute(
    "SELECT kept_path, info->>'group_dir' FROM groups "
    "WHERE info->>'group_dir' IN ('_IMG_X.jpg', '_IMG_X__2.jpg') ORDER BY id"
).fetchall()
assert len(kept_coll) == 2, kept_coll
assert (coll / "a" / "IMG_X.jpg").exists() and (coll / "b" / "IMG_X.jpg").exists(), \
    "оба оригинала IMG_X.jpg остались в src"
print("12c. коллизия имён папок групп разрешена суффиксом __2 OK")

# --- 13) crash-recovery одноимённых дубликатов + files.status='moved' ---
import shutil  # noqa: E402

recdir = src / "rec"
(recdir / "base").mkdir(parents=True)
(recdir / "d1").mkdir()
(recdir / "d2").mkdir()
# seed/idx подобраны вне последовательности make_testset (shared rng(42)):
# иначе «случайное» изображение совпадёт с bulk/original_000.jpg и прильнёт к группе
img13 = _mts.make_base(703, _random.Random(999))
buf13 = _io.BytesIO()
img13.save(buf13, "JPEG", quality=92)
blob13 = buf13.getvalue()
# три байт-в-байт копии с ОДИНАКОВЫМ basename в разных папках:
# при переносе дубликаты получают имена IMG.jpg и IMG_1.jpg (_unique_target)
(recdir / "base" / "IMG.jpg").write_bytes(blob13)
(recdir / "d1" / "IMG.jpg").write_bytes(blob13)
(recdir / "d2" / "IMG.jpg").write_bytes(blob13)
for p in recdir.rglob("*.jpg"):
    os.utime(p, (t12, t12))
assert cli_main(["--settings", str(settings_path), "scan"]) == 0
assert cli_main(["--settings", str(settings_path), "analyze"]) == 0
g_rec = conn.execute(
    "SELECT gm.group_id FROM group_members gm JOIN files f ON f.id=gm.file_id WHERE f.path=%s",
    (str(recdir / "base" / "IMG.jpg"),),
).fetchone()[0]
assert conn.execute("SELECT member_count FROM groups WHERE id=%s", (g_rec,)).fetchone()[0] == 3

# Эмулируем падение move между файловыми операциями и коммитом БД:
# оба дубликата уже лежат в папке группы (IMG.jpg и IMG_1.jpg), БД не знает об этом
dest13 = trash / "_IMG.jpg"
dest13.mkdir()
shutil.move(str(recdir / "d1" / "IMG.jpg"), str(dest13 / "IMG.jpg"))
shutil.move(str(recdir / "d2" / "IMG.jpg"), str(dest13 / "IMG_1.jpg"))
rc = cli_main(["--settings", str(settings_path), "move"])
assert rc == 0
rec_moved = conn.execute(
    "SELECT f.path, gm.moved_to FROM group_members gm JOIN files f ON f.id=gm.file_id "
    "WHERE gm.group_id=%s AND gm.moved_to IS NOT NULL ORDER BY f.path",
    (g_rec,),
).fetchall()
assert len(rec_moved) == 2, rec_moved
assert rec_moved[0][1] == str(dest13 / "IMG.jpg"), rec_moved
assert rec_moved[1][1] == str(dest13 / "IMG_1.jpg"), \
    f"второй одноимённый дубликат не должен «приклеиться» к первому файлу: {rec_moved}"
assert (recdir / "base" / "IMG.jpg").exists(), "оригинал остаётся в src"
assert conn.execute(
    "SELECT count(*) FROM group_members WHERE group_id=%s AND moved_to IS NOT NULL AND moved_at IS NULL",
    (g_rec,),
).fetchone()[0] == 0, "moved_at доносится recovery-строкам"
# хэши восстановленных дубликатов тоже удалены (остались только у kept)
n_hash_rec = conn.execute(
    "SELECT count(*) FROM hashes h JOIN files f ON f.id=h.file_id WHERE f.path LIKE %s",
    (str(recdir) + "%",),
).fetchone()[0]
assert n_hash_rec == 8, n_hash_rec
# строки files помечены status='moved'
assert conn.execute(
    "SELECT status FROM files WHERE path=%s", (str(recdir / "d1" / "IMG.jpg"),)
).fetchone()[0] == "moved"
moved_files_n = conn.execute("SELECT count(*) FROM files WHERE status='moved'").fetchone()[0]
assert moved_files_n >= 2
print(f"13. recovery одноимённых дубликатов (IMG.jpg × 2 → IMG.jpg/IMG_1.jpg) "
      f"+ files.status='moved' OK ({moved_files_n} строк помечено)")

# --- 14) EXIF-свойства: scan.read_exif → files.exif; отключение флага ---
exif_dir = src / "exifp"
exif_dir.mkdir()
imx = PILImage.new("RGB", (64, 48), (10, 200, 10))
exx = PILImage.Exif()
exx[271] = "Xiaomi"
exx[272] = "Redmi Note 13"
subx = exx.get_ifd(0x8769)
subx[36867] = "2026:09:12 16:39:20"  # DateTimeOriginal
subx[34855] = 160                    # ISO
imx.save(exif_dir / "with_exif.jpg", "JPEG", exif=exx)
_mts.make_base(810, _random.Random(810)).save(exif_dir / "no_exif.jpg", "JPEG", quality=90)
assert cli_main(["--settings", str(settings_path), "scan"]) == 0
exif_val = conn.execute("SELECT exif FROM files WHERE path=%s",
                        (str(exif_dir / "with_exif.jpg"),)).fetchone()[0]
assert exif_val is not None and exif_val.get("cameraBrand") == "Xiaomi" \
    and exif_val.get("cameraModel") == "Redmi Note 13" \
    and exif_val.get("isoSpeedRating") == "160" \
    and exif_val.get("createdOn") == "2026-09-12 16:39:20", exif_val
assert exif_val.get("imageType") == "jpeg (JPEG)", exif_val
exif_plain = conn.execute("SELECT exif FROM files WHERE path=%s",
                          (str(exif_dir / "no_exif.jpg"),)).fetchone()[0]
assert exif_plain is not None and "cameraBrand" not in exif_plain \
    and exif_plain.get("createdOn"), exif_plain  # createdOn=fallback mtime
print("14a. scan заполняет files.exif (jsonb): камера/ISO/createdOn OK")


def _write_settings_14(read_exif: bool) -> None:
    with open(settings_path, "wb") as f:
        tomli_w.dump({
            "paths": {"src": str(src), "trash": str(trash)},
            "scan": {"threads": 4, "batch_size": 10, "read_exif": read_exif},
            "analyze": {"threshold": 4},
            "move": {"mode": "auto", "keep_by": "size", "conflict": "suffix", "dry_run": False},
        }, f)


_write_settings_14(read_exif=False)
_mts.make_base(811, _random.Random(811)).save(exif_dir / "off_1.jpg", "JPEG", quality=90)
assert cli_main(["--settings", str(settings_path), "scan"]) == 0
assert conn.execute("SELECT exif FROM files WHERE path=%s",
                    (str(exif_dir / "off_1.jpg"),)).fetchone()[0] is None
_write_settings_14(read_exif=True)  # вернуть дефолт для последующих секций
print("14b. scan.read_exif=false → files.exif остаётся NULL OK")

# --- 15) webops: ручные операции UI — ↩️ ⭐ 📋 🗑️ ---
import engine.webops as webops
from engine.settings import load_settings as _load_settings_15

ops_dir = src / "ops"
ops_dir.mkdir()
img_ops = _mts.make_base(808, _random.Random(808))
buf_ops = _io.BytesIO()
img_ops.save(buf_ops, "JPEG", quality=92)
blob_ops = buf_ops.getvalue()
(ops_dir / "a_original.jpg").write_bytes(blob_ops)
(ops_dir / "b_copy.jpg").write_bytes(blob_ops)
(ops_dir / "c_copy.jpg").write_bytes(blob_ops)
for p in ops_dir.glob("*.jpg"):
    os.utime(p, (t12, t12))
assert cli_main(["--settings", str(settings_path), "scan"]) == 0
assert cli_main(["--settings", str(settings_path), "analyze"]) == 0
assert cli_main(["--settings", str(settings_path), "move"]) == 0

S15 = _load_settings_15(settings_path)
opconn = psycopg.connect(PG_DSN, autocommit=True)


def _fid(path: str) -> int:
    return opconn.execute("SELECT id FROM files WHERE path=%s", (path,)).fetchone()[0]


g_ops = opconn.execute(
    "SELECT gm.group_id FROM group_members gm JOIN files f ON f.id=gm.file_id WHERE f.path=%s",
    (str(ops_dir / "a_original.jpg"),),
).fetchone()[0]
assert opconn.execute("SELECT member_count FROM groups WHERE id=%s", (g_ops,)).fetchone()[0] == 3
fid_a, fid_b, fid_c = (_fid(str(ops_dir / n)) for n in
                       ("a_original.jpg", "b_copy.jpg", "c_copy.jpg"))
kept_before = opconn.execute("SELECT kept_path FROM groups WHERE id=%s", (g_ops,)).fetchone()[0]
assert kept_before == str(ops_dir / "a_original.jpg"), kept_before  # тай-брейк пути
gdir_ops = trash / opconn.execute(
    "SELECT info->>'group_dir' FROM groups WHERE id=%s", (g_ops,)
).fetchone()[0]
assert (gdir_ops / "b_copy.jpg").exists() and (gdir_ops / "c_copy.jpg").exists()

# 15a. ↩️ Вернуть: b из trash → src
webops.return_file(opconn, S15, fid_b)
assert (ops_dir / "b_copy.jpg").exists() and not (gdir_ops / "b_copy.jpg").exists()
assert opconn.execute("SELECT moved_to FROM group_members WHERE file_id=%s", (fid_b,)).fetchone()[0] is None
assert opconn.execute("SELECT status FROM files WHERE id=%s", (fid_b,)).fetchone()[0] == "ok"
print("15a. webops.return_file: файл вернулся в src, moved_to сброшен OK")

# 15b. ⭐ b — оригинал: b остаётся в src, a переносится в trash (обмен ролями)
webops.star_as_original(opconn, S15, fid_b)
assert (ops_dir / "b_copy.jpg").exists()
assert not (ops_dir / "a_original.jpg").exists() and (gdir_ops / "a_original.jpg").exists()
kept_now = opconn.execute("SELECT kept_path FROM groups WHERE id=%s", (g_ops,)).fetchone()[0]
assert kept_now == str(ops_dir / "b_copy.jpg"), kept_now
assert opconn.execute("SELECT status FROM files WHERE id=%s", (fid_a,)).fetchone()[0] == "moved"
print("15b. webops.star_as_original: обмен ролями с физическим переносом OK")

# 15c. ↩️ c → src; 📋 b (текущий оригинал) → trash, автовыбор нового оригинала (c)
webops.return_file(opconn, S15, fid_c)
webops.mark_as_duplicate(opconn, S15, fid_b)
assert not (ops_dir / "b_copy.jpg").exists() and (gdir_ops / "b_copy.jpg").exists()
kept_now2 = opconn.execute("SELECT kept_path FROM groups WHERE id=%s", (g_ops,)).fetchone()[0]
assert kept_now2 == str(ops_dir / "c_copy.jpg"), kept_now2
assert opconn.execute("SELECT role FROM group_members WHERE group_id=%s AND file_id=%s",
                      (g_ops, fid_c)).fetchone()[0] == "kept"
print("15c. webops.mark_as_duplicate: оригинал в trash, новый оригинал выбран автоматически OK")

# 15d. ⭐ a (из trash): a возвращается, c переносится в trash
webops.star_as_original(opconn, S15, fid_a)
assert (ops_dir / "a_original.jpg").exists() and not (ops_dir / "c_copy.jpg").exists()
kept_now3 = opconn.execute("SELECT kept_path FROM groups WHERE id=%s", (g_ops,)).fetchone()[0]
assert kept_now3 == str(ops_dir / "a_original.jpg")
print("15d. webops.star_as_original из trash: возврат + демотация текущего оригинала OK")

# 15e. 🗑️ c: файл удалён с диска, строки БД вычищены, счётчики группы пересчитаны
webops.delete_file(opconn, S15, fid_c)
assert opconn.execute("SELECT count(*) FROM files WHERE id=%s", (fid_c,)).fetchone()[0] == 0
assert opconn.execute("SELECT member_count FROM groups WHERE id=%s", (g_ops,)).fetchone()[0] == 2
assert not (gdir_ops / "c_copy.jpg").exists()
# хэши участников после возвратов отсутствуют (вернувшийся без хэшей переиндексируется scan-ом)
assert opconn.execute("SELECT count(*) FROM hashes WHERE file_id IN (%s,%s,%s)",
                      (fid_a, fid_b, fid_c)).fetchone()[0] == 0
# журнал: ручные операции записаны
n_op_events = opconn.execute(
    "SELECT count(*) FROM events WHERE message LIKE '↩️%' OR message LIKE '⭐%' "
    "OR message LIKE '📋%' OR message LIKE '🗑%'"
).fetchone()[0]
assert n_op_events >= 5, n_op_events
print(f"15e. webops.delete_file: файл/строки удалены, счётчики и журнал актуальны OK ({n_op_events} событий)")

# --- 16) undo: все переносы возвращаются, результаты и журнал очищаются ---
n_files_before_undo = opconn.execute("SELECT count(*) FROM files").fetchone()[0]
n_moved_before_undo = opconn.execute(
    "SELECT count(*) FROM group_members WHERE moved_to IS NOT NULL").fetchone()[0]
assert n_moved_before_undo > 0
assert cli_main(["--settings", str(settings_path), "undo", "--dry-run"]) == 0
assert opconn.execute("SELECT count(*) FROM files").fetchone()[0] == n_files_before_undo, \
    "dry-run undo не должен ничего менять"
assert (gdir_ops / "b_copy.jpg").exists(), "dry-run: файл не должен покидать trash"

assert cli_main(["--settings", str(settings_path), "undo"]) == 0
assert (ops_dir / "a_original.jpg").exists() and (ops_dir / "b_copy.jpg").exists(), \
    "a и b вернулись на исходные места"
assert not (ops_dir / "c_copy.jpg").exists(), "удаленный через 🗑️ файл не возвращается"
for table in ("files", "hashes", "groups", "group_members", "analysis_runs"):
    n = opconn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    assert n == 0, f"{table}: {n}"
stage16 = opconn.execute("SELECT stage FROM status WHERE id=1").fetchone()[0]
assert stage16 == "idle", stage16
n_undo_events = opconn.execute("SELECT count(*) FROM events").fetchone()[0]
assert n_undo_events >= 1, "после undo в свежем журнале есть запись"
leftover_images = [p for p in trash.rglob("*") if p.is_file() and p.suffix.lower() in
                   (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff")]
assert not leftover_images, f"в trash остались файлы: {leftover_images[:5]}"
print(f"16. undo: возвращено {n_moved_before_undo} файлов, trash пуст, "
      f"БД очищена (TRUNCATE), stage=idle OK")

# --- 17) clean-db: очистка результатов и журнала без файловых операций ---
opconn.execute(
    "INSERT INTO files (path, size, mtime, status) VALUES ('/x/a.jpg', 1, 0, 'ok')"
)
opconn.execute("INSERT INTO events (level, message) VALUES ('info', 'проверка clean-db')")
assert cli_main(["--settings", str(settings_path), "clean-db"]) == 0
for table in ("files", "groups"):
    n = opconn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    assert n == 0, f"{table}: {n}"
# журнал пуст не «навсегда»: clean-db оставляет ровно одну запись-отчёт о себе
n_ev17 = opconn.execute("SELECT count(*) FROM events").fetchone()[0]
assert n_ev17 == 1 and "БД очищена" in opconn.execute(
    "SELECT message FROM events LIMIT 1").fetchone()[0], n_ev17
stage17 = opconn.execute("SELECT stage FROM status WHERE id=1").fetchone()[0]
assert stage17 == "idle", stage17
opconn.close()
print("17. clean-db: результаты и журнал очищены, файлы на диске не тронуты OK")

# --- 18) очередь jobs + runner (Ф1): enqueue/claim/guard/heartbeat/finish/reap/execute ---
import threading as _threading
from psycopg import errors as pg_errors  # noqa: E402
from engine import jobs as jobq
from engine.runner import execute_job, job_state

jconn = psycopg.connect(PG_DSN, autocommit=True)
assert jconn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0, "очередь пуста до теста"

# 18a. enqueue + уникальный частичный индекс (одно queued-задание на команду)
jid18 = jobq.enqueue(jconn, "run", {"dry_run": True})
assert jconn.execute("SELECT state, command, params->>'dry_run' FROM jobs WHERE id=%s",
                     (jid18,)).fetchone() == ("queued", "run", "true")
try:
    jobq.enqueue(jconn, "run")
    raise AssertionError("ожидали UniqueViolation на дубликат queued 'run'")
except pg_errors.UniqueViolation:
    pass
print("18a. enqueue + uniq_jobs_queued_command OK")

# 18b. claim: атомарный взбор (SKIP LOCKED); повторный — None; heartbeat — только свой
job18 = jobq.claim_next(jconn, "runner-test")
assert job18 and job18["id"] == jid18 and job18["command"] == "run" \
    and job18["params"] == {"dry_run": True}, job18
assert jconn.execute("SELECT state, runner_id, taken_at IS NOT NULL, heartbeat_at IS NOT NULL "
                     "FROM jobs WHERE id=%s", (jid18,)).fetchone() \
    == ("running", "runner-test", True, True)
assert jobq.claim_next(jconn, "runner-test") is None, "running не пере-берётся"
assert jobq.heartbeat(jconn, jid18, "runner-test") is True
assert jobq.heartbeat(jconn, jid18, "runner-other") is False, "чужой runner метку не ставит"
print("18b. claim_next (SKIP LOCKED) + heartbeat OK")

# 18c. single-flight guard: свежий рабочий stage в status блокирует взбор
undo_jid = jobq.enqueue(jconn, "undo", {"dry_run": True})
jconn.execute("UPDATE status SET stage='scan', updated_at=now() WHERE id=1")
assert jobq.claim_next(jconn, "runner-test") is None, "guard должен блокировать"
jconn.execute("UPDATE status SET stage='idle', updated_at=now() WHERE id=1")
ujob = jobq.claim_next(jconn, "runner-test")
assert ujob and ujob["id"] == undo_jid and ujob["command"] == "undo", ujob
jobq.finish(jconn, undo_jid, state="stopped", exit_code=0)
jobq.finish(jconn, jid18, state="done", exit_code=0, result={"stage": "done"})
assert jconn.execute("SELECT state, exit_code, finished_at IS NOT NULL FROM jobs WHERE id=%s",
                     (undo_jid,)).fetchone() == ("stopped", 0, True)
assert jconn.execute("SELECT state, result->>'stage' FROM jobs WHERE id=%s",
                     (jid18,)).fetchone() == ("done", "done")
print("18c. single-flight guard + finish (done/stopped) OK")

# 18d. reap_stale: running с протухшим heartbeat → stale; свежий не трогается
stale_jid = jobq.enqueue(jconn, "scan")
jobq.claim_next(jconn, "runner-test")
jconn.execute("UPDATE jobs SET heartbeat_at = now() - interval '120 s' WHERE id=%s", (stale_jid,))
assert jobq.reap_stale(jconn, stale_sec=30) == 1
assert jconn.execute("SELECT state, finished_at IS NOT NULL FROM jobs WHERE id=%s",
                     (stale_jid,)).fetchone() == ("stale", True)
fresh_jid = jobq.enqueue(jconn, "analyze")
jobq.claim_next(jconn, "runner-test")
assert jobq.reap_stale(jconn, stale_sec=30) == 0, "свежий running не трогается"
assert jconn.execute("SELECT state FROM jobs WHERE id=%s", (fresh_jid,)).fetchone()[0] == "running"
jobq.finish(jconn, fresh_jid, state="done", exit_code=0)
print("18d. reap_stale (crash-recovery) OK")

# 18e. execute_job: РЕАЛЬНЫЙ полный `run` (move — dry_run) кодом runner'а
run_jid = jobq.enqueue(jconn, "run", {"dry_run": True})
rjob = jobq.claim_next(jconn, "runner-test")
assert rjob and rjob["id"] == run_jid
rc18, err18, res18 = execute_job(rjob, settings_path=str(settings_path),
                                 stop_event=_threading.Event(), runner_id="runner-test")
state18 = job_state(rc18, err18, res18)
jobq.finish(jconn, run_jid, state=state18, exit_code=rc18, error=err18, result=res18)
assert rc18 == 0 and err18 is None and state18 == "done", (rc18, err18, state18, res18)
assert res18.get("stage") == "done", res18
from engine.settings import load_settings as _ls18  # noqa: E402

s18 = _ls18(str(settings_path))
disk18 = [p for p in Path(s18.paths.src).rglob("*")
          if p.is_file() and p.suffix.lower() in set(s18.scan.extensions)]
n_files18 = jconn.execute("SELECT count(*) FROM files").fetchone()[0]
assert n_files18 == len(disk18), (n_files18, len(disk18))
assert jconn.execute("SELECT count(*) FROM analysis_runs").fetchone()[0] >= 1
assert jconn.execute(
    "SELECT count(*) FROM group_members WHERE moved_to IS NOT NULL").fetchone()[0] == 0, \
    "dry_run: физического переноса и moved_to быть не должно"
print(f"18e. execute_job('run', dry_run): done, exit 0, files={n_files18} == на диске OK")

# 18f. snapshot для Монитора
snap18 = jobq.snapshot(jconn)
assert snap18["queued"] == 0 and snap18["running"] is None
assert len(snap18["recent"]) >= 4 and all(r["state"] != "queued" for r in snap18["recent"])
print(f"18f. snapshot: counts={snap18['counts']}, recent={len(snap18['recent'])} OK")

# 18g. retention (Ф2): prune хранит последние N завершённых заданий
jconn.execute("UPDATE jobs SET state='done', finished_at=now() WHERE state <> 'done'")
for i in range(12):
    jid18 = jobq.enqueue(jconn, "scan")
    jconn.execute("UPDATE jobs SET state='done', finished_at=now() WHERE id=%s", (jid18,))
pruned18 = jobq.prune(jconn, keep=10)
n_term18 = jconn.execute("SELECT count(*) FROM jobs").fetchone()[0]
assert n_term18 == 10, (n_term18, pruned18)
ids18 = [r[0] for r in jconn.execute("SELECT id FROM jobs ORDER BY id").fetchall()]
assert ids18 == sorted(ids18)[-10:], "prune обязан оставить ПОСЛЕДНИЕ N по id"
assert jobq.prune(jconn, keep=10) == 0, "повторный prune — без изменений"
# queued не трогаются
q18 = jobq.enqueue(jconn, "scan")
assert jobq.prune(jconn, keep=10) == 0, "queued-задание не подлежит retention-очистке"
assert jconn.execute("SELECT state FROM jobs WHERE id=%s", (q18,)).fetchone()[0] == "queued"
jconn.execute("DELETE FROM jobs WHERE id=%s", (q18,))
print("18g. retention prune(keep=10): осталось 10 последних, queued не тронут OK")

# 18h. «Очистить БД» (Ф2): TRUNCATE_ALL вычищает и очередь jobs
from engine.webops import clean_db as _cdb18  # noqa: E402
from engine import db as webops_db  # noqa: E402  (first_value)
j18 = jobq.enqueue(jconn, "move")
msg18 = _cdb18(jconn)
assert "очереди" in msg18, msg18
assert jconn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0, \
    "clean_db обязан вычистить историю очереди"
assert jconn.execute("SELECT count(*) FROM events").fetchone()[0] == 1, \
    "в журнале остаётся ровно одна запись-отчёт clean_db"
print("18h. clean_db: очередь jobs вычищена, отчёт в журнале OK")

# 18i. регресс 1.6.1: enqueue/scalar/clean_db через dict_row-соединение (как пул web).
# Было: fetchone()[0] на dict-строке → KeyError: 0, INSERT откатывался пулом.
from psycopg.rows import dict_row as _dict_row  # noqa: E402
dconn = psycopg.connect(PG_DSN, row_factory=_dict_row)
djid = jobq.enqueue(dconn, "scan")          # упал бы KeyError'ом до фикса
assert isinstance(djid, int) and djid > 0
dconn.commit()
assert webops_db.scalar(dconn, "SELECT count(*) FROM jobs") == 1, \
    "db.scalar тоже должен быть row-factory-агностичным"
assert isinstance(webops_db.first_value({"n": 7}), int) and webops_db.first_value(None) is None
msg18i = _cdb18(dconn)                       # clean_db через dict_row (было: KeyError)
assert webops_db.scalar(dconn, "SELECT count(*) FROM jobs") == 0
dconn.close()
print("18i. dict_row-пул (как web): enqueue/scalar/clean_db OK")

jconn.close()
print("18. очередь jobs + runner (Ф1/Ф2) OK")

conn.close()
srv.cleanup()  # останавливает сервер и удаляет pgdata
shutil.rmtree(work, ignore_errors=True)
print("\nINTEGRATION TEST PASSED")
