"""Настройки: чтение/запись settings.toml и валидация путей.

Приоритет источников: CLI-флаги > settings.toml > встроенные дефолты.
Модуль используется и движком, и веб-UI (общая схема конфигурации),
бизнес-логики не содержит.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomli_w  # запись нужна только UI
except ImportError:  # pragma: no cover
    tomli_w = None  # type: ignore[assignment]


class SettingsError(Exception):
    """Понятная ошибка настроек (с указанием файла/строки, без трейсбека)."""


# ----------------------------- конфиги-дата классы -----------------------------

@dataclass(slots=True)
class PathsConfig:
    src: Path = Path("/data/src")
    trash: Path = Path("/data/trash")


@dataclass(slots=True)
class ScanConfig:
    extensions: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff")
    recursive: bool = True
    threads: int = 8
    batch_size: int = 200


@dataclass(slots=True)
class HashConfig:
    method: str = "phash"
    hash_size: int = 8
    hash_part_len: int = 4


@dataclass(slots=True)
class AnalyzeConfig:
    threshold: int = 4


@dataclass(slots=True)
class MoveConfig:
    mode: str = "auto"        # auto | manual
    keep_by: str = "size"     # size | pixels
    conflict: str = "suffix"
    dry_run: bool = False


@dataclass(slots=True)
class UiConfig:
    page_size: int = 15
    refresh_sec: int = 2


@dataclass(slots=True)
class Settings:
    paths: PathsConfig = field(default_factory=PathsConfig)
    scan: ScanConfig = field(default_factory=ScanConfig)
    hash: HashConfig = field(default_factory=HashConfig)
    analyze: AnalyzeConfig = field(default_factory=AnalyzeConfig)
    move: MoveConfig = field(default_factory=MoveConfig)
    ui: UiConfig = field(default_factory=UiConfig)


# ----------------------------- утилиты парсинга -----------------------------

def _line_of(text: str, key: str) -> int:
    m = re.search(rf"(?m)^\s*{re.escape(key)}\s*=", text)
    return text[: m.start()].count("\n") + 1 if m else 0


def _err(text: str, key: str, section: str, problem: str) -> SettingsError:
    ln = _line_of(text, key)
    where = f"[{section}] {key}" + (f" (строка {ln})" if ln else "")
    return SettingsError(f"{where}: {problem}")


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    raise ValueError("ожидалось true/false")


def _to_int(v: Any, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("ожидалось целое число")
    if not lo <= v <= hi:
        raise ValueError(f"вне допустимого диапазона {lo}..{hi}")
    return v


def _to_choice(v: Any, choices: tuple[str, ...]) -> str:
    if isinstance(v, str) and v in choices:
        return v
    raise ValueError(f"ожидалось одно из: {', '.join(choices)}")


# ----------------------------- применение секций -----------------------------

def _apply_paths(cfg: PathsConfig, raw: dict[str, Any], text: str) -> None:
    for key in ("src", "trash"):
        if key in raw:
            v = raw[key]
            if not isinstance(v, str) or not v.strip():
                raise _err(text, key, "paths", "ожидалась непустая строка с путём")
            setattr(cfg, key, Path(v).expanduser())


def _apply_scan(cfg: ScanConfig, raw: dict[str, Any], text: str) -> None:
    if "extensions" in raw:
        try:
            v = raw["extensions"]
            if (not isinstance(v, list) or not v
                    or not all(isinstance(x, str) and x.startswith(".") and len(x) > 1 for x in v)):
                raise ValueError('ожидался непустой массив строк вида ".jpg"')
            cfg.extensions = tuple(str(x).lower() for x in v)
        except ValueError as e:
            raise _err(text, "extensions", "scan", str(e)) from None
    if "recursive" in raw:
        try:
            cfg.recursive = _to_bool(raw["recursive"])
        except ValueError as e:
            raise _err(text, "recursive", "scan", str(e)) from None
    if "threads" in raw:
        try:
            cfg.threads = _to_int(raw["threads"], 1, 256)
        except ValueError as e:
            raise _err(text, "threads", "scan", str(e)) from None
    if "batch_size" in raw:
        try:
            cfg.batch_size = _to_int(raw["batch_size"], 1, 100_000)
        except ValueError as e:
            raise _err(text, "batch_size", "scan", str(e)) from None


def _apply_hash(cfg: HashConfig, raw: dict[str, Any], text: str) -> None:
    if "method" in raw:
        try:
            cfg.method = _to_choice(raw["method"], ("phash",))
        except ValueError as e:
            raise _err(text, "method", "hash", str(e)) from None
    if "hash_size" in raw:
        try:
            cfg.hash_size = _to_int(raw["hash_size"], 4, 16)
        except ValueError as e:
            raise _err(text, "hash_size", "hash", str(e)) from None
    if "hash_part_len" in raw:
        try:
            cfg.hash_part_len = _to_int(raw["hash_part_len"], 1, 32)
        except ValueError as e:
            raise _err(text, "hash_part_len", "hash", str(e)) from None


def _apply_analyze(cfg: AnalyzeConfig, raw: dict[str, Any], text: str) -> None:
    if "threshold" in raw:
        try:
            cfg.threshold = _to_int(raw["threshold"], 0, 64)
        except ValueError as e:
            raise _err(text, "threshold", "analyze", str(e)) from None


def _apply_move(cfg: MoveConfig, raw: dict[str, Any], text: str) -> None:
    if "mode" in raw:
        try:
            cfg.mode = _to_choice(raw["mode"], ("auto", "manual"))
        except ValueError as e:
            raise _err(text, "mode", "move", str(e)) from None
    if "keep_by" in raw:
        try:
            cfg.keep_by = _to_choice(raw["keep_by"], ("size", "pixels"))
        except ValueError as e:
            raise _err(text, "keep_by", "move", str(e)) from None
    if "conflict" in raw:
        try:
            cfg.conflict = _to_choice(raw["conflict"], ("suffix",))
        except ValueError as e:
            raise _err(text, "conflict", "move", str(e)) from None
    if "dry_run" in raw:
        try:
            cfg.dry_run = _to_bool(raw["dry_run"])
        except ValueError as e:
            raise _err(text, "dry_run", "move", str(e)) from None


def _apply_ui(cfg: UiConfig, raw: dict[str, Any], text: str) -> None:
    if "page_size" in raw:
        try:
            cfg.page_size = _to_int(raw["page_size"], 1, 500)
        except ValueError as e:
            raise _err(text, "page_size", "ui", str(e)) from None
    if "refresh_sec" in raw:
        try:
            cfg.refresh_sec = _to_int(raw["refresh_sec"], 1, 3600)
        except ValueError as e:
            raise _err(text, "refresh_sec", "ui", str(e)) from None


def _cross_validate(s: Settings) -> None:
    if s.hash.method != "phash":
        raise SettingsError('Поддерживается только hash.method = "phash" (imagehash.phash)')
    if s.hash.hash_part_len > s.hash.hash_size * 2:
        raise SettingsError(
            f"hash.hash_part_len ({s.hash.hash_part_len}) больше числа hex-символов "
            f"хэша ({s.hash.hash_size * 2})"
        )
    if s.analyze.threshold > s.hash.hash_size * 8:
        raise SettingsError(
            f"analyze.threshold ({s.analyze.threshold}) больше разрядности хэша "
            f"({s.hash.hash_size * 8} бит)"
        )


# ----------------------------- overrides из CLI -----------------------------

def _apply_overrides(s: Settings, overrides: dict[str, Any]) -> None:
    for key, val in overrides.items():
        try:
            if key == "scan.threads":
                s.scan.threads = _to_int(val, 1, 256)
            elif key == "scan.batch_size":
                s.scan.batch_size = _to_int(val, 1, 100_000)
            elif key == "analyze.threshold":
                s.analyze.threshold = _to_int(val, 0, 64)
            elif key == "move.mode":
                s.move.mode = _to_choice(val, ("auto", "manual"))
            elif key == "move.keep_by":
                s.move.keep_by = _to_choice(val, ("size", "pixels"))
            elif key == "move.dry_run":
                s.move.dry_run = _to_bool(val)
            else:
                raise SettingsError(f"Неизвестный override-ключ: {key}")
        except ValueError as e:
            raise SettingsError(f"CLI-флаг {key}: {e}") from None


# ----------------------------- публичное API -----------------------------

def load_settings(path: str | Path | None, overrides: dict[str, Any] | None = None) -> Settings:
    """Прочитать настройки. path=None → только дефолты. overrides — dotted-ключи ("scan.threads")."""
    data: dict[str, Any] = {}
    text = ""
    if path is not None:
        p = Path(path)
        if p.exists():
            try:
                raw = p.read_bytes()
                text = raw.decode("utf-8", errors="replace")
                data = tomllib.loads(text)
            except tomllib.TOMLDecodeError as e:
                raise SettingsError(f"Синтаксическая ошибка в {p}:\n  {e}") from None
            except OSError as e:
                raise SettingsError(f"Не удалось прочитать {p}: {e}") from None
        else:
            raise SettingsError(
                f"Файл настроек не найден: {p}\n"
                f"Скопируйте settings.example.toml → settings.toml и отредактируйте."
            )
    s = Settings()
    _apply_paths(s.paths, data.get("paths", {}), text)
    _apply_scan(s.scan, data.get("scan", {}), text)
    _apply_hash(s.hash, data.get("hash", {}), text)
    _apply_analyze(s.analyze, data.get("analyze", {}), text)
    _apply_move(s.move, data.get("move", {}), text)
    _apply_ui(s.ui, data.get("ui", {}), text)
    _cross_validate(s)
    if overrides:
        _apply_overrides(s, overrides)
    return s


def settings_to_dict(s: Settings) -> dict[str, Any]:
    """Плоское представление для UI / JSON (Path → str, tuple → list)."""
    return {
        "paths": {"src": str(s.paths.src), "trash": str(s.paths.trash)},
        "scan": {
            "extensions": list(s.scan.extensions),
            "recursive": s.scan.recursive,
            "threads": s.scan.threads,
            "batch_size": s.scan.batch_size,
        },
        "hash": {
            "method": s.hash.method,
            "hash_size": s.hash.hash_size,
            "hash_part_len": s.hash.hash_part_len,
        },
        "analyze": {"threshold": s.analyze.threshold},
        "move": {
            "mode": s.move.mode,
            "keep_by": s.move.keep_by,
            "conflict": s.move.conflict,
            "dry_run": s.move.dry_run,
        },
        "ui": {"page_size": s.ui.page_size, "refresh_sec": s.ui.refresh_sec},
    }


def update_settings_file(path: str | Path, updates: dict[str, dict[str, Any]]) -> None:
    """Атомарно по смыслу: читаем TOML, мерджим секции, пишем обратно (tomli-w).

    ВНИМАНИЕ: комментарии в файле теряются при перезаписи (ограничение tomli-w).
    Несинтаксически-валидный файл НЕ перезаписывается — чтобы не затиреть ручные правки.
    """
    if tomli_w is None:
        raise SettingsError("tomli_w не установлен — запись настроек недоступна")
    p = Path(path)
    data: dict[str, Any] = {}
    if p.exists():
        try:
            with open(p, "rb") as f:
                data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise SettingsError(f"Файл {p} содержит синтаксическую ошибку, не перезаписываю: {e}") from None
        except OSError as e:
            raise SettingsError(f"Не удалось прочитать {p}: {e}") from None
    for sec, kv in updates.items():
        data.setdefault(sec, {}).update(kv)
    try:
        with open(p, "wb") as f:
            tomli_w.dump(data, f)
    except OSError as e:
        raise SettingsError(f"Не удалось записать {p}: {e}") from None


def check_paths(s: Settings) -> None:
    """Валидация: trash не должен находиться внутри src (или совпадать с ним).

    Вызывается при старте движка; нарушение — аварийный выход с понятной ошибкой.
    """
    src = Path(s.paths.src).expanduser().resolve()
    trash = Path(s.paths.trash).expanduser().resolve()
    if trash == src:
        raise SettingsError(f"trash и src совпадают: {src}")
    if trash.is_relative_to(src):
        raise SettingsError(
            f"trash ({trash}) находится внутри src ({src}) — "
            f"дубликаты переносились бы внутрь самого архива. Исправьте [paths] в settings.toml."
        )
    if src.is_relative_to(trash):
        raise SettingsError(
            f"src ({src}) находится внутри trash ({trash}) — недопустимая конфигурация "
            f"[paths] в settings.toml."
        )
