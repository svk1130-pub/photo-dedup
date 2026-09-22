"""Этап 2 — анализ: бакеты по hash_part, расстояние Хэмминга, union-find.

Перебор бакетов потоковым курсором (cursor(name=...), itersize) — ничего
не тащим в RAM целиком. Внутри бакета — попарное расстояние Хэмминга по
полному phash (XOR + popcount; для хэшей ≤64 бит — векторизовано через numpy).

Группа = транзитивное замыкание через union-find ВНУТРИ бакета
(A~B, B~C ⇒ одна группа, даже если A!~C). Кандидат — любая пара вариантов
(file A, variant i) vs (file B, variant j) с дистанцией ≤ threshold.

Документированный trade-off: пары, чьи хэши различаются в ПЕРВЫХ
hash_part_len*4 битах (по умолчанию 16), не попадут в один бакет —
это осознанное ограничение ради скорости.
"""
from __future__ import annotations

import itertools
import logging
import os
import time
from typing import TYPE_CHECKING, Iterator

import numpy as np
from psycopg.types.json import Json
from psycopg.rows import dict_row

from .db import reset_status, set_stage, update_progress
from .hashing import hamming_distance

if TYPE_CHECKING:
    from .cli import EngineContext

logger = logging.getLogger("engine.analyze")

_MIN_EDGE_INF = 1 << 30


