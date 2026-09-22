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

# галерея: moved_to указывает в trash/group_*, файл существует
rows = conn.execute(
    "SELECT gm.moved_to FROM group_members gm WHERE gm.moved_to IS NOT NULL LIMIT 50"
).fetchall()
for (mv,) in rows:
    assert mv.startswith(str(trash / "group_")) and Path(mv).exists(), mv
kept_rows = conn.execute(
    "SELECT kept_path FROM groups WHERE kept_path IS NOT NULL"
).fetchall()
for (kp,) in kept_rows:
    assert Path(kp).exists() and str(kp).startswith(str(src)), kp
print("2. DB state consistent; moved_to -> trash/group_N exists on disk OK")

n_events = conn.execute("SELECT count(*) FROM events").fetchone()[0]
assert n_events > 0

gdirs = list(trash.glob("group_*"))
assert len(gdirs) == n_groups
for gd in gdirs:
    assert (gd / "info.txt").exists(), gd
print("3. trash/group_N с info.txt OK:", len(gdirs), "dirs")

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
gd = trash / f"group_{first_group}"
assert gd.exists() and (gd / "info.txt").exists()
print(f"9b. manual: подтверждённая группа #{first_group} перенесена ({members_of_first - 1} файлов) OK")

# ещё одна группа — через --group-id без подтверждения
second_group = conn.execute(
    "SELECT id FROM groups WHERE run_id=%s AND confirmed=false ORDER BY id LIMIT 1", (run_dry,)
).fetchone()[0]
rc = cli_main(["--settings", str(settings_path), "move", "--group-id", str(second_group)])
assert rc == 0
assert (trash / f"group_{second_group}").exists()
print(f"9c. move --group-id #{second_group}: принудительный перенос OK")

# --- 10) галерейные запросы UI (лимитированные) ---
page = conn.execute(
    "SELECT id, member_count, total_size, kept_path, confirmed, info FROM groups "
    "WHERE run_id=%s ORDER BY total_size DESC, id LIMIT %s OFFSET %s",
    (run_dry, 15, 0),
).fetchall()
assert len(page) >= 2
members = conn.execute(
    "SELECT gm.file_id, gm.role, gm.moved_to, f.path, f.size, f.width, f.height "
    "FROM group_members gm JOIN files f ON f.id=gm.file_id WHERE gm.group_id=%s "
    "ORDER BY (gm.role='kept') DESC, f.size DESC, f.path",
    (page[0][0],),
).fetchall()
assert len(members) == page[0][1]
# перенесённые показывают moved_to (актуальный путь), оригинал — путь в src
moved_rows = [m for m in members if m[2] is not None]
assert all(str(m[2]).startswith(str(trash)) for m in moved_rows)
print("10. gallery queries OK (page LIMIT/OFFSET, members, актуальные moved_to)")

conn.close()
srv.cleanup()  # останавливает сервер и удаляет pgdata
shutil.rmtree(work, ignore_errors=True)
print("\nINTEGRATION TEST PASSED")
