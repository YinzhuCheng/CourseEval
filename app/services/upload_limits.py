"""Helpers for bounded UploadFile reads."""

from __future__ import annotations

from fastapi import UploadFile

from app.config import get_settings


async def read_upload_file_limited(file: UploadFile, *, max_bytes: int | None = None) -> bytes:
    limit = max_bytes if max_bytes is not None else get_settings().upload_max_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise ValueError("file_too_large")
    return data