class UnionFind:
    """DSU с path compression + минимум ребра внутри компоненты."""

    def __init__(self) -> None:
        self.parent: dict[int, int] = {}
        self.min_edge: dict[int, int] = {}

    def add(self, x: int) -> None:
        if x not in self.parent:
            self.parent[x] = x
            self.min_edge[x] = _MIN_EDGE_INF

    def find(self, x: int) -> int:
        p = self.parent
        root = x
        while p[root] != root:
            root = p[root]
        while p[x] != root:  # path compression
            p[x], x = root, p[x]
        return root

    def union(self, a: int, b: int, dist: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            self.min_edge[ra] = min(self.min_edge[ra], dist)
            return
        self.parent[rb] = ra
        self.min_edge[ra] = min(self.min_edge[ra], self.min_edge[rb], dist)

    def components(self) -> dict[int, list[int]]:
        out: dict[int, list[int]] = {}
        for x in self.parent:
            out.setdefault(self.find(x), []).append(x)
        return out


def _bucket_pairs(fids: list[int], ints: list[int], threshold: int) -> Iterator[tuple[int, int, int]]:
    """Векторизованный вариант (длина хэша ≤ 8 байт): numpy XOR + bitwise_count.

    Пары обрабатываются блоками строк по всему столбцу с фильтром j > i —
    каждая пара учитывается ровно один раз, память ограничена блоком.
    """
    n = len(ints)
    arr = np.array(ints, dtype=np.uint64)
    fa = np.array(fids, dtype=np.int64)
    col_idx = np.arange(n)
    block = 2048
    for start in range(0, n, block):
        stop = min(start + block, n)
        xored = arr[start:stop, None] ^ arr[None, :]
        d = np.bitwise_count(xored)
        ok = (d <= threshold) & (fa[None, :] != fa[start:stop, None])
        ok &= col_idx[None, :] > col_idx[start:stop][:, None]
        for i, j in np.argwhere(ok):
            yield int(fids[start + i]), int(fids[j]), int(d[i, j])


def _bucket_pairs_python(fids: list[int], ints: list[int], threshold: int) -> Iterator[tuple[int, int, int]]:
    """Резерв для хэшей > 64 бит (hash_size > 8): чистый Python, XOR + bit_count."""
    n = len(ints)
    for i in range(n):
        for j in range(i + 1, n):
            if fids[i] == fids[j]:
                continue
            d = (ints[i] ^ ints[j]).bit_count()
            if d <= threshold:
                yield fids[i], fids[j], d


def _process_bucket(uf: UnionFind, fids: list[int], ints: list[int],
                    byte_len: int, threshold: int) -> None:
    if len(ints) < 2:
        return
    gen = _bucket_pairs(fids, ints, threshold) if byte_len <= 8 else _bucket_pairs_python(fids, ints, threshold)
    for a, b, d in gen:
        uf.add(a)
        uf.add(b)
        uf.union(a, b, d)


def choose_kept(rows: list[dict], keep_by: str) -> dict:
    """Выбор «оригинала»: по умолчанию максимальный размер; опция pixels — разрешение.
    Тай-брейк — лексикографически меньший путь (детерминизм)."""
    if keep_by == "pixels":
        key = lambda r: (-((r["width"] or 0) * (r["height"] or 0)), -r["size"], r["path"])
    else:
        key = lambda r: (-r["size"], r["path"])
    return min(rows, key=key)


def _fetch_files_info(conn, ids: list[int]) -> dict[int, dict]:
    out: dict[int, dict] = {}
    it = iter(ids)
    while chunk := list(itertools.islice(it, 10000)):
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT id, path, size, width, height FROM files WHERE id = ANY(%s)", (chunk,))
            for r in cur:
                out[r["id"]] = r
    return out


def run_analyze(ctx: "EngineContext") -> tuple[int | None, bool]:
    """Создаёт analysis_runs + groups/group_members. Возвращает (run_id, stopped)."""
    s = ctx.settings
    conn, conn_ctl = ctx.conn, ctx.conn_ctl
    threshold = s.analyze.threshold
    keep_by = s.move.keep_by

    n_files = conn.execute("SELECT count(DISTINCT file_id) FROM hashes").fetchone()[0]
    total_buckets = conn.execute(
        "SELECT count(*) FROM (SELECT 1 FROM hashes GROUP BY hash_part) t"
    ).fetchone()[0]
    if n_files < 2 or total_buckets == 0:
        ctx.log.warning("analyze: в БД нет хэшей — сначала выполните `scan`")
        set_stage(conn_ctl, "done")
        return None, False

    params = {
        "threshold": threshold,
        "hash_method": s.hash.method,
        "hash_size": s.hash.hash_size,
        "bucket_hex_chars": s.hash.hash_part_len,
        "bucket_bits": s.hash.hash_part_len * 4,
        "keep_by": keep_by,
        "files_with_hashes": n_files,
        "buckets": total_buckets,
    }
    reset_status(conn_ctl, stage="analyze", params=ctx.params_payload(params), pid=os.getpid())
    ctx.log.info(
        f"analyze: старт (threshold={threshold}, бакет={s.hash.hash_part_len * 4} бит, "
        f"файлов с хэшами={n_files}, бакетов={total_buckets})"
    )

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO analysis_runs (threshold, params) VALUES (%s, %s) RETURNING id",
                (threshold, Json(params)),
            )
            run_id: int = cur.fetchone()[0]

    uf = UnionFind()
    processed = 0
    last_t = 0.0
    stopped = False
    t0 = time.monotonic()

    # Потоковый серверный курсор: один проход по всем хэшам, бакеты группируются на лету
    with conn.transaction():
        with conn.cursor(name="analyze_buckets") as cur:
            cur.itersize = 20000
            cur.execute("SELECT hash_part, file_id, phash FROM hashes ORDER BY hash_part")
            cur_part: str | None = None
            fids: list[int] = []
            ints: list[int] = []
            byte_len = 0
            for part, fid, ph in cur:
                if part != cur_part:
                    _process_bucket(uf, fids, ints, byte_len, threshold)
                    cur_part = part
                    fids, ints = [], []
                    processed += 1
                    now = time.monotonic()
                    if now - last_t >= 0.5:
                        last_t = now
                        update_progress(
                            conn_ctl, stage="analyze", processed=processed, total=total_buckets,
                            files_per_sec=processed / max(now - t0, 1e-9),
                        )
                    if ctx.should_stop():
                        stopped = True
                        break
                fids.append(fid)
                ints.append(int.from_bytes(bytes(ph), "big"))
                byte_len = len(ph)
            else:
                _process_bucket(uf, fids, ints, byte_len, threshold)

    if stopped:
        with conn.transaction():
            conn.execute("DELETE FROM analysis_runs WHERE id = %s", (run_id,))
        set_stage(conn_ctl, "stopped")
        ctx.log.warning(f"analyze: остановлено по флагу, незавершённый run #{run_id} удалён")
        return None, True

    comps = [c for c in uf.components().values() if len(c) >= 2]
    if not comps:
        update_progress(conn_ctl, stage="analyze", processed=total_buckets, total=total_buckets)
        ctx.log.info("analyze: группы дубликатов не найдены")
        set_stage(conn_ctl, "done")
        return run_id, False

    info_by_id = _fetch_files_info(conn, [fid for c in comps for fid in c])
    comps.sort(key=lambda c: -sum(info_by_id[f]["size"] for f in c if f in info_by_id))

    group_rows: list[tuple] = []
    planned: list[tuple[dict, list[dict]]] = []
    for comp in comps:
        rows = [info_by_id[f] for f in comp if f in info_by_id]
        if len(rows) < 2:
            continue
        kept = choose_kept(rows, keep_by)
        total_size = sum(r["size"] for r in rows)
        min_d = uf.min_edge.get(uf.find(comp[0]), 0)
        info = {"threshold": threshold, "min_distance": min_d, "kept_by": keep_by, "method": "phash"}
        group_rows.append((run_id, len(rows), total_size, kept["path"], Json(info)))
        planned.append((kept, rows))

    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO groups (run_id, member_count, total_size, kept_path, info) "
                "VALUES (%s, %s, %s, %s, %s)",
                group_rows,
            )
            cur.execute("SELECT id, kept_path FROM groups WHERE run_id = %s", (run_id,))
            idmap = {kept_path: gid for gid, kept_path in cur.fetchall()}
            member_rows = []
            for (kept, rows), grow in zip(planned, group_rows):
                gid = idmap[kept["path"]]  # kept_path уникален в рамках run (компоненты непересекаются)
                for r in rows:
                    role = "kept" if r["id"] == kept["id"] else "dup"
                    member_rows.append((gid, r["id"], role))
            cur.executemany(
                "INSERT INTO group_members (group_id, file_id, role) VALUES (%s, %s, %s)",
                member_rows,
            )

    update_progress(conn_ctl, stage="analyze", processed=total_buckets, total=total_buckets)
    set_stage(conn_ctl, "done")
    ctx.log.info(
        f"analyze: завершено: {len(group_rows)} групп, "
        f"{sum(len(rows) for _k, rows in planned)} файлов в группах (run #{run_id})"
    )
    return run_id, False
