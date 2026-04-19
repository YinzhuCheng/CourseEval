"""Shared validation and normalization for uploaded raster images."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.config import get_settings


settings = get_settings()

ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_EXTENSION_FORMATS = {
    ".png": {"PNG"},
    ".jpg": {"JPEG"},
    ".jpeg": {"JPEG"},
    ".gif": {"GIF"},
    ".webp": {"WEBP"},
}

# Keep uploads comfortably below Pillow's default decompression-bomb threshold,
# but high enough for scanned pages and screenshots.
Image.MAX_IMAGE_PIXELS = 40_000_000


def normalize_uploaded_image(file_bytes: bytes, original_filename: str) -> tuple[str, bytes]:
    """Validate image bytes and return (extension, re-encoded bytes)."""
    ext = Path(original_filename).suffix.lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError("unsupported_image_type")
    if len(file_bytes) > settings.upload_max_bytes:
        raise ValueError("file_too_large")

    try:
        with Image.open(io.BytesIO(file_bytes)) as image:
            image.load()
            fmt = (image.format or "").upper()
            if fmt not in _EXTENSION_FORMATS[ext]:
                raise ValueError("unsupported_image_type")
            normalized = _encode_image(image, fmt)
    except ValueError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("unsupported_image_type") from exc

    if len(normalized) > settings.upload_max_bytes:
        raise ValueError("file_too_large")
    return ext, normalized


def _encode_image(image: Image.Image, fmt: str) -> bytes:
    out = io.BytesIO()
    if fmt == "JPEG":
        image.convert("RGB").save(out, format="JPEG", quality=88, optimize=True)
    elif fmt == "PNG":
        image.save(out, format="PNG", optimize=True)
    elif fmt == "WEBP":
        image.save(out, format="WEBP", quality=85, method=6)
    elif fmt == "GIF":
        image.save(out, format="GIF", save_all=True)
    else:
        raise ValueError("unsupported_image_type")
    return out.getvalue()

