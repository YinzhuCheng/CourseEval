"""Shared helpers for paths stored relative to the configured data directory."""

from __future__ import annotations

from pathlib import Path

from app.config import get_settings


settings = get_settings()


def relative_to_data(path: Path) -> str:
    data_dir = settings.data_dir.resolve()
    resolved = path.resolve()
    try:
        return resolved.relative_to(data_dir).as_posix()
    except ValueError as exc:
        raise ValueError("Path is outside the data directory.") from exc


def absolute_data_path(relative_path: str) -> Path:
    p = Path(relative_path)
    candidate = p if p.is_absolute() else settings.data_dir / p
    data_dir = settings.data_dir.resolve()
    resolved = candidate.resolve()
    if data_dir not in resolved.parents and resolved != data_dir:
        raise ValueError("Path is outside the data directory.")
    return resolved


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def ensure_writable_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o777)
