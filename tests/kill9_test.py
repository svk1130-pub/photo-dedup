"""Критерий приёмки 3: kill -9 движка в середине scan не портит БД,
повторный run продолжает с места остановки. Запуск: python3 kill9_test.py
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))
sys.path.insert(0, str(PROJ / "scripts"))

import pgserver

work = Path(tempfile.mkdtemp(prefix="pd-kill9-"))
srv = pgserver.get_server(str(work / "pgdata"))
uri = srv.get_uri()

import psycopg

adm = psycopg.connect(uri, autocommit=True)
adm.execute("CREATE ROLE photo LOGIN PASSWORD 'photo'")
adm.execute("CREATE DATABASE photo OWNER photo")
port = adm.execute("SHOW port").fetchone()[0]
adm.close()
sockdir = uri.split("host=")[-1].split("&")[0]
PG_DSN = f"host={sockdir} port={port} dbname=photo user=photo password=photo"

src = work / "src"
src.mkdir()
import make_testset

sys.argv = ["make_testset.py", str(src), "25"]  # ~226 файлов
assert make_testset.main() == 0

import tomli_w

settings_path = work / "settings.toml"
with open(settings_path, "wb") as f:
    tomli_w.dump({
        "paths": {"src": str(src), "trash": str(work / "trash")},
        "scan": {"threads": 2, "batch_size": 5},
    }, f)

env = dict(os.environ, PG_DSN=PG_DSN, SETTINGS_PATH=str(settings_path), PYTHONPATH=str(PROJ))
cmd = [sys.executable, "-m", "engine.cli", "scan"]

# --- запускаем scan и убиваем -9, как только появились обработанные файлы ---
proc = subprocess.Popen(cmd, cwd=str(PROJ), env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
conn = psycopg.connect(PG_DSN, autocommit=True)
killed = False
deadline = time.time() + 120
while time.time() < deadline and proc.poll() is None:
    try:
        row = conn.execute("SELECT processed FROM status WHERE id=1").fetchone()
        if row and row[0] > 10:
            os.kill(proc.pid, signal.SIGKILL)
            killed = True
            break
    except psycopg.Error:
        pass  # БД могла быть недоступна на мгновение — это не должно ломать её
    time.sleep(0.02)
proc.wait(timeout=30)
assert killed, "не успели убить процесс — увеличьте набор"
print("1. kill -9 отправлен при processed>10 OK")

# --- БД жива и консистентна ---
conn2 = psycopg.connect(PG_DSN, autocommit=True)
n1 = conn2.execute("SELECT count(*) FROM files").fetchone()[0]
h1 = conn2.execute("SELECT count(*) FROM hashes").fetchone()[0]
assert h1 % 8 == 0, "хэши должны коммититься полными файлами (8 на файл)"
assert conn2.execute("SELECT count(*) FROM hashes h JOIN files f ON f.id=h.file_id "
                     "WHERE f.path IS NULL").fetchone()[0] == 0
print(f"2. БД пережила kill -9: files={n1}, hashes={h1} (кратны 8) OK")

# --- повторный run завершает работу ---
rc = subprocess.run([sys.executable, "-m", "engine.cli", "run"], cwd=str(PROJ), env=env,
                    capture_output=True, text=True)
assert rc.returncode == 0, rc.stdout + rc.stderr
n2 = conn2.execute("SELECT count(*) FROM files").fetchone()[0]
n_groups = conn2.execute("SELECT count(*) FROM groups").fetchone()[0]
n_moved = conn2.execute("SELECT count(*) FROM group_members WHERE moved_to IS NOT NULL").fetchone()[0]
assert n2 == 25 * 9 + 1, n2
assert n_groups >= 25 and n_moved > 0
stage = conn2.execute("SELECT stage FROM status WHERE id=1").fetchone()[0]
assert stage == "done", stage
print(f"3. повторный run завершил цикл: files={n2}, groups={n_groups}, moved={n_moved}, stage={stage} OK")

conn2.close()
srv.cleanup()
shutil.rmtree(work, ignore_errors=True)
print("\nKILL-9 TEST PASSED")
